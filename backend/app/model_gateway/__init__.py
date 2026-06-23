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

  1.6 ships the 'hosted' provider (Anthropic API).  1.7 will add
  'in_boundary' and 'customer_tenant' by registering new implementations
  via ``register_provider()`` — no calling code changes required (AC7).
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
# Config key names and the R16-D1 1.6 default
# ---------------------------------------------------------------------------

_ENV_GENERATION: str = "MODEL_GENERATION_PROVIDER"
_ENV_EMBEDDING: str = "MODEL_EMBEDDING_PROVIDER"
_DEFAULT_PROVIDER: str = "hosted"


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
    _resolve_provider(gen_name, _ENV_GENERATION)
    _resolve_provider(emb_name, _ENV_EMBEDDING)
    logger.info(
        "model_gateway config validated: %s=%s %s=%s",
        _ENV_GENERATION, gen_name,
        _ENV_EMBEDDING, emb_name,
    )


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
    "validate_provider_config",
]
