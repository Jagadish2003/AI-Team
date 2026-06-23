"""R16-D1 T1 — Model Provider Gateway contract tests.

Verifies every T1 acceptance criterion:

  T1-AC1  backend/app/model_gateway/__init__.py exists and is importable.
  T1-AC2  ModelProvider is an abstract base class with generate() and
          embed() abstract methods.
  T1-AC3  GenerationRequest dataclass has prompt, max_tokens, and
          timeout_ms (default 30000) fields.
  T1-AC4  GenerationResult dataclass has text (str or None), provider (str),
          and ok (bool) fields.
  T1-AC5  get_generation_provider() and get_embedding_provider() are
          independently callable and return a ModelProvider instance each.
  T1-AC6  Generation and embedding providers are resolved independently —
          configuring one does not affect the other.
"""

import inspect
import os
from abc import ABC
from typing import List, Optional
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# T1-AC1 — importable with no errors
# ---------------------------------------------------------------------------


def test_ac1_package_importable():
    """backend/app/model_gateway/__init__.py exists and is importable."""
    import app.model_gateway as gw  # noqa: F401

    # All expected public names must be present
    for name in (
        "GenerationRequest",
        "GenerationResult",
        "ModelProvider",
        "get_generation_provider",
        "get_embedding_provider",
        "register_provider",
    ):
        assert hasattr(gw, name), f"model_gateway missing '{name}'"


# ---------------------------------------------------------------------------
# T1-AC2 — ModelProvider is an ABC with generate() and embed() abstract methods
# ---------------------------------------------------------------------------


def test_ac2_model_provider_is_abc():
    """ModelProvider is an abstract base class."""
    from app.model_gateway import ModelProvider

    assert issubclass(ModelProvider, ABC), "ModelProvider must subclass ABC"


def test_ac2_generate_is_abstract():
    """ModelProvider.generate() is an abstract method."""
    from app.model_gateway import ModelProvider

    assert "generate" in ModelProvider.__abstractmethods__, (
        "generate() must be declared abstractmethod on ModelProvider"
    )


def test_ac2_embed_is_abstract():
    """ModelProvider.embed() is an abstract method."""
    from app.model_gateway import ModelProvider

    assert "embed" in ModelProvider.__abstractmethods__, (
        "embed() must be declared abstractmethod on ModelProvider"
    )


def test_ac2_cannot_instantiate_model_provider_directly():
    """ModelProvider cannot be instantiated — it is abstract."""
    from app.model_gateway import ModelProvider

    with pytest.raises(TypeError):
        ModelProvider()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# T1-AC3 — GenerationRequest dataclass fields
# ---------------------------------------------------------------------------


def test_ac3_generation_request_has_prompt():
    from app.model_gateway import GenerationRequest

    req = GenerationRequest(prompt="hello", max_tokens=10)
    assert req.prompt == "hello"


def test_ac3_generation_request_has_max_tokens():
    from app.model_gateway import GenerationRequest

    req = GenerationRequest(prompt="x", max_tokens=512)
    assert req.max_tokens == 512


def test_ac3_generation_request_timeout_ms_defaults_to_30000():
    """timeout_ms must default to 30000 as specified in R16-D1 §1."""
    from app.model_gateway import GenerationRequest

    req = GenerationRequest(prompt="x", max_tokens=1)
    assert req.timeout_ms == 30000


def test_ac3_generation_request_timeout_ms_is_overridable():
    from app.model_gateway import GenerationRequest

    req = GenerationRequest(prompt="x", max_tokens=1, timeout_ms=500)
    assert req.timeout_ms == 500


def test_ac3_generation_request_is_dataclass():
    """GenerationRequest must be a dataclass (has __dataclass_fields__)."""
    from app.model_gateway import GenerationRequest
    import dataclasses

    assert dataclasses.is_dataclass(GenerationRequest)


# ---------------------------------------------------------------------------
# T1-AC4 — GenerationResult dataclass fields
# ---------------------------------------------------------------------------


def test_ac4_generation_result_text_can_be_none():
    from app.model_gateway import GenerationResult

    r = GenerationResult(text=None, provider="hosted", ok=False)
    assert r.text is None


def test_ac4_generation_result_text_can_be_str():
    from app.model_gateway import GenerationResult

    r = GenerationResult(text="hello world", provider="hosted", ok=True)
    assert isinstance(r.text, str)


def test_ac4_generation_result_has_provider():
    from app.model_gateway import GenerationResult

    r = GenerationResult(text=None, provider="hosted", ok=False)
    assert r.provider == "hosted"


def test_ac4_generation_result_has_ok():
    from app.model_gateway import GenerationResult

    r_ok = GenerationResult(text="out", provider="hosted", ok=True)
    r_fail = GenerationResult(text=None, provider="hosted", ok=False)
    assert r_ok.ok is True
    assert r_fail.ok is False


def test_ac4_generation_result_is_dataclass():
    from app.model_gateway import GenerationResult
    import dataclasses

    assert dataclasses.is_dataclass(GenerationResult)


# ---------------------------------------------------------------------------
# T1-AC5 — get_generation_provider() and get_embedding_provider() return
#           a ModelProvider instance each
# ---------------------------------------------------------------------------


def test_ac5_get_generation_provider_returns_model_provider():
    """get_generation_provider() returns a concrete ModelProvider instance."""
    from app.model_gateway import ModelProvider, get_generation_provider

    provider = get_generation_provider()
    assert isinstance(provider, ModelProvider)


def test_ac5_get_embedding_provider_returns_model_provider():
    """get_embedding_provider() returns a concrete ModelProvider instance."""
    from app.model_gateway import ModelProvider, get_embedding_provider

    provider = get_embedding_provider()
    assert isinstance(provider, ModelProvider)


def test_ac5_both_entry_points_callable_without_args():
    """Both entry points are callable without arguments."""
    from app.model_gateway import get_embedding_provider, get_generation_provider

    gen = get_generation_provider()
    emb = get_embedding_provider()
    assert gen is not None
    assert emb is not None


# ---------------------------------------------------------------------------
# T1-AC6 — Generation and embedding providers are resolved independently
# ---------------------------------------------------------------------------


def test_ac6_generation_provider_uses_separate_env_var(monkeypatch):
    """MODEL_GENERATION_PROVIDER controls only the generation provider."""
    from app.model_gateway import (
        ModelProvider,
        _PROVIDER_REGISTRY,
        register_provider,
    )
    from app.model_gateway._interface import GenerationRequest, GenerationResult

    class _StubGen(ModelProvider):
        name = "_stub_gen"

        def generate(self, req: GenerationRequest) -> GenerationResult:
            return GenerationResult(text="stub-gen", provider=self.name, ok=True)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return []

    stub = _StubGen()
    register_provider(stub)

    # Only override the generation env var — embedding should stay 'hosted'
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "_stub_gen")
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

    from app.model_gateway import get_generation_provider, get_embedding_provider

    gen = get_generation_provider()
    emb = get_embedding_provider()

    assert gen is stub, "generation provider should be the stub"
    assert emb is not stub, "embedding provider must NOT be the stub"
    assert emb.name == "hosted", "embedding provider should stay 'hosted'"

    # Cleanup
    _PROVIDER_REGISTRY.pop("_stub_gen", None)


def test_ac6_embedding_provider_uses_separate_env_var(monkeypatch):
    """MODEL_EMBEDDING_PROVIDER controls only the embedding provider."""
    from app.model_gateway import (
        ModelProvider,
        _PROVIDER_REGISTRY,
        register_provider,
    )
    from app.model_gateway._interface import GenerationRequest, GenerationResult

    class _StubEmb(ModelProvider):
        name = "_stub_emb"

        def generate(self, req: GenerationRequest) -> GenerationResult:
            return GenerationResult(text=None, provider=self.name, ok=False)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return [[0.1]] * len(texts)

    stub = _StubEmb()
    register_provider(stub)

    # Only override the embedding env var — generation should stay 'hosted'
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "_stub_emb")
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)

    from app.model_gateway import get_generation_provider, get_embedding_provider

    gen = get_generation_provider()
    emb = get_embedding_provider()

    assert emb is stub, "embedding provider should be the stub"
    assert gen is not stub, "generation provider must NOT be the stub"
    assert gen.name == "hosted", "generation provider should stay 'hosted'"

    # Cleanup
    _PROVIDER_REGISTRY.pop("_stub_emb", None)


def test_ac6_gen_and_emb_can_use_different_providers_simultaneously(monkeypatch):
    """A customer can run gen on 'hosted' and a custom embed, or vice versa."""
    from app.model_gateway import (
        ModelProvider,
        _PROVIDER_REGISTRY,
        register_provider,
    )
    from app.model_gateway._interface import GenerationRequest, GenerationResult

    class _AltGen(ModelProvider):
        name = "_alt_gen"

        def generate(self, req: GenerationRequest) -> GenerationResult:
            return GenerationResult(text="alt", provider=self.name, ok=True)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return []

    class _AltEmb(ModelProvider):
        name = "_alt_emb"

        def generate(self, req: GenerationRequest) -> GenerationResult:
            return GenerationResult(text=None, provider=self.name, ok=False)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return [[0.9]] * len(texts)

    alt_gen = _AltGen()
    alt_emb = _AltEmb()
    register_provider(alt_gen)
    register_provider(alt_emb)

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "_alt_gen")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "_alt_emb")

    from app.model_gateway import get_generation_provider, get_embedding_provider

    assert get_generation_provider() is alt_gen
    assert get_embedding_provider() is alt_emb
    # They are not the same object
    assert get_generation_provider() is not get_embedding_provider()

    # Cleanup
    _PROVIDER_REGISTRY.pop("_alt_gen", None)
    _PROVIDER_REGISTRY.pop("_alt_emb", None)


# ---------------------------------------------------------------------------
# Additional structural tests
# ---------------------------------------------------------------------------


def test_hosted_provider_name_is_hosted():
    """The default hosted provider has name='hosted'."""
    from app.model_gateway import get_generation_provider

    assert get_generation_provider().name == "hosted"


def test_hosted_provider_generate_returns_generation_result_on_missing_key(monkeypatch):
    """generate() returns ok=False / text=None when ANTHROPIC_API_KEY is unset."""
    from app.model_gateway import GenerationRequest, get_generation_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    result = get_generation_provider().generate(
        GenerationRequest(prompt="test", max_tokens=10)
    )
    assert isinstance(result.text, type(None))
    assert result.ok is False
    assert result.provider == "hosted"


def test_hosted_provider_embed_returns_list():
    """embed() returns a list (not raises) — graceful degradation."""
    from app.model_gateway import get_embedding_provider

    result = get_embedding_provider().embed(["hello", "world"])
    assert isinstance(result, list)


def test_register_provider_idempotent():
    """Registering the same instance twice is a no-op (no error)."""
    from app.model_gateway import _PROVIDER_REGISTRY, register_provider

    provider = _PROVIDER_REGISTRY.get("hosted")
    assert provider is not None
    # Re-registering the SAME instance must not raise
    register_provider(provider)


def test_register_different_provider_same_name_raises():
    """Registering a *different* instance under an existing name raises ValueError."""
    from app.model_gateway import (
        ModelProvider,
        _PROVIDER_REGISTRY,
        register_provider,
    )
    from app.model_gateway._interface import GenerationRequest, GenerationResult

    class _Dup(ModelProvider):
        name = "hosted"  # same name, different class

        def generate(self, req: GenerationRequest) -> GenerationResult:
            return GenerationResult(text=None, provider=self.name, ok=False)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return []

    with pytest.raises(ValueError):
        register_provider(_Dup())
