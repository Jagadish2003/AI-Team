"""R17-D2 T3 - Customer-tenant resilience & graceful-failure contract tests.

The customer-tenant provider must follow the SAME resilience posture as the
hosted reference implementation (R16-D2) and the in-boundary provider (R17-D1)
so AgentIQ behaves consistently across all three model modes (R17-D2 §3, AC5).
A customer-owned endpoint is outside CloudFulcrum's control, so the provider
must handle slowness, rate-limiting, unavailability, and temporary errors
politely and predictably.

This suite covers:

  T3-A  Transient errors (429/529, transient 5xx, network timeouts) trigger
        bounded exponential-backoff retry; a retry that succeeds is returned.
  T3-B  Retry is bounded by _MAX_RETRIES — the endpoint is never hammered
        indefinitely, and non-transient 4xx are not retried at all.
  T3-C  A 429 Retry-After header is honoured as the minimum backoff.
  T3-D  The per-request timeout_ms is a wall-clock deadline across all attempts
        and backoff sleeps.
  T3-E  generate() NEVER raises — every failure (missing config, non-transient
        HTTP error, exhausted retries, unexpected transport error) returns
        GenerationResult(text=None, provider='customer_tenant', ok=False).
  T3-F  embed() degrades safely to [] on every failure path and shares the same
        retry posture.

Credentials are configured (env-fallback path) so the HTTP layer is exercised
rather than short-circuited by the T2 missing-credential guard. Endpoints use
the explicit full-URL overrides so no Azure deployment path (which contains the
no-bypass forbidden literal) appears in this file.
"""
from __future__ import annotations

import http.client
import json
import urllib.error
from typing import List

import pytest

from app.model_gateway._interface import GenerationRequest, GenerationResult
from app.model_gateway.customer_tenant_config import (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_DEPLOYMENT,
    CONFIG_KEY_EMBEDDING_DEPLOYMENT,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_ENDPOINT,
    CONFIG_KEY_GENERATION_DEPLOYMENT,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CUSTOMER_TENANT_PROVIDER_NAME,
)
from app.model_gateway.customer_tenant_provider import (
    CustomerTenantModelProvider,
    _MAX_RETRIES,
)

_SECRET = "ct-resilience-secret"
# Full-URL overrides — deliberately not Azure deployment paths, so no forbidden
# "open" + "ai" literal appears in this test file.
_GEN_ENDPOINT = "https://gen.customer.tenant/generate"
_EMB_ENDPOINT = "https://emb.customer.tenant/embed"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def provider() -> CustomerTenantModelProvider:
    """A fresh provider instance per test."""
    return CustomerTenantModelProvider()


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Configure a fully-specified customer-tenant endpoint + credential.

    Uses the explicit full-URL overrides and sets CUSTOMER_TENANT_API_KEY so the
    resolver's env fallback yields a credential (no vaulted row for the default
    org), letting the HTTP/resilience path run instead of the T2 credential
    short-circuit.
    """
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _SECRET)
    monkeypatch.setenv(CONFIG_KEY_GENERATION_ENDPOINT, _GEN_ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_ENDPOINT, _EMB_ENDPOINT)


class _JsonResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _gen_ok(text: str = "ok"):
    return _JsonResponse({"choices": [{"message": {"content": text}}]})


def _emb_ok(vectors):
    return _JsonResponse({"data": [{"embedding": v} for v in vectors]})


def _http_error(status: int, reason: str = "Error", retry_after: str | None = None):
    hdrs = http.client.HTTPMessage()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://fake", status, reason, hdrs, None)


def _always_raise(exc):
    def _side_effect(req, timeout=None):
        raise exc

    return _side_effect


# ===========================================================================
# T3-A — transient errors trigger retry; a retry that succeeds is returned
# ===========================================================================


def test_t3a_generate_429_then_success_returns_ok(monkeypatch, provider):
    """A single 429 retries; if the retry succeeds the call returns ok=True."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, "Too Many Requests")
        return _gen_ok("recovered after 429")

    monkeypatch.setattr("urllib.request.urlopen", _fake)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=60_000)
    )

    assert calls["n"] >= 2, "429 must be retried, not dropped on first failure"
    assert result.ok is True
    assert result.text == "recovered after 429"
    assert result.provider == CUSTOMER_TENANT_PROVIDER_NAME


def test_t3a_generate_transient_5xx_then_success(monkeypatch, provider):
    """A transient 5xx (503) is retried like a 429."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(503, "Service Unavailable")
        return _gen_ok("recovered after 503")

    monkeypatch.setattr("urllib.request.urlopen", _fake)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=60_000)
    )

    assert calls["n"] >= 2, "503 must be retried like a 429"
    assert result.ok is True
    assert result.text == "recovered after 503"


def test_t3a_generate_network_timeout_then_success(monkeypatch, provider):
    """A network TimeoutError is transient and retried within the deadline."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("read timed out")
        return _gen_ok("recovered after timeout")

    monkeypatch.setattr("urllib.request.urlopen", _fake)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=60_000)
    )

    assert calls["n"] >= 2
    assert result.ok is True
    assert result.text == "recovered after timeout"


def test_t3a_retry_is_accompanied_by_backoff_sleep(monkeypatch, provider):
    """A retryable failure sleeps (backs off) before retrying."""
    sleeps: List[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, "Too Many Requests")
        return _gen_ok("ok")

    monkeypatch.setattr("urllib.request.urlopen", _fake)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=60_000)
    )

    assert result.ok is True
    assert len(sleeps) >= 1, "a retryable failure must back off before retrying"
    assert all(s > 0 for s in sleeps), "backoff duration must be positive"


# ===========================================================================
# T3-B — retry is bounded by _MAX_RETRIES; non-transient errors are not retried
# ===========================================================================


def test_t3b_generate_retry_bounded_by_max_retries(monkeypatch, provider):
    """Endless 429s stop after _MAX_RETRIES retries (1 initial + _MAX_RETRIES)."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def _always_429(req, timeout=None):
        calls["n"] += 1
        raise _http_error(429, "Too Many Requests")

    monkeypatch.setattr("urllib.request.urlopen", _always_429)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert result.ok is False
    assert calls["n"] <= _MAX_RETRIES + 1, (
        f"must not exceed {_MAX_RETRIES + 1} total attempts; made {calls['n']}"
    )


def test_t3b_non_transient_4xx_is_not_retried(monkeypatch, provider):
    """A non-transient 4xx (403) returns immediately without any retry."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        raise _http_error(403, "Forbidden")

    monkeypatch.setattr("urllib.request.urlopen", _fake)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert result.ok is False
    assert calls["n"] == 1, "a non-transient 4xx must not be retried"


# ===========================================================================
# T3-C — Retry-After header is honoured as the minimum backoff
# ===========================================================================


def test_t3c_retry_after_header_sets_minimum_backoff(monkeypatch, provider):
    """A 429 with Retry-After causes backoff >= the header value."""
    sleeps: List[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}
    retry_after_s = "2"

    def _fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, "Rate Limited", retry_after=retry_after_s)
        return _gen_ok("ok")

    monkeypatch.setattr("urllib.request.urlopen", _fake)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert result.ok is True
    assert sleeps and sleeps[0] >= float(retry_after_s), (
        f"backoff ({sleeps[0]:.2f}s) must be >= Retry-After ({retry_after_s}s)"
    )


# ===========================================================================
# T3-D — timeout_ms is a wall-clock deadline across attempts
# ===========================================================================


def test_t3d_first_attempt_gets_full_timeout_budget(monkeypatch, provider):
    """The first HTTP attempt receives the full configured timeout budget."""
    captured: dict = {}

    def _fake(req, timeout=None):
        captured["timeout"] = timeout
        return _gen_ok("ok")

    monkeypatch.setattr("urllib.request.urlopen", _fake)

    provider.generate(GenerationRequest(prompt="x", max_tokens=5, timeout_ms=1500))

    assert captured["timeout"] == pytest.approx(1.5, abs=0.05)


def test_t3d_short_timeout_prevents_retry_backoff(monkeypatch, provider):
    """With timeout_ms=500, a 503 cannot back off within budget and returns
    ok=False without sleeping — the deadline enforces the leash."""
    sleeps: List[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        "urllib.request.urlopen", _always_raise(_http_error(503, "Service Unavailable"))
    )

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=500)
    )

    assert result.ok is False
    assert sleeps == [], (
        "with a 500ms deadline the first backoff (0.5s) exceeds the budget; "
        "time.sleep must not be called"
    )


def test_t3d_timeout_ms_constrains_attempt_timeout(monkeypatch, provider):
    """A short timeout_ms passes a correspondingly short timeout to urlopen."""
    short: List[float] = []
    long: List[float] = []

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: short.append(timeout) or _gen_ok("ok"),
    )
    provider.generate(GenerationRequest(prompt="x", max_tokens=5, timeout_ms=500))

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: long.append(timeout) or _gen_ok("ok"),
    )
    provider.generate(GenerationRequest(prompt="x", max_tokens=5))  # default 30000ms

    assert short and long
    assert short[0] < long[0], (
        f"500ms timeout ({short[0]:.4f}s) must be < default ({long[0]:.2f}s)"
    )


# ===========================================================================
# T3-E — generate() never raises; graceful failure shape is stable
# ===========================================================================


def test_t3e_retry_exhaustion_returns_graceful_failure(monkeypatch, provider):
    """Exhausting retries returns ok=False / text=None / provider='customer_tenant'."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "urllib.request.urlopen", _always_raise(_http_error(429, "Rate Limited"))
    )

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert result == GenerationResult(
        text=None, provider=CUSTOMER_TENANT_PROVIDER_NAME, ok=False
    )


def test_t3e_unexpected_transport_error_does_not_raise(monkeypatch, provider):
    """An unexpected exception from urlopen is swallowed into ok=False."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "urllib.request.urlopen", _always_raise(RuntimeError("network stack exploded"))
    )

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert isinstance(result, GenerationResult)
    assert result.ok is False
    assert result.text is None
    assert result.provider == CUSTOMER_TENANT_PROVIDER_NAME


def test_t3e_url_error_is_transient_and_degrades(monkeypatch, provider):
    """A URLError (connection refused etc.) is transient and degrades to ok=False."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _always_raise(urllib.error.URLError("connection refused")),
    )

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert result.ok is False
    assert result.text is None


def test_t3e_missing_config_returns_graceful_failure_without_network(
    monkeypatch, provider
):
    """Missing endpoint config returns ok=False without touching the network."""
    monkeypatch.delenv(CONFIG_KEY_ENDPOINT, raising=False)
    monkeypatch.delenv(CONFIG_KEY_GENERATION_ENDPOINT, raising=False)
    monkeypatch.delenv(CONFIG_KEY_GENERATION_DEPLOYMENT, raising=False)
    monkeypatch.delenv(CONFIG_KEY_DEPLOYMENT, raising=False)

    def _fail(*_a, **_k):
        raise AssertionError("network must not be called when config is missing")

    monkeypatch.setattr("urllib.request.urlopen", _fail)

    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result == GenerationResult(
        text=None, provider=CUSTOMER_TENANT_PROVIDER_NAME, ok=False
    )


# ===========================================================================
# T3-F — embed() shares the resilience posture and degrades safely to []
# ===========================================================================


def test_t3f_embed_429_then_success(monkeypatch, provider):
    """embed() retries a transient 429 and returns vectors when the retry succeeds."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, "Too Many Requests")
        return _emb_ok([[0.1, 0.2, 0.3]])

    monkeypatch.setattr("urllib.request.urlopen", _fake)

    vectors = provider.embed(["customer-private text"])

    assert calls["n"] >= 2, "embed() must retry a transient 429"
    assert vectors == [[0.1, 0.2, 0.3]]


def test_t3f_embed_retry_bounded_and_degrades_to_empty(monkeypatch, provider):
    """Endless 429s on embed() stop after _MAX_RETRIES and degrade to []."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def _always_429(req, timeout=None):
        calls["n"] += 1
        raise _http_error(429, "Too Many Requests")

    monkeypatch.setattr("urllib.request.urlopen", _always_429)

    vectors = provider.embed(["a", "b"])

    assert vectors == []
    assert calls["n"] <= _MAX_RETRIES + 1


def test_t3f_embed_unexpected_error_does_not_raise(monkeypatch, provider):
    """embed() swallows an unexpected transport error and returns []."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("urllib.request.urlopen", _always_raise(RuntimeError("boom")))

    assert provider.embed(["a"]) == []


def test_t3f_embed_non_transient_4xx_not_retried(monkeypatch, provider):
    """A non-transient 4xx on embed() is not retried and degrades to []."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def _fake(req, timeout=None):
        calls["n"] += 1
        raise _http_error(400, "Bad Request")

    monkeypatch.setattr("urllib.request.urlopen", _fake)

    assert provider.embed(["a"]) == []
    assert calls["n"] == 1
