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
from app.auth.credentials import (
    CredentialsNotConfigured,
    get_connector_credentials,
    get_connector_secret,
    try_get_connector_credentials,
)
from app.auth.models import StaticCredentialRecord, TokenRecord

_VAULT_KEY = Fernet.generate_key().decode()

# Fake, non-real tokens used only in tests. Not live secrets.
_FAKE_TOKEN = "FAKE-ACCESS-TOKEN-0123456789abcdef"
_FAKE_REFRESH = "FAKE-REFRESH-TOKEN-fedcba9876543210"
_FAKE_STATIC_SECRET = "FAKE-static-api-token-abcdef0123456789"
_FAKE_STATIC_USER = "svc-agentiq@example.com"
_FAKE_STATIC_URL = "https://example.atlassian.net"


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


# ---------------------------------------------------------------------------
# Kind-awareness: a static credential resolves to a StaticCredentialRecord, an
# OAuth credential to a TokenRecord (never a bogus empty-token TokenRecord).
# ---------------------------------------------------------------------------


def test_resolves_oauth_record_as_token_record():
    org, connector = "org-kind-oauth", "salesforce"
    with patch.dict(os.environ, _vault_env()):
        _store(org, connector)
        try:
            record = get_connector_credentials(org, connector)
            assert isinstance(record, TokenRecord)
            assert record.access_token == _FAKE_TOKEN
        finally:
            vault.revoke_customer_tenant_credential(org, connector_id=connector)


def test_resolves_static_record_as_static_credential_record():
    """A static-credential connector resolves to a StaticCredentialRecord with
    its secret / username / base_url — not an OAuth TokenRecord (which would
    carry an empty access token for a static row)."""
    org, connector = "org-kind-static", "jira"
    with patch.dict(os.environ, _vault_env()):
        vault.store_static_credential(
            org, connector,
            username=_FAKE_STATIC_USER, secret=_FAKE_STATIC_SECRET, base_url=_FAKE_STATIC_URL,
        )
        try:
            record = get_connector_credentials(org, connector)
            assert isinstance(record, StaticCredentialRecord)
            assert record.secret == _FAKE_STATIC_SECRET
            assert record.username == _FAKE_STATIC_USER
            assert record.base_url == _FAKE_STATIC_URL
        finally:
            vault.revoke_static_credential(org, connector)


def test_get_connector_secret_returns_the_bearer_value_for_both_kinds():
    """get_connector_secret yields the OAuth access token or the static secret."""
    with patch.dict(os.environ, _vault_env()):
        oauth_org = "org-secret-oauth"
        _store(oauth_org, "salesforce")
        static_org = "org-secret-static"
        vault.store_static_credential(
            static_org, "jira",
            username=_FAKE_STATIC_USER, secret=_FAKE_STATIC_SECRET, base_url=_FAKE_STATIC_URL,
        )
        try:
            assert get_connector_secret(oauth_org, "salesforce") == _FAKE_TOKEN
            assert get_connector_secret(static_org, "jira") == _FAKE_STATIC_SECRET
        finally:
            vault.revoke_customer_tenant_credential(oauth_org, connector_id="salesforce")
            vault.revoke_static_credential(static_org, "jira")


def test_get_connector_secret_raises_when_not_configured():
    with patch.dict(os.environ, _vault_env()):
        with pytest.raises(CredentialsNotConfigured):
            get_connector_secret("org-secret-absent", "jira")


# ---------------------------------------------------------------------------
# try_get_connector_credentials — None instead of raising.
# ---------------------------------------------------------------------------


def test_try_get_returns_none_when_not_configured():
    with patch.dict(os.environ, _vault_env()):
        assert try_get_connector_credentials("org-try-absent", "jira") is None


def test_try_get_returns_record_when_configured():
    org, connector = "org-try-present", "salesforce"
    with patch.dict(os.environ, _vault_env()):
        _store(org, connector)
        try:
            record = try_get_connector_credentials(org, connector)
            assert record is not None
            assert record.access_token == _FAKE_TOKEN
        finally:
            vault.revoke_customer_tenant_credential(org, connector_id=connector)


# ---------------------------------------------------------------------------
# Ingest bridge: resolve_vault_connector is the env-free per-org credential
# fallback ingestors use (AC8/AC9). It reads the vault per the run org — never
# an env credential — and normalises both record kinds.
# ---------------------------------------------------------------------------


def test_ingest_helper_resolves_oauth_token_per_run_org():
    from discovery.ingest import clear_live_connectors, resolve_vault_connector, set_ingest_org

    org, connector = "org-ingest-oauth", "salesforce"
    with patch.dict(os.environ, _vault_env()):
        _store(org, connector)
        set_ingest_org(org)
        try:
            resolved = resolve_vault_connector(connector)
            assert resolved == {"token": _FAKE_TOKEN}
        finally:
            clear_live_connectors()
            vault.revoke_customer_tenant_credential(org, connector_id=connector)


def test_ingest_helper_resolves_static_with_url_and_username():
    from discovery.ingest import clear_live_connectors, resolve_vault_connector, set_ingest_org

    org, connector = "org-ingest-static", "servicenow"
    with patch.dict(os.environ, _vault_env()):
        vault.store_static_credential(
            org, connector,
            username=_FAKE_STATIC_USER, secret=_FAKE_STATIC_SECRET, base_url=_FAKE_STATIC_URL,
        )
        set_ingest_org(org)
        try:
            resolved = resolve_vault_connector(connector)
            assert resolved["token"] == _FAKE_STATIC_SECRET
            assert resolved["username"] == _FAKE_STATIC_USER
            assert resolved["url"] == _FAKE_STATIC_URL
        finally:
            clear_live_connectors()
            vault.revoke_static_credential(org, connector)


def test_ingest_helper_is_org_isolated_and_env_free():
    """AC8/AC9: the ingest fallback resolves per-org from the vault and NEVER a
    process-global env credential. Org A's credential is invisible to org B even
    with a credential-shaped env var present."""
    from discovery.ingest import clear_live_connectors, resolve_vault_connector, set_ingest_org

    org_a, org_b, connector = "org-ingest-iso-a", "org-ingest-iso-b", "salesforce"
    env = _vault_env()
    env["SF_ACCESS_TOKEN"] = "ENV-FALLBACK-SHOULD-NEVER-BE-USED"
    with patch.dict(os.environ, env):
        _store(org_a, connector)
        try:
            set_ingest_org(org_a)
            assert resolve_vault_connector(connector) == {"token": _FAKE_TOKEN}

            # Org B has no credential — the env var must NOT satisfy resolution.
            set_ingest_org(org_b)
            assert resolve_vault_connector(connector) is None
        finally:
            clear_live_connectors()
            vault.revoke_customer_tenant_credential(org_a, connector_id=connector)


def test_ingest_helper_never_raises_on_absent():
    from discovery.ingest import clear_live_connectors, resolve_vault_connector, set_ingest_org

    with patch.dict(os.environ, _vault_env()):
        set_ingest_org("org-ingest-none")
        try:
            assert resolve_vault_connector("jira") is None
        finally:
            clear_live_connectors()


# ---------------------------------------------------------------------------
# Native DB connectors (R17-D3 Addendum A, T11 / AC8-AC9). The DB pool resolves
# its service-account username/password through the SAME single per-org vault
# path — connection_pool.resolve_db_credentials — keyed by (org_id,
# connector_id). Host/port/database stay instance config; the credential does
# not come from process-global env on a shared multi-tenant instance.
# ---------------------------------------------------------------------------


def _db_config(org_id: str, connector_id: str = "postgresql"):
    from connectors.db import DBConnectorConfig

    return DBConnectorConfig(
        connector_id=connector_id,
        org_id=org_id,
        host="db.internal",
        port=5432,
        database="analytics",
        driver="psycopg2",
        username_key="POSTGRESQL_USERNAME",
        password_key="POSTGRESQL_PASSWORD",
    )


def test_db_credentials_resolve_from_vault_not_env():
    """AC8: a vaulted static credential wins and no env credential is read."""
    from connectors.db.connection_pool import resolve_db_credentials

    org = "org-db-vault"
    env = _vault_env()
    # A credential-shaped env var that must be ignored in favour of the vault.
    env["POSTGRESQL_PASSWORD"] = "ENV-PASSWORD-SHOULD-NEVER-BE-USED"
    with patch.dict(os.environ, env):
        vault.store_static_credential(
            org, "postgresql",
            username="svc_agentiq", secret=_FAKE_STATIC_SECRET, base_url="db.internal:5432",
        )
        try:
            username, password = resolve_db_credentials(_db_config(org))
            assert username == "svc_agentiq"
            assert password == _FAKE_STATIC_SECRET  # vault, never the env value
        finally:
            vault.revoke_static_credential(org, "postgresql")


def test_db_credentials_are_org_isolated():
    """AC9: two orgs on one instance hold independent DB credentials."""
    from connectors.db.connection_pool import resolve_db_credentials

    org_a, org_b = "org-db-a", "org-db-b"
    with patch.dict(os.environ, _vault_env()):
        vault.store_static_credential(
            org_a, "postgresql", username="user_a", secret="secret-a", base_url="",
        )
        vault.store_static_credential(
            org_b, "postgresql", username="user_b", secret="secret-b", base_url="",
        )
        try:
            assert resolve_db_credentials(_db_config(org_a)) == ("user_a", "secret-a")
            assert resolve_db_credentials(_db_config(org_b)) == ("user_b", "secret-b")
        finally:
            vault.revoke_static_credential(org_a, "postgresql")
            vault.revoke_static_credential(org_b, "postgresql")


def test_db_credentials_fall_back_to_env_only_for_standalone():
    """With NO vaulted credential, the documented CLI/standalone env vars are the
    fallback — a single-tenant convenience, not a per-client shared secret."""
    from connectors.db.connection_pool import resolve_db_credentials

    env = _vault_env()
    env["POSTGRESQL_USERNAME"] = "cli_user"
    env["POSTGRESQL_PASSWORD"] = "cli_pass"
    with patch.dict(os.environ, env):
        # Org with no vault credential → env fallback.
        assert resolve_db_credentials(_db_config("org-db-standalone")) == (
            "cli_user",
            "cli_pass",
        )
