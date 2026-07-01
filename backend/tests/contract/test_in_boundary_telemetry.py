"""R17-D1 T5 - In-boundary provider-identity telemetry contract tests.

Every in-boundary generation and embedding call must record provider identity
in telemetry with ``provider='in_boundary'`` (Story AC5), extending the same
observability the hosted provider already has (R16-D2 T6).  This lets support
and audit teams confirm which provider served each model call — proof a
regulated customer may need that the in-boundary provider was used.

Covered here:

  T5-A  Every generate() call (direct or via the gateway) emits exactly one
        ``model.generation_completed`` event with provider='in_boundary'.
  T5-B  Every embed() call emits exactly one ``model.embedding_completed`` event
        with provider='in_boundary', plus text/vector counts.
  T5-C  Failures (missing config, HTTP error, exhausted retries) still emit a
        provider-identifying event with ok=False — failures are observable.
  T5-D  Telemetry is emitted exactly once per call — never per retry, never per
        token — and the gateway does not double-count (emits_own_telemetry).
  T5-E  SAFETY: the payload never contains prompt text, generated output, input
        texts, embedding vectors, endpoint credentials, or customer secrets.
  T5-F  The event types are registered before use, and a telemetry failure never
        breaks the model call (best-effort).
"""
from __future__ import annotations

import json
from typing import Any, List

import pytest

from app.model_gateway import (
    embed as gw_embed,
    generate as gw_generate,
)
from app.model_gateway._interface import GenerationRequest
from app.model_gateway.in_boundary_config import (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_BASE_URL,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_EMBEDDING_MODEL,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CONFIG_KEY_GENERATION_MODEL,
    CONFIG_KEY_MODEL,
    IN_BOUNDARY_PROVIDER_NAME,
)
from app.model_gateway.in_boundary_provider import (
    InBoundaryModelProvider,
    _MAX_RETRIES,
)

_GEN_EVENT = "model.generation_completed"
_EMB_EVENT = "model.embedding_completed"

_SECRET = "ib-telemetry-secret-DO-NOT-LOG"
_GEN_ENDPOINT = "https://models.customer.internal/v1/chat/completions"
_EMB_ENDPOINT = "https://models.customer.internal/v1/embeddings"
_PROMPT = "customer-private prompt text that must never be in telemetry"
_GEN_TEXT = "customer-private generated answer that must never be in telemetry"
_INPUT_TEXTS = ["customer-private embedding input that must never be in telemetry"]


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def provider() -> InBoundaryModelProvider:
    return InBoundaryModelProvider()


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every record_event() call without writing to the DB."""
    events: List[tuple] = []

    def _fake(event_type, payload=None):
        events.append((event_type, payload or {}))

    monkeypatch.setattr("app.telemetry.record_event", _fake)
    return events


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _SECRET)
    monkeypatch.setenv(CONFIG_KEY_GENERATION_ENDPOINT, _GEN_ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_ENDPOINT, _EMB_ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_GENERATION_MODEL, "customer-gen-model")
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_MODEL, "customer-embedding-model")


class _JsonResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _gen_ok(text: str):
    return lambda req, timeout=None: _JsonResponse(
        {"choices": [{"message": {"content": text}}]}
    )


def _emb_ok(vectors):
    return lambda req, timeout=None: _JsonResponse(
        {"data": [{"embedding": v} for v in vectors]}
    )


# ===========================================================================
# T5-A — generate() records provider='in_boundary'
# ===========================================================================


def test_t5a_generate_records_provider_in_boundary_on_success(
    monkeypatch, provider, captured_events, _configured
):
    monkeypatch.setattr("urllib.request.urlopen", _gen_ok(_GEN_TEXT))

    result = provider.generate(GenerationRequest(prompt=_PROMPT, max_tokens=5))

    assert result.ok is True
    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 1
    assert gen[0][1]["provider"] == IN_BOUNDARY_PROVIDER_NAME
    assert gen[0][1]["ok"] is True


def test_t5a_gateway_generate_records_provider_in_boundary(
    monkeypatch, captured_events, _configured
):
    """Selecting in_boundary via config routes an unchanged gateway call and
    still records exactly one provider='in_boundary' event."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    monkeypatch.setattr("urllib.request.urlopen", _gen_ok("inside"))

    result = gw_generate(GenerationRequest(prompt=_PROMPT, max_tokens=5))

    assert result.provider == IN_BOUNDARY_PROVIDER_NAME
    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 1
    assert gen[0][1]["provider"] == IN_BOUNDARY_PROVIDER_NAME


# ===========================================================================
# T5-B — embed() records provider='in_boundary' with counts
# ===========================================================================


def test_t5b_embed_records_provider_in_boundary_with_counts(
    monkeypatch, provider, captured_events, _configured
):
    monkeypatch.setattr("urllib.request.urlopen", _emb_ok([[0.1, 0.2], [0.3, 0.4]]))

    vectors = provider.embed(["one", "two"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    emb = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(emb) == 1
    payload = emb[0][1]
    assert payload["provider"] == IN_BOUNDARY_PROVIDER_NAME
    assert payload["ok"] is True
    assert payload["text_count"] == 2
    assert payload["vector_count"] == 2


def test_t5b_embed_empty_input_still_records_provider(
    provider, captured_events
):
    """An empty-input no-op still records a provider='in_boundary' event (ok=True)."""
    vectors = provider.embed([])

    assert vectors == []
    emb = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(emb) == 1
    assert emb[0][1]["provider"] == IN_BOUNDARY_PROVIDER_NAME
    assert emb[0][1]["ok"] is True
    assert emb[0][1]["text_count"] == 0
    assert emb[0][1]["vector_count"] == 0


def test_t5b_gateway_embed_records_provider_in_boundary(
    monkeypatch, captured_events, _configured
):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    monkeypatch.setattr("urllib.request.urlopen", _emb_ok([[1.0, 2.0, 3.0]]))

    vectors = gw_embed(["hello"])

    assert vectors == [[1.0, 2.0, 3.0]]
    emb = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(emb) == 1
    assert emb[0][1]["provider"] == IN_BOUNDARY_PROVIDER_NAME


# ===========================================================================
# T5-C — failures still emit a provider-identifying event (ok=False)
# ===========================================================================


def test_t5c_generate_missing_config_still_records_provider(
    monkeypatch, provider, captured_events
):
    monkeypatch.delenv(CONFIG_KEY_BASE_URL, raising=False)
    monkeypatch.delenv(CONFIG_KEY_GENERATION_ENDPOINT, raising=False)
    monkeypatch.delenv(CONFIG_KEY_GENERATION_MODEL, raising=False)
    monkeypatch.delenv(CONFIG_KEY_MODEL, raising=False)

    result = provider.generate(GenerationRequest(prompt=_PROMPT, max_tokens=5))

    assert result.ok is False
    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 1
    assert gen[0][1]["provider"] == IN_BOUNDARY_PROVIDER_NAME
    assert gen[0][1]["ok"] is False


def test_t5c_generate_retry_exhaustion_still_records_provider(
    monkeypatch, provider, captured_events, _configured
):
    import http.client
    import urllib.error

    monkeypatch.setattr("time.sleep", lambda *_: None)

    def _always_429(req, timeout=None):
        raise urllib.error.HTTPError(
            "http://fake", 429, "Too Many Requests", http.client.HTTPMessage(), None
        )

    monkeypatch.setattr("urllib.request.urlopen", _always_429)

    result = provider.generate(
        GenerationRequest(prompt=_PROMPT, max_tokens=5, timeout_ms=300_000)
    )

    assert result.ok is False
    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 1, "exhausted retries still emit exactly one event"
    assert gen[0][1]["provider"] == IN_BOUNDARY_PROVIDER_NAME
    assert gen[0][1]["ok"] is False


def test_t5c_embed_failure_still_records_provider_ok_false(
    monkeypatch, provider, captured_events, _configured
):
    """A partial/failed embedding response degrades to [] and records ok=False."""
    monkeypatch.setattr("urllib.request.urlopen", _emb_ok([[0.1, 0.2]]))

    vectors = provider.embed(["one", "two"])  # 2 inputs, 1 vector → mismatch

    assert vectors == []
    emb = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(emb) == 1
    assert emb[0][1]["provider"] == IN_BOUNDARY_PROVIDER_NAME
    assert emb[0][1]["ok"] is False
    assert emb[0][1]["text_count"] == 2
    assert emb[0][1]["vector_count"] == 0


# ===========================================================================
# T5-D — exactly once per call; no gateway double-count
# ===========================================================================


def test_t5d_generate_emits_exactly_once_per_call(
    monkeypatch, provider, captured_events, _configured
):
    monkeypatch.setattr("urllib.request.urlopen", _gen_ok("ok"))

    for _ in range(3):
        provider.generate(GenerationRequest(prompt=_PROMPT, max_tokens=5))

    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 3
    assert all(e[1]["provider"] == IN_BOUNDARY_PROVIDER_NAME for e in gen)


def test_t5d_retry_emits_exactly_one_event_not_per_attempt(
    monkeypatch, provider, captured_events, _configured
):
    """A call that retries before succeeding still emits exactly one event."""
    import http.client
    import urllib.error

    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def _fail_once(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                "http://fake", 503, "Service Unavailable", http.client.HTTPMessage(), None
            )
        return _JsonResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", _fail_once)

    provider.generate(GenerationRequest(prompt=_PROMPT, max_tokens=5, timeout_ms=60_000))

    assert calls["n"] >= 2, "the call must have retried"
    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 1, "telemetry is once-per-call, not once-per-retry"


def test_t5d_in_boundary_sets_emits_own_telemetry(provider):
    """emits_own_telemetry=True so the gateway skips its own emission."""
    assert provider.emits_own_telemetry is True


def test_t5d_gateway_does_not_double_count(
    monkeypatch, captured_events, _configured
):
    """One gateway generate() through in_boundary → exactly one event, not two."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    monkeypatch.setattr("urllib.request.urlopen", _gen_ok("ok"))

    gw_generate(GenerationRequest(prompt=_PROMPT, max_tokens=5))

    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 1, "gateway + self-emitting provider must produce exactly one event"


# ===========================================================================
# T5-E — SAFETY: payload carries no sensitive values
# ===========================================================================


def _assert_no_sensitive_values(payload: dict) -> None:
    """The telemetry payload must contain only safe scalar metadata."""
    blob = json.dumps(payload)
    assert _PROMPT not in blob, "prompt text leaked into telemetry"
    assert _GEN_TEXT not in blob, "generated output leaked into telemetry"
    for text in _INPUT_TEXTS:
        assert text not in blob, "embedding input text leaked into telemetry"
    assert _SECRET not in blob, "endpoint credential leaked into telemetry"
    # Only safe metadata keys are permitted.
    allowed = {"provider", "ok", "text_count", "vector_count"}
    assert set(payload).issubset(allowed), (
        f"unexpected telemetry keys: {set(payload) - allowed}"
    )


def test_t5e_generation_payload_has_no_prompt_output_or_secret(
    monkeypatch, provider, captured_events, _configured
):
    monkeypatch.setattr("urllib.request.urlopen", _gen_ok(_GEN_TEXT))

    provider.generate(GenerationRequest(prompt=_PROMPT, max_tokens=5))

    gen = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(gen) == 1
    _assert_no_sensitive_values(gen[0][1])


def test_t5e_embedding_payload_has_no_texts_vectors_or_secret(
    monkeypatch, provider, captured_events, _configured
):
    monkeypatch.setattr("urllib.request.urlopen", _emb_ok([[0.123456, 0.654321]]))

    provider.embed(_INPUT_TEXTS)

    emb = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(emb) == 1
    payload = emb[0][1]
    _assert_no_sensitive_values(payload)
    # The actual vector values must not appear anywhere in the payload.
    assert "0.123456" not in json.dumps(payload)


# ===========================================================================
# T5-F — registered before use; telemetry failure never breaks the call
# ===========================================================================


def test_t5f_event_types_registered_before_emit():
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert _GEN_EVENT in REGISTERED_EVENT_TYPES
    assert _EMB_EVENT in REGISTERED_EVENT_TYPES


def test_t5f_telemetry_failure_does_not_break_generate(
    monkeypatch, provider, _configured
):
    """If record_event raises, generate() still returns its result (best-effort)."""
    monkeypatch.setattr("urllib.request.urlopen", _gen_ok("ok"))

    def _boom(*_a, **_k):
        raise RuntimeError("telemetry backend down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)

    result = provider.generate(GenerationRequest(prompt=_PROMPT, max_tokens=5))

    assert result.ok is True
    assert result.text == "ok"


def test_t5f_telemetry_failure_does_not_break_embed(
    monkeypatch, provider, _configured
):
    monkeypatch.setattr("urllib.request.urlopen", _emb_ok([[1.0, 2.0]]))

    def _boom(*_a, **_k):
        raise RuntimeError("telemetry backend down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)

    vectors = provider.embed(["x"])

    assert vectors == [[1.0, 2.0]]
