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
  MODEL_GENERATION_PROVIDER  env var (default: 'hosted')
  MODEL_EMBEDDING_PROVIDER   env var (default: 'hosted')

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
from typing import Dict, List

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

    Resolved independently from MODEL_GENERATION_PROVIDER (default: 'hosted').
    Changing this env var does not affect the embedding provider (T2-AC3).

    Raises:
        ValueError: when MODEL_GENERATION_PROVIDER names an unregistered provider.
    """
    name = os.getenv(_ENV_GENERATION, _DEFAULT_PROVIDER)
    return _resolve_provider(name, _ENV_GENERATION)


def get_embedding_provider() -> ModelProvider:
    """Return the active embedding provider.

    Resolved independently from MODEL_EMBEDDING_PROVIDER (default: 'hosted').
    Changing this env var does not affect the generation provider (T2-AC3).

    Raises:
        ValueError: when MODEL_EMBEDDING_PROVIDER names an unregistered provider.
    """
    name = os.getenv(_ENV_EMBEDDING, _DEFAULT_PROVIDER)
    return _resolve_provider(name, _ENV_EMBEDDING)


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


def embed(texts: List[str]) -> List[List[float]]:
    """Run an embedding call through the active provider and record telemetry.

    Resolves the embedding provider, performs exactly one ``embed()`` call, and
    emits exactly one ``model.embedding_completed`` telemetry event naming the
    provider that served it (T5-AC2/AC3). Returns the provider's vectors
    unchanged (an empty list on graceful failure).
    """
    provider = get_embedding_provider()
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


# ---------------------------------------------------------------------------
# Startup validation  (T2-AC4)
# ---------------------------------------------------------------------------


def validate_provider_config() -> None:
    """Validate that the configured provider names exist in the registry.

    Call this from the application lifespan so a misconfigured
    MODEL_GENERATION_PROVIDER or MODEL_EMBEDDING_PROVIDER is detected before
    the first model call — surfacing it as a ``ValueError`` at startup rather
    than mid-run (T2-AC4).

    Raises:
        ValueError: when either env var names an unregistered provider.
    """
    gen_name = os.getenv(_ENV_GENERATION, _DEFAULT_PROVIDER)
    emb_name = os.getenv(_ENV_EMBEDDING, _DEFAULT_PROVIDER)
    # Both calls raise ValueError on unknown names (T2-AC4).
    gen_provider = _resolve_provider(gen_name, _ENV_GENERATION)
    emb_provider = _resolve_provider(emb_name, _ENV_EMBEDDING)

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
    "ModelProvider",
    "generate",
    "embed",
    "get_generation_provider",
    "get_embedding_provider",
    "register_provider",
    "validate_provider_config",
]
