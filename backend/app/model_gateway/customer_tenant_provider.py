"""R17-D2 T1 - Customer-tenant model provider implementation.

``CustomerTenantModelProvider`` is the gateway adapter for a *managed* model
service the customer operates inside their own cloud tenant — the canonical
example is Azure OpenAI provisioned in the customer's subscription. CloudFulcrum
owns this adapter; the customer owns and operates (and can revoke) the managed
model service and the credential into it.

This is the third provider behind the R16-D1 gateway, completing the three model
modes (hosted, in-boundary, customer-tenant). It follows the exact same pattern
as the previous two: no calling code changes, selection is configuration, and
BOTH generation and embedding are implemented (R17-D2 §1/§3, AC1/AC3/AC8).

All endpoint, deployment, auth, and API-version configuration is read from
``customer_tenant_config`` inside the gateway package, so callers never know
which backend is active.

Authentication into the customer's tenant (R17-D2 §2)
-----------------------------------------------------
The differentiating detail of this mode is authentication: it authenticates into
the customer's own cloud tenant using a credential the customer provides and
controls. Azure OpenAI accepts the credential via the ``api-key`` request
header. The credential gets vault-grade hygiene — read from config/secrets only,
never hardcoded, never logged, and re-read live per call so a customer rotation
or revocation takes effect without a restart. (T2 layers the encrypted
credential vault behind the same ``resolve_api_key()`` accessor.)

Resilience (R17-D2 T3, mirrors hosted/in-boundary modes)
--------------------------------------------------------
Same resilience posture as the other two providers so customers get consistent
behaviour regardless of mode:

- Transient HTTP errors (rate-limit 429/529, transient 5xx) and network timeouts
  trigger bounded exponential-backoff retry. Non-transient errors (other 4xx)
  are returned immediately without retry.
- The per-request ``timeout_ms`` is a wall-clock deadline for the entire
  generate() call including all retries and backoff sleeps. The first attempt
  gets the full budget; each retry gets only the time remaining.
- Rate-limit responses (429) honour the ``Retry-After`` header when present.
- Retry is bounded by ``_MAX_RETRIES`` — never retries indefinitely.

Graceful failure (R17-D2 T3, AC5)
---------------------------------
``generate()`` must never raise into the application. Missing config, malformed
responses, transport errors, and exhausted retries/deadlines all become
``GenerationResult(text=None, provider='customer_tenant', ok=False)``.
``embed()`` degrades to an empty list on every failure path. AgentIQ consumes
model output as proposed/inferred content, so an unavailable model surfaces
fewer findings rather than unsafe or unverified facts.

Provider-identity telemetry (R17-D2 T5, AC6)
--------------------------------------------
Like the hosted and in-boundary providers, this provider records its OWN per-call
telemetry so every customer-tenant call is observable no matter how it is invoked
— directly or through the gateway. ``emits_own_telemetry = True`` tells the
gateway wrappers to skip their emission, so a single logical call always produces
exactly one event (never a duplicate).

- ``generate()`` emits exactly one ``model.generation_completed`` event after the
  call completes; ``embed()`` emits exactly one ``model.embedding_completed``
  event — on success and failure alike, never per retry, never per token.
- Every event carries ``provider='customer_tenant'`` so support/audit teams (and
  regulated customers) can prove which provider served each model call.
- PII GUARD: the payload carries only the provider name, the ok flag, and (for
  embeddings) text/vector counts. It NEVER carries prompt text, generated output,
  input texts, embedding vectors, endpoint credentials, or any customer secret.
- Telemetry is best-effort: a DB/import problem is swallowed and never propagates
  into the model call, preserving the graceful-failure contract.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, List, Optional, Tuple

from app.model_gateway._interface import (
    ROLE_EMBEDDING,
    GenerationRequest,
    GenerationResult,
    ModelProvider,
    ProviderProbeTarget,
)
from app.model_gateway.customer_tenant_config import (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_DEPLOYMENT,
    CONFIG_KEY_EMBEDDING_DEPLOYMENT,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_ENDPOINT,
    CONFIG_KEY_GENERATION_DEPLOYMENT,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CUSTOMER_TENANT_PROVIDER_NAME,
    CustomerTenantConfig,
)

logger = logging.getLogger(__name__)

_DEFAULT_EMBED_TIMEOUT_S: float = 30.0

# Resilience constants (R17-D2 T3 — same posture as hosted/in-boundary modes).
_MAX_RETRIES: int = 3                # bounded — never retries more than this
_BASE_BACKOFF_S: float = 0.5         # first backoff window: 0.5s, then 1s, 2s, 4s…
_MAX_BACKOFF_S: float = 8.0          # cap so a single backoff never exceeds 8s
_RETRYABLE_STATUS_CODES: frozenset = frozenset({429, 529})
_TRANSIENT_5XX_CODES: frozenset = frozenset({500, 502, 503, 504})
# Minimum remaining budget to start another attempt — skip if less than this to
# avoid an attempt that would instantly time out.
_MIN_ATTEMPT_BUDGET_S: float = 0.05

# Telemetry event types (R17-D2 T5). The same event types the gateway, hosted,
# and in-boundary providers use — registered in app.telemetry — so
# customer-tenant usage is observable through the existing telemetry_events store
# with no new table.
_GENERATION_EVENT: str = "model.generation_completed"
_EMBEDDING_EVENT: str = "model.embedding_completed"


def _record_customer_tenant_telemetry(event_type: str, payload: dict) -> None:
    """Emit one telemetry event for a completed customer-tenant call (R17-D2 T5).

    Imported lazily to avoid a circular import at module load time (the same
    pattern the gateway, hosted, and in-boundary providers use). Wrapped so a
    telemetry failure never propagates to the caller — model calls are not
    conditional on telemetry succeeding.

    PII GUARD: callers pass only the provider name, the ok flag, and counts.
    Prompt text, generated output, input texts, embedding vectors, and
    credentials must never appear in ``payload``.
    """
    try:
        from app.telemetry import record_event

        record_event(event_type, payload)
    except Exception:  # pragma: no cover - telemetry is best-effort
        logger.debug(
            "customer_tenant_provider: telemetry emit failed for %s",
            event_type,
            exc_info=True,
        )


class CustomerTenantModelProvider(ModelProvider):
    """ModelProvider backed by the customer's managed in-tenant model endpoint."""

    name = CUSTOMER_TENANT_PROVIDER_NAME

    # This provider records its own per-call telemetry (T5), so the gateway's
    # instrumented generate()/embed() wrappers skip their emission — one logical
    # call always produces exactly one event, never a duplicate.
    emits_own_telemetry = True

    def __init__(self) -> None:
        # Snapshot-at-init, used ONLY for the startup log line below. The live
        # per-call config is re-read fresh inside _run_generate()/_run_embed()
        # (each constructs a new CustomerTenantConfig) so endpoint/credential/
        # deployment changes take effect without a restart. Do NOT treat
        # self._config as the live config source — read a fresh
        # CustomerTenantConfig() instead.
        self._config = CustomerTenantConfig()
        logger.info(
            "CustomerTenantModelProvider initialised "
            "(generation_endpoint_configured=%s, embedding_endpoint_configured=%s)",
            bool(self._config.generation_endpoint()),
            bool(self._config.embedding_endpoint()),
        )

    def validate(self) -> None:
        """Warn at startup when customer_tenant is selected but not configured.

        Called by ``validate_provider_config()`` only when customer_tenant is the
        selected generation and/or embedding provider. The runtime
        graceful-failure contract (every call returns ok=False / [] when the
        endpoint is unset) is correct for transient failures, but it is NOT an
        acceptable *sole* signal for a boot-time misconfiguration: an operator
        who sets ``MODEL_GENERATION_PROVIDER=customer_tenant`` without an endpoint
        or deployment would otherwise see the system silently surface no findings.
        Surface a clear warning instead.

        Never raises — startup must not be blocked.
        """
        cfg = CustomerTenantConfig()
        if not (cfg.generation_endpoint() or cfg.embedding_endpoint()):
            # Distinguish the two root causes so the fix is unambiguous: an
            # endpoint that was never set vs. an endpoint base with no deployment
            # name (Azure addresses a model by deployment, so the URL cannot be
            # built without one).
            if not cfg.has_endpoint():
                logger.warning(
                    "customer_tenant provider is active but no endpoint is "
                    "configured (%s unset; or set %s / %s for a full-URL override) "
                    "— all model calls will fail (generation returns ok=False, "
                    "embedding returns []) until an endpoint is configured.",
                    CONFIG_KEY_ENDPOINT,
                    CONFIG_KEY_GENERATION_ENDPOINT,
                    CONFIG_KEY_EMBEDDING_ENDPOINT,
                )
            else:
                logger.warning(
                    "customer_tenant provider has an endpoint base (%s) but no "
                    "deployment name (%s / %s / %s all unset) — the request URL "
                    "cannot be built, so all model calls will fail until a "
                    "deployment is configured.",
                    CONFIG_KEY_ENDPOINT,
                    CONFIG_KEY_DEPLOYMENT,
                    CONFIG_KEY_GENERATION_DEPLOYMENT,
                    CONFIG_KEY_EMBEDDING_DEPLOYMENT,
                )
            return

        if not cfg.has_credential():
            logger.warning(
                "customer_tenant provider has an endpoint configured but no "
                "tenant credential — model calls will fail until a credential is "
                "configured (the credential value is never logged)."
            )

    def generate(self, req: GenerationRequest) -> GenerationResult:
        """Generate text through the customer's tenant endpoint, with telemetry.

        Performs the resilient generation call and emits exactly one
        ``model.generation_completed`` telemetry event after it completes — on
        success and failure alike (T5). The event carries
        ``provider='customer_tenant'`` and the ok flag only; never the prompt or
        generated text.
        """
        result = self._run_generate(req)
        _record_customer_tenant_telemetry(
            _GENERATION_EVENT,
            {"provider": result.provider, "ok": result.ok},
        )
        return result

    def _run_generate(self, req: GenerationRequest) -> GenerationResult:
        """Resilient generation call. Telemetry is emitted once by generate().

        Never raises to callers. Missing config, malformed responses, transport
        errors, and exhausted retries/deadlines all become
        ``GenerationResult(text=None, provider='customer_tenant', ok=False)``.
        """
        cfg = CustomerTenantConfig()
        url = cfg.generation_endpoint()
        if not url:
            logger.warning(
                "CustomerTenantModelProvider generation skipped: %s / deployment "
                "not configured",
                CONFIG_KEY_ENDPOINT,
            )
            return GenerationResult(text=None, provider=self.name, ok=False)

        # R17-D2 T2: resolve the tenant credential from the vault (env fallback).
        # A missing/revoked credential short-circuits to a graceful failure — no
        # network call — rather than authenticating without a key (AC2/AC5). The
        # value is never logged.
        api_key = cfg.resolve_api_key()
        if not api_key:
            logger.warning(
                "CustomerTenantModelProvider generation skipped: no tenant "
                "credential resolved (missing or revoked)"
            )
            return GenerationResult(text=None, provider=self.name, ok=False)

        payload = {
            "max_tokens": req.max_tokens,
            "messages": [{"role": "user", "content": req.prompt}],
        }

        # Never raises: the resilient POST returns None on every failure path.
        data = self._post_with_resilience(
            url=url,
            payload=payload,
            api_key=api_key,
            total_timeout_s=max(req.timeout_ms / 1000.0, _MIN_ATTEMPT_BUDGET_S),
            label="generation",
        )
        if data is None:
            return GenerationResult(text=None, provider=self.name, ok=False)

        text = _extract_generation_text(data)
        if text is None:
            logger.warning(
                "CustomerTenantModelProvider generation response had no text content"
            )
            return GenerationResult(text=None, provider=self.name, ok=False)
        return GenerationResult(text=text, provider=self.name, ok=True)

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Return embeddings from the customer's tenant endpoint, with telemetry.

        Performs the resilient embedding call and emits exactly one
        ``model.embedding_completed`` telemetry event after it completes (T5). The
        event carries ``provider='customer_tenant'``, the ok flag, and the
        text/vector counts only — never the input texts or the vectors. ``ok`` is
        True when a vector was returned for every input (an empty-input no-op is
        vacuously ok).
        """
        vectors = self._run_embed(texts)
        _record_customer_tenant_telemetry(
            _EMBEDDING_EVENT,
            {
                "provider": self.name,
                "ok": len(vectors) == len(texts),
                "text_count": len(texts),
                "vector_count": len(vectors),
            },
        )
        return vectors

    def probe_target(self, role: str) -> ProviderProbeTarget:
        """HP-2.3 startup probe target — per role.

        The credential IS required: this is the customer's own managed model
        service (Azure OpenAI and similar), which authenticates every request, so
        a missing key means every call 401s. Only presence and the variable NAME
        are reported — the value is resolved from the vault and never surfaced.
        """
        cfg = CustomerTenantConfig()
        endpoint = (
            cfg.embedding_endpoint() if role == ROLE_EMBEDDING else cfg.generation_endpoint()
        )
        role_key = (
            CONFIG_KEY_EMBEDDING_ENDPOINT
            if role == ROLE_EMBEDDING
            else CONFIG_KEY_GENERATION_ENDPOINT
        )
        return ProviderProbeTarget(
            endpoint=endpoint or None,
            endpoint_config_keys=(CONFIG_KEY_ENDPOINT, role_key),
            credential_required=True,
            credential_present=cfg.has_credential(),
            credential_config_key=CONFIG_KEY_API_KEY,
        )

    def embedding_identity(self) -> "tuple[str, str]":
        """Stamp identity/version for customer-tenant vectors (R18-B1 T3 / AC8).

        The stamp is the compatibility marker the retrieval substrate filters on,
        so it must name what actually determines the VECTOR SPACE — the embedding
        model — and change only when that space changes.

        Preferred form (``CUSTOMER_TENANT_EMBEDDING_MODEL`` declared): identity is
        the provider name qualified by the underlying model
        (``customer_tenant:text-embedding-3-small``) and version is the model's own
        version (``CUSTOMER_TENANT_EMBEDDING_MODEL_VERSION``, e.g. ``1``). This is
        the accurate marker on two counts:

        * A managed deployment NAME is an operator-chosen alias. Two deployments of
          the same model produce the same, mutually comparable vector space, so
          keying identity on the alias would needlessly invalidate every vector if
          the deployment were ever recreated under a different name.
        * The service ``api-version`` versions the REST surface, not the model.
          Moving from ``2023-05-15`` to ``2024-12-01-preview`` changes nothing about
          the embeddings, so stamping it as the model version would invalidate the
          entire index and force a full re-embed of unchanged content on a routine
          API-version bump.

        Fallback (model undeclared): the previous behaviour — deployment alias +
        API version — so an existing deployment's stamps do not shift underneath it.
        An unconfigured embedding deployment falls back to ``(name, "")``.

        Read live from a fresh config, like ``_run_embed``.
        """
        cfg = CustomerTenantConfig()
        model = cfg.embedding_model()
        if model:
            return (f"{self.name}:{model}", cfg.embedding_model_version())
        deployment = cfg.embedding_deployment()
        if not deployment:
            return (self.name, "")
        return (f"{self.name}:{deployment}", cfg.api_version())

    def _run_embed(self, texts: List[str]) -> List[List[float]]:
        """Resilient embedding call. Telemetry is emitted once by embed().

        Empty input is a successful no-op. Any endpoint/config/parse failure or
        exhausted retries returns an empty list so embedding callers degrade
        safely instead of raising.
        """
        if not texts:
            return []

        cfg = CustomerTenantConfig()
        url = cfg.embedding_endpoint()
        if not url:
            logger.warning(
                "CustomerTenantModelProvider embedding skipped: %s / deployment "
                "not configured",
                CONFIG_KEY_ENDPOINT,
            )
            return []

        # R17-D2 T2: a missing/revoked tenant credential degrades to [] with no
        # network call, rather than authenticating without a key (AC2/AC5).
        api_key = cfg.resolve_api_key()
        if not api_key:
            logger.warning(
                "CustomerTenantModelProvider embedding skipped: no tenant "
                "credential resolved (missing or revoked)"
            )
            return []

        payload = {"input": texts}

        # Never raises: the resilient POST returns None on every failure path.
        data = self._post_with_resilience(
            url=url,
            payload=payload,
            api_key=api_key,
            total_timeout_s=_DEFAULT_EMBED_TIMEOUT_S,
            label="embedding",
        )
        if data is None:
            return []

        vectors = _extract_embedding_vectors(data, expected_count=len(texts))
        if len(vectors) != len(texts):
            logger.warning(
                "CustomerTenantModelProvider embedding response count mismatch "
                "(texts=%d vectors=%d)",
                len(texts),
                len(vectors),
            )
            return []
        return vectors

    # ------------------------------------------------------------------
    # Resilience helpers (R17-D2 T3)
    # ------------------------------------------------------------------

    def _post_with_resilience(
        self,
        *,
        url: str,
        payload: dict,
        api_key: str,
        total_timeout_s: float,
        label: str,
    ) -> Optional[Any]:
        """POST with bounded retry/backoff inside a wall-clock deadline.

        Returns the decoded JSON response on the first successful attempt, or
        ``None`` once a non-transient error occurs, the retry budget is spent, or
        the deadline is exhausted. Never raises — graceful failure is the
        contract both generate() and embed() rely on.

        The first attempt receives the full ``total_timeout_s`` budget; each
        retry receives only the time remaining in that budget, so the entire call
        (all attempts + backoff sleeps) stays within the deadline.
        """
        deadline = time.monotonic() + total_timeout_s

        for attempt in range(_MAX_RETRIES + 1):
            if attempt == 0:
                attempt_timeout_s = total_timeout_s
            else:
                attempt_timeout_s = deadline - time.monotonic()
                if attempt_timeout_s < _MIN_ATTEMPT_BUDGET_S:
                    logger.warning(
                        "CustomerTenantModelProvider %s: timeout budget exhausted "
                        "after %d attempt(s)",
                        label,
                        attempt,
                    )
                    return None

            ok, data, retryable, retry_after_s = self._attempt_post(
                url=url,
                payload=payload,
                api_key=api_key,
                timeout_s=attempt_timeout_s,
            )

            if ok:
                return data

            if not retryable or attempt >= _MAX_RETRIES:
                return None

            # Backoff: exponential, capped, never below an explicit Retry-After.
            backoff_s = min(_BASE_BACKOFF_S * (2 ** attempt), _MAX_BACKOFF_S)
            if retry_after_s is not None:
                backoff_s = max(backoff_s, retry_after_s)

            remaining_after_backoff = deadline - time.monotonic() - backoff_s
            if remaining_after_backoff < _MIN_ATTEMPT_BUDGET_S:
                # Not enough budget left for backoff + a meaningful retry.
                logger.warning(
                    "CustomerTenantModelProvider %s: insufficient budget for "
                    "backoff on attempt %d — returning failure",
                    label,
                    attempt,
                )
                return None

            logger.info(
                "CustomerTenantModelProvider %s: attempt %d failed (retryable); "
                "backing off %.2fs before retry",
                label,
                attempt,
                backoff_s,
            )
            time.sleep(backoff_s)

        # Unreachable — the loop always returns, but satisfies type checkers.
        return None  # pragma: no cover

    def _attempt_post(
        self,
        *,
        url: str,
        payload: dict,
        api_key: str,
        timeout_s: float,
    ) -> Tuple[bool, Optional[Any], bool, Optional[float]]:
        """Perform one HTTP POST to the customer's tenant endpoint.

        Returns a 4-tuple ``(ok, data, retryable, retry_after_s)``:
            ok           True only on an HTTP 200 with a decodable JSON body.
            data         The decoded JSON response when ``ok`` is True, else None.
            retryable    True for transient failures (429/529, transient 5xx,
                         network timeouts, unexpected transport errors); False for
                         non-transient HTTP errors that should not be retried.
            retry_after_s  The Retry-After header value when the server signals
                         rate-limiting; None otherwise.

        Never raises — every failure mode maps onto the returned tuple.

        Azure OpenAI authenticates via the ``api-key`` request header rather than
        a bearer token; the credential is attached only when present so a missing
        credential degrades to an auth failure rather than a crash. The credential
        is never logged.

        Known limitation — socket vs. total read time:
            ``urllib.request.urlopen(..., timeout=timeout_s)`` sets a *socket*
            timeout (each socket op must complete within ``timeout_s``), not a
            total read-time deadline. The between-attempt deadline check in
            ``_post_with_resilience`` guards across attempts, not within one
            urlopen. Acceptable here because responses are not streamed chunk by
            chunk to urllib and ``max_tokens`` bounds the body size.
        """
        headers = {"Content-Type": "application/json"}
        if api_key:
            # Azure OpenAI credential header. Never logged.
            headers["api-key"] = api_key

        http_req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            # timeout is a per-socket-op deadline, not a total-read deadline — see
            # the "Known limitation" note in this method's docstring.
            with urllib.request.urlopen(http_req, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8")
            return True, json.loads(raw), False, None

        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # pragma: no cover - body read is best-effort
                body = ""

            if status in _RETRYABLE_STATUS_CODES:
                retry_after_s = _parse_retry_after(exc)
                logger.warning(
                    "CustomerTenantModelProvider HTTP %s "
                    "(rate-limited/overloaded): %s",
                    status,
                    body,
                )
                return False, None, True, retry_after_s

            if status in _TRANSIENT_5XX_CODES:
                logger.warning(
                    "CustomerTenantModelProvider HTTP %s (transient): %s",
                    status,
                    body,
                )
                return False, None, True, None

            # Non-transient 4xx or other — return immediately, do not retry.
            logger.error("CustomerTenantModelProvider HTTP %s: %s", status, body)
            return False, None, False, None

        except TimeoutError:
            # Network timeout is transient — retry within the remaining budget.
            logger.warning(
                "CustomerTenantModelProvider: request timed out (%.2fs budget)",
                timeout_s,
            )
            return False, None, True, None

        except Exception as exc:
            # Treat unexpected network/OS errors (incl. URLError) as transient.
            logger.error("CustomerTenantModelProvider transport error: %s", exc)
            return False, None, True, None


def _parse_retry_after(exc: urllib.error.HTTPError) -> Optional[float]:
    """Extract a numeric delay from the Retry-After response header.

    Returns None when the header is absent or non-numeric.
    """
    try:
        value = exc.headers.get("Retry-After", "")
        if value:
            return float(value)
    except (ValueError, AttributeError):
        pass
    return None


def _extract_generation_text(data: Any) -> Optional[str]:
    """Extract text from common chat/completions response shapes."""
    if not isinstance(data, dict):
        return None

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content.strip()
            text = first.get("text")
            if isinstance(text, str):
                return text.strip()

    content = data.get("content")
    if isinstance(content, str):
        return content.strip()

    return None


def _extract_embedding_vectors(
    data: Any,
    *,
    expected_count: Optional[int] = None,
) -> List[List[float]]:
    """Extract input-ordered embedding vectors from a compatible response."""
    if not isinstance(data, dict):
        return []

    rows = data.get("data")
    if not isinstance(rows, list):
        return []

    if not all(isinstance(row, dict) for row in rows):
        return []

    indexed = [row for row in rows if "index" in row]
    if indexed:
        if len(indexed) != len(rows):
            return []

        indexes = [row.get("index") for row in rows]
        if not all(isinstance(index, int) and not isinstance(index, bool) for index in indexes):
            return []
        if len(set(indexes)) != len(indexes):
            return []
        if expected_count is not None and sorted(indexes) != list(range(expected_count)):
            return []

        rows = sorted(rows, key=lambda row: row["index"])

    vectors: List[List[float]] = []
    for row in rows:
        embedding = row.get("embedding")
        if not isinstance(embedding, list):
            return []
        try:
            vectors.append([float(value) for value in embedding])
        except (TypeError, ValueError):
            return []

    return vectors
