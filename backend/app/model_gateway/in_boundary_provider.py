"""R17-D1 T1 - In-boundary model provider implementation.

``InBoundaryModelProvider`` is the gateway adapter for a customer-operated
model endpoint running inside the customer's own network. CloudFulcrum owns
this adapter; the customer owns and operates the model serving infrastructure.

The endpoint is expected to be OpenAI-compatible, for example vLLM or TGI
serving chat completions and embeddings. All endpoint, auth, and model-name
configuration is read from ``in_boundary_config`` inside the gateway package,
so callers do not know which backend is active.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, List, Optional

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

        The provider never raises to callers. Missing config, malformed
        responses, and transport errors all become
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

        try:
            data = self._post_json(
                url=cfg.generation_endpoint,
                payload=payload,
                api_key=cfg.resolve_api_key(),
                timeout_s=max(req.timeout_ms / 1000.0, 0.001),
            )
            text = _extract_generation_text(data)
            if text is None:
                logger.warning(
                    "InBoundaryModelProvider generation response had no text content"
                )
                return GenerationResult(text=None, provider=self.name, ok=False)
            return GenerationResult(text=text, provider=self.name, ok=True)
        except Exception as exc:
            logger.error("InBoundaryModelProvider generation failed: %s", exc)
            return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Return embeddings from the configured in-boundary endpoint.

        Empty input is a successful no-op. Any endpoint/config/parse failure
        returns an empty list so embedding callers degrade safely.
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

        try:
            data = self._post_json(
                url=cfg.embedding_endpoint,
                payload=payload,
                api_key=cfg.resolve_api_key(),
                timeout_s=_DEFAULT_EMBED_TIMEOUT_S,
            )
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
        except Exception as exc:
            logger.error("InBoundaryModelProvider embedding failed: %s", exc)
            return []

    def _post_json(
        self,
        *,
        url: str,
        payload: dict,
        api_key: str,
        timeout_s: float,
    ) -> Any:
        """POST JSON to the customer endpoint and decode the JSON response."""
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
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc

        return json.loads(raw)


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
