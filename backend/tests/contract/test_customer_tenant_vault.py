"""R17-D2 T2 - Customer-tenant credential vault contract tests.

Verifies the customer-tenant model credential is Fernet-encrypted at rest in the
shared `credentials` table, round-trips through decryption, is rotatable and
revocable, is org-isolated, and degrades to None (never raises) on the read path
so the model gateway can fail gracefully (R17-D2 §2, AC2/AC5).

FAKE CREDENTIALS: the ``az-FAKE-*`` values below are non-real, test-only Azure
tenant keys. They are not live credentials.
"""
from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app import db
from app.auth.vault import (
    CUSTOMER_TENANT_CONNECTOR_ID,
    get_customer_tenant_credential,
    revoke_customer_tenant_credential,
    store_customer_tenant_credential,
)
from app.middleware import tenancy
from app.model_gateway.customer_tenant_config import CONFIG_KEY_API_KEY
from app.model_gateway.customer_tenant_vault import (
    resolve_customer_tenant_api_key,
    validate_no_production_env_fallback,
)

_VAULT_KEY = Fernet.generate_key().decode()

# Fake, non-real Azure-style tenant keys used only in tests. Not live secrets.
_FAKE_AZURE_KEY = "az-FAKE-TENANT-KEY-0123456789abcdef"
_FAKE_AZURE_KEY_ROTATED = "az-FAKE-ROTATED-KEY-fedcba9876543210"


def _vault_env() -> dict:
    return {"CREDENTIAL_VAULT_KEY": _VAULT_KEY}


def _raw_access_token(org_id: str, connector_id: str = CUSTOMER_TENANT_CONNECTOR_ID):
    """Return the raw (still-encrypted) access_token column for an active row."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT access_token FROM credentials "
            "WHERE org_id=%s AND connector_id=%s AND is_deleted = FALSE",
            (org_id, connector_id),
        )
        row = cur.fetchone()
    finally:
        con.close()
    return row[0] if row else None


def test_store_then_get_round_trips_plaintext():
    org = "org-ct-roundtrip"
    with patch.dict(os.environ, _vault_env()):
        store_customer_tenant_credential(org, _FAKE_AZURE_KEY)
        try:
            assert get_customer_tenant_credential(org) == _FAKE_AZURE_KEY
        finally:
            revoke_customer_tenant_credential(org)


def test_credential_is_encrypted_at_rest_never_plaintext():
    """The raw DB column must never contain the plaintext credential (AC2)."""
    org = "org-ct-encrypted"
    with patch.dict(os.environ, _vault_env()):
        store_customer_tenant_credential(org, _FAKE_AZURE_KEY)
        try:
            raw = _raw_access_token(org)
            assert raw is not None
            assert raw != _FAKE_AZURE_KEY
            assert _FAKE_AZURE_KEY not in raw
        finally:
            revoke_customer_tenant_credential(org)


def test_get_returns_none_when_nothing_stored():
    with patch.dict(os.environ, _vault_env()):
        assert get_customer_tenant_credential("org-ct-absent") is None


def test_rotation_overwrites_in_place_single_active_row():
    """Storing again rotates the credential; only one active row per org."""
    org = "org-ct-rotate"
    with patch.dict(os.environ, _vault_env()):
        store_customer_tenant_credential(org, _FAKE_AZURE_KEY)
        store_customer_tenant_credential(org, _FAKE_AZURE_KEY_ROTATED)
        try:
            assert get_customer_tenant_credential(org) == _FAKE_AZURE_KEY_ROTATED

            con = db.connect()
            try:
                cur = con.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM credentials "
                    "WHERE org_id=%s AND connector_id=%s AND is_deleted = FALSE",
                    (org, CUSTOMER_TENANT_CONNECTOR_ID),
                )
                count = cur.fetchone()[0]
            finally:
                con.close()
            assert count == 1
        finally:
            revoke_customer_tenant_credential(org)


def test_revocation_makes_credential_unavailable():
    """After the customer revokes, the credential reads as unavailable (AC2)."""
    org = "org-ct-revoke"
    with patch.dict(os.environ, _vault_env()):
        store_customer_tenant_credential(org, _FAKE_AZURE_KEY)
        assert get_customer_tenant_credential(org) == _FAKE_AZURE_KEY

        revoke_customer_tenant_credential(org)
        assert get_customer_tenant_credential(org) is None


def test_revoke_is_idempotent_when_no_credential():
    # Must not raise even when there is nothing to revoke.
    with patch.dict(os.environ, _vault_env()):
        revoke_customer_tenant_credential("org-ct-never-stored")


def test_store_reactivates_after_revocation():
    org = "org-ct-reactivate"
    with patch.dict(os.environ, _vault_env()):
        store_customer_tenant_credential(org, _FAKE_AZURE_KEY)
        revoke_customer_tenant_credential(org)
        assert get_customer_tenant_credential(org) is None

        store_customer_tenant_credential(org, _FAKE_AZURE_KEY_ROTATED)
        try:
            assert get_customer_tenant_credential(org) == _FAKE_AZURE_KEY_ROTATED
        finally:
            revoke_customer_tenant_credential(org)


def test_credentials_are_org_isolated():
    org_a, org_b = "org-ct-iso-a", "org-ct-iso-b"
    with patch.dict(os.environ, _vault_env()):
        store_customer_tenant_credential(org_a, _FAKE_AZURE_KEY)
        try:
            assert get_customer_tenant_credential(org_a) == _FAKE_AZURE_KEY
            assert get_customer_tenant_credential(org_b) is None
        finally:
            revoke_customer_tenant_credential(org_a)


def test_store_rejects_empty_credential():
    with patch.dict(os.environ, _vault_env()):
        with pytest.raises(ValueError):
            store_customer_tenant_credential("org-ct-empty", "")
        with pytest.raises(ValueError):
            store_customer_tenant_credential("org-ct-empty", "   ")


def test_get_returns_none_when_vault_key_unavailable(monkeypatch):
    """An undecryptable value (rotated/missing vault key) reads as None, not a raise."""
    org = "org-ct-nokey"
    with patch.dict(os.environ, _vault_env()):
        store_customer_tenant_credential(org, _FAKE_AZURE_KEY)

    try:
        # Remove the vault key: the stored ciphertext can no longer be decrypted.
        monkeypatch.delenv("CREDENTIAL_VAULT_KEY", raising=False)
        assert get_customer_tenant_credential(org) is None
    finally:
        with patch.dict(os.environ, _vault_env()):
            revoke_customer_tenant_credential(org)


# ---------------------------------------------------------------------------
# resolve_customer_tenant_api_key — env-fallback production guard (review HIGH).
# In production (REQUIRE_CONNECTOR_SECRETS=1) the credential must come from the
# vault; the CUSTOMER_TENANT_API_KEY env fallback must be suppressed so a stale
# env var can never override a revoked vault credential and bypass the vault.
# ---------------------------------------------------------------------------


def test_env_fallback_active_when_not_production(monkeypatch):
    """Dev/standalone: with no vault credential, the env var is used (unchanged)."""
    monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
    monkeypatch.delenv("CREDENTIAL_VAULT_KEY", raising=False)  # no usable vault
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_AZURE_KEY)
    token = tenancy._current_org_id.set("org-ct-envfallback-dev")
    try:
        assert resolve_customer_tenant_api_key() == _FAKE_AZURE_KEY
    finally:
        tenancy._current_org_id.reset(token)


def test_env_fallback_not_suppressed_by_require_connector_secrets(monkeypatch):
    """REQUIRE_CONNECTOR_SECRETS=1 is not the production profile by itself."""
    monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("CREDENTIAL_VAULT_KEY", raising=False)  # no usable vault
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_AZURE_KEY)
    token = tenancy._current_org_id.set("org-ct-envfallback-require-secrets")
    try:
        assert resolve_customer_tenant_api_key() == _FAKE_AZURE_KEY
    finally:
        tenancy._current_org_id.reset(token)


def test_vault_credential_still_used_in_production(monkeypatch):
    """The guard suppresses only the ENV fallback — a vaulted credential still
    resolves normally when REQUIRE_CONNECTOR_SECRETS=1 is set."""
    org = "org-ct-prod-vault"
    monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)
    monkeypatch.delenv(CONFIG_KEY_API_KEY, raising=False)
    store_customer_tenant_credential(org, _FAKE_AZURE_KEY)
    token = tenancy._current_org_id.set(org)
    try:
        assert resolve_customer_tenant_api_key() == _FAKE_AZURE_KEY
    finally:
        tenancy._current_org_id.reset(token)
        revoke_customer_tenant_credential(org)


# ---------------------------------------------------------------------------
# R1.9.1-H1 T4 (F4 fix) — production guard also covers ENVIRONMENT=production,
# and pairs the runtime suppression with a startup-visibility warning.
# ---------------------------------------------------------------------------


def test_env_fallback_suppressed_under_environment_production(monkeypatch):
    """ENVIRONMENT=production (no REQUIRE_CONNECTOR_SECRETS) also suppresses the
    env fallback — the guard is not narrowly tied to the older signal alone."""
    monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("CREDENTIAL_VAULT_KEY", raising=False)  # no usable vault
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_AZURE_KEY)  # present but ignored
    token = tenancy._current_org_id.set("org-ct-envfallback-prod-envvar")
    try:
        assert resolve_customer_tenant_api_key() == ""
    finally:
        tenancy._current_org_id.reset(token)


def test_vault_credential_still_used_under_environment_production(monkeypatch):
    """A vaulted credential still resolves under ENVIRONMENT=production — the
    guard suppresses only the env fallback, never the vault path."""
    org = "org-ct-prod-vault-envvar"
    monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)
    monkeypatch.delenv(CONFIG_KEY_API_KEY, raising=False)
    store_customer_tenant_credential(org, _FAKE_AZURE_KEY)
    token = tenancy._current_org_id.set(org)
    try:
        assert resolve_customer_tenant_api_key() == _FAKE_AZURE_KEY
    finally:
        tenancy._current_org_id.reset(token)
        revoke_customer_tenant_credential(org)


def test_env_fallback_unaffected_by_non_production_environment_value(monkeypatch):
    """ENVIRONMENT set to something other than 'production' (e.g. 'staging',
    'development') behaves like dev/standalone — env fallback stays active."""
    monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.delenv("CREDENTIAL_VAULT_KEY", raising=False)
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_AZURE_KEY)
    token = tenancy._current_org_id.set("org-ct-envfallback-staging")
    try:
        assert resolve_customer_tenant_api_key() == _FAKE_AZURE_KEY
    finally:
        tenancy._current_org_id.reset(token)


# ---------------------------------------------------------------------------
# validate_no_production_env_fallback() — the paired startup-visibility check.
# The env var is already provably unused in production (tests above); this is
# pure operator-facing hygiene, and must never raise.
# ---------------------------------------------------------------------------


def _warning_messages(caplog) -> list:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_no_production_warning_when_env_var_unset(caplog, monkeypatch):
    monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
    monkeypatch.delenv(CONFIG_KEY_API_KEY, raising=False)

    with caplog.at_level(logging.WARNING):
        validate_no_production_env_fallback()

    assert _warning_messages(caplog) == []


def test_no_warning_when_env_var_set_under_require_connector_secrets_only(caplog, monkeypatch):
    monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_AZURE_KEY)

    with caplog.at_level(logging.WARNING):
        validate_no_production_env_fallback()

    msgs = _warning_messages(caplog)
    assert not any(CONFIG_KEY_API_KEY in m for m in msgs), msgs


def test_warns_when_env_var_set_under_environment_production(caplog, monkeypatch):
    monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_AZURE_KEY)

    with caplog.at_level(logging.WARNING):
        validate_no_production_env_fallback()

    assert any(CONFIG_KEY_API_KEY in m for m in _warning_messages(caplog))


def test_no_warning_in_dev_even_with_env_var_set(caplog, monkeypatch):
    """Dev/standalone is unchanged: no warning, no behaviour change."""
    monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_AZURE_KEY)

    with caplog.at_level(logging.WARNING):
        validate_no_production_env_fallback()

    assert _warning_messages(caplog) == []


def test_validate_no_production_env_fallback_never_raises(monkeypatch):
    monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_AZURE_KEY)
    validate_no_production_env_fallback()  # must not raise
