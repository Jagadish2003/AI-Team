"""R17-D3 Addendum A T9 (AT-510) — single connector-credential resolution path.

Verifies that ``get_connector_credentials`` is the one path to connector
credentials: it resolves from the per-org encrypted vault, is org-isolated,
surfaces a missing credential as a clear ``CredentialsNotConfigured`` state, and
NEVER falls back to environment variables (AC11).

Also exercises the ``vault.get_credential`` read primitive it wraps: a
synchronous, non-refreshing read that returns the stored record or ``None``.

FAKE CREDENTIALS: the ``FAKE-*`` token values below are non-real, test-only
strings. They are not live credentials.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.auth import vault
from app.auth.credentials import CredentialsNotConfigured, get_connector_credentials

_VAULT_KEY = Fernet.generate_key().decode()

# Fake, non-real tokens used only in tests. Not live secrets.
_FAKE_TOKEN = "FAKE-ACCESS-TOKEN-0123456789abcdef"
_FAKE_REFRESH = "FAKE-REFRESH-TOKEN-fedcba9876543210"


def _vault_env() -> dict:
    return {"CREDENTIAL_VAULT_KEY": _VAULT_KEY}


def _store(org_id: str, connector_id: str, *, expires_at: str | None = None) -> None:
    """Store a fake OAuth token for org+connector via the existing vault path."""
    token_response = {
        "access_token": _FAKE_TOKEN,
        "refresh_token": _FAKE_REFRESH,
        "scope": "read write",
    }
    if expires_at is not None:
        token_response["expires_at"] = expires_at
    else:
        token_response["expires_in"] = 3600
    vault.store_token(org_id, connector_id, token_response)


# ---------------------------------------------------------------------------
# get_connector_credentials — the single resolution path
# ---------------------------------------------------------------------------


def test_resolves_stored_credential_decrypted():
    """A stored credential resolves to a decrypted TokenRecord for the right org."""
    org, connector = "org-cred-resolve", "salesforce"
    with patch.dict(os.environ, _vault_env()):
        _store(org, connector)
        try:
            record = get_connector_credentials(org, connector)
            assert record.org_id == org
            assert record.connector_id == connector
            assert record.access_token == _FAKE_TOKEN  # decrypted at use
            assert record.refresh_token == _FAKE_REFRESH
        finally:
            vault.revoke_customer_tenant_credential(org, connector_id=connector)


def test_raises_when_not_configured():
    """A missing credential surfaces as CredentialsNotConfigured (AC11)."""
    with patch.dict(os.environ, _vault_env()):
        with pytest.raises(CredentialsNotConfigured) as exc_info:
            get_connector_credentials("org-cred-absent", "jira")
    # The error carries the org + connector so callers can surface a clear state.
    assert exc_info.value.org_id == "org-cred-absent"
    assert exc_info.value.connector_id == "jira"
    assert "not configured" in str(exc_info.value)


def test_raises_after_revocation():
    """Once a credential is revoked (soft-deleted), resolution reports not configured."""
    org, connector = "org-cred-revoked", "servicenow"
    with patch.dict(os.environ, _vault_env()):
        _store(org, connector)
        assert get_connector_credentials(org, connector).access_token == _FAKE_TOKEN

        vault.revoke_customer_tenant_credential(org, connector_id=connector)
        with pytest.raises(CredentialsNotConfigured):
            get_connector_credentials(org, connector)


def test_credentials_are_org_isolated():
    """Two orgs are independent: org A's credential is not visible to org B (AC9)."""
    org_a, org_b, connector = "org-cred-iso-a", "org-cred-iso-b", "jira"
    with patch.dict(os.environ, _vault_env()):
        _store(org_a, connector)
        try:
            assert get_connector_credentials(org_a, connector).access_token == _FAKE_TOKEN
            with pytest.raises(CredentialsNotConfigured):
                get_connector_credentials(org_b, connector)
        finally:
            vault.revoke_customer_tenant_credential(org_a, connector_id=connector)


def test_no_env_fallback_for_missing_credential():
    """AC11: a credential-shaped env var must NEVER satisfy resolution.

    Even with SF_ACCESS_TOKEN / JIRA_TOKEN present in the environment, an org with
    no vault credential must raise CredentialsNotConfigured — the whole point of
    the fix is that env vars are process-global and can never be per-org.
    """
    env = _vault_env()
    env.update(
        {
            "SF_ACCESS_TOKEN": "ENV-FALLBACK-SHOULD-NEVER-BE-USED",
            "SALESFORCE_ACCESS_TOKEN": "ENV-FALLBACK-SHOULD-NEVER-BE-USED",
            "JIRA_TOKEN": "ENV-FALLBACK-SHOULD-NEVER-BE-USED",
        }
    )
    with patch.dict(os.environ, env):
        with pytest.raises(CredentialsNotConfigured):
            get_connector_credentials("org-cred-noenv", "salesforce")
        with pytest.raises(CredentialsNotConfigured):
            get_connector_credentials("org-cred-noenv", "jira")


# ---------------------------------------------------------------------------
# vault.get_credential — the read primitive the resolution layer wraps
# ---------------------------------------------------------------------------


def test_get_credential_returns_none_when_absent():
    with patch.dict(os.environ, _vault_env()):
        assert vault.get_credential("org-getcred-absent", "salesforce") is None


def test_get_credential_does_not_refresh_or_raise_on_expired():
    """Unlike get_token(), get_credential is a pure read: it returns an expired
    token as-is rather than attempting a refresh or raising."""
    org, connector = "org-getcred-expired", "salesforce"
    with patch.dict(os.environ, _vault_env()):
        _store(org, connector, expires_at="2000-01-01T00:00:00+00:00")
        try:
            record = vault.get_credential(org, connector)
            assert record is not None
            assert record.access_token == _FAKE_TOKEN
            # And the resolution layer still returns it (config present, not a
            # 'not configured' state — token validity is a separate concern).
            assert get_connector_credentials(org, connector).access_token == _FAKE_TOKEN
        finally:
            vault.revoke_customer_tenant_credential(org, connector_id=connector)
