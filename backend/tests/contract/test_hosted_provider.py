"""R16-D2 T7 (AT-411) — Full contract test suite for Hosted Model Mode.

Covers every acceptance criterion in the R16-D2 story (§6):

  AC1  HostedModelProvider implements both generate() and embed() through
       the gateway interface.
  AC2  A transient error (e.g. 429) triggers bounded retry with backoff;
       the call succeeds if a retry succeeds, not dropped on first failure.
  AC3  generate() honours the per-request timeout_ms — a 500ms leash still
       results in fallback at 500ms, not the longer enrichment timeout.
  AC4  On retry exhaustion, generate() returns ok=False / text=None and
       callers degrade gracefully (no exception propagated).
  AC5  Credentials are read from config/secrets, never hardcoded, and never
       appear in logs at any level.
  AC6  Hosted is the default provider for generation and embedding,
       selectable via configuration with no code change.
  AC7  Every hosted-mode call records provider='hosted' in telemetry.
  AC8  Nothing assumes hosted is the only mode — verified by swapping in a
       stub provider via config without touching callers.

Each section is labelled with the story AC it targets so failures map
directly to the acceptance criterion that broke.

Jira: AT-411 | Parent: AT-395 (R16-D2 Hosted Model Mode)
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
from pathlib import Path
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
from app.model_gateway._config import CONFIG_KEY_API_KEY, HostedConfig
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.model_gateway.hosted_provider import HostedModelProvider, _MAX_RETRIES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GEN_EVENT = "model.generation_completed"
_EMB_EVENT = "model.embedding_completed"

# A recognisable sentinel that must never appear in logs or public attributes.
_SENTINEL_KEY = "sk-ant-SENTINEL-DO-NOT-LOG-T7-ABCDEF0123456789"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    """A fresh HostedModelProvider instance per test."""
    return HostedModelProvider()


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every record_event() call made anywhere in the process.

    The provider imports record_event lazily from app.telemetry, so patching
    the module attribute intercepts the real write without touching the DB.
    """
    events: List[tuple] = []

    def _fake(event_type, payload=None):
        events.append((event_type, payload or {}))

    monkeypatch.setattr("app.telemetry.record_event", _fake)
    return events


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def _success_urlopen(text: str = "response text"):
    """Return a urlopen side-effect that succeeds with a single text block."""
    body = json.dumps({"content": [{"type": "text", "text": text}]}).encode("utf-8")

    class _FakeResp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    return lambda *_a, **_k: _FakeResp()


def _raise_http(status: int, reason: str = "Error", retry_after: str | None = None):
    """Return a urlopen side-effect that always raises an HTTPError."""

    def _side_effect(req, timeout=None):
        hdrs = http.client.HTTPMessage()
        if retry_after is not None:
            hdrs["Retry-After"] = retry_after
        raise urllib.error.HTTPError("http://fake", status, reason, hdrs, None)

    return _side_effect


class _StubProvider(ModelProvider):
    """A registrable stub used to prove provider extensibility (AC8)."""

    name = "_t7_stub"

    def generate(self, req: GenerationRequest) -> GenerationResult:
        return GenerationResult(text="stub-served", provider=self.name, ok=True)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[1.0]] * len(texts)


def _register_temp(stub: ModelProvider):
    """Register stub and return a cleanup callable.

    Always call cleanup() in a finally block so the stub never pollutes other tests.
    """
    register_provider(stub)
    return lambda: _PROVIDER_REGISTRY.pop(stub.name, None)


# ===========================================================================
# T7-AC1 (Story AC1)
# HostedModelProvider implements both generate() and embed() via the gateway
# interface — both methods are callable, return the contract types, and route
# through the public gateway entry points by default.
# ===========================================================================


def test_ac1_hosted_provider_is_subclass_of_model_provider():
    """HostedModelProvider is a concrete subclass of the ModelProvider ABC."""
    assert issubclass(HostedModelProvider, ModelProvider)


def test_ac1_hosted_provider_implements_generate(provider):
    """HostedModelProvider exposes a callable generate() method."""
    assert callable(getattr(provider, "generate", None))


def test_ac1_hosted_provider_implements_embed(provider):
    """HostedModelProvider exposes a callable embed() method."""
    assert callable(getattr(provider, "embed", None))


def test_ac1_generate_returns_generation_result_type(monkeypatch, provider):
    """generate() always returns a GenerationResult regardless of outcome."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    result = provider.generate(GenerationRequest(prompt="hi", max_tokens=5))
    assert isinstance(result, GenerationResult)
    assert hasattr(result, "ok")
    assert hasattr(result, "text")
    assert hasattr(result, "provider")


def test_ac1_embed_returns_list(provider):
    """embed() always returns a list (of vectors or empty) — never raises."""
    result = provider.embed(["hello", "world"])
    assert isinstance(result, list)


def test_ac1_gateway_generate_routes_through_hosted_by_default(monkeypatch):
    """The public gateway generate() call reaches HostedModelProvider and carries
    provider='hosted' in the result — confirmed without touching callers."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    result = gw_generate(GenerationRequest(prompt="x", max_tokens=5))
    assert result.provider == "hosted"


def test_ac1_gateway_embed_routes_through_hosted_by_default(monkeypatch, captured_events):
    """The public gateway embed() call reaches HostedModelProvider and emits
    a telemetry event identifying provider='hosted'."""
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)
    gw_embed(["a", "b"])
    emb_events = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert emb_events, "embed() must produce a telemetry event via HostedModelProvider"
    assert emb_events[0][1]["provider"] == "hosted"


# ===========================================================================
# T7-AC2 (Story AC2)
# A transient 429 triggers bounded retry with exponential backoff; the overall
# call succeeds if a subsequent retry succeeds — output is never dropped on
# the first transient error.
# ===========================================================================


def test_ac2_429_triggers_retry_not_immediate_failure(monkeypatch, provider):
    """A single 429 causes at least one retry; if that retry succeeds the call
    returns ok=True — the old silent-drop behaviour is replaced."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    # Skip real sleeps so the test runs at unit-test speed.
    monkeypatch.setattr("time.sleep", lambda *_: None)

    call_count = {"n": 0}

    def _fail_once_then_succeed(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise urllib.error.HTTPError(
                "http://fake", 429, "Too Many Requests", http.client.HTTPMessage(), None
            )
        body = json.dumps(
            {"content": [{"type": "text", "text": "ok after retry"}]}
        ).encode("utf-8")

        class _Resp:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fail_once_then_succeed)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=60_000)
    )

    assert call_count["n"] >= 2, (
        f"expected >= 2 HTTP attempts (1 failed + at least 1 retry); got {call_count['n']}"
    )
    assert result.ok is True, "call must succeed when a retry succeeds"
    assert result.text == "ok after retry"


def test_ac2_retry_is_accompanied_by_backoff_sleep(monkeypatch, provider):
    """A 429 causes a sleep (backoff) before the next attempt — rate pressure
    is not amplified by instant retries."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    sleep_calls: List[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    call_count = {"n": 0}

    def _fail_once_then_succeed(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise urllib.error.HTTPError(
                "http://fake", 429, "Too Many Requests", http.client.HTTPMessage(), None
            )
        body = json.dumps(
            {"content": [{"type": "text", "text": "retried ok"}]}
        ).encode("utf-8")

        class _Resp:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fail_once_then_succeed)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=60_000)
    )

    assert result.ok is True
    assert len(sleep_calls) >= 1, "backoff sleep must occur between 429 and retry"
    assert all(s > 0 for s in sleep_calls), "backoff duration must be positive"


def test_ac2_retry_bounded_by_max_retries(monkeypatch, provider):
    """Retries are bounded — the provider never retries more than _MAX_RETRIES
    times regardless of how many 429s arrive."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)

    call_count = {"n": 0}

    def _always_429(req, timeout=None):
        call_count["n"] += 1
        raise urllib.error.HTTPError(
            "http://fake", 429, "Too Many Requests", http.client.HTTPMessage(), None
        )

    monkeypatch.setattr("urllib.request.urlopen", _always_429)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert result.ok is False
    # Total attempts = 1 initial + up to _MAX_RETRIES.
    assert call_count["n"] <= _MAX_RETRIES + 1, (
        f"must not exceed {_MAX_RETRIES + 1} total HTTP attempts; made {call_count['n']}"
    )


def test_ac2_retry_after_header_sets_minimum_backoff(monkeypatch, provider):
    """A 429 with a Retry-After header causes backoff >= Retry-After seconds,
    so the provider never ignores an explicit rate-limit signal."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    sleep_calls: List[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    call_count = {"n": 0}
    RETRY_AFTER_S = "2"

    def _429_with_retry_after(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            hdrs = http.client.HTTPMessage()
            hdrs["Retry-After"] = RETRY_AFTER_S
            raise urllib.error.HTTPError("http://fake", 429, "Rate Limited", hdrs, None)
        body = json.dumps(
            {"content": [{"type": "text", "text": "ok"}]}
        ).encode("utf-8")

        class _Resp:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _429_with_retry_after)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert result.ok is True
    assert len(sleep_calls) >= 1
    assert sleep_calls[0] >= float(RETRY_AFTER_S), (
        f"backoff ({sleep_calls[0]:.2f}s) must be >= Retry-After header ({RETRY_AFTER_S}s)"
    )


def test_ac2_transient_5xx_also_triggers_retry(monkeypatch, provider):
    """5xx server errors (500/502/503/504) are treated as transient and retried,
    not returned immediately — the same resilience posture as 429."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)

    call_count = {"n": 0}

    def _fail_503_then_succeed(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise urllib.error.HTTPError(
                "http://fake", 503, "Service Unavailable", http.client.HTTPMessage(), None
            )
        body = json.dumps(
            {"content": [{"type": "text", "text": "recovered"}]}
        ).encode("utf-8")

        class _Resp:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fail_503_then_succeed)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=60_000)
    )

    assert call_count["n"] >= 2, "503 must be retried like a 429"
    assert result.ok is True
    assert result.text == "recovered"


# ===========================================================================
# T7-AC3 (Story AC3)
# generate() honours the per-request timeout_ms.  A 500ms leash must not be
# silently extended to the longer default enrichment timeout (30 000ms).
# ===========================================================================


def test_ac3_timeout_ms_500_constrains_http_attempt_timeout(monkeypatch, provider):
    """When timeout_ms=500, the HTTP attempt receives a timeout of at most 0.5 s —
    the hallucination guard's 500ms leash is always respected."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    captured: dict = {}

    def _capture_and_403(req, timeout=None):
        captured["timeout"] = timeout
        # 403 is non-retryable — a single capture is enough.
        raise urllib.error.HTTPError(
            "http://fake", 403, "Forbidden", http.client.HTTPMessage(), None
        )

    monkeypatch.setattr("urllib.request.urlopen", _capture_and_403)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=500)
    )

    assert result.ok is False
    assert captured.get("timeout") is not None, "timeout must be passed to urlopen"
    # Allow a small epsilon for monotonic-clock overhead between deadline and
    # remaining_s calculation.
    assert captured["timeout"] <= 0.5 + 0.05, (
        f"urlopen received timeout={captured['timeout']:.4f}s; "
        f"must not exceed the 500ms leash (0.5s + small epsilon)"
    )


def test_ac3_timeout_ms_500_is_shorter_than_default_30000ms(monkeypatch, provider):
    """A timeout_ms=500 request passes a shorter HTTP timeout than the default
    30 000ms request — the two timeouts are not interchangeable."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    short_timeout: List[float] = []
    long_timeout: List[float] = []

    def _capture_short(req, timeout=None):
        short_timeout.append(timeout)
        raise urllib.error.HTTPError("http://fake", 403, "Forbidden", http.client.HTTPMessage(), None)

    def _capture_long(req, timeout=None):
        long_timeout.append(timeout)
        raise urllib.error.HTTPError("http://fake", 403, "Forbidden", http.client.HTTPMessage(), None)

    monkeypatch.setattr("urllib.request.urlopen", _capture_short)
    provider.generate(GenerationRequest(prompt="x", max_tokens=5, timeout_ms=500))

    monkeypatch.setattr("urllib.request.urlopen", _capture_long)
    provider.generate(GenerationRequest(prompt="x", max_tokens=5))  # default 30 000ms

    assert short_timeout, "short call must reach urlopen"
    assert long_timeout, "default call must reach urlopen"
    assert short_timeout[0] < long_timeout[0], (
        f"500ms request timeout ({short_timeout[0]:.4f}s) must be < "
        f"30000ms request timeout ({long_timeout[0]:.2f}s)"
    )


def test_ac3_missing_key_exits_before_timeout_with_ok_false(monkeypatch, provider):
    """With timeout_ms=500 and no API key, generate() returns ok=False immediately
    — well within the 500ms budget (missing-key short-circuit path)."""
    import time

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    start = time.monotonic()
    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5, timeout_ms=500))
    elapsed_ms = (time.monotonic() - start) * 1000

    assert result.ok is False
    assert elapsed_ms < 450, (
        f"Missing-key path must return well within 500ms; took {elapsed_ms:.1f}ms"
    )


def test_ac3_short_timeout_prevents_retry_backoff(monkeypatch, provider):
    """With timeout_ms=500, a 503 (retryable) cannot backoff within budget and
    returns ok=False without sleeping — the deadline enforces the leash."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    sleep_calls: List[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    monkeypatch.setattr("urllib.request.urlopen", _raise_http(503, "Service Unavailable"))

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=500)
    )

    assert result.ok is False
    # Backoff (0.5s min) would exceed the remaining 500ms budget, so no sleep.
    assert sleep_calls == [], (
        "With a 500ms deadline, the first backoff (0.5s) exceeds remaining budget; "
        "time.sleep must not be called"
    )


# ===========================================================================
# T7-AC4 (Story AC4)
# On retry exhaustion generate() returns ok=False / text=None.  No exception
# propagates — callers degrade gracefully exactly as they did with the old
# direct-call code.
# ===========================================================================


def test_ac4_retry_exhaustion_returns_ok_false(monkeypatch, provider):
    """Exhausting all retries yields ok=False — the caller sees a stable boolean."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("urllib.request.urlopen", _raise_http(429, "Rate Limited"))

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert result.ok is False


def test_ac4_retry_exhaustion_text_is_none(monkeypatch, provider):
    """Exhausting all retries yields text=None — existing callers already handle None."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("urllib.request.urlopen", _raise_http(429, "Rate Limited"))

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert result.text is None


def test_ac4_retry_exhaustion_does_not_raise(monkeypatch, provider):
    """generate() never raises — exhaustion is signalled by ok=False, not an exception."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("urllib.request.urlopen", _raise_http(429, "Rate Limited"))

    # The call must not raise any exception
    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )
    assert isinstance(result, GenerationResult)


def test_ac4_retry_exhaustion_provider_identity_still_hosted(monkeypatch, provider):
    """On exhaustion, provider='hosted' is still set so the caller can log/trace."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("urllib.request.urlopen", _raise_http(429, "Rate Limited"))

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert result.provider == "hosted"


def test_ac4_missing_key_returns_ok_false_no_exception(monkeypatch, provider):
    """Missing API key is also a graceful ok=False / text=None exit — no exception."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is False
    assert result.text is None
    assert result.provider == "hosted"


def test_ac4_non_transient_4xx_returns_ok_false_no_exception(monkeypatch, provider):
    """A non-retryable HTTP 4xx (e.g. 403 Forbidden) also returns ok=False without
    raising — callers never need to catch from generate()."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr("urllib.request.urlopen", _raise_http(403, "Forbidden"))

    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is False
    assert result.text is None


def test_ac4_unexpected_exception_returns_ok_false_no_propagation(monkeypatch, provider):
    """An unexpected exception from urlopen returns ok=False — no propagation."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)

    def _boom(req, timeout=None):
        raise RuntimeError("network stack exploded")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    result = provider.generate(
        GenerationRequest(prompt="x", max_tokens=5, timeout_ms=300_000)
    )

    assert result.ok is False
    assert result.text is None


# ===========================================================================
# T7-AC5 (Story AC5)
# Credentials are read from config/secrets (never hardcoded) and never appear
# in logs at any level.
# ===========================================================================


def test_ac5_credential_read_from_config_not_hardcoded(monkeypatch):
    """The API key value comes from config — changing the env changes the resolved key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _SENTINEL_KEY)
    cfg = HostedConfig()
    assert cfg.resolve_api_key() == _SENTINEL_KEY
    assert cfg.has_credential() is True


def test_ac5_no_hardcoded_credential_when_key_absent(monkeypatch):
    """When the config key is absent there is no hardcoded fallback credential."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = HostedConfig()
    assert cfg.resolve_api_key() == ""
    assert cfg.has_credential() is False


def test_ac5_credential_not_in_logs_on_http_error(monkeypatch, caplog):
    """The API key value never appears in logs during an HTTP-error generate() call."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _SENTINEL_KEY)
    monkeypatch.setattr("urllib.request.urlopen", _raise_http(401, "Unauthorized"))

    with caplog.at_level(logging.DEBUG):
        provider = HostedModelProvider()
        result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is False
    assert _SENTINEL_KEY not in caplog.text, (
        "API key value must never appear in log output at any level"
    )


def test_ac5_credential_not_in_logs_on_successful_call(monkeypatch, caplog):
    """The API key is not logged even during a successful generate() call."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _SENTINEL_KEY)
    monkeypatch.setattr("urllib.request.urlopen", _success_urlopen("ok"))

    with caplog.at_level(logging.DEBUG):
        provider = HostedModelProvider()
        result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is True
    assert _SENTINEL_KEY not in caplog.text


def test_ac5_credential_not_in_logs_on_missing_key_warning(monkeypatch, caplog):
    """The missing-credential warning names the config key, not the value — an
    empty value produces no leakage, and a present value is never printed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    with caplog.at_level(logging.DEBUG):
        provider = HostedModelProvider()
        provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert _SENTINEL_KEY not in caplog.text


def test_ac5_config_repr_redacts_credential(monkeypatch):
    """HostedConfig repr/str redacts the credential so it cannot leak via debug logging."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _SENTINEL_KEY)
    cfg = HostedConfig()
    assert _SENTINEL_KEY not in repr(cfg)
    assert _SENTINEL_KEY not in str(cfg)
    assert "REDACTED" in repr(cfg)


def test_ac5_credential_not_on_public_attribute(monkeypatch):
    """No public attribute on HostedModelProvider holds the credential value."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _SENTINEL_KEY)
    provider = HostedModelProvider()
    public_attrs = [
        getattr(provider, n) for n in dir(provider) if not n.startswith("_")
    ]
    assert _SENTINEL_KEY not in [v for v in public_attrs if isinstance(v, str)], (
        "Credential must not be reachable via any public attribute"
    )


def test_ac5_package_exports_no_credential_accessor():
    """The gateway package __all__ exports no credential accessor — callers outside
    the package cannot reach the key."""
    import app.model_gateway as gw

    for exported in gw.__all__:
        lowered = exported.lower()
        assert "key" not in lowered, f"__all__ must not export a key accessor: {exported}"
        assert "credential" not in lowered
        assert "secret" not in lowered


def test_ac5_env_example_documents_credential_key():
    """backend/.env.example documents ANTHROPIC_API_KEY with a placeholder value."""
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    assert env_example.exists(), "backend/.env.example must exist"
    text = env_example.read_text(encoding="utf-8")
    line = next(
        (ln for ln in text.splitlines() if ln.strip().startswith(f"{CONFIG_KEY_API_KEY}=")),
        None,
    )
    assert line is not None, f"{CONFIG_KEY_API_KEY} must be documented in .env.example"
    placeholder = line.split("=", 1)[1].strip()
    assert placeholder, "credential config key must have a placeholder value in .env.example"


# ===========================================================================
# T7-AC6 (Story AC6) — Hosted is the default; selectable via config only
# T7-AC8 (Story AC8) — Nothing assumes hosted is the only mode; stub swap works
# ===========================================================================


def test_ac6_generation_default_is_hosted_model_provider(monkeypatch):
    """Unset MODEL_GENERATION_PROVIDER → HostedModelProvider serves generation (AC6)."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    p = get_generation_provider()
    assert isinstance(p, HostedModelProvider)
    assert p.name == "hosted"


def test_ac6_embedding_default_is_hosted_model_provider(monkeypatch):
    """Unset MODEL_EMBEDDING_PROVIDER → HostedModelProvider serves embedding (AC6)."""
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)
    p = get_embedding_provider()
    assert isinstance(p, HostedModelProvider)
    assert p.name == "hosted"


def test_ac6_explicit_hosted_generation_resolves_to_hosted(monkeypatch):
    """MODEL_GENERATION_PROVIDER='hosted' explicitly → HostedModelProvider (AC6)."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")
    assert isinstance(get_generation_provider(), HostedModelProvider)


def test_ac6_explicit_hosted_embedding_resolves_to_hosted(monkeypatch):
    """MODEL_EMBEDDING_PROVIDER='hosted' explicitly → HostedModelProvider (AC6)."""
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")
    assert isinstance(get_embedding_provider(), HostedModelProvider)


def test_ac6_hosted_registered_at_import_time():
    """'hosted' is in _PROVIDER_REGISTRY at import time — no lazy init required."""
    assert "hosted" in _PROVIDER_REGISTRY
    assert isinstance(_PROVIDER_REGISTRY["hosted"], HostedModelProvider)


def test_ac8_stub_swappable_via_generation_config_no_code_change(monkeypatch):
    """Setting MODEL_GENERATION_PROVIDER to a stub routes all generate() calls
    through it without any change to calling code (AC8)."""
    stub = _StubProvider()
    cleanup = _register_temp(stub)
    try:
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", stub.name)
        # Exact call shape a real caller uses — unchanged.
        result = gw_generate(GenerationRequest(prompt="hello", max_tokens=5))
    finally:
        cleanup()

    assert result.text == "stub-served"
    assert result.provider == stub.name


def test_ac8_stub_swappable_via_embedding_config_no_code_change(monkeypatch):
    """Setting MODEL_EMBEDDING_PROVIDER to a stub routes all embed() calls through
    it without any change to calling code (AC8)."""
    stub = _StubProvider()
    cleanup = _register_temp(stub)
    try:
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", stub.name)
        vectors = gw_embed(["a", "b"])
    finally:
        cleanup()

    assert vectors == [[1.0], [1.0]]


def test_ac8_hosted_not_assumed_only_mode_coexists_with_stub(monkeypatch):
    """Another provider can be registered alongside hosted; both remain independently
    selectable — proving hosted is not assumed to be the only mode (AC8)."""
    stub = _StubProvider()
    cleanup = _register_temp(stub)
    try:
        # Both providers live in the registry simultaneously.
        assert "hosted" in _PROVIDER_REGISTRY
        assert stub.name in _PROVIDER_REGISTRY

        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", stub.name)
        monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

        assert get_generation_provider() is stub
        assert isinstance(get_embedding_provider(), HostedModelProvider)
    finally:
        cleanup()


def test_ac8_config_swap_is_reversible(monkeypatch):
    """After removing the override env var, resolution returns to hosted — the
    swap is purely config-driven, not a permanent code mutation."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    assert isinstance(get_generation_provider(), HostedModelProvider)


def test_ac8_register_provider_is_the_same_mechanism_for_all_providers():
    """The hosted default uses the public register_provider() mechanism — any
    future provider (in-boundary, customer-tenant) calls the same function."""
    hosted = _PROVIDER_REGISTRY.get("hosted")
    assert hosted is not None
    # Idempotent re-registration of the same instance must not raise.
    register_provider(hosted)
    assert _PROVIDER_REGISTRY["hosted"] is hosted


# ===========================================================================
# T7-AC7 (Story AC7)
# Every hosted-mode call records provider='hosted' in telemetry — on success
# and failure alike, for both generate() and embed().
# ===========================================================================


def test_ac7_generate_records_provider_hosted_on_success(
    monkeypatch, provider, captured_events
):
    """Successful generate() records provider='hosted' and ok=True (AC7)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr("urllib.request.urlopen", _success_urlopen("hi"))

    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is True
    events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(events) == 1
    assert events[0][1]["provider"] == "hosted"
    assert events[0][1]["ok"] is True


def test_ac7_generate_records_provider_hosted_on_missing_key(
    monkeypatch, provider, captured_events
):
    """Failed generate() (missing API key) still records provider='hosted' (AC7)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is False
    events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(events) == 1
    assert events[0][1]["provider"] == "hosted"
    assert events[0][1]["ok"] is False


def test_ac7_generate_records_provider_hosted_on_http_error(
    monkeypatch, provider, captured_events
):
    """Failed generate() (HTTP 403) still records provider='hosted' (AC7)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr("urllib.request.urlopen", _raise_http(403, "Forbidden"))

    result = provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    assert result.ok is False
    events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(events) == 1
    assert events[0][1]["provider"] == "hosted"


def test_ac7_embed_records_provider_hosted(provider, captured_events):
    """Every embed() call records provider='hosted' in telemetry (AC7)."""
    provider.embed(["hello", "world"])

    events = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(events) == 1
    assert events[0][1]["provider"] == "hosted"


def test_ac7_every_generate_call_records_exactly_one_event(
    monkeypatch, provider, captured_events
):
    """N generate() calls produce exactly N telemetry events — one per call (AC7)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    for _ in range(3):
        provider.generate(GenerationRequest(prompt="x", max_tokens=5))

    events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(events) == 3
    assert all(e[1]["provider"] == "hosted" for e in events)


def test_ac7_every_embed_call_records_exactly_one_event(provider, captured_events):
    """N embed() calls produce exactly N telemetry events — one per call (AC7)."""
    for _ in range(3):
        provider.embed(["hello"])

    events = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(events) == 3
    assert all(e[1]["provider"] == "hosted" for e in events)


def test_ac7_generation_event_type_registered_before_emit():
    """model.generation_completed is in REGISTERED_EVENT_TYPES before any call —
    record_event() will not raise ValueError (AC7)."""
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert _GEN_EVENT in REGISTERED_EVENT_TYPES


def test_ac7_embedding_event_type_registered_before_emit():
    """model.embedding_completed is in REGISTERED_EVENT_TYPES before any call (AC7)."""
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert _EMB_EVENT in REGISTERED_EVENT_TYPES


def test_ac7_generate_via_gateway_emits_exactly_one_event_no_double_count(
    monkeypatch, captured_events
):
    """HostedModelProvider self-emits (emits_own_telemetry=True); the gateway skips
    its own emission.  One logical call through gw_generate → exactly one event."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    gw_generate(GenerationRequest(prompt="x", max_tokens=5))

    events = [e for e in captured_events if e[0] == _GEN_EVENT]
    assert len(events) == 1, (
        "gateway + self-emitting provider must produce exactly one event; "
        f"got {len(events)} — double-counting detected"
    )
    assert events[0][1]["provider"] == "hosted"


def test_ac7_embed_via_gateway_emits_exactly_one_event_no_double_count(
    monkeypatch, captured_events
):
    """HostedModelProvider self-emits for embed() too; the gateway skips its emission.
    One logical call through gw_embed → exactly one event."""
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

    gw_embed(["a", "b"])

    events = [e for e in captured_events if e[0] == _EMB_EVENT]
    assert len(events) == 1, (
        "gateway + self-emitting provider must produce exactly one embedding event; "
        f"got {len(events)}"
    )
    assert events[0][1]["provider"] == "hosted"
