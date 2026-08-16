"""R16-D1 — Model Provider Gateway.

Single enforced entry point for every AI model call (generation and embedding).
The model behind AgentIQ can be swapped without touching any calling code.

Usage
-----
    from app.model_gateway import (
        GenerationRequest,
        GenerationResult,
        ModelProvider,
        get_generation_provider,
        get_embedding_provider,
    )

    result = get_generation_provider().generate(
        GenerationRequest(prompt="...", max_tokens=512)
    )
    if result.ok:
        text = result.text

Design rules (from R16-D1 spec)
--------------------------------
- Generation and embedding providers are resolved INDEPENDENTLY.  A customer
  can run hosted generation but in-boundary embeddings, or vice versa.
- The ONLY code permitted to reference a model provider endpoint, SDK, or
  api-key header is this package.  All direct calls in llm_enrichment.py,
  hallucination_guard.py, and normalization_enrichment.py are migrated to
  route through here (T3).
- On provider failure generate() returns ok=False / text=None.  Callers
  already handle None — behaviour is preserved exactly.

Provider resolution  (T2 — R16-D1 §3)
---------------------------------------
  MODEL_GENERATION_PROVIDER  env var (default: 'hosted' — SaaS profile only)
  MODEL_EMBEDDING_PROVIDER   env var (default: 'hosted' — SaaS profile only)

  HP-2 T2: the default is conditional on the DEPLOYMENT PROFILE. Under
  ``DEPLOYMENT_PROFILE=customer_hosted`` there is NO default — an unset
  variable raises ``MissingProviderConfiguration`` at startup instead of
  inheriting 'hosted', which calls api.anthropic.com. In a deployment that sits
  inside the customer's boundary, a call that leaves it must be a configured
  choice and never an inherited one. Under 'saas' (the default profile)
  resolution is unchanged. ``_configured_provider_name()`` is the single place
  that fallback is decided.

  Both are resolved independently at call time.  Setting generation to one
  value and embedding to another works without conflict (T2-AC3).

  Unknown values raise ``ValueError`` at startup via
  ``validate_provider_config()`` — before the first model call (T2-AC4).

  1.6 ships the 'hosted' provider (Anthropic API).  1.7 adds 'in_boundary'
  (R17-D1) and 'customer_tenant' (R17-D2) by registering new implementations
  via ``register_provider()`` — no calling code changes required (AC7).  All
  three modes are now selectable, independently, for generation and embedding.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

from app.deployment_profile import is_customer_hosted
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config key names and the R16-D1 1.6 default
# ---------------------------------------------------------------------------

_ENV_GENERATION: str = "MODEL_GENERATION_PROVIDER"
_ENV_EMBEDDING: str = "MODEL_EMBEDDING_PROVIDER"
#: The SaaS default. HP-2 T2 makes this conditional on the deployment profile —
#: see :func:`_default_provider_name`. Never read it directly.
_DEFAULT_PROVIDER: str = "hosted"

# Telemetry event types emitted by the gateway call paths (R16-D1 T5 / AT-366).
# Registered in app.telemetry; recorded exactly once per generate()/embed().
_GENERATION_EVENT: str = "model.generation_completed"
_EMBEDDING_EVENT: str = "model.embedding_completed"


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDER_REGISTRY: Dict[str, ModelProvider] = {}


def register_provider(provider: ModelProvider) -> None:
    """Register a ModelProvider instance under its ``name`` key.

    Idempotent when called with the same instance.
    Raises ``ValueError`` if a *different* instance tries to claim the same name.
    """
    existing = _PROVIDER_REGISTRY.get(provider.name)
    if existing is not None and existing is not provider:
        raise ValueError(
            f"A different provider is already registered under '{provider.name}'. "
            "Deregister the existing provider before replacing it."
        )
    _PROVIDER_REGISTRY[provider.name] = provider


class MissingProviderConfiguration(ValueError):
    """No model provider is configured, and this deployment has no default.

    HP-2 T2. Raised only under the ``customer_hosted`` deployment profile, where
    inheriting a cloud-calling default is the defect HP-2 removes. A
    ``ValueError`` subclass so callers that already handle the gateway's
    configuration errors generically keep working.
    """


def _default_provider_name() -> Optional[str]:
    """The provider applied when the env var is unset — or ``None`` if there is none.

    HP-2 T2, story item 1. The SaaS default is ``hosted``, which reaches
    ``api.anthropic.com``. Under ``customer_hosted`` there is deliberately NO
    default: the deployment sits in the customer's boundary (on-prem, air-gapped,
    or data-residency constrained), so a call that leaves it must be a configured
    choice and never an inherited one.

    Resolved live, so this tracks the profile the way every other reader in this
    package tracks its env var.
    """
    if is_customer_hosted():
        return None
    return _DEFAULT_PROVIDER


def _configured_provider_name(env_var: str) -> str:
    """The provider name configured in ``env_var``, applying the profile default.

    The SINGLE place the ``MODEL_*_PROVIDER`` fallback is decided — before HP-2
    T2 the same ``os.getenv(var, _DEFAULT_PROVIDER)`` line appeared in four
    places, which is the shape that lets one copy drift.

    Under ``saas`` the behaviour is byte-identical to before HP-2: unset yields
    ``hosted``, and any set value (including a blank or whitespace one) is passed
    through to :func:`_resolve_provider` exactly as it was, so a blank var still
    fails with the existing "not a registered model provider" message rather than
    silently becoming ``hosted`` (AC2).

    Raises:
        MissingProviderConfiguration: under ``customer_hosted`` when the variable
            is unset or blank. The message names BOTH provider variables and
            every valid value, because an operator who has to set one almost
            always has to set the other, and the pair is the actual decision.
    """
    raw = os.environ.get(env_var)
    if raw is not None and raw.strip():
        # Deliberately NOT stripped: preserve the pre-HP-2 resolution exactly.
        return raw

    default = _default_provider_name()
    if default is not None:
        # SaaS: reproduce ``os.getenv(env_var, _DEFAULT_PROVIDER)`` EXACTLY. Only
        # an ABSENT variable takes the default; a present-but-blank one is passed
        # straight through so it still fails in _resolve_provider as an
        # unregistered name, exactly as it did before HP-2. Treating blank as
        # unset here would be a real behaviour change smuggled in under "no
        # change to SaaS" — and it silently turns a typo into a cloud call.
        return raw if raw is not None else default

    # customer_hosted: no default exists, so an unset OR blank variable is a
    # missing configuration rather than something to fall back from.
    valid = ", ".join(f"'{n}'" for n in sorted(_PROVIDER_REGISTRY)) or "none registered"
    raise MissingProviderConfiguration(
        f"{env_var} is not set, and this deployment runs under "
        f"DEPLOYMENT_PROFILE=customer_hosted, which has no default model "
        f"provider. Set both {_ENV_GENERATION} and {_ENV_EMBEDDING} "
        f"explicitly. Valid values: {valid}. There is no default here on "
        f"purpose: the '{_DEFAULT_PROVIDER}' provider calls an external API, "
        "and in a customer-hosted deployment a call that leaves the boundary "
        "must be a configured choice, never an inherited one. Use "
        "'in_boundary' for a model served inside the boundary (Ollama, vLLM, "
        "any OpenAI-compatible endpoint), 'customer_tenant' for the "
        f"customer's own managed model service, or '{_DEFAULT_PROVIDER}' to "
        "deliberately accept the external call."
    )


def _configured_provider_name_or_none(env_var: str) -> Optional[str]:
    """Like :func:`_configured_provider_name` but reports ``None`` instead of raising.

    For :func:`resolve_provider_names`, whose documented contract is to describe
    the configuration rather than refuse it. Under ``customer_hosted`` with the
    variable unset the honest answer is "nothing is configured" — reporting
    ``'{hosted}'`` there would put a provider into a run's reproducibility record
    that the deployment would never have used.
    """
    try:
        return _configured_provider_name(env_var)
    except MissingProviderConfiguration:
        return None


def _resolve_provider(name: str, env_var: str) -> ModelProvider:
    """Look up ``name`` in the registry; raise ``ValueError`` with a helpful
    message if not found.

    ``env_var`` is the environment variable the caller read ``name`` from —
    including it in the error message lets operators find and fix the problem
    without reading source code.
    """
    provider = _PROVIDER_REGISTRY.get(name)
    if provider is None:
        registered = sorted(_PROVIDER_REGISTRY.keys())
        raise ValueError(
            f"{env_var}='{name}' is not a registered model provider. "
            f"Valid values: {registered}. "
            f"Update {env_var} in your .env file, or call register_provider() "
            f"before the application starts."
        )
    return provider


# ---------------------------------------------------------------------------
# Public entry points — the ONE interface the whole platform uses
# ---------------------------------------------------------------------------


def get_generation_provider() -> ModelProvider:
    """Return the active text-generation provider.

    Resolved independently from MODEL_GENERATION_PROVIDER (default: 'hosted' under
    the SaaS profile; no default under customer_hosted — HP-2 T2).
    Changing this env var does not affect the embedding provider (T2-AC3).

    Raises:
        ValueError: when MODEL_GENERATION_PROVIDER names an unregistered provider.
        MissingProviderConfiguration: under ``customer_hosted`` with the variable
            unset. In a served process ``validate_provider_config()`` has already
            refused at startup, so this path is reached only by an entry point
            that skipped startup validation (a CLI script, a standalone worker) —
            where a loud failure is still far better than silently calling out of
            the boundary.
    """
    name = _configured_provider_name(_ENV_GENERATION)
    return _resolve_provider(name, _ENV_GENERATION)


def get_embedding_provider() -> ModelProvider:
    """Return the active embedding provider.

    Resolved independently from MODEL_EMBEDDING_PROVIDER (default: 'hosted' under
    the SaaS profile; no default under customer_hosted — HP-2 T2).
    Changing this env var does not affect the generation provider (T2-AC3).

    Raises:
        ValueError: when MODEL_EMBEDDING_PROVIDER names an unregistered provider.
        MissingProviderConfiguration: see :func:`get_generation_provider`.
    """
    name = _configured_provider_name(_ENV_EMBEDDING)
    return _resolve_provider(name, _ENV_EMBEDDING)


def resolve_provider_names() -> tuple[Optional[str], Optional[str]]:
    """The configured ``(generation, embedding)`` provider NAMES.

    Added for 2.0-D4 T3's run-reproducibility record, which needs to record WHICH
    providers served a run without instantiating them. It lives here rather than in
    the caller because this package owns the provider configuration — the module
    docstring's rule is that no calling code reads these env vars — and a second
    reader would be free to drift from the resolution above.

    Returns the names as configured (falling back to the default), NOT a single
    collapsed "AI mode": the two resolve independently and the shipped configuration
    deliberately mixes them (embeddings via ``customer_tenant``, generation via
    ``hosted``), so one field would be wrong for the default deployment.

    Unlike :func:`get_generation_provider` this never raises for an unregistered
    name — it reports what is configured, and reporting an invalid configuration is
    more useful to a reproducibility record than refusing to describe the run.

    HP-2 T2: under ``customer_hosted`` an unset variable reports ``None`` rather
    than ``'hosted'``. There is no default in that profile, so naming one would
    record a provider the deployment would never have used — the same reason
    2.0-D4 T3 records an absent assembly-policy version as ``None`` with a reason
    rather than a placeholder.
    """
    return (
        _configured_provider_name_or_none(_ENV_GENERATION),
        _configured_provider_name_or_none(_ENV_EMBEDDING),
    )


# ---------------------------------------------------------------------------
# Provider telemetry  (T5 — AT-366)
#
# The gateway records WHICH provider served each call so model usage is
# observable across hosted, in-boundary, and future customer-tenant modes.
# Recorded exactly once per generate()/embed() call (never per token, never
# per retry — retries live inside a single provider call). Telemetry must
# never break a model call, so any emit failure is swallowed.
# ---------------------------------------------------------------------------


def _record_provider_telemetry(event_type: str, payload: dict) -> None:
    """Emit one telemetry event for a completed model call.

    Imported lazily to avoid an import-time dependency on the telemetry/DB
    layer, and wrapped so a telemetry failure never propagates to the caller.
    record_event() is itself fire-and-forget for DB errors; the guard here
    covers any unexpected import/registration problem.
    """
    try:
        from app.telemetry import record_event

        record_event(event_type, payload)
    except Exception:  # pragma: no cover - telemetry is best-effort
        logger.debug(
            "model_gateway: telemetry emit failed for %s", event_type, exc_info=True
        )


# ---------------------------------------------------------------------------
# Instrumented call paths — the entry points the platform calls (T5)
#
# These wrap provider.generate()/embed() with a single telemetry write so
# every model call is observable regardless of which call site made it. The
# provider result/behaviour is returned unchanged — graceful-failure
# (text=None on error) is preserved exactly.
#
# Exactly-once across layers: a provider may record its own per-call event
# (ModelProvider.emits_own_telemetry). When it does — as the default
# HostedModelProvider does (R16-D2 T6) — the wrapper skips its emission so a
# single logical call yields exactly one event, never a duplicate.
# ---------------------------------------------------------------------------


def generate(req: GenerationRequest) -> GenerationResult:
    """Run a text-generation call through the active provider and record telemetry.

    Resolves the generation provider, performs exactly one ``generate()`` call,
    and emits exactly one ``model.generation_completed`` telemetry event naming
    the provider that served it — on success and failure alike (T5-AC1/AC3/AC4).
    Returns the provider's ``GenerationResult`` unchanged.

    Exactly-once across layers: a provider that records its own per-call event
    (``emits_own_telemetry``, e.g. the D2 HostedModelProvider that is now the
    default — R16-D2 T5/AT-409) owns the single event, so the gateway skips its
    emission here.  One logical call always produces exactly one event.
    """
    provider = get_generation_provider()
    result = provider.generate(req)
    # AC1: provider name comes from GenerationResult.provider — the backend
    # that actually served the request. AC4: emitted even when ok is False.
    # Skip when the provider already emitted (avoids a duplicate event).
    if not provider.emits_own_telemetry:
        _record_provider_telemetry(
            _GENERATION_EVENT,
            {"provider": result.provider, "ok": result.ok},
        )
    return result


def _embed_with_provider(
    provider: ModelProvider, texts: List[str]
) -> List[List[float]]:
    """Embed ``texts`` on an already-resolved provider and record telemetry.

    The shared body of :func:`embed` and :func:`embed_with_identity`. The caller
    owns provider resolution, so a caller that needs BOTH the vectors and the
    provider's model identity can resolve the provider ONCE and get a consistent
    pair (no provider-swap race between the two reads).
    """
    vectors = provider.embed(texts)
    # ok = the provider returned a vector for every input (empty input is a
    # successful no-op). AC2: telemetry carries the provider name.
    # Skip when the provider already emitted its own event (exactly-once).
    if not provider.emits_own_telemetry:
        _record_provider_telemetry(
            _EMBEDDING_EVENT,
            {
                "provider": provider.name,
                "ok": len(vectors) == len(texts),
                "text_count": len(texts),
                "vector_count": len(vectors),
            },
        )
    return vectors


def embed(texts: List[str]) -> List[List[float]]:
    """Run an embedding call through the active provider and record telemetry.

    Resolves the embedding provider, performs exactly one ``embed()`` call, and
    emits exactly one ``model.embedding_completed`` telemetry event naming the
    provider that served it (T5-AC2/AC3). Returns the provider's vectors
    unchanged (an empty list on graceful failure).
    """
    return _embed_with_provider(get_embedding_provider(), texts)


def embed_with_identity(
    texts: List[str],
) -> "tuple[List[List[float]], tuple[str, str]]":
    """Embed ``texts`` AND return the serving provider's model identity together.

    Resolves the embedding provider EXACTLY ONCE and reads both the vectors and
    the provider's ``embedding_identity()`` from that same object. This closes the
    TOCTOU where separate ``embed()`` and ``get_embedding_provider().embedding_identity()``
    calls could resolve two different providers across a reconfiguration/failover
    and end up comparing a model-A query vector against a model-B identity filter
    (R18-B1 AC8). Returns ``(vectors, (identity, version))``; identity degrades to
    ``("", "")`` if the provider cannot report it, mirroring the embedder's
    never-raise posture.
    """
    provider = get_embedding_provider()
    vectors = _embed_with_provider(provider, texts)
    try:
        identity = provider.embedding_identity()
    except Exception:  # noqa: BLE001 — identity lookup must never break the call
        logger.warning(
            "model_gateway: could not resolve embedding model identity", exc_info=True
        )
        identity = ("", "")
    return vectors, identity


# ---------------------------------------------------------------------------
# Startup validation  (T2-AC4)
# ---------------------------------------------------------------------------


def validate_provider_config() -> None:
    """Validate that the configured provider names exist in the registry.

    Call this from the application lifespan so a misconfigured
    MODEL_GENERATION_PROVIDER or MODEL_EMBEDDING_PROVIDER is detected before
    the first model call — surfacing it as a ``ValueError`` at startup rather
    than mid-run (T2-AC4).

    HP-2 T2 makes this the refusal point for a MISSING provider too, under the
    ``customer_hosted`` profile. It belongs here rather than in the per-call
    getters because the lifespan already calls this unconditionally: a
    configuration error should stop the process, not surface as a runtime error
    on whichever request first needs a model.

    Raises:
        ValueError: when either env var names an unregistered provider.
        MissingProviderConfiguration: under ``customer_hosted`` when either
            provider variable is unset — no cloud-calling default is inherited
            (HP-2 AC1). Under ``saas`` unset still resolves to 'hosted' (AC2).
    """
    gen_name = _configured_provider_name(_ENV_GENERATION)
    emb_name = _configured_provider_name(_ENV_EMBEDDING)
    # Both calls raise ValueError on unknown names (T2-AC4).
    gen_provider = _resolve_provider(gen_name, _ENV_GENERATION)
    emb_provider = _resolve_provider(emb_name, _ENV_EMBEDDING)

    # The 'hosted' provider has no embeddings endpoint — its embed() returns []
    # by design (Anthropic's hosted API does not expose embeddings). Selecting it
    # for embeddings therefore silently DISABLES retrieval: every retrieval_chunk
    # stays pending (embedding IS NULL) forever and search returns nothing. Make
    # that misconfiguration loud at startup instead of leaving it to be inferred
    # from an endless "gateway returned 0 vectors" worker log.
    if emb_provider.name == "hosted":
        logger.warning(
            "%s=hosted: the hosted provider does not support embeddings — retrieval "
            "embedding is DISABLED (chunks never embed; search returns nothing). Set "
            "%s to 'in_boundary' (any OpenAI-compatible embeddings API) or "
            "'customer_tenant' to enable retrieval. See backend/.env.template.",
            _ENV_EMBEDDING,
            _ENV_EMBEDDING,
        )

    # R17-D2 T2 — reserved-connector-id collision guard. The customer-tenant model
    # credential is vaulted in the shared `credentials` table under a reserved
    # connector_id ("customer_tenant"). If a REAL OAuth connector were ever
    # registered under that same id, the two would read/write the same credential
    # row and silently corrupt each other. Fail fast at startup so the collision
    # is caught in review, never in production. Import lazily and skip if the auth
    # subsystem is not importable in a minimal context.
    try:
        from app.auth.configs import CONNECTOR_AUTH_CONFIGS
        from app.auth.vault import CUSTOMER_TENANT_CONNECTOR_ID
    except ImportError:
        logger.debug(
            "model_gateway: auth subsystem not importable; skipping reserved "
            "connector-id collision check", exc_info=True
        )
    else:
        if CUSTOMER_TENANT_CONNECTOR_ID in CONNECTOR_AUTH_CONFIGS:
            raise ValueError(
                f"connector_id '{CUSTOMER_TENANT_CONNECTOR_ID}' is reserved for the "
                "customer-tenant model credential vault, but a real connector is "
                "registered under the same id in CONNECTOR_AUTH_CONFIGS. This would "
                "corrupt the shared credentials row — rename the connector."
            )

    # Per-provider completeness check for the SELECTED providers. A provider may
    # be a registered name yet still be misconfigured — e.g. in_boundary selected
    # with no endpoint URL would pass name resolution but then fail every call at
    # runtime with ok=False. provider.validate() logs a startup warning so that
    # boot-time misconfiguration is visible, not silent. It never raises, but we
    # also guard here so a hook bug can never block startup. Dedupe by identity so
    # a provider serving both roles is validated once.
    for provider in {id(gen_provider): gen_provider, id(emb_provider): emb_provider}.values():
        try:
            provider.validate()
        except Exception:  # pragma: no cover - validation must never block startup
            logger.debug(
                "model_gateway: provider %s validate() raised", provider.name, exc_info=True
            )

    # R1.9.1-H1 T4 (F4 fix): warn at startup if CUSTOMER_TENANT_API_KEY is set
    # under the production deployment profile. Unconditional — checked
    # regardless of which provider is currently selected, so the warning
    # fires as soon as the var is set in production, not only once
    # customer_tenant becomes the active provider.
    try:
        from app.model_gateway.customer_tenant_vault import (
            validate_no_production_env_fallback,
        )

        validate_no_production_env_fallback()
    except Exception:  # pragma: no cover - validation must never block startup
        logger.debug(
            "model_gateway: customer_tenant production env-fallback check raised",
            exc_info=True,
        )

    logger.info(
        "model_gateway config validated: %s=%s %s=%s",
        _ENV_GENERATION, gen_name,
        _ENV_EMBEDDING, emb_name,
    )


# ---------------------------------------------------------------------------
# Bootstrap: register HostedModelProvider as the default 'hosted' provider at
# import time (R16-D2 T5 / AT-409).
#
# HostedModelProvider is the full R16-D2 implementation — bounded retry,
# exponential backoff, per-request deadline enforcement, rate-limit-aware
# backoff (T2), credential/config hygiene owned inside the gateway (T4), and
# provider telemetry (T6).  Because it registers under name 'hosted', BOTH
# get_generation_provider() and get_embedding_provider() resolve to it when
# MODEL_GENERATION_PROVIDER / MODEL_EMBEDDING_PROVIDER are unset or set to
# 'hosted' — so the platform works out of the box (T5-AC1 / T5-AC2).
#
# This registration is "the default, not the only" (R16-D2 §4): it goes through
# the same register_provider() any future in-boundary / customer-tenant provider
# will use, makes no assumption that hosted is the sole mode (T5-AC4), and
# selecting a different provider is purely a config change — no caller is
# affected (T5-AC3).
#
# Imported at the bottom to avoid a circular import: hosted_provider.py imports
# ModelProvider/GenerationRequest/GenerationResult from _interface.py, not from
# this module, so there is no cycle.
# ---------------------------------------------------------------------------

from app.model_gateway.hosted_provider import HostedModelProvider as _HostedModelProvider  # noqa: E402
from app.model_gateway.in_boundary_provider import InBoundaryModelProvider as _InBoundaryModelProvider  # noqa: E402
from app.model_gateway.customer_tenant_provider import CustomerTenantModelProvider as _CustomerTenantModelProvider  # noqa: E402

register_provider(_HostedModelProvider())
register_provider(_InBoundaryModelProvider())
register_provider(_CustomerTenantModelProvider())


__all__ = [
    "GenerationRequest",
    "GenerationResult",
    "MissingProviderConfiguration",
    "ModelProvider",
    "generate",
    "embed",
    "get_generation_provider",
    "get_embedding_provider",
    "register_provider",
    "resolve_provider_names",
    "validate_provider_config",
]
