"""R16-D2 T6 (AT-410) — HostedModelProvider provider-identity telemetry.

Every generate() and embed() call on HostedModelProvider records a telemetry
event so hosted-mode usage is observable across all runs, including failures.

Acceptance Criteria
-------------------
  T6-AC1  Every generate() call emits a telemetry event with provider='hosted'.
  T6-AC2  Every embed() call emits a telemetry event with provider='hosted'.
  T6-AC3  Telemetry is emitted exactly once per call — not per retry, not per token.
  T6-AC4  A failed call (ok=False) still emits telemetry so failures are observable.
  T6-AC5  New event types are registered in REGISTERED_EVENT_TYPES before use —
          record_event() does not raise ValueError.
"""

from __future__ import annotations

import http.client
import urllib.error
from typing import List

import pytest

from app.model_gateway._interface import GenerationRequest, GenerationResult
from app.model_gateway.hosted_provider import HostedModelProvider

_GEN_EVENT = "model.generation_completed"
_EMB_EVENT = "model.embedding_completed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    return HostedModelProvider()


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every record_event() call made by HostedModelProvider.

    The provider imports record_event lazily from app.telemetry, so patching
    the module attribute intercepts the real write.
    """
    events: List[tuple] = []

    def _fake_record_event(event_type, payload=None):
        events.append((event_type, payload or {}))

    monkeypatch.setattr("app.telemetry.record_event", _fake_record_event)
    return events


# ---------------------------------------------------------------------------
# T6-AC1 — generate() emits model.generation_completed with provider='hosted'
# ---------------------------------------------------------------------------


def test_ac1_generate_emits_generation_event(monkeypatch, provider, captured_events):
    """generate() emits model.generation_completed on a successful call."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    # Stub urlopen to return a synthetic successful response.
    import io
    import json

    body = json.dumps({
        "content": [{"type": "text", "text": "Hello from stub"}]
    }).encode("utf-8")

    class _FakeResp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _FakeResp())

    result = provider.generate(GenerationRequest(prompt="hi", max_tokens=10))

    assert result.ok is True
    gen_events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen_events) == 1, "exactly one generation telemetry event expected"
    _, payload = gen_events[0]
    assert payload["provider"] == "hosted"
    assert payload["ok"] is True


def test_ac1_generate_emits_event_with_provider_hosted_on_missing_key(
    monkeypatch, provider, captured_events
):
    """generate() emits provider='hosted' even when the API key is absent."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is False
    gen_events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen_events) == 1
    assert gen_events[0][1]["provider"] == "hosted"


# ---------------------------------------------------------------------------
# T6-AC2 — embed() emits model.embedding_completed with provider='hosted'
# ---------------------------------------------------------------------------


def test_ac2_embed_emits_embedding_event_with_provider(provider, captured_events):
    """embed() emits model.embedding_completed with provider='hosted'."""
    vectors = provider.embed(["hello", "world"])

    assert vectors == []
    emb_events = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(emb_events) == 1, "exactly one embedding telemetry event expected"
    _, payload = emb_events[0]
    assert payload["provider"] == "hosted"


def test_ac2_embed_payload_carries_counts(provider, captured_events):
    """The embedding event payload records text_count and vector_count."""
    provider.embed(["a", "b", "c"])

    _, payload = [e for e in captured_events if e[0] == _EMB_EVENT][0]
    assert payload["text_count"] == 3
    assert payload["vector_count"] == 0  # hosted provider returns no vectors


def test_ac2_embed_ok_false_when_texts_supplied_but_no_vectors(
    provider, captured_events
):
    """ok=False in the embedding event when the provider returned no vectors."""
    provider.embed(["text"])

    _, payload = [e for e in captured_events if e[0] == _EMB_EVENT][0]
    assert payload["ok"] is False  # 0 vectors for 1 text


def test_ac2_embed_ok_true_for_empty_input(provider, captured_events):
    """ok=True for an empty-texts call (0 vectors == 0 texts is a no-op success)."""
    provider.embed([])

    _, payload = [e for e in captured_events if e[0] == _EMB_EVENT][0]
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# T6-AC3 — Telemetry is emitted exactly once per call
# ---------------------------------------------------------------------------


def test_ac3_generate_emits_exactly_once_on_success(monkeypatch, provider, captured_events):
    """One generate() call => exactly one telemetry event."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # triggers immediate ok=False exit

    provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert sum(1 for e in captured_events if e[0] == _GEN_EVENT) == 1


def test_ac3_generate_emits_exactly_once_even_after_retries(
    monkeypatch, provider, captured_events
):
    """Multiple retry attempts must produce exactly ONE telemetry event, not one per retry."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    call_count = {"n": 0}

    def _always_429(req, timeout=None):
        call_count["n"] += 1
        hdrs = http.client.HTTPMessage()
        raise urllib.error.HTTPError("http://fake", 429, "Too Many Requests", hdrs, None)

    monkeypatch.setattr("urllib.request.urlopen", _always_429)

    provider.generate(GenerationRequest(prompt="x", max_tokens=5, timeout_ms=60_000))

    # The retry loop fires multiple HTTP attempts...
    assert call_count["n"] > 1, "expected multiple retry attempts"
    # ...but telemetry is emitted ONCE at the end.
    assert sum(1 for e in captured_events if e[0] == _GEN_EVENT) == 1, (
        "telemetry must be emitted exactly once per generate() call, not per retry"
    )


def test_ac3_two_generate_calls_emit_two_events(monkeypatch, provider, captured_events):
    """N generate() calls produce exactly N telemetry events."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    provider.generate(GenerationRequest(prompt="x", max_tokens=5))
    provider.generate(GenerationRequest(prompt="y", max_tokens=5))

    assert sum(1 for e in captured_events if e[0] == _GEN_EVENT) == 2


def test_ac3_embed_emits_exactly_once(provider, captured_events):
    """One embed() call => exactly one telemetry event."""
    provider.embed(["text"])

    assert sum(1 for e in captured_events if e[0] == _EMB_EVENT) == 1


def test_ac3_two_embed_calls_emit_two_events(provider, captured_events):
    """N embed() calls produce exactly N telemetry events."""
    provider.embed(["a"])
    provider.embed(["b", "c"])

    assert sum(1 for e in captured_events if e[0] == _EMB_EVENT) == 2


# ---------------------------------------------------------------------------
# T6-AC4 — A failed call still emits telemetry
# ---------------------------------------------------------------------------


def test_ac4_generate_failure_missing_key_emits_telemetry(
    monkeypatch, provider, captured_events
):
    """ok=False on missing API key still produces a telemetry event."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is False
    gen_events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen_events) == 1, "failure must still be observable in telemetry"
    assert gen_events[0][1]["ok"] is False
    assert gen_events[0][1]["provider"] == "hosted"


def test_ac4_generate_failure_exception_emits_telemetry(
    monkeypatch, provider, captured_events
):
    """ok=False due to a network exception still produces a telemetry event."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    def _boom(req, timeout=None):
        raise RuntimeError("network exploded")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is False
    gen_events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen_events) == 1
    assert gen_events[0][1]["ok"] is False


def test_ac4_generate_failure_http_4xx_emits_telemetry(
    monkeypatch, provider, captured_events
):
    """ok=False due to a non-retryable HTTP 4xx still produces a telemetry event."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    def _always_403(req, timeout=None):
        hdrs = http.client.HTTPMessage()
        raise urllib.error.HTTPError("http://fake", 403, "Forbidden", hdrs, None)

    monkeypatch.setattr("urllib.request.urlopen", _always_403)

    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is False
    gen_events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen_events) == 1
    assert gen_events[0][1]["ok"] is False


def test_ac4_embed_failure_emits_telemetry_with_ok_false(provider, captured_events):
    """embed() returning no vectors for supplied texts emits ok=False telemetry."""
    provider.embed(["text1", "text2"])

    emb_events = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(emb_events) == 1
    assert emb_events[0][1]["ok"] is False
    assert emb_events[0][1]["provider"] == "hosted"


# ---------------------------------------------------------------------------
# T6-AC5 — Event types registered before use; record_event() does not raise
# ---------------------------------------------------------------------------


def test_ac5_generation_event_type_is_registered():
    """model.generation_completed is in REGISTERED_EVENT_TYPES."""
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert _GEN_EVENT in REGISTERED_EVENT_TYPES


def test_ac5_embedding_event_type_is_registered():
    """model.embedding_completed is in REGISTERED_EVENT_TYPES."""
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert _EMB_EVENT in REGISTERED_EVENT_TYPES


def test_ac5_record_event_does_not_raise_for_generation_type():
    """record_event() accepts model.generation_completed without ValueError."""
    from app.telemetry import record_event

    record_event(_GEN_EVENT, {"provider": "hosted", "ok": True})


def test_ac5_record_event_does_not_raise_for_embedding_type():
    """record_event() accepts model.embedding_completed without ValueError."""
    from app.telemetry import record_event

    record_event(_EMB_EVENT, {"provider": "hosted", "ok": False, "text_count": 2, "vector_count": 0})


def test_ac5_provider_generate_does_not_raise_for_unregistered_type(
    monkeypatch, provider
):
    """End-to-end: a real generate() on the provider does not raise ValueError.

    Proves that event registration landed before the first emit call-site.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    # Must not raise ValueError from record_event (unregistered type guard).
    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))
    assert result.provider == "hosted"


def test_ac5_provider_embed_does_not_raise_for_unregistered_type(provider):
    """End-to-end: a real embed() on the provider does not raise ValueError."""
    vectors = provider.embed(["test"])
    assert vectors == []
