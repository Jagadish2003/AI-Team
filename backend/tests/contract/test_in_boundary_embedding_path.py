"""R17-D1 T2 - In-boundary embedding path contract tests."""
from __future__ import annotations

import json
from typing import Any

from app.model_gateway import embed as gw_embed
from app.model_gateway.in_boundary_config import (
    CONFIG_KEY_BASE_URL,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_EMBEDDING_MODEL,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CONFIG_KEY_GENERATION_MODEL,
    IN_BOUNDARY_PROVIDER_NAME,
)
from app.model_gateway.in_boundary_provider import InBoundaryModelProvider

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
    return json.loads((req.data or b"{}").decode("utf-8"))


def _configure_embedding(monkeypatch) -> None:
    monkeypatch.setenv(CONFIG_KEY_GENERATION_ENDPOINT, _GEN_ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_ENDPOINT, _EMB_ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_GENERATION_MODEL, "customer-gen-model")
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_MODEL, "customer-embedding-model")


def test_t2_embedding_provider_calls_only_configured_embedding_endpoint(monkeypatch):
    """Embedding can run in-boundary while generation stays on another provider."""
    _configure_embedding(monkeypatch)
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)

    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_urlopen(req, timeout=None):
        calls.append((req.full_url, _request_json(req)))
        if req.full_url != _EMB_ENDPOINT:
            raise AssertionError(f"embedding left configured endpoint: {req.full_url}")
        return _JsonResponse({"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    vectors = gw_embed(["customer-private text"])

    assert vectors == [[0.1, 0.2, 0.3]]
    assert len(calls) == 1
    assert calls[0][0] == _EMB_ENDPOINT
    assert calls[0][1] == {
        "model": "customer-embedding-model",
        "input": ["customer-private text"],
    }
    assert "messages" not in calls[0][1]
    assert "max_tokens" not in calls[0][1]


def test_t2_embedding_missing_endpoint_has_no_hosted_or_generation_fallback(monkeypatch):
    """If embedding endpoint is absent, texts do not fall back to another endpoint."""
    monkeypatch.delenv(CONFIG_KEY_BASE_URL, raising=False)
    monkeypatch.setenv(CONFIG_KEY_GENERATION_ENDPOINT, _GEN_ENDPOINT)
    monkeypatch.delenv(CONFIG_KEY_EMBEDDING_ENDPOINT, raising=False)
    monkeypatch.setenv(CONFIG_KEY_GENERATION_MODEL, "customer-gen-model")
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_MODEL, "customer-embedding-model")

    called = {"network": False}

    def _fail_if_called(*_args, **_kwargs):
        called["network"] = True
        raise AssertionError("embedding must not call any network endpoint")

    monkeypatch.setattr("urllib.request.urlopen", _fail_if_called)

    assert InBoundaryModelProvider().embed(["customer-private text"]) == []
    assert called["network"] is False


def test_t2_indexed_embedding_response_is_returned_in_input_order(monkeypatch):
    """Indexed embedding rows are sorted back to input order."""
    _configure_embedding(monkeypatch)

    def _fake_urlopen(req, timeout=None):
        return _JsonResponse(
            {
                "data": [
                    {"index": 1, "embedding": [0.4, 0.5]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    assert InBoundaryModelProvider().embed(["first", "second"]) == [
        [0.1, 0.2],
        [0.4, 0.5],
    ]


def test_t2_bad_embedding_index_set_degrades_to_empty_list(monkeypatch):
    """Duplicate/missing indexes are unsafe for retrieval, so fail closed."""
    _configure_embedding(monkeypatch)

    def _fake_urlopen(req, timeout=None):
        return _JsonResponse(
            {
                "data": [
                    {"index": 1, "embedding": [0.4, 0.5]},
                    {"index": 1, "embedding": [0.1, 0.2]},
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    assert InBoundaryModelProvider().embed(["first", "second"]) == []


def test_t2_partial_embedding_response_degrades_to_empty_list(monkeypatch):
    """Every input text must receive a vector, otherwise retrieval should skip."""
    _configure_embedding(monkeypatch)

    def _fake_urlopen(req, timeout=None):
        return _JsonResponse({"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    assert InBoundaryModelProvider().embed(["first", "second"]) == []
