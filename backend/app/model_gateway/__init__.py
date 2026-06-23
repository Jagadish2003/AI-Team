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

Provider resolution
-------------------
  MODEL_GENERATION_PROVIDER env var (default: 'hosted')
  MODEL_EMBEDDING_PROVIDER  env var (default: 'hosted')

  'hosted' → AnthropicHostedProvider (Anthropic API, api.anthropic.com)

  New providers are registered with register_provider().  Adding one requires
  no change to any calling code (AC7 of the parent story R16-D1).
"""
from __future__ import annotations

import logging
import os
from typing import Dict

from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)

logger = logging.getLogger(__name__)

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


def _resolve_provider(name: str, capability: str) -> ModelProvider:
    """Look up ``name`` in the registry; raise clearly if not found."""
    provider = _PROVIDER_REGISTRY.get(name)
    if provider is None:
        registered = list(_PROVIDER_REGISTRY.keys())
        raise KeyError(
            f"No '{capability}' provider registered under name '{name}'. "
            f"Registered providers: {registered}. "
            "Call register_provider() before using the gateway."
        )
    return provider


# ---------------------------------------------------------------------------
# Public entry points — the ONE interface the whole platform uses
# ---------------------------------------------------------------------------


def get_generation_provider() -> ModelProvider:
    """Return the active text-generation provider.

    Resolved independently from MODEL_GENERATION_PROVIDER (default: 'hosted').
    Changing this env var does not affect the embedding provider.
    """
    name = os.getenv("MODEL_GENERATION_PROVIDER", "hosted")
    return _resolve_provider(name, "generation")


def get_embedding_provider() -> ModelProvider:
    """Return the active embedding provider.

    Resolved independently from MODEL_EMBEDDING_PROVIDER (default: 'hosted').
    Changing this env var does not affect the generation provider.
    """
    name = os.getenv("MODEL_EMBEDDING_PROVIDER", "hosted")
    return _resolve_provider(name, "embedding")


# ---------------------------------------------------------------------------
# Bootstrap: register the default 'hosted' provider at import time.
# Imported at the bottom to avoid a circular import: _hosted.py imports
# ModelProvider/GenerationRequest/GenerationResult from _interface.py, not
# from this module, so there is no cycle.
# ---------------------------------------------------------------------------

from app.model_gateway._hosted import AnthropicHostedProvider as _AnthropicHostedProvider  # noqa: E402

register_provider(_AnthropicHostedProvider())


__all__ = [
    "GenerationRequest",
    "GenerationResult",
    "ModelProvider",
    "get_generation_provider",
    "get_embedding_provider",
    "register_provider",
]
