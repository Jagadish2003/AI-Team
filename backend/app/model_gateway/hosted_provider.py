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
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import List

from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)

logger = logging.getLogger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_DEFAULT_MODEL = "claude-sonnet-4-5"


class HostedModelProvider(ModelProvider):
    """ModelProvider backed by the Anthropic hosted API.

    This is the D2 public implementation.  Subsequent D2 tasks (T2 resilience,
    T4 credential/config handling) extend this class without affecting callers.
    """

    name = "hosted"

    # ------------------------------------------------------------------
    # ModelProvider interface
    # ------------------------------------------------------------------

    def generate(self, req: GenerationRequest) -> GenerationResult:
        """Call the Anthropic messages API and return the first text block.

        Returns ``GenerationResult(text=None, ok=False, provider='hosted')``
        on any failure — missing API key, HTTP error, JSON parse error, or
        network timeout.  Never raises.
        """
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY not set — model generation skipped")
            return GenerationResult(text=None, provider=self.name, ok=False)

        model = os.getenv("MODEL_NAME", _DEFAULT_MODEL)
        timeout_s = req.timeout_ms / 1000.0

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
                        return GenerationResult(
                            text=block["text"].strip(),
                            provider=self.name,
                            ok=True,
                        )
            return GenerationResult(text=None, provider=self.name, ok=False)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:200]
            logger.error("HostedModelProvider HTTP %s: %s", exc.code, body)
            return GenerationResult(text=None, provider=self.name, ok=False)

        except Exception as exc:
            logger.error("HostedModelProvider error: %s", exc)
            return GenerationResult(text=None, provider=self.name, ok=False)

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
