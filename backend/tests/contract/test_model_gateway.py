"""R16-D1 — Model Provider Gateway contract tests.

This is the contract test file named in AT-367 (T6). It holds the T1 and T2
acceptance tests and the full Section 6 contract suite (T6-AC1..AC7) that
asserts every story-level acceptance criterion of R16-D1 §6 in one place —
gateway interface, provider independence, call-site migration, no-bypass
enforcement, graceful failure, telemetry, and stub-provider extensibility.

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


# =============================================================================
# T6 (AT-367) — Full Section 6 contract suite
#
# Asserts the seven story-level acceptance criteria of R16-D1 §6 end to end:
#
#   T6-AC1  All model generation flows through get_generation_provider().
#           generate() — no direct provider calls exist outside the gateway.
#   T6-AC2  The no-bypass enforcement test fails the build when a direct call
#           is introduced.
#   T6-AC3  Generation and embedding providers are resolved independently by
#           configuring each to a *different* stub.
#   T6-AC4  The hallucination guard preserves its 500ms timeout and rule-based
#           fallback through the gateway.
#   T6-AC5  generate() returns ok=False / text=None on provider failure and no
#           exception propagates.
#   T6-AC6  Each model call records the serving provider in telemetry.
#   T6-AC7  A new stub provider can be registered and all calls route through
#           it without changing any calling code.
#
# AC1/AC2 reuse the exact scanner the T4 CI enforcement test runs, so this
# suite and the build gate cannot diverge — there is one source of truth.
# =============================================================================

from app.model_gateway import (  # noqa: E402
    _PROVIDER_REGISTRY,
    embed as _gw_embed,
    generate as _gw_generate,
    get_embedding_provider as _gw_get_embedding_provider,
    get_generation_provider as _gw_get_generation_provider,
    register_provider,
)
from app.model_gateway._interface import (  # noqa: E402
    GenerationRequest as _GenReq,
    GenerationResult as _GenRes,
    ModelProvider as _ModelProvider,
)

# The gateway emits these two event types (registered in app.telemetry).
_T6_GEN_EVENT = "model.generation_completed"
_T6_EMB_EVENT = "model.embedding_completed"


class _T6StubGenProvider(_ModelProvider):
    """A registrable stub generation provider with a recognisable output."""

    name = "_t6_stub_gen"

    def __init__(self) -> None:
        self.generate_calls = 0

    def generate(self, req: _GenReq) -> _GenRes:
        self.generate_calls += 1
        # Echo the timeout so a test can prove the request reached the provider
        # with the caller's timeout_ms intact (used by AC4).
        return _GenRes(text=f"stub-gen:{req.timeout_ms}", provider=self.name, ok=True)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return []


class _T6StubEmbProvider(_ModelProvider):
    """A registrable stub embedding provider returning a unit vector per text."""

    name = "_t6_stub_emb"

    def __init__(self) -> None:
        self.embed_calls = 0

    def generate(self, req: _GenReq) -> _GenRes:
        return _GenRes(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        self.embed_calls += 1
        return [[1.0]] * len(texts)


class _T6FailingProvider(_ModelProvider):
    """A provider that always reports graceful failure (never raises)."""

    name = "_t6_failing"

    def generate(self, req: _GenReq) -> _GenRes:
        return _GenRes(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return []


@pytest.fixture
def t6_captured_events(monkeypatch):
    """Capture every record_event() call the gateway makes.

    The gateway imports ``record_event`` lazily from ``app.telemetry`` on each
    call, so patching the module attribute intercepts the real write while still
    proving the gateway routes through the shared telemetry infrastructure.
    """
    events: List[tuple] = []

    def _fake_record_event(event_type, payload=None):
        events.append((event_type, payload or {}))

    monkeypatch.setattr("app.telemetry.record_event", _fake_record_event)
    return events


def _t6_register_temp(provider: _ModelProvider):
    """Register a stub and return a cleanup callable for use in a finally."""
    register_provider(provider)
    return lambda: _PROVIDER_REGISTRY.pop(provider.name, None)


# ---------------------------------------------------------------------------
# T6-AC1 — All model generation flows through the gateway (no direct calls)
# ---------------------------------------------------------------------------


def test_t6_ac1_no_direct_model_calls_outside_gateway():
    """No source file outside backend/app/model_gateway/ contains a direct
    provider endpoint / SDK / api-key reference (R16-D1 §2 + §4).

    Re-uses the exact scanner the CI enforcement test runs, so AC1 and the
    build gate cannot diverge.
    """
    from tests.contract.test_model_gateway_no_bypass import (
        BACKEND_ROOT,
        _collect_scan_targets,
        _scan_file,
    )

    violations = []
    for py_file in _collect_scan_targets():
        for lineno, line, pattern in _scan_file(py_file):
            rel = py_file.relative_to(BACKEND_ROOT)
            violations.append(f"  {rel}:{lineno}: [{pattern!r}]  {line}")

    assert not violations, (
        "AC1 violated — direct model-provider references found outside the "
        "gateway. Route every model call through get_generation_provider() / "
        "get_embedding_provider().\n\nViolations:\n" + "\n".join(violations)
    )


def test_t6_ac1_migrated_call_sites_use_the_gateway():
    """The three known call sites (R16-D1 §4) import and call the gateway.

    Proves the migration target — not just the absence of direct calls. Each
    site must reference ``app.model_gateway`` and the gateway ``generate``.
    """
    from pathlib import Path

    backend_root = Path(__file__).resolve().parents[2]
    for rel in (
        "app/llm_enrichment.py",
        "app/hallucination_guard.py",
        "app/normalization_enrichment.py",
    ):
        src = (backend_root / rel).read_text(encoding="utf-8")
        assert "app.model_gateway" in src, f"{rel} must import the model gateway"
        assert "generate(" in src, f"{rel} must call the gateway generate()"


# ---------------------------------------------------------------------------
# T6-AC2 — The enforcement test fails the build when a direct call appears
# ---------------------------------------------------------------------------


def test_t6_ac2_scanner_flags_an_introduced_direct_call(tmp_path):
    """Writing a file with a forbidden pattern makes the scanner report it.

    This is what makes the build fail the moment a bypass is introduced.
    """
    from tests.contract.test_model_gateway_no_bypass import (
        FORBIDDEN_PATTERNS,
        _scan_file,
    )

    # Build the forbidden strings at runtime so THIS file never self-matches.
    endpoint = "https://api.anthrop" + "ic.com/v1/messages"
    header = "x-api-" + "key"
    bypass = tmp_path / "rogue_caller.py"
    bypass.write_text(
        f'URL = "{endpoint}"\nHEADERS = {{"{header}": "sk-..."}}\n',
        encoding="utf-8",
    )

    violations = _scan_file(bypass)
    assert violations, "AC2 broken — scanner did not flag an obvious bypass"
    assert {v[2] for v in violations}.issubset(set(FORBIDDEN_PATTERNS))


def test_t6_ac2_clean_file_is_not_flagged(tmp_path):
    """A file that routes through the gateway is NOT flagged (no false fail)."""
    from tests.contract.test_model_gateway_no_bypass import _scan_file

    clean = tmp_path / "good_caller.py"
    clean.write_text(
        "from app.model_gateway import GenerationRequest, generate\n"
        "result = generate(GenerationRequest(prompt='hi', max_tokens=10))\n",
        encoding="utf-8",
    )
    assert _scan_file(clean) == []


# ---------------------------------------------------------------------------
# T6-AC3 — Generation and embedding providers resolved independently
# ---------------------------------------------------------------------------


def test_t6_ac3_gen_and_emb_resolve_to_different_stubs(monkeypatch):
    """Configuring generation and embedding to two different stubs resolves
    each to its own provider — neither clobbers the other."""
    gen_stub = _T6StubGenProvider()
    emb_stub = _T6StubEmbProvider()
    cleanup_gen = _t6_register_temp(gen_stub)
    cleanup_emb = _t6_register_temp(emb_stub)
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", gen_stub.name)
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", emb_stub.name)

        resolved_gen = _gw_get_generation_provider()
        resolved_emb = _gw_get_embedding_provider()

        assert resolved_gen is gen_stub
        assert resolved_emb is emb_stub
        assert resolved_gen is not resolved_emb
    finally:
        cleanup_gen()
        cleanup_emb()


def test_t6_ac3_changing_one_does_not_affect_the_other(monkeypatch):
    """Setting only the generation provider leaves embedding on its default."""
    gen_stub = _T6StubGenProvider()
    cleanup_gen = _t6_register_temp(gen_stub)
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", gen_stub.name)
        monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

        assert _gw_get_generation_provider() is gen_stub
        # Embedding falls back to the default 'hosted' — untouched.
        assert _gw_get_embedding_provider().name == "hosted"
    finally:
        cleanup_gen()


# ---------------------------------------------------------------------------
# T6-AC4 — Hallucination guard keeps its 500ms timeout + rule-based fallback
# ---------------------------------------------------------------------------


def test_t6_ac4_guard_request_carries_500ms_timeout(monkeypatch):
    """The hallucination guard's rewrite call reaches the gateway with
    timeout_ms == REWRITE_TIMEOUT_MS (500), routed through the gateway."""
    from app import hallucination_guard as hg

    captured: dict = {}

    def _spy_generate(req: _GenReq) -> _GenRes:
        captured["timeout_ms"] = req.timeout_ms
        return _GenRes(text="rewritten safely", provider="hosted", ok=True)

    # Patch the gateway symbol as the guard imports it (lazy import inside fn).
    monkeypatch.setattr("app.model_gateway.generate", _spy_generate)

    out = hg._invoke_rewrite_llm("rewrite this bullet please")

    assert out == "rewritten safely"
    assert hg.REWRITE_TIMEOUT_MS == 500
    assert captured["timeout_ms"] == 500, (
        "the 500ms leash must survive as timeout_ms on the gateway request"
    )


def test_t6_ac4_rule_based_fallback_works_without_llm():
    """When no LLM rewrite is possible, the guard recovers a hallucinated
    bullet deterministically (rule-based) and never returns a bad name.

    This exercises the rule-based path with NO gateway/LLM call at all, proving
    the fallback survives the gateway migration.
    """
    from app.hallucination_guard import validate_and_recover

    # "Alice Carter" is not in the resolved set → hallucinated proper noun.
    out = validate_and_recover(
        bullet="Alice Carter owns the overdue covenant review",
        resolved_names=["covenant review"],
        org_id=None,
        run_id=None,
    )

    assert out is not None, "rule-based rewrite should recover this bullet"
    assert "Alice Carter" not in out, "no hallucinated name may survive"


def test_t6_ac4_guard_drops_bullet_when_rewrite_unavailable(monkeypatch):
    """If the rule-based rewrite is incoherent and the LLM rewrite times out,
    the guard drops the bullet (returns None) — it never raises and never
    leaks the hallucinated name. Proves graceful behaviour through the gateway.
    """
    from app import hallucination_guard as hg

    # Force the LLM path to behave as a timeout so only the fallback remains.
    def _timeout_rewrite(*args, **kwargs):
        raise TimeoutError("simulated slow model")

    monkeypatch.setattr(hg, "llm_rewrite_bullet", _timeout_rewrite)
    # Make the deterministic rule rewrite look incoherent so we reach step 2b.
    monkeypatch.setattr(hg, "is_coherent", lambda _text: False)
    monkeypatch.setattr(hg, "is_worth_saving", lambda *_a, **_k: True)

    out = hg.validate_and_recover(
        bullet="Zenon Quortib escalated the issue to Marcus Delacroix",
        resolved_names=["the issue"],
        org_id=None,
        run_id=None,
    )
    assert out is None  # dropped, not raised


# ---------------------------------------------------------------------------
# T6-AC5 — generate() returns ok=False / text=None on failure, never raises
# ---------------------------------------------------------------------------


def test_t6_ac5_failure_returns_graceful_result(monkeypatch, t6_captured_events):
    """A provider that reports failure yields ok=False / text=None — no raise."""
    failing = _T6FailingProvider()
    cleanup = _t6_register_temp(failing)
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", failing.name)
        result = _gw_generate(_GenReq(prompt="x", max_tokens=5))
    finally:
        cleanup()

    assert result.ok is False
    assert result.text is None
    assert result.provider == failing.name


def test_t6_ac5_hosted_missing_key_is_graceful(monkeypatch, t6_captured_events):
    """The real hosted provider with no API key returns ok=False / text=None
    rather than raising — the behaviour callers already handle."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)  # default hosted
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    result = _gw_generate(_GenReq(prompt="anything", max_tokens=5))

    assert result.ok is False
    assert result.text is None
    assert result.provider == "hosted"


def test_t6_ac5_provider_exception_does_not_propagate(monkeypatch, t6_captured_events):
    """Even when the underlying transport raises, the contract holds: the
    hosted provider catches everything internally and returns a graceful
    result (ModelProvider.generate must never raise)."""
    from app.model_gateway._hosted import AnthropicHostedProvider

    provider = AnthropicHostedProvider()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "definitely-invalid-key")

    def _boom(*_a, **_k):
        raise RuntimeError("network exploded")

    # urlopen blowing up must be swallowed into a graceful result.
    monkeypatch.setattr("urllib.request.urlopen", _boom)

    result = provider.generate(_GenReq(prompt="x", max_tokens=5))
    assert result.ok is False
    assert result.text is None


# ---------------------------------------------------------------------------
# T6-AC6 — Each model call records the serving provider in telemetry
# ---------------------------------------------------------------------------


def test_t6_ac6_generate_records_serving_provider(monkeypatch, t6_captured_events):
    """generate() emits exactly one generation event naming the serving
    provider taken from GenerationResult.provider."""
    gen_stub = _T6StubGenProvider()
    cleanup = _t6_register_temp(gen_stub)
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", gen_stub.name)
        _gw_generate(_GenReq(prompt="hi", max_tokens=5))
    finally:
        cleanup()

    gen_events = [e for e in t6_captured_events if e[0] == _T6_GEN_EVENT]
    assert len(gen_events) == 1
    assert gen_events[0][1]["provider"] == gen_stub.name


def test_t6_ac6_embed_records_serving_provider(monkeypatch, t6_captured_events):
    """embed() emits exactly one embedding event naming the serving provider."""
    emb_stub = _T6StubEmbProvider()
    cleanup = _t6_register_temp(emb_stub)
    try:
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", emb_stub.name)
        _gw_embed(["a", "b", "c"])
    finally:
        cleanup()

    emb_events = [e for e in t6_captured_events if e[0] == _T6_EMB_EVENT]
    assert len(emb_events) == 1
    assert emb_events[0][1]["provider"] == emb_stub.name


def test_t6_ac6_failed_call_still_records_provider(monkeypatch, t6_captured_events):
    """A failed generation still records telemetry — failures stay observable."""
    failing = _T6FailingProvider()
    cleanup = _t6_register_temp(failing)
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", failing.name)
        _gw_generate(_GenReq(prompt="x", max_tokens=5))
    finally:
        cleanup()

    gen_events = [e for e in t6_captured_events if e[0] == _T6_GEN_EVENT]
    assert len(gen_events) == 1
    assert gen_events[0][1]["provider"] == failing.name
    assert gen_events[0][1]["ok"] is False


def test_t6_ac6_event_types_registered_before_use():
    """The gateway's event types are registered, so record_event never raises
    for them (the telemetry write signature is locked to registered types)."""
    from app.telemetry import REGISTERED_EVENT_TYPES, record_event

    assert _T6_GEN_EVENT in REGISTERED_EVENT_TYPES
    assert _T6_EMB_EVENT in REGISTERED_EVENT_TYPES
    # Must not raise ValueError for an unregistered type.
    record_event(_T6_GEN_EVENT, {"provider": "hosted", "ok": True})
    record_event(_T6_EMB_EVENT, {"provider": "hosted", "ok": True, "text_count": 0})


# ---------------------------------------------------------------------------
# T6-AC7 — A new stub provider routes all calls without changing calling code
# ---------------------------------------------------------------------------


def test_t6_ac7_new_provider_routes_generation_without_code_change(
    monkeypatch, t6_captured_events
):
    """Registering a brand-new provider and pointing config at it makes the
    UNCHANGED gateway generate() entry point route through it.

    The caller code (generate / GenerationRequest) is identical to every other
    call site — only configuration changed. This is the 1.7 extensibility
    promise: in-boundary / customer-tenant providers drop in with no caller
    edits.
    """

    class _BrandNewProvider(_ModelProvider):
        name = "_t6_brand_new_gen"

        def generate(self, req: _GenReq) -> _GenRes:
            return _GenRes(text="from-new-provider", provider=self.name, ok=True)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return []

    new_provider = _BrandNewProvider()
    cleanup = _t6_register_temp(new_provider)
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", new_provider.name)

        # Identical call shape to llm_enrichment / hallucination_guard.
        result = _gw_generate(_GenReq(prompt="hi", max_tokens=5))
    finally:
        cleanup()

    assert result.text == "from-new-provider"
    assert result.provider == new_provider.name
    gen_events = [e for e in t6_captured_events if e[0] == _T6_GEN_EVENT]
    assert gen_events and gen_events[0][1]["provider"] == new_provider.name


def test_t6_ac7_new_provider_routes_embedding_without_code_change(
    monkeypatch, t6_captured_events
):
    """The same drop-in extensibility holds for the embedding entry point."""

    class _BrandNewEmb(_ModelProvider):
        name = "_t6_brand_new_emb"

        def generate(self, req: _GenReq) -> _GenRes:
            return _GenRes(text=None, provider=self.name, ok=False)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return [[42.0]] * len(texts)

    new_provider = _BrandNewEmb()
    cleanup = _t6_register_temp(new_provider)
    try:
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", new_provider.name)
        vectors = _gw_embed(["one", "two"])
    finally:
        cleanup()

    assert vectors == [[42.0], [42.0]]
    emb_events = [e for e in t6_captured_events if e[0] == _T6_EMB_EVENT]
    assert emb_events and emb_events[0][1]["provider"] == new_provider.name


def test_t6_ac7_duplicate_name_different_instance_rejected():
    """Registering a *different* instance under an existing name is rejected —
    extensibility never lets a second provider silently hijack a name."""

    class _Impostor(_ModelProvider):
        name = "hosted"  # collide with the built-in default

        def generate(self, req: _GenReq) -> _GenRes:
            return _GenRes(text=None, provider=self.name, ok=False)

        def embed(self, texts: List[str]) -> List[List[float]]:
            return []

    with pytest.raises(ValueError):
        register_provider(_Impostor())


# ---------------------------------------------------------------------------
# T3 (AT-407) — Graceful degradation on retry exhaustion
#
#   T3-AC1  On retry exhaustion, generate() returns ok=False / text=None —
#           no exception propagates to callers.
#   T3-AC2  Existing callers (llm_enrichment, hallucination_guard,
#           normalization_enrichment) behave exactly as before when the
#           provider exhausts retries.
#   T3-AC3  provider='hosted' is always set on the returned GenerationResult,
#           even on failure.
# ---------------------------------------------------------------------------


def test_t3_ac1_retry_exhaustion_returns_graceful_result(monkeypatch):
    """HostedModelProvider.generate() returns ok=False/text=None on exhaustion,
    never raises."""
    from app.model_gateway.hosted_provider import HostedModelProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    def _always_fail(req, timeout=None):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr("urllib.request.urlopen", _always_fail)

    provider = HostedModelProvider()
    result = provider.generate(_GenReq(prompt="test", max_tokens=5))

    assert result.ok is False
    assert result.text is None


def test_t3_ac1_no_exception_propagates_on_exhaustion(monkeypatch):
    """No exception escapes generate() when all retry attempts fail."""
    import urllib.error as _ue
    import http.client

    from app.model_gateway.hosted_provider import HostedModelProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    def _always_429(req, timeout=None):
        hdrs = http.client.HTTPMessage()
        raise _ue.HTTPError("http://fake", 429, "Too Many Requests", hdrs, None)

    monkeypatch.setattr("urllib.request.urlopen", _always_429)

    provider = HostedModelProvider()
    # Must not raise — exhaustion must be swallowed and returned as ok=False.
    try:
        result = provider.generate(
            _GenReq(prompt="test", max_tokens=5, timeout_ms=60000)
        )
    except Exception as exc:
        raise AssertionError(
            f"T3-AC1 FAIL: generate() raised {type(exc).__name__}: {exc}"
        ) from exc

    assert result.ok is False
    assert result.text is None


def test_t3_ac2_llm_enrichment_returns_none_on_exhaustion(monkeypatch):
    """llm_enrichment._call_claude() returns None when the provider exhausts
    retries — the same value callers have always handled."""
    from app import llm_enrichment as le

    def _failing_generate(req):
        return _GenRes(text=None, provider="hosted", ok=False)

    monkeypatch.setattr("app.model_gateway.generate", _failing_generate)

    result = le._call_claude("test prompt", 10)
    assert result is None


def test_t3_ac2_hallucination_guard_returns_none_on_exhaustion(monkeypatch):
    """hallucination_guard._invoke_rewrite_llm() returns None when the
    provider exhausts retries — the guard's caller already handles None."""
    from app import hallucination_guard as hg

    def _failing_generate(req):
        return _GenRes(text=None, provider="hosted", ok=False)

    monkeypatch.setattr("app.model_gateway.generate", _failing_generate)

    result = hg._invoke_rewrite_llm("rewrite this")
    assert result is None


def test_t3_ac2_normalization_enrichment_returns_none_on_exhaustion(monkeypatch):
    """normalization_enrichment degrades to None when the provider exhausts
    retries — the existing ok/text guard is preserved."""
    from app import normalization_enrichment as ne
    from app.model_gateway import GenerationRequest

    def _failing_generate(req: GenerationRequest):
        return _GenRes(text=None, provider="hosted", ok=False)

    monkeypatch.setattr("app.model_gateway.generate", _failing_generate)

    # _call_claude_batch returns None when the gateway returns ok=False.
    fields = [{"id": "f1", "sourceField": "Account__c", "sourceSystem": "SF", "sampleValues": []}]
    result = ne._call_claude_batch(fields, "service_cloud")
    assert result is None


def test_t3_ac3_provider_field_is_always_hosted_on_failure(monkeypatch):
    """GenerationResult.provider is always 'hosted' on any failure path."""
    from app.model_gateway.hosted_provider import HostedModelProvider

    provider = HostedModelProvider()

    # Missing API key path
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    result = provider.generate(_GenReq(prompt="test", max_tokens=5))
    assert result.provider == "hosted", "provider must be 'hosted' on missing-key failure"

    # Exception path
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    def _boom(req, timeout=None):
        raise RuntimeError("exploded")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    result = provider.generate(_GenReq(prompt="test", max_tokens=5))
    assert result.provider == "hosted", "provider must be 'hosted' on exception failure"


def test_t3_ac3_provider_field_is_hosted_on_exhaustion(monkeypatch):
    """provider='hosted' is set even when all retry attempts are exhausted."""
    import urllib.error as _ue
    import http.client

    from app.model_gateway.hosted_provider import HostedModelProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    def _always_429(req, timeout=None):
        hdrs = http.client.HTTPMessage()
        raise _ue.HTTPError("http://fake", 429, "Too Many Requests", hdrs, None)

    monkeypatch.setattr("urllib.request.urlopen", _always_429)

    provider = HostedModelProvider()
    result = provider.generate(
        _GenReq(prompt="test", max_tokens=5, timeout_ms=60000)
    )
    assert result.provider == "hosted"
    assert result.ok is False
    assert result.text is None
