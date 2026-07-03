"""
R17-A4 / T3 + T7 — Per-deployment .NET app configuration + vault credentials (AC4).

Target applications are configured per deployment (no network scanning);
credentials are resolved from the vault and NEVER stored in config or written to
logs; and endpoint failures are reported with safe information only (application
id, endpoint type, error category) — never credentials or connection strings.

These tests exercise the config loader, credential resolver, and safe error
reporter in isolation (no DB, no live HTTP). The credential-handling primitives are
shared with Java, so this also proves the shared secret rules hold for the .NET
target shape.
"""
from __future__ import annotations

import json
import logging

import pytest

from discovery.ingest.dotnet_app_config import (
    DEFAULT_CREDENTIAL_REF,
    DotNetAppTarget,
    classify_endpoint_error,
    load_targets,
    log_endpoint_failure,
    resolve_secret,
    safe_endpoint_error,
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


# ─────────────────────────────────────────────────────────────────────────────
# Configured, not auto-discovered (R17-A4 §3)
# ─────────────────────────────────────────────────────────────────────────────
def test_offline_loads_targets_from_fixture():
    targets = load_targets("org-1")
    ids = {t.app_id for t in targets}
    assert {"orders-api", "inventory-svc"} <= ids
    oa = next(t for t in targets if t.app_id == "orders-api")
    assert oa.diagnostics_url and oa.log_source
    assert oa.environment == "production"     # environment info captured
    assert oa.service == "orders"             # from metadata, used for linkage
    assert oa.credential_ref == DEFAULT_CREDENTIAL_REF


def test_live_reads_targets_from_env_json(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv(
        "DOTNET_APP_TARGETS",
        json.dumps([
            {"app_id": "billing", "name": "Billing", "diagnostics_url": "https://b/diagnostics",
             "log_source": "https://b/logs", "environment": "staging",
             "metadata": {"service": "billing"}}
        ]),
    )
    targets = load_targets("org-9")
    assert [t.app_id for t in targets] == ["billing"]
    assert targets[0].environment == "staging"


def test_live_with_no_targets_env_yields_nothing(monkeypatch):
    # No network scanning: an unconfigured deployment simply reads nothing.
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.delenv("DOTNET_APP_TARGETS", raising=False)
    assert load_targets("org-9") == []


def test_target_requires_a_surface_to_read():
    from discovery.ingest.dotnet_app_config import DotNetAppConfigError
    with pytest.raises(DotNetAppConfigError):
        DotNetAppTarget(app_id="empty", name="e", diagnostics_url="", log_source="")


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — credentials never live in config (inline secrets are rejected)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "secret_field",
    ["token", "password", "username", "api_key", "basic_auth", "secret",
     "connection_string", "certificate", "pfx"],
)
def test_inline_secret_in_target_is_rejected(monkeypatch, caplog, secret_field):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv(
        "DOTNET_APP_TARGETS",
        json.dumps([
            {"app_id": "leaky", "diagnostics_url": "https://x/diagnostics",
             secret_field: "SUPERSECRETVALUE"}
        ]),
    )
    with caplog.at_level(logging.WARNING):
        targets = load_targets("org-9")
    # The insecure target is dropped...
    assert targets == []
    # ...and the secret value never reaches the log (only the offending key name).
    assert "SUPERSECRETVALUE" not in caplog.text
    assert secret_field in caplog.text


def test_certificate_reference_is_allowed_not_rejected(monkeypatch):
    # A *reference* (certificate_ref / credential_ref) is a vault key, not a secret.
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv(
        "DOTNET_APP_TARGETS",
        json.dumps([
            {"app_id": "svc", "diagnostics_url": "https://s/healthz",
             "credential_ref": "dotnet_app", "certificate_ref": "vault://certs/svc"}
        ]),
    )
    targets = load_targets("org-9")
    assert [t.app_id for t in targets] == ["svc"]


def test_target_has_no_field_that_could_hold_a_plaintext_secret():
    t = DotNetAppTarget(app_id="a", name="A", diagnostics_url="u", log_source="l")
    assert t.credential_ref == DEFAULT_CREDENTIAL_REF
    for attr in ("token", "password", "username", "secret", "api_key", "basic_auth",
                 "authorization", "connection_string", "certificate"):
        assert not hasattr(t, attr)


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — credentials resolved from the vault (per-run context), env as CLI fallback
# ─────────────────────────────────────────────────────────────────────────────
def _target(ref=DEFAULT_CREDENTIAL_REF):
    return DotNetAppTarget(app_id="orders-api", name="O", diagnostics_url="u",
                           log_source="l", credential_ref=ref)


def test_secret_resolved_from_per_run_vault_context():
    captured = {"token": "VAULT-TOKEN-123"}
    secret = resolve_secret(
        "org-1", _target(),
        connector_lookup=lambda ref: captured if ref == DEFAULT_CREDENTIAL_REF else None,
        env={},
    )
    assert secret == "VAULT-TOKEN-123"


def test_secret_env_fallback_for_cli():
    secret = resolve_secret(
        "org-1", _target(),
        connector_lookup=lambda ref: None,        # nothing in the per-run context
        env={"DOTNET_APP_TOKEN": "ENV-TOKEN-456"},
    )
    assert secret == "ENV-TOKEN-456"


def test_secret_custom_credential_ref_uses_namespaced_env():
    secret = resolve_secret(
        "org-1", _target(ref="orders_diagnostics"),
        connector_lookup=lambda ref: None,
        env={"ORDERS_DIAGNOSTICS_TOKEN": "NS-TOKEN"},
    )
    assert secret == "NS-TOKEN"


def test_no_credential_ref_means_no_secret():
    t = DotNetAppTarget(app_id="a", name="A", diagnostics_url="u", log_source="l", credential_ref=None)
    assert resolve_secret("org-1", t, connector_lookup=lambda ref: {"token": "x"}, env={}) is None


def test_secret_lookup_failure_degrades_to_env(caplog):
    def _boom(ref):
        raise RuntimeError("vault down")

    with caplog.at_level(logging.WARNING):
        secret = resolve_secret(
            "org-1", _target(), connector_lookup=_boom, env={"DOTNET_APP_TOKEN": "FALLBACK"}
        )
    assert secret == "FALLBACK"          # degrade, don't crash


def test_resolved_secret_is_never_logged(caplog):
    with caplog.at_level(logging.DEBUG):
        secret = resolve_secret(
            "org-1", _target(),
            connector_lookup=lambda ref: {"token": "VAULT_SECRET_XYZ"},
            env={},
        )
    assert secret == "VAULT_SECRET_XYZ"
    assert "VAULT_SECRET_XYZ" not in caplog.text
    assert "VAULT_SECRET_XYZ" not in repr(_target())


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — safe failure reporting: never leak credentials / connection strings
# ─────────────────────────────────────────────────────────────────────────────
def test_safe_endpoint_error_reports_only_safe_fields():
    t = _target()
    # An exception whose message embeds a connection string + password.
    exc = ConnectionError(
        "Failed: Server=db;Database=orders;User Id=sa;Password=P@ssw0rd!;Encrypt=true"
    )
    safe = safe_endpoint_error(t, "diagnostics", exc)
    assert safe["app_id"] == "orders-api"
    assert safe["endpoint_type"] == "diagnostics"
    assert safe["error_category"] == "connection_error"
    assert safe["exception_type"] == "ConnectionError"
    # The credential / connection string must NEVER appear in the safe record.
    blob = json.dumps(safe)
    assert "P@ssw0rd!" not in blob
    assert "Password=" not in blob
    assert "Server=db" not in blob


@pytest.mark.parametrize(
    "exc,expected",
    [
        (TimeoutError("operation timed out"), "timeout"),
        (ConnectionRefusedError("connection refused"), "connection_error"),
        (PermissionError("401 Unauthorized"), "auth_error"),
        (ValueError("Expecting value: line 1 column 1"), "parse_error"),
        (RuntimeError("something odd"), "unknown_error"),
    ],
)
def test_error_categories_are_classified(exc, expected):
    assert classify_endpoint_error(exc) == expected


def test_log_endpoint_failure_never_writes_the_secret(caplog):
    t = _target()
    exc = RuntimeError("auth failed for token=SECRET-BEARER-XYZ 403 Forbidden")
    with caplog.at_level(logging.WARNING):
        safe = log_endpoint_failure("org-1", t, "logs", exc)
    # Category is captured; the secret in the message is not logged.
    assert safe["error_category"] == "auth_error"
    assert safe["endpoint_type"] == "logs"
    assert "SECRET-BEARER-XYZ" not in caplog.text
    assert "orders-api" in caplog.text            # safe id IS logged
