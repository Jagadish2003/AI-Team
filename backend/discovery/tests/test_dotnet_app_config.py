"""
R17-A4 / T3 + T7 — Per-deployment .NET app configuration + vault credentials (AC4).

Target applications are configured per deployment (no network scanning);
credentials are resolved from the vault and NEVER stored in config or written to
logs. These tests exercise the .NET config loader and credential resolver in
isolation (no DB, no live HTTP). The credential-handling primitives are shared with
Java, so this also proves the shared secret rules hold for the .NET target shape.
"""
from __future__ import annotations

import json
import logging

import pytest

from discovery.ingest.dotnet_app_config import (
    DEFAULT_CREDENTIAL_REF,
    DotNetAppTarget,
    load_targets,
    resolve_secret,
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
    assert oa.service == "orders"             # from metadata, used for linkage
    assert oa.credential_ref == DEFAULT_CREDENTIAL_REF


def test_live_reads_targets_from_env_json(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv(
        "DOTNET_APP_TARGETS",
        json.dumps([
            {"app_id": "billing", "name": "Billing", "diagnostics_url": "https://b/diagnostics",
             "log_source": "https://b/logs", "metadata": {"service": "billing"}}
        ]),
    )
    targets = load_targets("org-9")
    assert [t.app_id for t in targets] == ["billing"]


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
@pytest.mark.parametrize("secret_field", ["token", "password", "api_key", "basic_auth", "secret"])
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


def test_target_has_no_field_that_could_hold_a_plaintext_secret():
    t = DotNetAppTarget(app_id="a", name="A", diagnostics_url="u", log_source="l")
    assert t.credential_ref == DEFAULT_CREDENTIAL_REF
    for attr in ("token", "password", "secret", "api_key", "basic_auth", "authorization"):
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
