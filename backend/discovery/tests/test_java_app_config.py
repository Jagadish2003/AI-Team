"""
R17-A3 / T3 + T7 — Per-deployment Java app configuration + vault credentials (AC3).

Target applications are configured per deployment (no network scanning);
credentials are resolved from the vault and NEVER stored in config or written to
logs. These tests exercise the config loader and credential resolver in
isolation (no DB, no live HTTP).
"""
from __future__ import annotations

import json
import logging

import pytest

from discovery.ingest.java_app_config import (
    DEFAULT_CREDENTIAL_REF,
    JavaAppTarget,
    load_targets,
    resolve_secret,
)
from discovery.ingest.operational_config import OperationalCredentialMissing


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


# ─────────────────────────────────────────────────────────────────────────────
# Configured, not auto-discovered (R17-A3 §2)
# ─────────────────────────────────────────────────────────────────────────────
def test_offline_loads_targets_from_fixture():
    targets = load_targets("org-1")
    ids = {t.app_id for t in targets}
    assert {"payments-api", "ledger-svc"} <= ids
    pa = next(t for t in targets if t.app_id == "payments-api")
    assert pa.actuator_url and pa.log_source
    assert pa.service == "payments"           # from metadata, used for linkage
    assert pa.credential_ref == DEFAULT_CREDENTIAL_REF


def test_live_reads_targets_from_env_json(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv(
        "JAVA_APP_TARGETS",
        json.dumps([
            {"app_id": "orders", "name": "Orders", "actuator_url": "https://o/actuator",
             "log_source": "https://o/logs", "metadata": {"service": "orders"}}
        ]),
    )
    targets = load_targets("org-9")
    assert [t.app_id for t in targets] == ["orders"]


def test_live_with_no_targets_env_yields_nothing(monkeypatch):
    # No network scanning: an unconfigured deployment simply reads nothing.
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.delenv("JAVA_APP_TARGETS", raising=False)
    assert load_targets("org-9") == []


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — credentials never live in config (inline secrets are rejected)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("secret_field", ["token", "password", "api_key", "basic_auth", "secret"])
def test_inline_secret_in_target_is_rejected(monkeypatch, caplog, secret_field):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv(
        "JAVA_APP_TARGETS",
        json.dumps([
            {"app_id": "leaky", "actuator_url": "https://x/actuator",
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
    # The target carries only a credential_ref (a vault key name), never a secret
    # value — there is no attribute a credential could be stored in.
    t = JavaAppTarget(app_id="a", name="A", actuator_url="u", log_source="l")
    assert t.credential_ref == DEFAULT_CREDENTIAL_REF
    for attr in ("token", "password", "secret", "api_key", "basic_auth", "authorization"):
        assert not hasattr(t, attr)


# ─────────────────────────────────────────────────────────────────────────────
# AC3 / R191-H1 T1 (AC1) — credentials resolved from the vault ONLY; a vault miss
# fails CLOSED (no env fallback). The env-fallback path R17-D3 Addendum A removed
# everywhere else lived on here (the 1.8 verification's critical F1 finding); it
# is gone. ``env=`` is accepted for signature compatibility but never read.
# ─────────────────────────────────────────────────────────────────────────────
def _target(ref=DEFAULT_CREDENTIAL_REF):
    return JavaAppTarget(app_id="payments-api", name="P", actuator_url="u",
                         log_source="l", credential_ref=ref)


def test_secret_resolved_from_per_run_vault_context():
    captured = {"token": "VAULT-TOKEN-123"}
    secret = resolve_secret(
        "org-1", _target(),
        connector_lookup=lambda ref: captured if ref == DEFAULT_CREDENTIAL_REF else None,
    )
    assert secret == "VAULT-TOKEN-123"


def test_vault_miss_fails_closed_no_env_fallback(monkeypatch):
    # A JAVA_APP_TOKEN in the environment must NOT be used — a vault miss fails
    # closed regardless of what the environment holds (R191-H1 / T1, F1 fix).
    monkeypatch.setenv("JAVA_APP_TOKEN", "ENV-TOKEN-456")
    with pytest.raises(OperationalCredentialMissing) as exc:
        resolve_secret(
            "org-1", _target(),
            connector_lookup=lambda ref: None,        # nothing in the vault
            env={"JAVA_APP_TOKEN": "ENV-TOKEN-456"},   # accepted but never read
        )
    # The exception names the org, the target, and the credential ref (actionable).
    assert exc.value.org_id == "org-1"
    assert exc.value.app_id == "payments-api"
    assert exc.value.credential_ref == DEFAULT_CREDENTIAL_REF
    # The environment token is never leaked into the raised message.
    assert "ENV-TOKEN-456" not in str(exc.value)


def test_custom_credential_ref_vault_miss_fails_closed():
    with pytest.raises(OperationalCredentialMissing) as exc:
        resolve_secret(
            "org-1", _target(ref="payments_actuator"),
            connector_lookup=lambda ref: None,
            env={"PAYMENTS_ACTUATOR_TOKEN": "NS-TOKEN"},  # accepted but never read
        )
    assert exc.value.credential_ref == "payments_actuator"


def test_no_credential_ref_means_no_secret():
    # An endpoint declared as needing no credential resolves to None (not an error).
    t = JavaAppTarget(app_id="a", name="A", actuator_url="u", log_source="l", credential_ref=None)
    assert resolve_secret("org-1", t, connector_lookup=lambda ref: None, env={}) is None


def test_secret_lookup_failure_fails_closed(caplog):
    # A lookup EXCEPTION is treated as a vault miss — fail closed, never env fallback.
    def _boom(ref):
        raise RuntimeError("vault down")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(OperationalCredentialMissing):
            resolve_secret(
                "org-1", _target(), connector_lookup=_boom,
                env={"JAVA_APP_TOKEN": "FALLBACK"},  # accepted but never read
            )
    # The warning names the credential ref, never the (would-be) token value.
    assert "FALLBACK" not in caplog.text
