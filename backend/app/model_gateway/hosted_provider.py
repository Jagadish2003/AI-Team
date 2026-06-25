"""R16-D2 — Hosted model provider (public implementation).

``HostedModelProvider`` is the concrete ``ModelProvider`` for the Anthropic
hosted API (api.anthropic.com).  It is the default provider shipped with
AgentIQ 1.6 and the reference implementation that in-boundary and
customer-tenant modes (1.7) will follow.

Design notes
------------
- Credentials and endpoint config are owned entirely inside this module.
  No caller ever sees an API key or endpoint URL.
- generate() never raises — it returns GenerationResult(ok=False, text=None)
  on every failure so callers can rely on a stable return type.
- embed() returns an empty list (graceful degradation) because the Anthropic
  API does not yet expose a first-class embeddings endpoint.  Callers that
  need embeddings should configure MODEL_EMBEDDING_PROVIDER to a provider
  that supports them; this stub keeps the interface contract satisfied.
- Credentials are read from ANTHROPIC_API_KEY at call time (not at import
  time) so a key rotation never requires a restart.
- The model name is read from MODEL_NAME (default 'claude-sonnet-4-5') so
  operators can pin a specific version without a code change.

Resilience (R16-D2 T2)
-----------------------
- Transient errors (HTTP 429, 529) and network timeouts trigger bounded
  exponential-backoff retry.  Non-transient errors (4xx other than 429/529)
  are returned immediately without retry.
- The per-request ``timeout_ms`` is a hard wall-clock deadline for the
  *entire* generate() call including all retries and backoff sleeps.  Each
  individual HTTP attempt receives the remaining time in that budget, so the
  caller's leash (e.g. the hallucination guard's 500ms) is always honoured.
- Rate-limit responses (429) respect the ``Retry-After`` header when present;
  they never hammer a throttled endpoint.
- Retry is bounded by ``_MAX_RETRIES`` — the provider never retries
  indefinitely regardless of error type.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)

logger = logging.getLogger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_DEFAULT_MODEL = "claude-sonnet-4-5"

# Resilience constants (R16-D2 T2)
_MAX_RETRIES: int = 3                # bounded — never retries more than this
_BASE_BACKOFF_S: float = 0.5         # first backoff window: 0.5s, then 1s, 2s, 4s…
_MAX_BACKOFF_S: float = 8.0          # cap so a single backoff never exceeds 8s
_RETRYABLE_STATUS_CODES: frozenset = frozenset({429, 529})
_TRANSIENT_5XX_CODES: frozenset = frozenset({500, 502, 503, 504})
# Minimum remaining budget to start another attempt — skip if less than this
# to avoid an attempt that would instantly time out.
_MIN_ATTEMPT_BUDGET_S: float = 0.05


class HostedModelProvider(ModelProvider):
    """ModelProvider backed by the Anthropic hosted API.

    This is the D2 public implementation with production-grade resilience:
    bounded retry, exponential backoff, per-request deadline enforcement, and
    rate-limit-aware backoff.
    """

    name = "hosted"

    # ------------------------------------------------------------------
    # ModelProvider interface
    # ------------------------------------------------------------------

    def generate(self, req: GenerationRequest) -> GenerationResult:
        """Call the Anthropic messages API and return the first text block.

        Retries transient errors (429, 529, 5xx, network timeouts) with
        exponential backoff up to ``_MAX_RETRIES`` times.  The entire call
        (all attempts + backoff) is bounded by ``req.timeout_ms``.

        Returns ``GenerationResult(text=None, ok=False, provider='hosted')``
        on permanent failure — missing API key, non-transient HTTP error, or
        exhausted retries/budget.  Never raises.
        """
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY not set — model generation skipped")
            return GenerationResult(text=None, provider=self.name, ok=False)

        model = os.getenv("MODEL_NAME", _DEFAULT_MODEL)
        deadline = time.monotonic() + req.timeout_ms / 1000.0

        for attempt in range(_MAX_RETRIES + 1):
            remaining_s = deadline - time.monotonic()
            if remaining_s < _MIN_ATTEMPT_BUDGET_S:
                logger.warning(
                    "HostedModelProvider: timeout budget exhausted after %d attempt(s)",
                    attempt,
                )
                return GenerationResult(text=None, provider=self.name, ok=False)

            result, retryable, retry_after_s = self._attempt_generate(
                api_key=api_key,
                model=model,
                req=req,
                timeout_s=remaining_s,
            )

            if result.ok:
                return result

            if not retryable or attempt >= _MAX_RETRIES:
                return result

            # T3 contract: exhaustion returns ok=False/text=None/provider='hosted'
            # without raising.  The caller sees the same stable shape regardless
            # of whether the failure came from a missing key, a 429, or all
            # retries being spent — existing callers already handle text=None.

            # Compute backoff: exponential, capped, respects Retry-After header.
            backoff_s = min(_BASE_BACKOFF_S * (2 ** attempt), _MAX_BACKOFF_S)
            if retry_after_s is not None:
                # Retry-After overrides the computed backoff (rate-limit signal).
                backoff_s = max(backoff_s, retry_after_s)

            remaining_after_backoff = deadline - time.monotonic() - backoff_s
            if remaining_after_backoff < _MIN_ATTEMPT_BUDGET_S:
                # Not enough budget left for backoff + a meaningful attempt.
                logger.warning(
                    "HostedModelProvider: insufficient budget for backoff on "
                    "attempt %d — returning failure",
                    attempt,
                )
                return GenerationResult(text=None, provider=self.name, ok=False)

            logger.info(
                "HostedModelProvider: attempt %d failed (retryable); "
                "backing off %.2fs before retry",
                attempt,
                backoff_s,
            )
            time.sleep(backoff_s)

        # Unreachable — the loop always returns, but satisfies type checkers.
        return GenerationResult(text=None, provider=self.name, ok=False)  # pragma: no cover

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Return embedding vectors for each input string.

        The Anthropic API does not yet expose a first-class embeddings endpoint.
        Returns an empty list so callers degrade gracefully.  Configure
        MODEL_EMBEDDING_PROVIDER to a provider that supports embeddings when
        vector search is required.
        """
        if texts:
            logger.debug(
                "HostedModelProvider.embed called for %d texts — "
                "embeddings not available on the hosted provider; returning []",
                len(texts),
            )
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _attempt_generate(
        self,
        api_key: str,
        model: str,
        req: GenerationRequest,
        timeout_s: float,
    ) -> Tuple[GenerationResult, bool, Optional[float]]:
        """Perform one HTTP call to the Anthropic messages API.

        Returns a 3-tuple:
            (GenerationResult, retryable: bool, retry_after_s: float | None)

        ``retryable`` is True for transient errors (429, 529, 5xx, timeouts).
        ``retry_after_s`` carries the Retry-After header value when the server
        signals rate-limiting; None otherwise.
        """
        payload = json.dumps(
            {
                "model": model,
                "max_tokens": req.max_tokens,
                "messages": [{"role": "user", "content": req.prompt}],
            }
        ).encode("utf-8")

        http_req = urllib.request.Request(
            _API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": _API_VERSION,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(http_req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return (
                            GenerationResult(
                                text=block["text"].strip(),
                                provider=self.name,
                                ok=True,
                            ),
                            False,
                            None,
                        )
            # Response parsed but no text block found — not retryable.
            return GenerationResult(text=None, provider=self.name, ok=False), False, None

        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8", errors="replace")[:200]

            if status in _RETRYABLE_STATUS_CODES:
                retry_after_s = _parse_retry_after(exc)
                logger.warning(
                    "HostedModelProvider HTTP %s (rate-limited/overloaded): %s",
                    status,
                    body,
                )
                return (
                    GenerationResult(text=None, provider=self.name, ok=False),
                    True,
                    retry_after_s,
                )

            if status in _TRANSIENT_5XX_CODES:
                logger.warning("HostedModelProvider HTTP %s (transient): %s", status, body)
                return (
                    GenerationResult(text=None, provider=self.name, ok=False),
                    True,
                    None,
                )

            # Non-transient 4xx or other — return immediately, do not retry.
            logger.error("HostedModelProvider HTTP %s: %s", status, body)
            return GenerationResult(text=None, provider=self.name, ok=False), False, None

        except TimeoutError:
            # Network timeout is transient — retry within remaining budget.
            logger.warning("HostedModelProvider: request timed out (%.2fs budget)", timeout_s)
            return GenerationResult(text=None, provider=self.name, ok=False), True, None

        except Exception as exc:
            # Treat unexpected network/OS errors as transient.
            logger.error("HostedModelProvider error: %s", exc)
            return GenerationResult(text=None, provider=self.name, ok=False), True, None


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
