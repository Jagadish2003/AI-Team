"""R17-D2 review fixes - contract tests for the two applied hardening changes.

Covers the two review findings that warranted a code change:

  H1  The CUSTOMER_TENANT_API_KEY env fallback is dev-only by CONTRACT: when
      REQUIRE_CONNECTOR_SECRETS=1 (production) the resolver must NOT fall back to
      the env var, so a stray env value cannot bypass the vault and a customer's
      vault revoke fully cuts access (R17-D2 §2, AC2). The vault path is
      unaffected — a vaulted credential still resolves in production.

  H2  The reserved ``customer_tenant`` credential namespace is enforced at
      startup: if a real Integration Hub connector ever claims that connector_id,
      validate_provider_config() fails fast rather than letting the two
      subsystems cross-contaminate the shared credentials row.

The other review findings were verified as already-handled or invalid (notably
the "%s vs ?" claim — the project runs on PostgreSQL via psycopg2, so %s is
correct), so they carry no code change.

FAKE CREDENTIALS: the ``az-FAKE-*`` values below are non-real, test-only Azure
tenant keys. They are not live credentials.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.middleware import tenancy
from app.model_gateway import validate_provider_config
from app.model_gateway.customer_tenant_vault import resolve_customer_tenant_api_key
from app.auth import vault
from app.auth.vault import (
    assert_customer_tenant_namespace_unclaimed,
    revoke_customer_tenant_credential,
    store_customer_tenant_credential,
    CUSTOMER_TENANT_CONNECTOR_ID,
)

_VAULT_KEY = Fernet.generate_key().decode()
_FAKE_ENV_KEY = "az-FAKE-ENV-KEY-0123456789abcdef"
_FAKE_VAULT_KEY = "az-FAKE-VAULT-KEY-fedcba9876543210"


def _with_org(org_id: str):
    """Context helper: set the tenancy org for the duration of a with-block."""
    class _Ctx:
        def __enter__(self):
            self._token = tenancy._current_org_id.set(org_id)
            return self

        def __exit__(self, *_):
            tenancy._current_org_id.reset(self._token)

    return _Ctx()


# ---------------------------------------------------------------------------
# H1 — REQUIRE_CONNECTOR_SECRETS makes the env fallback dev-only
# ---------------------------------------------------------------------------


def test_h1_env_fallback_skipped_in_production(monkeypatch):
    """REQUIRE_CONNECTOR_SECRETS=1 + env key + no vault row → resolver returns ''."""
    monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
    monkeypatch.setenv("CUSTOMER_TENANT_API_KEY", _FAKE_ENV_KEY)
    # A unique org with no vaulted credential — the vault path yields nothing, so
    # only the (now-guarded) env fallback could have produced a value.
    with _with_org("org-h1-prod-no-vault"):
        assert resolve_customer_tenant_api_key() == ""


def test_h1_env_fallback_used_in_dev(monkeypatch):
    """Without REQUIRE_CONNECTOR_SECRETS, the env var is still a dev fallback."""
    monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
    monkeypatch.setenv("CUSTOMER_TENANT_API_KEY", _FAKE_ENV_KEY)
    with _with_org("org-h1-dev-no-vault"):
        assert resolve_customer_tenant_api_key() == _FAKE_ENV_KEY


def test_h1_vault_credential_still_resolves_in_production(monkeypatch):
    """The production guard only skips the ENV fallback — the vault path wins."""
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)
    monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
    monkeypatch.setenv("CUSTOMER_TENANT_API_KEY", _FAKE_ENV_KEY)  # must be ignored
    org = "org-h1-prod-vaulted"
    store_customer_tenant_credential(org, _FAKE_VAULT_KEY)
    try:
        with _with_org(org):
            # Vault-first: the vaulted value resolves, never the env var.
            assert resolve_customer_tenant_api_key() == _FAKE_VAULT_KEY
    finally:
        revoke_customer_tenant_credential(org)


def test_h1_production_without_any_credential_returns_empty(monkeypatch):
    """REQUIRE_CONNECTOR_SECRETS=1, no vault row, no env key → '' (graceful)."""
    monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
    monkeypatch.delenv("CUSTOMER_TENANT_API_KEY", raising=False)
    with _with_org("org-h1-prod-nothing"):
        assert resolve_customer_tenant_api_key() == ""


# ---------------------------------------------------------------------------
# H2 — the reserved customer_tenant connector namespace is enforced at startup
# ---------------------------------------------------------------------------


def test_h2_namespace_guard_passes_when_unclaimed():
    """With no real connector named customer_tenant, the guard is a no-op."""
    assert CUSTOMER_TENANT_CONNECTOR_ID not in vault.CONNECTOR_AUTH_CONFIGS
    assert_customer_tenant_namespace_unclaimed()  # must not raise


def test_h2_namespace_guard_raises_on_collision(monkeypatch):
    """If a real connector claims the reserved id, the guard raises ValueError."""
    collided = {**vault.CONNECTOR_AUTH_CONFIGS, CUSTOMER_TENANT_CONNECTOR_ID: object()}
    monkeypatch.setattr(vault, "CONNECTOR_AUTH_CONFIGS", collided)

    with pytest.raises(ValueError, match="reserved"):
        assert_customer_tenant_namespace_unclaimed()


def test_h2_validate_provider_config_raises_on_collision(monkeypatch):
    """The startup validator fails fast on a reserved-namespace collision."""
    collided = {**vault.CONNECTOR_AUTH_CONFIGS, CUSTOMER_TENANT_CONNECTOR_ID: object()}
    monkeypatch.setattr(vault, "CONNECTOR_AUTH_CONFIGS", collided)
    # Default provider selection (hosted) — the collision, not provider naming,
    # is what must trip the validator.
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

    with pytest.raises(ValueError, match="reserved"):
        validate_provider_config()


def test_h2_validate_provider_config_ok_without_collision(monkeypatch):
    """Normal config (no collision) still validates cleanly and does not raise."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)
    validate_provider_config()  # must not raise
