"""R16-D1 T5 (AT-366) — Model gateway provider telemetry contract tests.

Every model call through the gateway records WHICH provider served it, so
provider usage is observable across hosted, in-boundary, and future
customer-tenant modes.

Acceptance Criteria
-------------------
  T5-AC1  Every generate() call emits a telemetry event containing the
          provider name from GenerationResult.provider.
  T5-AC2  Every embed() call emits a telemetry event containing the provider
          name.
  T5-AC3  Telemetry is emitted exactly once per call — not per token, not per
          retry.
  T5-AC4  A provider failure (ok=False) still emits telemetry so failures are
          observable.
  T5-AC5  New event types are registered in REGISTERED_EVENT_TYPES before use —
          record_event() does not raise ValueError.
"""

from typing import List

import pytest

from app.model_gateway import (
    ModelProvider,
    _PROVIDER_REGISTRY,
    embed,
    generate,
    register_provider,
)
from app.model_gateway._interface import GenerationRequest, GenerationResult

_GEN_EVENT = "model.generation_completed"
_EMB_EVENT = "model.embedding_completed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every record_event() call made by the gateway.

    The gateway imports record_event lazily from app.telemetry on each call, so
    patching the module attribute intercepts the real write while still proving
    the gateway uses record_event() (the shared telemetry infrastructure).
    """
    events: List[tuple] = []

    def _fake_record_event(event_type, payload=None):
        events.append((event_type, payload or {}))

    monkeypatch.setattr("app.telemetry.record_event", _fake_record_event)
    return events


class _StubGen(ModelProvider):
    """Generation provider whose result.provider differs from its registry
    name — so a test can prove the telemetry reads GenerationResult.provider."""

    name = "_t5_gen"

    def generate(self, req: GenerationRequest) -> GenerationResult:
        return GenerationResult(text="generated text", provider="_t5_served", ok=True)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return []


class _StubFailGen(ModelProvider):
    name = "_t5_fail_gen"

    def generate(self, req: GenerationRequest) -> GenerationResult:
        return GenerationResult(text=None, provider="_t5_fail_served", ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return []


class _StubEmb(ModelProvider):
    name = "_t5_emb"

    def generate(self, req: GenerationRequest) -> GenerationResult:
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[0.1, 0.2]] * len(texts)


def _use_gen(monkeypatch, provider: ModelProvider):
    register_provider(provider)
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", provider.name)


def _use_emb(monkeypatch, provider: ModelProvider):
    register_provider(provider)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", provider.name)


# ---------------------------------------------------------------------------
# T5-AC1 — generate() emits telemetry containing GenerationResult.provider
# ---------------------------------------------------------------------------


def test_t5_ac1_generate_emits_event_with_provider(monkeypatch, captured_events):
    _use_gen(monkeypatch, _StubGen())
    try:
        result = generate(GenerationRequest(prompt="hello", max_tokens=10))
    finally:
        _PROVIDER_REGISTRY.pop("_t5_gen", None)

    gen_events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen_events) == 1, "exactly one generation telemetry event expected"
    _, payload = gen_events[0]
    # AC1: provider name comes from GenerationResult.provider, not the registry key.
    assert payload["provider"] == "_t5_served"
    assert payload["provider"] == result.provider


def test_t5_ac1_generate_returns_provider_result_unchanged(monkeypatch, captured_events):
    """Telemetry is side-channel only — the provider's result is untouched."""
    _use_gen(monkeypatch, _StubGen())
    try:
        result = generate(GenerationRequest(prompt="hello", max_tokens=10))
    finally:
        _PROVIDER_REGISTRY.pop("_t5_gen", None)

    assert result.text == "generated text"
    assert result.ok is True


# ---------------------------------------------------------------------------
# T5-AC2 — embed() emits telemetry containing the provider name
# ---------------------------------------------------------------------------


def test_t5_ac2_embed_emits_event_with_provider(monkeypatch, captured_events):
    _use_emb(monkeypatch, _StubEmb())
    try:
        vectors = embed(["a", "b"])
    finally:
        _PROVIDER_REGISTRY.pop("_t5_emb", None)

    emb_events = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(emb_events) == 1, "exactly one embedding telemetry event expected"
    _, payload = emb_events[0]
    assert payload["provider"] == "_t5_emb"
    # vectors returned unchanged
    assert vectors == [[0.1, 0.2], [0.1, 0.2]]
    assert payload["text_count"] == 2
    assert payload["vector_count"] == 2
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# T5-AC3 — emitted exactly once per call
# ---------------------------------------------------------------------------


def test_t5_ac3_generate_emits_exactly_once(monkeypatch, captured_events):
    _use_gen(monkeypatch, _StubGen())
    try:
        generate(GenerationRequest(prompt="x", max_tokens=5))
    finally:
        _PROVIDER_REGISTRY.pop("_t5_gen", None)

    assert sum(1 for e in captured_events if e[0] == _GEN_EVENT) == 1


def test_t5_ac3_embed_emits_exactly_once(monkeypatch, captured_events):
    _use_emb(monkeypatch, _StubEmb())
    try:
        embed(["only-one-call"])
    finally:
        _PROVIDER_REGISTRY.pop("_t5_emb", None)

    assert sum(1 for e in captured_events if e[0] == _EMB_EVENT) == 1


def test_t5_ac3_two_calls_emit_two_events(monkeypatch, captured_events):
    """One event per call — N calls produce N events (never per-token/per-retry)."""
    _use_gen(monkeypatch, _StubGen())
    try:
        generate(GenerationRequest(prompt="x", max_tokens=5))
        generate(GenerationRequest(prompt="y", max_tokens=5))
    finally:
        _PROVIDER_REGISTRY.pop("_t5_gen", None)

    assert sum(1 for e in captured_events if e[0] == _GEN_EVENT) == 2


# ---------------------------------------------------------------------------
# T5-AC4 — a provider failure (ok=False) still emits telemetry
# ---------------------------------------------------------------------------


def test_t5_ac4_failed_generation_still_emits(monkeypatch, captured_events):
    _use_gen(monkeypatch, _StubFailGen())
    try:
        result = generate(GenerationRequest(prompt="x", max_tokens=5))
    finally:
        _PROVIDER_REGISTRY.pop("_t5_fail_gen", None)

    assert result.ok is False and result.text is None  # graceful failure preserved
    gen_events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen_events) == 1, "failure must still be observable in telemetry"
    _, payload = gen_events[0]
    assert payload["provider"] == "_t5_fail_served"
    assert payload["ok"] is False


def test_t5_ac4_hosted_missing_key_failure_emits(monkeypatch, captured_events):
    """The real hosted provider with no API key (ok=False) still emits."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)  # default 'hosted'
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    result = generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is False
    gen_events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen_events) == 1
    assert gen_events[0][1]["provider"] == "hosted"
    assert gen_events[0][1]["ok"] is False


# ---------------------------------------------------------------------------
# T5-AC5 — event types registered before use; record_event does not raise
# ---------------------------------------------------------------------------


def test_t5_ac5_event_types_are_registered():
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert _GEN_EVENT in REGISTERED_EVENT_TYPES
    assert _EMB_EVENT in REGISTERED_EVENT_TYPES


def test_t5_ac5_record_event_does_not_raise_for_gateway_types():
    """record_event() must accept both new event types without ValueError."""
    from app.telemetry import record_event

    # No exception => the types were registered before use.
    record_event(_GEN_EVENT, {"provider": "hosted", "ok": True})
    record_event(_EMB_EVENT, {"provider": "hosted", "ok": False, "text_count": 1})


def test_t5_ac5_real_generate_path_does_not_raise(monkeypatch):
    """End-to-end: a real generate() call through the gateway emits via the real
    record_event() without raising (proves registration landed before emission)."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    # Must not raise ValueError from record_event for an unregistered type.
    result = generate(GenerationRequest(prompt="x", max_tokens=5))
    assert result.provider == "hosted"
