"""
R17-A4 / T3 — Per-deployment .NET app configuration + vault credentials.

Supporting coverage for the signal producer: targets are configured per deployment
(no network scanning), inline secrets are rejected, and credentials resolve from
the vault (never config/logs). No DB, no live HTTP.
"""
from __future__ import annotations

import json
import logging

import pytest

from discovery.ingest.dotnet_app_config import (
    DEFAULT_CREDENTIAL_REF,
    DotNetAppConfigError,
    DotNetAppTarget,
    load_targets,
    resolve_secret,
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


def test_offline_loads_targets_from_fixture():
    targets = load_targets("org-1")
    assert {"orders-api", "inventory-svc"} <= {t.app_id for t in targets}
    oa = next(t for t in targets if t.app_id == "orders-api")
    assert oa.diagnostics_url and oa.log_source
    assert oa.service == "orders"
    assert oa.credential_ref == DEFAULT_CREDENTIAL_REF


def test_live_reads_targets_from_env_json(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv("DOTNET_APP_TARGETS", json.dumps([
        {"app_id": "billing", "diagnostics_url": "https://b/diagnostics",
         "log_source": "https://b/logs", "metadata": {"service": "billing"}}
    ]))
    assert [t.app_id for t in load_targets("org-9")] == ["billing"]


def test_live_with_no_targets_env_yields_nothing(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.delenv("DOTNET_APP_TARGETS", raising=False)
    assert load_targets("org-9") == []


def test_target_requires_a_surface():
    with pytest.raises(DotNetAppConfigError):
        DotNetAppTarget(app_id="x", name="x", diagnostics_url="", log_source="")


@pytest.mark.parametrize("secret_field", ["token", "password", "api_key", "basic_auth", "secret"])
def test_inline_secret_in_target_is_rejected(monkeypatch, caplog, secret_field):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv("DOTNET_APP_TARGETS", json.dumps([
        {"app_id": "leaky", "diagnostics_url": "https://x/diagnostics", secret_field: "SUPERSECRET"}
    ]))
    with caplog.at_level(logging.WARNING):
        targets = load_targets("org-9")
    assert targets == []
    assert "SUPERSECRET" not in caplog.text
    assert secret_field in caplog.text


def _target(ref=DEFAULT_CREDENTIAL_REF):
    return DotNetAppTarget(app_id="orders-api", name="O", diagnostics_url="u",
                           log_source="l", credential_ref=ref)


def test_secret_resolved_from_vault_context():
    secret = resolve_secret("org-1", _target(),
                            connector_lookup=lambda ref: {"token": "VAULT-123"} if ref == "dotnet_app" else None,
                            env={})
    assert secret == "VAULT-123"


def test_secret_env_fallback_for_cli():
    secret = resolve_secret("org-1", _target(),
                            connector_lookup=lambda ref: None,
                            env={"DOTNET_APP_TOKEN": "ENV-456"})
    assert secret == "ENV-456"


def test_secret_custom_ref_uses_namespaced_env():
    secret = resolve_secret("org-1", _target(ref="orders_diag"),
                            connector_lookup=lambda ref: None,
                            env={"ORDERS_DIAG_TOKEN": "NS"})
    assert secret == "NS"


def test_no_credential_ref_means_no_secret():
    t = DotNetAppTarget(app_id="a", name="A", diagnostics_url="u", log_source="l", credential_ref=None)
    assert resolve_secret("org-1", t, connector_lookup=lambda ref: {"token": "x"}, env={}) is None


def test_resolved_secret_never_logged(caplog):
    with caplog.at_level(logging.DEBUG):
        secret = resolve_secret("org-1", _target(),
                                connector_lookup=lambda ref: {"token": "VAULT_XYZ"}, env={})
    assert secret == "VAULT_XYZ"
    assert "VAULT_XYZ" not in caplog.text
    assert "VAULT_XYZ" not in repr(_target())
