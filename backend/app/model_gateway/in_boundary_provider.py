"""R17-D1 T1/T3 - In-boundary model provider implementation.

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
    CONFIG_KEY_GENERATION_ENDPOINT,
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


class InBoundaryModelProvider(ModelProvider):
    """ModelProvider backed by a customer-operated in-network endpoint."""

    name = IN_BOUNDARY_PROVIDER_NAME

    def __init__(self) -> None:
        self._config = InBoundaryConfig()
        logger.info(
            "InBoundaryModelProvider initialised "
            "(generation_endpoint_configured=%s, embedding_endpoint_configured=%s)",
            bool(self._config.generation_endpoint),
            bool(self._config.embedding_endpoint),
        )

    def generate(self, req: GenerationRequest) -> GenerationResult:
        """Generate text through the configured in-boundary endpoint.

        Applies the hosted-mode resilience posture: transient failures (429/529,
        transient 5xx, network timeouts) are retried with bounded exponential
        backoff inside the ``req.timeout_ms`` wall-clock deadline.

        The provider never raises to callers. Missing config, malformed
        responses, transport errors, and exhausted retries/deadlines all become
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
        """Return embeddings from the configured in-boundary endpoint.

        Applies the same resilience posture as generate(): transient failures
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
