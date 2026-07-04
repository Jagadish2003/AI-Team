"""R17-D3 Addendum A (T10) - Static-credential vault record contract tests.

The vault gains a second record type alongside OAuth tokens: static
credentials (Jira URL + user + API token, ServiceNow URL + user + password,
native DB connection credentials). Verifies the record is Fernet-encrypted at
rest with the SAME key and scheme as token records, keyed per
(org_id, connector_id) on the SAME table (one credential per connector per
org across both kinds), round-trips through decryption, is rotatable and
revocable, is org-isolated, and never exposes plaintext through repr/logs
(AC10 at the vault layer).

FAKE CREDENTIALS: every value below is a non-real, test-only credential.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app import db
from app.auth.models import ConnectorNotAuthenticatedError, StaticCredentialRecord
from app.auth.secrets import MissingSecretError
from app.auth.vault import (
    OAUTH_CREDENTIAL_KIND,
    STATIC_CREDENTIAL_KIND,
    get_static_credential,
    get_token,
    revoke_static_credential,
    store_static_credential,
    store_token,
)

_VAULT_KEY = Fernet.generate_key().decode()

# Fake, non-real credentials used only in these tests. Not live secrets.
_FAKE_USER = "svc-agentiq@example.com"
_FAKE_SECRET = "FAKE-jira-api-token-0123456789abcdef"
_FAKE_SECRET_ROTATED = "FAKE-rotated-token-fedcba9876543210"
_FAKE_BASE_URL = "https://example.atlassian.net"


def _vault_env() -> dict:
    return {"CREDENTIAL_VAULT_KEY": _VAULT_KEY}


def _raw_row(org_id: str, connector_id: str):
    """Return (kind, enc_username, enc_secret, base_url) raw (still-encrypted)
    columns for the active row, or None."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT kind, enc_username, enc_secret, base_url FROM credentials "
            "WHERE org_id=%s AND connector_id=%s AND is_deleted = FALSE",
            (org_id, connector_id),
        )
        row = cur.fetchone()
    finally:
        con.close()
    return row


def _active_row_count(org_id: str, connector_id: str) -> int:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM credentials "
            "WHERE org_id=%s AND connector_id=%s AND is_deleted = FALSE",
            (org_id, connector_id),
        )
        count = cur.fetchone()[0]
    finally:
        con.close()
    return count


def _fake_token_response() -> dict:
    return {
        "access_token": "FAKE-oauth-access-token",
        "refresh_token": "FAKE-oauth-refresh-token",
        "expires_in": 3600,
        "scope": "read",
    }


def test_store_then_get_round_trips_all_fields():
    org = "org-static-roundtrip"
    with patch.dict(os.environ, _vault_env()):
        store_static_credential(
            org, "jira", username=_FAKE_USER, secret=_FAKE_SECRET, base_url=_FAKE_BASE_URL
        )
        try:
            record = get_static_credential(org, "jira")
            assert isinstance(record, StaticCredentialRecord)
            assert record.org_id == org
            assert record.connector_id == "jira"
            assert record.kind == STATIC_CREDENTIAL_KIND
            assert record.username == _FAKE_USER
            assert record.secret == _FAKE_SECRET
            assert record.base_url == _FAKE_BASE_URL
            assert record.created_at.tzinfo is not None
            assert record.updated_at.tzinfo is not None
        finally:
            revoke_static_credential(org, "jira")


def test_username_and_secret_encrypted_at_rest_with_vault_key():
    """Raw columns never hold plaintext, and the ciphertext decrypts with the
    SAME Fernet vault key used for token records (AC10)."""
    org = "org-static-encrypted"
    with patch.dict(os.environ, _vault_env()):
        store_static_credential(
            org, "servicenow", username=_FAKE_USER, secret=_FAKE_SECRET, base_url=_FAKE_BASE_URL
        )
        try:
            kind, enc_username, enc_secret, base_url = _raw_row(org, "servicenow")
            assert kind == STATIC_CREDENTIAL_KIND

            assert enc_username != _FAKE_USER and _FAKE_USER not in enc_username
            assert enc_secret != _FAKE_SECRET and _FAKE_SECRET not in enc_secret

            # Same encryption as tokens: plain Fernet under CREDENTIAL_VAULT_KEY.
            f = Fernet(_VAULT_KEY.encode())
            assert f.decrypt(enc_username.encode()).decode() == _FAKE_USER
            assert f.decrypt(enc_secret.encode()).decode() == _FAKE_SECRET

            # base_url is not a secret and is stored as-is.
            assert base_url == _FAKE_BASE_URL
        finally:
            revoke_static_credential(org, "servicenow")


def test_get_returns_none_when_nothing_stored():
    with patch.dict(os.environ, _vault_env()):
        assert get_static_credential("org-static-absent", "jira") is None


def test_rotation_overwrites_in_place_single_active_row():
    """Storing again rotates in place: one active row, created_at preserved."""
    org = "org-static-rotate"
    with patch.dict(os.environ, _vault_env()):
        first = store_static_credential(
            org, "jira", username=_FAKE_USER, secret=_FAKE_SECRET, base_url=_FAKE_BASE_URL
        )
        second = store_static_credential(
            org, "jira", username=_FAKE_USER, secret=_FAKE_SECRET_ROTATED, base_url=_FAKE_BASE_URL
        )
        try:
            record = get_static_credential(org, "jira")
            assert record.secret == _FAKE_SECRET_ROTATED
            assert _active_row_count(org, "jira") == 1
            # The upsert preserves the original created_at and advances updated_at.
            assert second.created_at == first.created_at
            assert second.updated_at >= first.updated_at
        finally:
            revoke_static_credential(org, "jira")


def test_revocation_makes_credential_unavailable_and_is_idempotent():
    org = "org-static-revoke"
    with patch.dict(os.environ, _vault_env()):
        store_static_credential(
            org, "oracle", username=_FAKE_USER, secret=_FAKE_SECRET, base_url="db.example.com:1521"
        )
        assert get_static_credential(org, "oracle") is not None

        revoke_static_credential(org, "oracle")
        assert get_static_credential(org, "oracle") is None

        # Idempotent — revoking again (or with nothing stored) must not raise.
        revoke_static_credential(org, "oracle")
        revoke_static_credential("org-static-never-stored", "oracle")


def test_store_reactivates_after_revocation():
    org = "org-static-reactivate"
    with patch.dict(os.environ, _vault_env()):
        store_static_credential(
            org, "jira", username=_FAKE_USER, secret=_FAKE_SECRET, base_url=_FAKE_BASE_URL
        )
        revoke_static_credential(org, "jira")
        assert get_static_credential(org, "jira") is None

        store_static_credential(
            org, "jira", username=_FAKE_USER, secret=_FAKE_SECRET_ROTATED, base_url=_FAKE_BASE_URL
        )
        try:
            record = get_static_credential(org, "jira")
            assert record.secret == _FAKE_SECRET_ROTATED
        finally:
            revoke_static_credential(org, "jira")


def test_credentials_are_org_isolated():
    """Two orgs hold independent static credentials for the same connector."""
    org_a, org_b = "org-static-iso-a", "org-static-iso-b"
    with patch.dict(os.environ, _vault_env()):
        store_static_credential(
            org_a, "jira", username="user-a", secret="FAKE-secret-a", base_url="https://a.example.com"
        )
        store_static_credential(
            org_b, "jira", username="user-b", secret="FAKE-secret-b", base_url="https://b.example.com"
        )
        try:
            rec_a = get_static_credential(org_a, "jira")
            rec_b = get_static_credential(org_b, "jira")
            assert rec_a.secret == "FAKE-secret-a" and rec_a.base_url == "https://a.example.com"
            assert rec_b.secret == "FAKE-secret-b" and rec_b.base_url == "https://b.example.com"
            assert get_static_credential("org-static-iso-c", "jira") is None
        finally:
            revoke_static_credential(org_a, "jira")
            revoke_static_credential(org_b, "jira")


@pytest.mark.anyio
async def test_same_keying_as_token_records_one_credential_per_connector():
    """Same (org_id, connector_id) keying as token records: the two kinds
    share the table and the unique constraint, so a connector holds one
    credential — static replaces OAuth and vice versa (the ServiceNow
    either/or case)."""
    org = "org-static-keying"
    with patch.dict(os.environ, _vault_env()):
        try:
            # OAuth token first — invisible to the static read path.
            store_token(org, "servicenow", _fake_token_response())
            assert get_static_credential(org, "servicenow") is None
            token = await get_token(org, "servicenow")
            assert token.access_token == "FAKE-oauth-access-token"

            # Static store replaces it: kind flips, token readers now see
            # the connector as not authenticated rather than a bogus token.
            store_static_credential(
                org, "servicenow", username=_FAKE_USER, secret=_FAKE_SECRET, base_url=_FAKE_BASE_URL
            )
            assert _active_row_count(org, "servicenow") == 1
            assert get_static_credential(org, "servicenow").secret == _FAKE_SECRET
            with pytest.raises(ConnectorNotAuthenticatedError):
                await get_token(org, "servicenow")

            # OAuth reconnect switches back and clears the static fields.
            store_token(org, "servicenow", _fake_token_response())
            assert _active_row_count(org, "servicenow") == 1
            assert get_static_credential(org, "servicenow") is None
            kind, enc_username, enc_secret, base_url = _raw_row(org, "servicenow")
            assert kind == OAUTH_CREDENTIAL_KIND
            assert enc_username is None and enc_secret is None and base_url is None
        finally:
            con = db.connect()
            try:
                cur = con.cursor()
                cur.execute(
                    "UPDATE credentials SET is_deleted = TRUE WHERE org_id = %s",
                    (org,),
                )
                con.commit()
            finally:
                con.close()


def test_revoke_static_never_removes_an_oauth_token_row():
    org = "org-static-revoke-scope"
    with patch.dict(os.environ, _vault_env()):
        store_token(org, "jira", _fake_token_response())
        try:
            revoke_static_credential(org, "jira")
            # The OAuth row is untouched — revoke_static is scoped to kind='static'.
            assert _active_row_count(org, "jira") == 1
        finally:
            con = db.connect()
            try:
                cur = con.cursor()
                cur.execute(
                    "UPDATE credentials SET is_deleted = TRUE WHERE org_id = %s",
                    (org,),
                )
                con.commit()
            finally:
                con.close()


def test_record_repr_never_exposes_username_or_secret():
    """AC10: values are write-only — the record can never leak plaintext into
    logs via repr/str formatting."""
    org = "org-static-repr"
    with patch.dict(os.environ, _vault_env()):
        store_static_credential(
            org, "jira", username=_FAKE_USER, secret=_FAKE_SECRET, base_url=_FAKE_BASE_URL
        )
        try:
            record = get_static_credential(org, "jira")
            for rendered in (repr(record), str(record)):
                assert _FAKE_SECRET not in rendered
                assert _FAKE_USER not in rendered
        finally:
            revoke_static_credential(org, "jira")


def test_store_rejects_empty_secret():
    with patch.dict(os.environ, _vault_env()):
        with pytest.raises(ValueError):
            store_static_credential(
                "org-static-empty", "jira", username=_FAKE_USER, secret="", base_url=_FAKE_BASE_URL
            )
        with pytest.raises(ValueError):
            store_static_credential(
                "org-static-empty", "jira", username=_FAKE_USER, secret="   ", base_url=_FAKE_BASE_URL
            )


def test_store_requires_vault_key(monkeypatch):
    """Same hygiene as token records: no CREDENTIAL_VAULT_KEY, no storage —
    the vault never falls back to plaintext at rest."""
    monkeypatch.delenv("CREDENTIAL_VAULT_KEY", raising=False)
    with pytest.raises(MissingSecretError):
        store_static_credential(
            "org-static-nokey", "jira", username=_FAKE_USER, secret=_FAKE_SECRET, base_url=_FAKE_BASE_URL
        )
