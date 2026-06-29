"""R17-D1 T4 - In-boundary provider configuration contract tests.

Task 4 covers configuration only: endpoint/auth/model-name settings live inside
the model gateway package, and generation/embedding provider selection remains
independent. The actual HTTP adapter is implemented by the later provider task.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from app.model_gateway import (
    ModelProvider,
    _PROVIDER_REGISTRY,
    get_embedding_provider,
    get_generation_provider,
    register_provider,
)
from app.model_gateway._interface import GenerationRequest, GenerationResult
from app.model_gateway.in_boundary_config import (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_BASE_URL,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_EMBEDDING_MODEL,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CONFIG_KEY_GENERATION_MODEL,
    CONFIG_KEY_MODEL,
    IN_BOUNDARY_PROVIDER_NAME,
    InBoundaryConfig,
)

_SECRET = "ib-SECRET-DO-NOT-LOG-0123456789"
_ROTATED_SECRET = "ib-ROTATED-SECRET-0123456789"

_ALL_IN_BOUNDARY_KEYS = (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_BASE_URL,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_EMBEDDING_MODEL,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CONFIG_KEY_GENERATION_MODEL,
    CONFIG_KEY_MODEL,
)


class _InBoundaryConfigStub(ModelProvider):
    """Stub used only to prove config-level gateway selection."""

    name = IN_BOUNDARY_PROVIDER_NAME

    def generate(self, req: GenerationRequest) -> GenerationResult:
        return GenerationResult(text="in-boundary-stub", provider=self.name, ok=True)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[1.0]] * len(texts)


def _clear_in_boundary_env(monkeypatch) -> None:
    for key in _ALL_IN_BOUNDARY_KEYS:
        monkeypatch.delenv(key, raising=False)


def _ensure_in_boundary_provider_registered():
    """Return a registered provider named in_boundary and a cleanup function.

    This keeps the test future-proof: once Task 1 registers the real provider,
    these config tests reuse it instead of trying to replace it.
    """
    existing = _PROVIDER_REGISTRY.get(IN_BOUNDARY_PROVIDER_NAME)
    if existing is not None:
        return existing, lambda: None

    stub = _InBoundaryConfigStub()
    register_provider(stub)
    return stub, lambda: _PROVIDER_REGISTRY.pop(IN_BOUNDARY_PROVIDER_NAME, None)


def test_t4_config_module_lives_inside_gateway_and_is_not_re_exported():
    """Endpoint/auth/model config is owned by the gateway package."""
    import app.model_gateway as gateway

    assert InBoundaryConfig.__module__ == "app.model_gateway.in_boundary_config"
    assert "InBoundaryConfig" not in gateway.__all__


def test_t4_base_url_derives_provider_compatible_generation_and_embedding_paths(
    monkeypatch,
):
    """A common customer base URL produces separate gen/embedding endpoints."""
    _clear_in_boundary_env(monkeypatch)
    monkeypatch.setenv(CONFIG_KEY_BASE_URL, "https://models.customer.internal/")

    cfg = InBoundaryConfig()

    assert cfg.base_url == "https://models.customer.internal"
    assert (
        cfg.generation_endpoint
        == "https://models.customer.internal/v1/chat/completions"
    )
    assert cfg.embedding_endpoint == "https://models.customer.internal/v1/embeddings"


def test_t4_generation_and_embedding_endpoint_overrides_are_independent(monkeypatch):
    """Customers may expose generation and embedding at different URLs."""
    _clear_in_boundary_env(monkeypatch)
    monkeypatch.setenv(CONFIG_KEY_BASE_URL, "https://models.customer.internal")
    monkeypatch.setenv(
        CONFIG_KEY_GENERATION_ENDPOINT,
        "https://gen.customer.internal/chat",
    )
    monkeypatch.setenv(
        CONFIG_KEY_EMBEDDING_ENDPOINT,
        "https://emb.customer.internal/embeddings",
    )

    cfg = InBoundaryConfig()

    assert cfg.generation_endpoint == "https://gen.customer.internal/chat"
    assert cfg.embedding_endpoint == "https://emb.customer.internal/embeddings"


def test_t4_no_hardcoded_external_endpoint_when_unconfigured(monkeypatch):
    """Task 4 must not invent a hosted endpoint for in-boundary mode."""
    _clear_in_boundary_env(monkeypatch)

    cfg = InBoundaryConfig()

    assert cfg.base_url == ""
    assert cfg.generation_endpoint == ""
    assert cfg.embedding_endpoint == ""
    hosted_endpoint = "api.anthrop" + "ic.com"
    assert hosted_endpoint not in repr(cfg)


def test_t4_model_names_have_common_fallback_and_independent_live_pins(monkeypatch):
    """Generation and embedding model names can be pinned independently."""
    _clear_in_boundary_env(monkeypatch)
    monkeypatch.setenv(CONFIG_KEY_MODEL, "customer-common-model")
    cfg = InBoundaryConfig()

    assert cfg.generation_model() == "customer-common-model"
    assert cfg.embedding_model() == "customer-common-model"

    monkeypatch.setenv(CONFIG_KEY_GENERATION_MODEL, "customer-gen-model")
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_MODEL, "customer-embedding-model")

    assert cfg.generation_model() == "customer-gen-model"
    assert cfg.embedding_model() == "customer-embedding-model"


def test_t4_auth_token_is_config_sourced_live_and_redacted(monkeypatch):
    """The in-boundary credential is read from config and never exposed by repr."""
    _clear_in_boundary_env(monkeypatch)
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _SECRET)
    cfg = InBoundaryConfig()

    assert cfg.resolve_api_key() == _SECRET
    assert cfg.has_credential() is True
    assert _SECRET not in repr(cfg)
    assert "REDACTED" in repr(cfg)

    monkeypatch.setenv(CONFIG_KEY_API_KEY, _ROTATED_SECRET)
    assert cfg.resolve_api_key() == _ROTATED_SECRET
    assert _ROTATED_SECRET not in repr(cfg)


def test_t4_in_boundary_generation_can_be_selected_independently(monkeypatch):
    """MODEL_GENERATION_PROVIDER=in_boundary does not force embedding provider."""
    provider, cleanup = _ensure_in_boundary_provider_registered()
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")

        assert get_generation_provider() is provider
        assert get_embedding_provider().name == "hosted"
    finally:
        cleanup()


def test_t4_in_boundary_embedding_can_be_selected_independently(monkeypatch):
    """MODEL_EMBEDDING_PROVIDER=in_boundary does not force generation provider."""
    provider, cleanup = _ensure_in_boundary_provider_registered()
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)

        assert get_generation_provider().name == "hosted"
        assert get_embedding_provider() is provider
    finally:
        cleanup()


def test_t4_env_example_documents_in_boundary_config_keys():
    """backend/.env.example documents endpoint/auth/model config for Task 4."""
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    text = env_example.read_text(encoding="utf-8")

    assert "MODEL_GENERATION_PROVIDER" in text
    assert "MODEL_EMBEDDING_PROVIDER" in text
    assert IN_BOUNDARY_PROVIDER_NAME in text

    for key in _ALL_IN_BOUNDARY_KEYS:
        assert f"{key}=" in text, f"{key} missing from backend/.env.example"
