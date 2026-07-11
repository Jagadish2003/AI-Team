"""R17-D1 T1/T3/T5 - In-boundary model provider implementation.

``InBoundaryModelProvider`` is the gateway adapter for a customer-operated
model endpoint running inside the customer's own network. CloudFulcrum owns
this adapter; the customer owns and operates the model serving infrastructure.

The endpoint is expected to be OpenAI-compatible, for example vLLM or TGI
serving chat completions and embeddings. All endpoint, auth, and model-name
configuration is read from ``in_boundary_config`` inside the gateway package,
so callers do not know which backend is active.

Resilience (R17-D1 T3, mirrors R16-D2 hosted mode)
--------------------------------------------------
The in-boundary provider follows the same resilience posture as the hosted
reference implementation so customers get consistent behaviour regardless of
provider mode:

- Transient HTTP errors (rate-limit 429/529, temporary 5xx) and network
  timeouts trigger bounded exponential-backoff retry.  Non-transient errors
  (other 4xx) are returned immediately without retry.
- The per-request ``timeout_ms`` is a wall-clock deadline for the *entire*
  generate() call including all retries and backoff sleeps.  The first attempt
  receives the full configured budget; each retry receives only the time
  remaining in that budget, so the caller's leash is always honoured.
- Rate-limit responses (429) respect the ``Retry-After`` header when present so
  a throttled endpoint is never hammered.
- Retry is bounded by ``_MAX_RETRIES`` — the provider never retries
  indefinitely regardless of error type.

Graceful failure (R17-D1 T3)
----------------------------
``generate()`` must never raise into the application.  Missing config, malformed
responses, transport errors, and exhausted retries/deadlines all become
``GenerationResult(text=None, provider='in_boundary', ok=False)``.  ``embed()``
degrades to an empty list on every failure path.  AgentIQ's model output is
consumed as proposed/inferred content, so when the model is unavailable the
system surfaces fewer findings rather than unsafe or unverified facts.

Provider-identity telemetry (R17-D1 T5, mirrors R16-D2 hosted mode)
-------------------------------------------------------------------
Like the hosted provider, this provider records its OWN per-call telemetry so
every in-boundary call is observable no matter how it is invoked — directly or
through the gateway.  ``emits_own_telemetry = True`` tells the gateway wrappers
to skip their emission, so a single logical call always produces exactly one
event (never a duplicate).

- ``generate()`` emits exactly one ``model.generation_completed`` event after
  the call completes; ``embed()`` emits exactly one ``model.embedding_completed``
  event — on success and failure alike, never per retry, never per token.
- Every event carries ``provider='in_boundary'`` so support/audit teams (and
  regulated customers) can prove which provider served each model call.
- PII GUARD: the payload carries only the provider name, the ok flag, and
  (for embeddings) text/vector counts.  It NEVER carries prompt text, generated
  output, input texts, embedding vectors, endpoint credentials, or any customer
  secret.
- Telemetry is best-effort: a DB/import problem is swallowed and never
  propagates into the model call, preserving the graceful-failure contract.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, List, Optional, Tuple

from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.model_gateway.in_boundary_config import (
    CONFIG_KEY_BASE_URL,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_EMBEDDING_MODEL,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CONFIG_KEY_GENERATION_MODEL,
    CONFIG_KEY_MODEL,
    IN_BOUNDARY_PROVIDER_NAME,
    InBoundaryConfig,
)

logger = logging.getLogger(__name__)

_DEFAULT_EMBED_TIMEOUT_S: float = 30.0

# Resilience constants (R17-D1 T3 — same posture as hosted_provider).
_MAX_RETRIES: int = 3                # bounded — never retries more than this
_BASE_BACKOFF_S: float = 0.5         # first backoff window: 0.5s, then 1s, 2s, 4s…
_MAX_BACKOFF_S: float = 8.0          # cap so a single backoff never exceeds 8s
_RETRYABLE_STATUS_CODES: frozenset = frozenset({429, 529})
_TRANSIENT_5XX_CODES: frozenset = frozenset({500, 502, 503, 504})
# Minimum remaining budget to start another attempt — skip if less than this
# to avoid an attempt that would instantly time out.
_MIN_ATTEMPT_BUDGET_S: float = 0.05

# Telemetry event types (R17-D1 T5). These are the same event types the gateway
# and hosted provider use — registered in app.telemetry — so in-boundary usage
# is observable through the existing telemetry_events store with no new table.
_GENERATION_EVENT: str = "model.generation_completed"
_EMBEDDING_EVENT: str = "model.embedding_completed"


def _record_in_boundary_telemetry(event_type: str, payload: dict) -> None:
    """Emit one telemetry event for a completed in-boundary call (R17-D1 T5).

    Imported lazily to avoid a circular import at module load time (the same
    pattern the gateway and hosted provider use).  Wrapped so a telemetry
    failure never propagates to the caller — model calls are not conditional on
    telemetry succeeding.

    PII GUARD: callers pass only the provider name, the ok flag, and counts.
    Prompt text, generated output, input texts, embedding vectors, and
    credentials must never appear in ``payload``.
    """
    try:
        from app.telemetry import record_event

        record_event(event_type, payload)
    except Exception:  # pragma: no cover - telemetry is best-effort
        logger.debug(
            "in_boundary_provider: telemetry emit failed for %s",
            event_type,
            exc_info=True,
        )


class InBoundaryModelProvider(ModelProvider):
    """ModelProvider backed by a customer-operated in-network endpoint."""

    name = IN_BOUNDARY_PROVIDER_NAME

    # This provider records its own per-call telemetry (T5), so the gateway's
    # instrumented generate()/embed() wrappers skip their emission — one logical
    # call always produces exactly one event, never a duplicate.
    emits_own_telemetry = True

    def __init__(self) -> None:
        # Snapshot-at-init, used ONLY for the startup log line below. The live
        # per-call config is re-read fresh inside _run_generate()/_run_embed()
        # (each constructs a new InBoundaryConfig) so endpoint/token/model
        # changes take effect without a restart. Do NOT treat self._config as
        # the live config source — read a fresh InBoundaryConfig() instead.
        self._config = InBoundaryConfig()
        logger.info(
            "InBoundaryModelProvider initialised "
            "(generation_endpoint_configured=%s, embedding_endpoint_configured=%s)",
            bool(self._config.generation_endpoint),
            bool(self._config.embedding_endpoint),
        )

    def validate(self) -> None:
        """Warn at startup when in_boundary is selected but not fully configured.

        Called by ``validate_provider_config()`` only when in_boundary is the
        selected generation and/or embedding provider. The runtime
        graceful-failure contract (every call returns ok=False / [] when the
        endpoint is unset) is correct for transient failures, but it is NOT an
        acceptable *sole* signal for a boot-time misconfiguration: an operator
        who sets ``MODEL_GENERATION_PROVIDER=in_boundary`` without any endpoint
        URL would otherwise see the system silently surface no findings, run no
        enrichment, and emit no error. Surface a clear warning instead.

        Never raises — startup must not be blocked.
        """
        cfg = InBoundaryConfig()
        if not (cfg.base_url or cfg.generation_endpoint or cfg.embedding_endpoint):
            logger.warning(
                "in_boundary provider is active but no endpoint URL is configured "
                "(%s / %s / %s all unset) — all model calls will fail "
                "(generation returns ok=False, embedding returns []) until an "
                "endpoint is configured.",
                CONFIG_KEY_BASE_URL,
                CONFIG_KEY_GENERATION_ENDPOINT,
                CONFIG_KEY_EMBEDDING_ENDPOINT,
            )
            return

        # Endpoint present but no model name is also a guaranteed-failure config.
        if not (cfg.generation_model() or cfg.embedding_model()):
            logger.warning(
                "in_boundary provider has an endpoint configured but no model name "
                "(%s / %s / %s all unset) — model calls will fail until a model "
                "name is configured.",
                CONFIG_KEY_MODEL,
                CONFIG_KEY_GENERATION_MODEL,
                CONFIG_KEY_EMBEDDING_MODEL,
            )

    def generate(self, req: GenerationRequest) -> GenerationResult:
        """Generate text through the in-boundary endpoint, recording telemetry.

        Performs the resilient generation call and emits exactly one
        ``model.generation_completed`` telemetry event after it completes — on
        success and failure alike (T5). The event carries ``provider='in_boundary'``
        and the ok flag only; never the prompt or generated text.
        """
        result = self._run_generate(req)
        _record_in_boundary_telemetry(
            _GENERATION_EVENT,
            {"provider": result.provider, "ok": result.ok},
        )
        return result

    def _run_generate(self, req: GenerationRequest) -> GenerationResult:
        """Resilient generation call. Telemetry is emitted once by generate().

        Applies the hosted-mode resilience posture: transient failures (429/529,
        transient 5xx, network timeouts) are retried with bounded exponential
        backoff inside the ``req.timeout_ms`` wall-clock deadline.

        Never raises to callers. Missing config, malformed responses, transport
        errors, and exhausted retries/deadlines all become
        ``GenerationResult(text=None, provider='in_boundary', ok=False)``.
        """
        cfg = InBoundaryConfig()
        if not cfg.generation_endpoint:
            logger.warning(
                "InBoundaryModelProvider generation skipped: %s or %s not configured",
                CONFIG_KEY_GENERATION_ENDPOINT,
                CONFIG_KEY_BASE_URL,
            )
            return GenerationResult(text=None, provider=self.name, ok=False)

        model = cfg.generation_model()
        if not model:
            logger.warning(
                "InBoundaryModelProvider generation skipped: %s not configured",
                CONFIG_KEY_MODEL,
            )
            return GenerationResult(text=None, provider=self.name, ok=False)

        payload = {
            "model": model,
            "max_tokens": req.max_tokens,
            "messages": [{"role": "user", "content": req.prompt}],
        }

        # Never raises: the resilient POST returns None on every failure path.
        data = self._post_with_resilience(
            url=cfg.generation_endpoint,
            payload=payload,
            api_key=cfg.resolve_api_key(),
            total_timeout_s=max(req.timeout_ms / 1000.0, _MIN_ATTEMPT_BUDGET_S),
            label="generation",
        )
        if data is None:
            return GenerationResult(text=None, provider=self.name, ok=False)

        text = _extract_generation_text(data)
        if text is None:
            logger.warning(
                "InBoundaryModelProvider generation response had no text content"
            )
            return GenerationResult(text=None, provider=self.name, ok=False)
        return GenerationResult(text=text, provider=self.name, ok=True)

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Return embeddings from the in-boundary endpoint, recording telemetry.

        Performs the resilient embedding call and emits exactly one
        ``model.embedding_completed`` telemetry event after it completes (T5).
        The event carries ``provider='in_boundary'``, the ok flag, and the
        text/vector counts only — never the input texts or the vectors.
        ``ok`` is True when a vector was returned for every input (an empty-input
        no-op is vacuously ok).
        """
        vectors = self._run_embed(texts)
        _record_in_boundary_telemetry(
            _EMBEDDING_EVENT,
            {
                "provider": self.name,
                "ok": len(vectors) == len(texts),
                "text_count": len(texts),
                "vector_count": len(vectors),
            },
        )
        return vectors

    def embedding_identity(self) -> "tuple[str, str]":
        """Stamp identity/version for in-boundary vectors (R18-B1 T3 / AC8).

        Identity qualifies the provider name with the configured embedding model
        so repinning ``IN_BOUNDARY_EMBEDDING_MODEL`` to a different model yields a
        distinct identity — retrieval then never compares the old model's vectors
        against the new one's. Read live from a fresh config, like ``_run_embed``.
        The in-boundary OpenAI-compatible endpoint carries no separate model
        version, so the version component is the model name itself (an empty model
        name falls back to the base ``(name, "")``).
        """
        model = InBoundaryConfig().embedding_model()
        if not model:
            return (self.name, "")
        return (f"{self.name}:{model}", model)

    def _run_embed(self, texts: List[str]) -> List[List[float]]:
        """Resilient embedding call. Telemetry is emitted once by embed().

        Applies the same resilience posture as generation: transient failures
        are retried with bounded backoff inside a wall-clock deadline. Empty
        input is a successful no-op. Any endpoint/config/parse failure or
        exhausted retries returns an empty list so embedding callers degrade
        safely instead of raising.
        """
        if not texts:
            return []

        cfg = InBoundaryConfig()
        if not cfg.embedding_endpoint:
            logger.warning(
                "InBoundaryModelProvider embedding skipped: %s or %s not configured",
                CONFIG_KEY_EMBEDDING_ENDPOINT,
                CONFIG_KEY_BASE_URL,
            )
            return []

        model = cfg.embedding_model()
        if not model:
            logger.warning(
                "InBoundaryModelProvider embedding skipped: %s not configured",
                CONFIG_KEY_MODEL,
            )
            return []

        payload = {"model": model, "input": texts}

        # Never raises: the resilient POST returns None on every failure path.
        data = self._post_with_resilience(
            url=cfg.embedding_endpoint,
            payload=payload,
            api_key=cfg.resolve_api_key(),
            total_timeout_s=_DEFAULT_EMBED_TIMEOUT_S,
            label="embedding",
        )
        if data is None:
            return []

        vectors = _extract_embedding_vectors(data, expected_count=len(texts))
        if len(vectors) != len(texts):
            logger.warning(
                "InBoundaryModelProvider embedding response count mismatch "
                "(texts=%d vectors=%d)",
                len(texts),
                len(vectors),
            )
            return []
        return vectors

    # ------------------------------------------------------------------
    # Resilience helpers (R17-D1 T3)
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
        ``None`` once a non-transient error occurs, the retry budget is spent,
        or the deadline is exhausted. Never raises — graceful failure is the
        contract both generate() and embed() rely on.

        The first attempt receives the full ``total_timeout_s`` budget; each
        retry receives only the time remaining in that budget, so the entire
        call (all attempts + backoff sleeps) stays within the deadline.
        """
        deadline = time.monotonic() + total_timeout_s

        for attempt in range(_MAX_RETRIES + 1):
            if attempt == 0:
                attempt_timeout_s = total_timeout_s
            else:
                attempt_timeout_s = deadline - time.monotonic()
                if attempt_timeout_s < _MIN_ATTEMPT_BUDGET_S:
                    logger.warning(
                        "InBoundaryModelProvider %s: timeout budget exhausted "
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
                    "InBoundaryModelProvider %s: insufficient budget for backoff "
                    "on attempt %d — returning failure",
                    label,
                    attempt,
                )
                return None

            logger.info(
                "InBoundaryModelProvider %s: attempt %d failed (retryable); "
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
        """Perform one HTTP POST to the customer endpoint.

        Returns a 4-tuple ``(ok, data, retryable, retry_after_s)``:
            ok           True only on an HTTP 200 with a decodable JSON body.
            data         The decoded JSON response when ``ok`` is True, else None.
            retryable    True for transient failures (429/529, transient 5xx,
                         network timeouts, unexpected transport errors); False
                         for non-transient HTTP errors that should not be retried.
            retry_after_s  The Retry-After header value when the server signals
                         rate-limiting; None otherwise.

        Never raises — every failure mode maps onto the returned tuple.

        Known limitation — socket vs. total read time:
            ``urllib.request.urlopen(..., timeout=timeout_s)`` sets a *socket*
            timeout: each individual socket operation (connect, each recv) must
            complete within ``timeout_s``. It does NOT bound the total wall-clock
            time to read the full response body, so a slow endpoint that trickles
            a large body in many sub-``timeout_s`` chunks could let a single
            attempt exceed ``timeout_s``. The deadline check in
            ``_post_with_resilience`` guards *between* attempts, not *within* one
            urlopen. This is acceptable here because in-boundary responses are not
            streamed chunk-by-chunk to urllib and ``max_tokens`` bounds the body
            size. If full read-time coverage is ever required, switch to
            ``httpx.Client`` with ``httpx.Timeout(read=timeout_s, connect=...)``,
            which the rest of the codebase already uses for dedicated read
            timeouts.
        """
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

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
                    "InBoundaryModelProvider HTTP %s (rate-limited/overloaded): %s",
                    status,
                    body,
                )
                return False, None, True, retry_after_s

            if status in _TRANSIENT_5XX_CODES:
                logger.warning(
                    "InBoundaryModelProvider HTTP %s (transient): %s", status, body
                )
                return False, None, True, None

            # Non-transient 4xx or other — return immediately, do not retry.
            logger.error("InBoundaryModelProvider HTTP %s: %s", status, body)
            return False, None, False, None

        except TimeoutError:
            # Network timeout is transient — retry within the remaining budget.
            logger.warning(
                "InBoundaryModelProvider: request timed out (%.2fs budget)", timeout_s
            )
            return False, None, True, None

        except Exception as exc:
            # Treat unexpected network/OS errors (incl. URLError) as transient.
            logger.error("InBoundaryModelProvider transport error: %s", exc)
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
    """Extract input-ordered embedding vectors from a provider-compatible response."""
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
