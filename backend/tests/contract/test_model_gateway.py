"""R16-D1 T1 + T2 — Model Provider Gateway contract tests.

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

And every T2 acceptance criterion (AT-363):

  T2-AC1  get_generation_provider() reads MODEL_GENERATION_PROVIDER from
          config and returns the matching registered provider.
  T2-AC2  get_embedding_provider() reads MODEL_EMBEDDING_PROVIDER from
          config and returns the matching registered provider.
  T2-AC3  Both providers are resolved independently — setting generation to
          one value and embedding to another works without conflict.
  T2-AC4  An unknown provider value raises a clear ValueError with a helpful
          message (surfaced via validate_provider_config() at startup).
  T2-AC5  backend/.env.example documents MODEL_GENERATION_PROVIDER and
          MODEL_EMBEDDING_PROVIDER with valid example values.
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


# =============================================================================
# T2 — Provider resolution from config (AT-363)
# =============================================================================


# ---------------------------------------------------------------------------
# T2-AC1 — get_generation_provider() reads MODEL_GENERATION_PROVIDER
# ---------------------------------------------------------------------------


def test_t2_ac1_reads_model_generation_provider_env_var(monkeypatch):
    """get_generation_provider() uses MODEL_GENERATION_PROVIDER to pick the provider."""
    from app.model_gateway import (
        ModelProvider,
        _PROVIDER_REGISTRY,
        get_generation_provider,
        register_provider,
    )
    from app.model_gateway._interface import GenerationRequest, GenerationResult

    class _T2GenProvider(ModelProvider):
        name = "_t2_gen"

        def generate(self, req: GenerationRequest) -> GenerationResult:
            return GenerationResult(text="t2-gen", provider=self.name, ok=True)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return []

    provider = _T2GenProvider()
    register_provider(provider)
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "_t2_gen")

    result = get_generation_provider()
    assert result is provider
    assert result.name == "_t2_gen"

    _PROVIDER_REGISTRY.pop("_t2_gen", None)


def test_t2_ac1_default_is_hosted_when_env_var_unset(monkeypatch):
    """MODEL_GENERATION_PROVIDER defaults to 'hosted' when not set."""
    from app.model_gateway import get_generation_provider

    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    assert get_generation_provider().name == "hosted"


# ---------------------------------------------------------------------------
# T2-AC2 — get_embedding_provider() reads MODEL_EMBEDDING_PROVIDER
# ---------------------------------------------------------------------------


def test_t2_ac2_reads_model_embedding_provider_env_var(monkeypatch):
    """get_embedding_provider() uses MODEL_EMBEDDING_PROVIDER to pick the provider."""
    from app.model_gateway import (
        ModelProvider,
        _PROVIDER_REGISTRY,
        get_embedding_provider,
        register_provider,
    )
    from app.model_gateway._interface import GenerationRequest, GenerationResult

    class _T2EmbProvider(ModelProvider):
        name = "_t2_emb"

        def generate(self, req: GenerationRequest) -> GenerationResult:
            return GenerationResult(text=None, provider=self.name, ok=False)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return [[0.5]] * len(texts)

    provider = _T2EmbProvider()
    register_provider(provider)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "_t2_emb")

    result = get_embedding_provider()
    assert result is provider
    assert result.name == "_t2_emb"

    _PROVIDER_REGISTRY.pop("_t2_emb", None)


def test_t2_ac2_default_is_hosted_when_env_var_unset(monkeypatch):
    """MODEL_EMBEDDING_PROVIDER defaults to 'hosted' when not set."""
    from app.model_gateway import get_embedding_provider

    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)
    assert get_embedding_provider().name == "hosted"


# ---------------------------------------------------------------------------
# T2-AC3 — Both resolved independently; different values work without conflict
# ---------------------------------------------------------------------------


def test_t2_ac3_different_providers_for_gen_and_emb_work_without_conflict(monkeypatch):
    """Separate gen and embed providers can be active simultaneously."""
    from app.model_gateway import (
        ModelProvider,
        _PROVIDER_REGISTRY,
        get_embedding_provider,
        get_generation_provider,
        register_provider,
    )
    from app.model_gateway._interface import GenerationRequest, GenerationResult

    class _T2IndepGen(ModelProvider):
        name = "_t2_indep_gen"

        def generate(self, req: GenerationRequest) -> GenerationResult:
            return GenerationResult(text="gen", provider=self.name, ok=True)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return []

    class _T2IndepEmb(ModelProvider):
        name = "_t2_indep_emb"

        def generate(self, req: GenerationRequest) -> GenerationResult:
            return GenerationResult(text=None, provider=self.name, ok=False)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return [[1.0]] * len(texts)

    pgen = _T2IndepGen()
    pemb = _T2IndepEmb()
    register_provider(pgen)
    register_provider(pemb)

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "_t2_indep_gen")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "_t2_indep_emb")

    gen = get_generation_provider()
    emb = get_embedding_provider()

    # Both resolved to the correct provider
    assert gen is pgen
    assert emb is pemb
    # They are different objects — setting one didn't clobber the other
    assert gen is not emb

    _PROVIDER_REGISTRY.pop("_t2_indep_gen", None)
    _PROVIDER_REGISTRY.pop("_t2_indep_emb", None)


def test_t2_ac3_changing_gen_does_not_affect_emb(monkeypatch):
    """Changing MODEL_GENERATION_PROVIDER leaves MODEL_EMBEDDING_PROVIDER untouched."""
    from app.model_gateway import (
        ModelProvider,
        _PROVIDER_REGISTRY,
        get_embedding_provider,
        get_generation_provider,
        register_provider,
    )
    from app.model_gateway._interface import GenerationRequest, GenerationResult

    class _T2AltGen(ModelProvider):
        name = "_t2_alt_gen"

        def generate(self, req: GenerationRequest) -> GenerationResult:
            return GenerationResult(text="alt", provider=self.name, ok=True)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return []

    register_provider(_T2AltGen())
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "_t2_alt_gen")
    # Do NOT set MODEL_EMBEDDING_PROVIDER → should stay 'hosted'
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

    assert get_generation_provider().name == "_t2_alt_gen"
    assert get_embedding_provider().name == "hosted"  # unchanged

    _PROVIDER_REGISTRY.pop("_t2_alt_gen", None)


# ---------------------------------------------------------------------------
# T2-AC4 — Unknown provider raises ValueError with helpful message at startup
# ---------------------------------------------------------------------------


def test_t2_ac4_unknown_generation_provider_raises_value_error(monkeypatch):
    """get_generation_provider() raises ValueError for an unregistered name."""
    from app.model_gateway import get_generation_provider

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "no_such_provider_xyz")

    with pytest.raises(ValueError) as exc_info:
        get_generation_provider()

    msg = str(exc_info.value)
    # Must name the env var in the message
    assert "MODEL_GENERATION_PROVIDER" in msg
    # Must mention the bad value
    assert "no_such_provider_xyz" in msg


def test_t2_ac4_unknown_embedding_provider_raises_value_error(monkeypatch):
    """get_embedding_provider() raises ValueError for an unregistered name."""
    from app.model_gateway import get_embedding_provider

    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "no_such_provider_xyz")

    with pytest.raises(ValueError) as exc_info:
        get_embedding_provider()

    msg = str(exc_info.value)
    assert "MODEL_EMBEDDING_PROVIDER" in msg
    assert "no_such_provider_xyz" in msg


def test_t2_ac4_raises_value_error_not_key_error(monkeypatch):
    """The exception type is ValueError, not KeyError."""
    from app.model_gateway import get_generation_provider

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "ghost_provider")

    with pytest.raises(ValueError):
        get_generation_provider()

    # Must NOT raise KeyError
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "ghost_provider")
    try:
        get_generation_provider()
    except ValueError:
        pass  # expected
    except KeyError:
        pytest.fail("Expected ValueError, got KeyError")


def test_t2_ac4_error_message_lists_valid_values(monkeypatch):
    """The error message includes the list of registered provider names."""
    from app.model_gateway import get_generation_provider

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "bad_name")

    with pytest.raises(ValueError) as exc_info:
        get_generation_provider()

    msg = str(exc_info.value)
    # 'hosted' is always registered
    assert "hosted" in msg


def test_t2_ac4_validate_provider_config_raises_for_unknown_gen(monkeypatch):
    """validate_provider_config() raises ValueError on a bad gen provider."""
    from app.model_gateway import validate_provider_config

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "bad_gen_provider")
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

    with pytest.raises(ValueError) as exc_info:
        validate_provider_config()

    assert "MODEL_GENERATION_PROVIDER" in str(exc_info.value)


def test_t2_ac4_validate_provider_config_raises_for_unknown_emb(monkeypatch):
    """validate_provider_config() raises ValueError on a bad emb provider."""
    from app.model_gateway import validate_provider_config

    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "bad_emb_provider")

    with pytest.raises(ValueError) as exc_info:
        validate_provider_config()

    assert "MODEL_EMBEDDING_PROVIDER" in str(exc_info.value)


def test_t2_ac4_validate_provider_config_passes_for_default(monkeypatch):
    """validate_provider_config() passes when both env vars are default/unset."""
    from app.model_gateway import validate_provider_config

    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

    # Must not raise
    validate_provider_config()


def test_t2_ac4_validate_provider_config_passes_when_set_to_hosted(monkeypatch):
    """validate_provider_config() passes when both vars are explicitly 'hosted'."""
    from app.model_gateway import validate_provider_config

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")

    validate_provider_config()  # must not raise


# ---------------------------------------------------------------------------
# T2-AC5 — .env.example documents both config keys with valid example values
# ---------------------------------------------------------------------------


def test_t2_ac5_env_example_contains_model_generation_provider():
    """backend/.env.example must document MODEL_GENERATION_PROVIDER."""
    import os
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parents[2]
    env_example = backend_dir / ".env.example"
    assert env_example.exists(), ".env.example must exist"

    content = env_example.read_text(encoding="utf-8")
    assert "MODEL_GENERATION_PROVIDER" in content, (
        "backend/.env.example must document MODEL_GENERATION_PROVIDER"
    )


def test_t2_ac5_env_example_contains_model_embedding_provider():
    """backend/.env.example must document MODEL_EMBEDDING_PROVIDER."""
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parents[2]
    env_example = backend_dir / ".env.example"
    content = env_example.read_text(encoding="utf-8")
    assert "MODEL_EMBEDDING_PROVIDER" in content, (
        "backend/.env.example must document MODEL_EMBEDDING_PROVIDER"
    )


def test_t2_ac5_env_example_values_are_valid(monkeypatch):
    """The example values in .env.example are actually registered providers."""
    import re
    from pathlib import Path

    from app.model_gateway import _PROVIDER_REGISTRY

    backend_dir = Path(__file__).resolve().parents[2]
    content = (backend_dir / ".env.example").read_text(encoding="utf-8")

    for env_var in ("MODEL_GENERATION_PROVIDER", "MODEL_EMBEDDING_PROVIDER"):
        m = re.search(rf"^{env_var}=(.+)$", content, re.MULTILINE)
        assert m is not None, f"{env_var} must have an example value in .env.example"
        example_value = m.group(1).strip()
        assert example_value in _PROVIDER_REGISTRY, (
            f".env.example example value '{example_value}' for {env_var} "
            f"is not a registered provider. Valid: {sorted(_PROVIDER_REGISTRY.keys())}"
        )
