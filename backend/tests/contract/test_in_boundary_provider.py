"""R17-D1 T1 - In-boundary ModelProvider adapter contract tests."""
from __future__ import annotations

import json
from typing import Any

from app.model_gateway import (
    _PROVIDER_REGISTRY,
    embed as gw_embed,
    generate as gw_generate,
)
from app.model_gateway._interface import GenerationRequest, GenerationResult, ModelProvider
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
from app.model_gateway.in_boundary_provider import InBoundaryModelProvider

_SECRET = "ib-test-secret"
_GEN_ENDPOINT = "https://models.customer.internal/v1/chat/completions"
_EMB_ENDPOINT = "https://models.customer.internal/v1/embeddings"


class _JsonResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _request_json(req) -> dict[str, Any]:
    data = req.data or b"{}"
    return json.loads(data.decode("utf-8"))


def _configure_in_boundary(monkeypatch) -> None:
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _SECRET)
    monkeypatch.setenv(CONFIG_KEY_GENERATION_ENDPOINT, _GEN_ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_ENDPOINT, _EMB_ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_GENERATION_MODEL, "customer-gen-model")
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_MODEL, "customer-embedding-model")


def test_t1_provider_is_registered_behind_gateway():
    """The in_boundary implementation is selectable by gateway config."""
    provider = _PROVIDER_REGISTRY.get(IN_BOUNDARY_PROVIDER_NAME)

    assert isinstance(provider, InBoundaryModelProvider)
    assert isinstance(provider, ModelProvider)
    assert provider.name == IN_BOUNDARY_PROVIDER_NAME


def test_t1_generate_posts_to_configured_customer_endpoint(monkeypatch):
    """generate() translates AgentIQ's request to the configured endpoint."""
    _configure_in_boundary(monkeypatch)
    captured: dict[str, Any] = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["authorization"] = req.get_header("Authorization")
        captured["payload"] = _request_json(req)
        return _JsonResponse(
            {"choices": [{"message": {"content": "  approved response  "}}]}
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    result = InBoundaryModelProvider().generate(
        GenerationRequest(prompt="Summarize this", max_tokens=64, timeout_ms=1500)
    )

    assert result == GenerationResult(
        text="approved response",
        provider=IN_BOUNDARY_PROVIDER_NAME,
        ok=True,
    )
    assert captured["url"] == _GEN_ENDPOINT
    assert captured["timeout"] == 1.5
    assert captured["authorization"] == f"Bearer {_SECRET}"
    assert captured["payload"] == {
        "model": "customer-gen-model",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "Summarize this"}],
    }


def test_t1_embed_posts_to_configured_customer_endpoint(monkeypatch):
    """embed() sends all texts to the configured in-boundary embedding endpoint."""
    _configure_in_boundary(monkeypatch)
    captured: dict[str, Any] = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["authorization"] = req.get_header("Authorization")
        captured["payload"] = _request_json(req)
        return _JsonResponse(
            {
                "data": [
                    {"embedding": [0.1, "0.2", 0.3]},
                    {"embedding": [0.4, 0.5, 0.6]},
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    vectors = InBoundaryModelProvider().embed(["first", "second"])

    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert captured["url"] == _EMB_ENDPOINT
    assert captured["authorization"] == f"Bearer {_SECRET}"
    assert captured["payload"] == {
        "model": "customer-embedding-model",
        "input": ["first", "second"],
    }


def test_t1_gateway_routes_generation_and_embedding_without_caller_change(
    monkeypatch,
):
    """Selecting in_boundary by config routes unchanged gateway calls to it."""
    _configure_in_boundary(monkeypatch)
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)

    events = []
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda event_type, payload=None: events.append((event_type, payload or {})),
    )

    calls = []

    def _fake_urlopen(req, timeout=None):
        calls.append((req.full_url, _request_json(req)))
        if req.full_url == _GEN_ENDPOINT:
            return _JsonResponse({"choices": [{"message": {"content": "inside"}}]})
        if req.full_url == _EMB_ENDPOINT:
            return _JsonResponse({"data": [{"embedding": [1, 2, 3]}]})
        raise AssertionError(f"unexpected endpoint: {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    result = gw_generate(GenerationRequest(prompt="hello", max_tokens=5))
    vectors = gw_embed(["hello"])

    assert result.text == "inside"
    assert result.provider == IN_BOUNDARY_PROVIDER_NAME
    assert vectors == [[1.0, 2.0, 3.0]]
    assert [url for url, _payload in calls] == [_GEN_ENDPOINT, _EMB_ENDPOINT]
    assert len(events) == 2
    assert all(event[1]["provider"] == IN_BOUNDARY_PROVIDER_NAME for event in events)


def test_t1_generate_missing_config_fails_gracefully_without_network(monkeypatch):
    """Missing endpoint config returns ok=False/text=None and never calls network."""
    monkeypatch.delenv(CONFIG_KEY_BASE_URL, raising=False)
    monkeypatch.delenv(CONFIG_KEY_GENERATION_ENDPOINT, raising=False)
    monkeypatch.delenv(CONFIG_KEY_GENERATION_MODEL, raising=False)
    monkeypatch.delenv(CONFIG_KEY_MODEL, raising=False)
    called = {"network": False}

    def _fail_if_called(*_args, **_kwargs):
        called["network"] = True
        raise AssertionError("network should not be called")

    monkeypatch.setattr("urllib.request.urlopen", _fail_if_called)

    result = InBoundaryModelProvider().generate(
        GenerationRequest(prompt="hello", max_tokens=5)
    )

    assert result == GenerationResult(
        text=None,
        provider=IN_BOUNDARY_PROVIDER_NAME,
        ok=False,
    )
    assert called["network"] is False


def test_t1_embed_empty_input_is_noop_without_network(monkeypatch):
    """Empty embedding input returns [] without hitting any endpoint."""
    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("network should not be called")

    monkeypatch.setattr("urllib.request.urlopen", _fail_if_called)

    assert InBoundaryModelProvider().embed([]) == []


def test_t1_embed_response_count_mismatch_fails_gracefully(monkeypatch):
    """Embedding must return one vector per input; otherwise it degrades to []."""
    _configure_in_boundary(monkeypatch)

    def _fake_urlopen(req, timeout=None):
        return _JsonResponse({"data": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    assert InBoundaryModelProvider().embed(["one", "two"]) == []
