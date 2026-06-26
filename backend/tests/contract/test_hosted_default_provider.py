"""R16-D2 T5 (AT-409) — Hosted registered as the default provider.

``HostedModelProvider`` (the full D2 implementation — resilience, credential
hygiene, and telemetry) is registered as the default ``'hosted'`` provider for
BOTH generation and embedding via gateway config, so the platform works out of
the box.  Selecting any other provider is a configuration change only — no
calling code is affected, and nothing in the registration assumes hosted is the
only mode.

Acceptance Criteria
-------------------
  T5-AC1  get_generation_provider() returns HostedModelProvider when
          MODEL_GENERATION_PROVIDER is unset or set to 'hosted'.
  T5-AC2  get_embedding_provider() returns HostedModelProvider when
          MODEL_EMBEDDING_PROVIDER is unset or set to 'hosted'.
  T5-AC3  Swapping to a stub provider via config requires no code change in any
          caller.
  T5-AC4  Nothing in the registration assumes hosted is the only mode — other
          providers can be registered and selected.
"""
from __future__ import annotations

from typing import List

import pytest

from app.model_gateway import (
    _PROVIDER_REGISTRY,
    embed as gw_embed,
    generate as gw_generate,
    get_embedding_provider,
    get_generation_provider,
    register_provider,
)
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.model_gateway.hosted_provider import HostedModelProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubProvider(ModelProvider):
    """A registrable stub used to prove provider swaps are config-only.

    Its generate()/embed() return recognisable sentinels so a test can prove a
    call reached this provider — and therefore that the gateway resolved it from
    config without any caller code change.
    """

    name = "_t5_stub"

    def generate(self, req: GenerationRequest) -> GenerationResult:
        return GenerationResult(text="stub-served", provider=self.name, ok=True)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[7.0]] * len(texts)


def _register_temp(provider: ModelProvider):
    """Register a stub and return a cleanup callable for use in a finally."""
    register_provider(provider)
    return lambda: _PROVIDER_REGISTRY.pop(provider.name, None)


# ---------------------------------------------------------------------------
# T5-AC1 — get_generation_provider() returns HostedModelProvider by default
# ---------------------------------------------------------------------------


def test_t5_ac1_generation_default_is_hosted_model_provider_when_unset(monkeypatch):
    """Unset MODEL_GENERATION_PROVIDER → the D2 HostedModelProvider serves generation."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    provider = get_generation_provider()
    assert isinstance(provider, HostedModelProvider)
    assert provider.name == "hosted"


def test_t5_ac1_generation_default_is_hosted_model_provider_when_explicit(monkeypatch):
    """MODEL_GENERATION_PROVIDER='hosted' → the D2 HostedModelProvider serves generation."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")
    provider = get_generation_provider()
    assert isinstance(provider, HostedModelProvider)
    assert provider.name == "hosted"


# ---------------------------------------------------------------------------
# T5-AC2 — get_embedding_provider() returns HostedModelProvider by default
# ---------------------------------------------------------------------------


def test_t5_ac2_embedding_default_is_hosted_model_provider_when_unset(monkeypatch):
    """Unset MODEL_EMBEDDING_PROVIDER → the D2 HostedModelProvider serves embedding."""
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)
    provider = get_embedding_provider()
    assert isinstance(provider, HostedModelProvider)
    assert provider.name == "hosted"


def test_t5_ac2_embedding_default_is_hosted_model_provider_when_explicit(monkeypatch):
    """MODEL_EMBEDDING_PROVIDER='hosted' → the D2 HostedModelProvider serves embedding."""
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")
    provider = get_embedding_provider()
    assert isinstance(provider, HostedModelProvider)
    assert provider.name == "hosted"


def test_t5_ac1_ac2_same_hosted_instance_serves_both(monkeypatch):
    """Both entry points resolve to the SAME registered hosted instance — one
    HostedModelProvider serves generation and embedding by default."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)
    gen = get_generation_provider()
    emb = get_embedding_provider()
    assert gen is emb
    assert isinstance(gen, HostedModelProvider)


def test_t5_registered_hosted_is_d2_implementation():
    """The provider registered under 'hosted' is the D2 HostedModelProvider —
    the full implementation (resilience + credential hygiene + telemetry), not
    the bare D1 provider. This is the crux of T5: D2 is now the default."""
    registered = _PROVIDER_REGISTRY.get("hosted")
    assert registered is not None, "'hosted' must be registered at import time"
    assert isinstance(registered, HostedModelProvider)


# ---------------------------------------------------------------------------
# T5-AC3 — Swapping to a stub provider via config requires no caller change
# ---------------------------------------------------------------------------


def test_t5_ac3_config_swap_routes_generation_to_stub(monkeypatch):
    """Pointing MODEL_GENERATION_PROVIDER at a stub makes the UNCHANGED gateway
    generate() entry point route through it — only configuration changed."""
    stub = _StubProvider()
    cleanup = _register_temp(stub)
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", stub.name)
        # Identical call shape to every real caller (llm_enrichment etc.).
        result = gw_generate(GenerationRequest(prompt="hi", max_tokens=5))
    finally:
        cleanup()

    assert result.text == "stub-served"
    assert result.provider == stub.name


def test_t5_ac3_config_swap_routes_embedding_to_stub(monkeypatch):
    """The same config-only swap holds for the embedding entry point."""
    stub = _StubProvider()
    cleanup = _register_temp(stub)
    try:
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", stub.name)
        vectors = gw_embed(["a", "b"])
    finally:
        cleanup()

    assert vectors == [[7.0], [7.0]]


def test_t5_ac3_default_restored_after_swap(monkeypatch):
    """With the override removed, resolution falls straight back to the hosted
    default — the swap is config-driven, not a permanent code change."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    assert isinstance(get_generation_provider(), HostedModelProvider)


# ---------------------------------------------------------------------------
# T5-AC4 — Registration does not assume hosted is the only mode
# ---------------------------------------------------------------------------


def test_t5_ac4_another_provider_can_be_registered_and_selected(monkeypatch):
    """A non-hosted provider registers and is selectable for both gen and emb —
    proving the registration leaves room for in-boundary / customer-tenant modes."""
    stub = _StubProvider()
    cleanup = _register_temp(stub)
    try:
        # The stub coexists with the hosted default in the registry.
        assert "hosted" in _PROVIDER_REGISTRY
        assert stub.name in _PROVIDER_REGISTRY

        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", stub.name)
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", stub.name)
        assert get_generation_provider() is stub
        assert get_embedding_provider() is stub
    finally:
        cleanup()


def test_t5_ac4_hosted_remains_selectable_alongside_others(monkeypatch):
    """Registering another provider never displaces 'hosted' — both remain
    independently selectable via config (gen on a stub, emb still hosted)."""
    stub = _StubProvider()
    cleanup = _register_temp(stub)
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", stub.name)
        monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)
        assert get_generation_provider() is stub
        assert isinstance(get_embedding_provider(), HostedModelProvider)
    finally:
        cleanup()


def test_t5_ac4_registration_uses_the_public_register_provider():
    """The hosted default is registered through the same public register_provider()
    any future provider uses — re-registering the SAME instance is an idempotent
    no-op (no special-casing of hosted)."""
    hosted = _PROVIDER_REGISTRY.get("hosted")
    assert hosted is not None
    # Idempotent: re-registering the existing instance must not raise.
    register_provider(hosted)
    assert _PROVIDER_REGISTRY["hosted"] is hosted


# ---------------------------------------------------------------------------
# Exactly-once telemetry across layers
#
# Registering the self-emitting D2 HostedModelProvider behind the gateway's
# instrumented generate()/embed() wrappers must NOT double-count: one logical
# call still produces exactly one telemetry event.  This guards the integration
# point T5 introduces (a self-reporting provider as the gateway default).
# ---------------------------------------------------------------------------


_GEN_EVENT = "model.generation_completed"
_EMB_EVENT = "model.embedding_completed"


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every record_event() call (gateway- or provider-level)."""
    events: List[tuple] = []

    def _fake_record_event(event_type, payload=None):
        events.append((event_type, payload or {}))

    monkeypatch.setattr("app.telemetry.record_event", _fake_record_event)
    return events


def test_t5_default_generate_emits_exactly_one_event(monkeypatch, captured_events):
    """A default-hosted generate() through the gateway emits exactly one
    generation event — the provider self-emits, the gateway does not duplicate."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # graceful ok=False, no network

    gw_generate(GenerationRequest(prompt="x", max_tokens=5))

    gen_events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen_events) == 1, "default-hosted call must emit exactly one event"
    assert gen_events[0][1]["provider"] == "hosted"


def test_t5_default_embed_emits_exactly_one_event(monkeypatch, captured_events):
    """A default-hosted embed() through the gateway emits exactly one embedding
    event — no gateway/provider duplicate."""
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

    gw_embed(["a", "b"])

    emb_events = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(emb_events) == 1, "default-hosted embed must emit exactly one event"
    assert emb_events[0][1]["provider"] == "hosted"


def test_t5_non_self_emitting_stub_still_observable(monkeypatch, captured_events):
    """A provider that does NOT self-emit stays observable: the gateway emits
    on its behalf — so swapping in a simpler provider loses no telemetry."""
    stub = _StubProvider()  # inherits emits_own_telemetry=False
    cleanup = _register_temp(stub)
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", stub.name)
        gw_generate(GenerationRequest(prompt="x", max_tokens=5))
    finally:
        cleanup()

    gen_events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen_events) == 1
    assert gen_events[0][1]["provider"] == stub.name
