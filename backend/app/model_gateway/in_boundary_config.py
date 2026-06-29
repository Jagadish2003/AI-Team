"""R17-D1 T4 - In-boundary model provider configuration.

This module owns the endpoint, auth, and model-name configuration for the
future ``in_boundary`` provider. It deliberately lives inside the model gateway
package so callers stay unaware of customer-private model endpoints and
credentials.

The customer operates the model endpoint inside their own network. CloudFulcrum
owns the adapter code that will read this config and call that endpoint.
"""
from __future__ import annotations

import os

# Provider name used by MODEL_GENERATION_PROVIDER / MODEL_EMBEDDING_PROVIDER.
IN_BOUNDARY_PROVIDER_NAME: str = "in_boundary"

# Common base URL. When endpoint-specific values are absent, OpenAI-compatible
# paths are derived from this root.
CONFIG_KEY_BASE_URL: str = "IN_BOUNDARY_BASE_URL"

# Endpoint-specific overrides. Use these when generation and embedding are
# served from different URLs or when the customer's server uses custom paths.
CONFIG_KEY_GENERATION_ENDPOINT: str = "IN_BOUNDARY_GENERATION_ENDPOINT"
CONFIG_KEY_EMBEDDING_ENDPOINT: str = "IN_BOUNDARY_EMBEDDING_ENDPOINT"

# Secret bearer token / API key for the customer-operated endpoint.
CONFIG_KEY_API_KEY: str = "IN_BOUNDARY_API_KEY"

# Model-name config. IN_BOUNDARY_MODEL is a common fallback; the generation and
# embedding names can be pinned independently.
CONFIG_KEY_MODEL: str = "IN_BOUNDARY_MODEL"
CONFIG_KEY_GENERATION_MODEL: str = "IN_BOUNDARY_GENERATION_MODEL"
CONFIG_KEY_EMBEDDING_MODEL: str = "IN_BOUNDARY_EMBEDDING_MODEL"

_DEFAULT_GENERATION_PATH: str = "/v1/chat/completions"
_DEFAULT_EMBEDDING_PATH: str = "/v1/embeddings"


def _clean_url(value: str) -> str:
    """Normalize configured URLs without inventing a default external endpoint."""
    return value.strip().rstrip("/")


def _endpoint_from_base(base_url: str, path: str) -> str:
    """Return an OpenAI-compatible endpoint path derived from a base URL."""
    if not base_url:
        return ""
    return f"{base_url}/{path.lstrip('/')}"


def _resolve_endpoint(endpoint_key: str, base_url: str, default_path: str) -> str:
    explicit = _clean_url(os.getenv(endpoint_key, ""))
    if explicit:
        return explicit
    return _endpoint_from_base(base_url, default_path)


def _resolve_model(specific_key: str) -> str:
    """Resolve a specific model name, falling back to IN_BOUNDARY_MODEL."""
    return (
        os.getenv(specific_key, "").strip()
        or os.getenv(CONFIG_KEY_MODEL, "").strip()
    )


class InBoundaryConfig:
    """Config holder for the customer-operated in-boundary endpoint.

    Non-secret endpoint values are read at construction. The auth token and
    model names are resolved live so token rotation and model repinning can
    happen without process restart.
    """

    __slots__ = (
        "_base_url",
        "_generation_endpoint",
        "_embedding_endpoint",
        "_api_key_at_init",
    )

    def __init__(self) -> None:
        self._base_url: str = _clean_url(os.getenv(CONFIG_KEY_BASE_URL, ""))
        self._generation_endpoint: str = _resolve_endpoint(
            CONFIG_KEY_GENERATION_ENDPOINT,
            self._base_url,
            _DEFAULT_GENERATION_PATH,
        )
        self._embedding_endpoint: str = _resolve_endpoint(
            CONFIG_KEY_EMBEDDING_ENDPOINT,
            self._base_url,
            _DEFAULT_EMBEDDING_PATH,
        )
        self._api_key_at_init: str = os.getenv(CONFIG_KEY_API_KEY, "")

    @property
    def base_url(self) -> str:
        """Configured customer endpoint root, if provided."""
        return self._base_url

    @property
    def generation_endpoint(self) -> str:
        """OpenAI-compatible chat/completions endpoint for generation."""
        return self._generation_endpoint

    @property
    def embedding_endpoint(self) -> str:
        """OpenAI-compatible embeddings endpoint."""
        return self._embedding_endpoint

    def generation_model(self) -> str:
        """Generation model name, resolved live from config."""
        return _resolve_model(CONFIG_KEY_GENERATION_MODEL)

    def embedding_model(self) -> str:
        """Embedding model name, resolved live from config."""
        return _resolve_model(CONFIG_KEY_EMBEDDING_MODEL)

    def resolve_api_key(self) -> str:
        """Return the live endpoint credential without logging it."""
        return os.getenv(CONFIG_KEY_API_KEY, "")

    def has_credential(self) -> bool:
        """True when a non-empty endpoint credential is configured."""
        return bool(self.resolve_api_key())

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            "InBoundaryConfig("
            f"base_url={self._base_url!r}, "
            f"generation_endpoint={self._generation_endpoint!r}, "
            f"embedding_endpoint={self._embedding_endpoint!r}, "
            "api_key='***REDACTED***'"
            ")"
        )

    __str__ = __repr__


__all__ = [
    "CONFIG_KEY_API_KEY",
    "CONFIG_KEY_BASE_URL",
    "CONFIG_KEY_EMBEDDING_ENDPOINT",
    "CONFIG_KEY_EMBEDDING_MODEL",
    "CONFIG_KEY_GENERATION_ENDPOINT",
    "CONFIG_KEY_GENERATION_MODEL",
    "CONFIG_KEY_MODEL",
    "IN_BOUNDARY_PROVIDER_NAME",
    "InBoundaryConfig",
]
