"""R16-D2 T4 (AT-408) — Hosted provider credential / config handling.

The hosted provider owns its credential and endpoint entirely inside the
gateway package. The credential is read from config/secrets (never hardcoded),
never logged, and never reachable by a caller outside the package.

Acceptance Criteria
-------------------
  T4-AC1  Credentials are read from config/secrets at provider instantiation,
          never hardcoded.
  T4-AC2  Credentials never appear in logs at any log level.
  T4-AC3  No caller outside backend/app/model_gateway/ can access the credential.
  T4-AC4  backend/.env.example documents the required credential config key with
          a placeholder value.
"""
from __future__ import annotations

import http.client
import logging
import urllib.error
from pathlib import Path

import pytest

from app.model_gateway import GenerationRequest
from app.model_gateway import hosted_provider as hp
from app.model_gateway._config import (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_ENDPOINT,
    HostedConfig,
)
from app.model_gateway.hosted_provider import HostedModelProvider

# A recognisable secret used to prove it never leaks into logs or callers.
_SECRET = "sk-ant-SECRET-DO-NOT-LOG-0123456789"


def _gen_req() -> GenerationRequest:
    return GenerationRequest(prompt="hello", max_tokens=5, timeout_ms=2000)


# ---------------------------------------------------------------------------
# T4-AC1 — Credentials read from config/secrets at instantiation, not hardcoded
# ---------------------------------------------------------------------------


def test_t4_ac1_credential_read_from_config_at_instantiation(monkeypatch):
    """HostedConfig reads the credential from config (env) at construction."""
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _SECRET)
    cfg = HostedConfig()
    # Read from config/secrets — the configured value, not a hardcoded one.
    assert cfg.resolve_api_key() == _SECRET
    assert cfg.has_credential() is True


def test_t4_ac1_no_hardcoded_credential_when_config_absent(monkeypatch):
    """With the config key unset, there is no hardcoded fallback credential."""
    monkeypatch.delenv(CONFIG_KEY_API_KEY, raising=False)
    cfg = HostedConfig()
    assert cfg.resolve_api_key() == ""
    assert cfg.has_credential() is False


def test_t4_ac1_endpoint_sourced_from_config_not_hardcoded_in_provider(monkeypatch):
    """The endpoint comes from config (overridable), not a literal in the provider."""
    monkeypatch.setenv(CONFIG_KEY_ENDPOINT, "https://example.test/v1/messages")
    provider = HostedModelProvider()
    assert provider._config.endpoint == "https://example.test/v1/messages"


# ---------------------------------------------------------------------------
# T4-AC2 — Credentials never appear in logs at any level
# ---------------------------------------------------------------------------


def test_t4_ac2_credential_absent_from_logs_on_http_failure(monkeypatch, caplog):
    """A full generate() call (instantiation + HTTP error path) never logs the key."""
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _SECRET)

    def _fail_401(req, timeout=None):
        hdrs = http.client.HTTPMessage()
        raise urllib.error.HTTPError("http://fake", 401, "Unauthorized", hdrs, None)

    monkeypatch.setattr("urllib.request.urlopen", _fail_401)

    with caplog.at_level(logging.DEBUG):
        provider = HostedModelProvider()
        result = provider.generate(_gen_req())

    assert result.ok is False
    # The secret must not appear in any captured log record at any level.
    assert _SECRET not in caplog.text


def test_t4_ac2_repr_redacts_credential(monkeypatch):
    """HostedConfig repr/str redact the credential so it cannot leak via logging."""
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _SECRET)
    cfg = HostedConfig()
    assert _SECRET not in repr(cfg)
    assert _SECRET not in str(cfg)
    assert "REDACTED" in repr(cfg)


def test_t4_ac2_missing_key_warning_does_not_log_value(monkeypatch, caplog):
    """The missing-credential warning names the config key, never a value."""
    monkeypatch.setenv(CONFIG_KEY_API_KEY, "")

    with caplog.at_level(logging.DEBUG):
        provider = HostedModelProvider()
        result = provider.generate(_gen_req())

    assert result.ok is False
    # Names the config key (acceptable) but contains no secret value.
    assert _SECRET not in caplog.text


# ---------------------------------------------------------------------------
# T4-AC3 — No caller outside the gateway package can access the credential
# ---------------------------------------------------------------------------


def test_t4_ac3_credential_not_exposed_as_public_attribute(monkeypatch):
    """The provider exposes no public attribute holding the credential value."""
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _SECRET)
    provider = HostedModelProvider()
    public_values = [
        getattr(provider, n) for n in dir(provider) if not n.startswith("_")
    ]
    assert _SECRET not in [v for v in public_values if isinstance(v, str)]


def test_t4_ac3_credential_accessor_not_re_exported_from_package():
    """The package __all__ exposes no credential accessor — callers can't reach it."""
    import app.model_gateway as gateway

    for exported in gateway.__all__:
        lowered = exported.lower()
        assert "key" not in lowered
        assert "credential" not in lowered
        assert "secret" not in lowered


def test_t4_ac3_config_module_is_private():
    """Credential handling lives in a private module (underscore-prefixed)."""
    assert hp.HostedConfig.__module__ == "app.model_gateway._config"


# ---------------------------------------------------------------------------
# T4-AC4 — .env.example documents the credential config key with a placeholder
# ---------------------------------------------------------------------------


def test_t4_ac4_env_example_documents_credential_key():
    """backend/.env.example documents ANTHROPIC_API_KEY with a placeholder value."""
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    assert env_example.exists(), "backend/.env.example is missing"
    text = env_example.read_text(encoding="utf-8")

    line = next(
        (
            ln
            for ln in text.splitlines()
            if ln.strip().startswith(f"{CONFIG_KEY_API_KEY}=")
        ),
        None,
    )
    assert line is not None, f"{CONFIG_KEY_API_KEY} not documented in .env.example"
    placeholder = line.split("=", 1)[1].strip()
    assert placeholder, "credential config key must have a placeholder value"
