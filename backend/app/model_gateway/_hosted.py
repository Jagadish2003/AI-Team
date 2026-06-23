"""R16-D1 — Hosted (Anthropic) model provider.

This is the sole file in the gateway package that is permitted to reference
the Anthropic API endpoint, SDK, or api-key header.  All LLM calls that
previously lived in llm_enrichment.py, hallucination_guard.py, and
normalization_enrichment.py are migrated to route through this provider (T3).

Implementation notes
--------------------
- generate() mirrors the exact behaviour of the old _call_claude() helpers:
  missing API key → ok=False / text=None (no exception, no log noise beyond
  a single WARNING).
- The HTTP timeout comes from GenerationRequest.timeout_ms — callers that
  need the hallucination-guard's 500ms leash pass timeout_ms=500; normal
  enrichment passes timeout_ms=30000 (the default).
- embed() returns an empty list — the Anthropic API does not yet offer a
  first-class embeddings endpoint; this satisfies the interface contract and
  allows callers to degrade gracefully.
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


class AnthropicHostedProvider(ModelProvider):
    """Model provider backed by the Anthropic hosted API (api.anthropic.com).

    Credentials are read from ANTHROPIC_API_KEY at call time so a key rotation
    does not require a restart.  The model is read from MODEL_NAME (default
    'claude-sonnet-4-5') to allow operators to pin a specific version.
    """

    name = "hosted"

    # ------------------------------------------------------------------
    # ModelProvider interface
    # ------------------------------------------------------------------

    def generate(self, req: GenerationRequest) -> GenerationResult:
        """Call the Anthropic messages API and return the first text block.

        Returns ``GenerationResult(text=None, ok=False, provider='hosted')``
        on any failure — HTTP errors, JSON parse errors, missing API key, or
        network timeouts.  Never raises.
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
            # Response parsed but no text block found
            return GenerationResult(text=None, provider=self.name, ok=False)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:200]
            logger.error("Hosted provider HTTP %s: %s", exc.code, body)
            return GenerationResult(text=None, provider=self.name, ok=False)

        except Exception as exc:
            logger.error("Hosted provider error: %s", exc)
            return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Return embedding vectors.

        The Anthropic API does not yet expose a first-class embeddings
        endpoint.  Returns an empty list so callers degrade gracefully.
        This stub satisfies the ModelProvider interface contract and will be
        replaced when the endpoint is available (or when an in-boundary
        embedding provider is configured via MODEL_EMBEDDING_PROVIDER).
        """
        if texts:
            logger.debug(
                "AnthropicHostedProvider.embed called for %d texts — "
                "embeddings not yet available on the hosted provider; returning []",
                len(texts),
            )
        return []
