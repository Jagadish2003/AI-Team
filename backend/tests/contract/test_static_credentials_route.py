"""R17-D3 Addendum A (T12 / AC10) — static-credential entry route contract tests.

T10 gave the vault a static-credential record type; T12 adds the Integration Hub
entry surface on top of it:

  POST   /api/connectors/{connector_id}/credentials  — Owner-only, encrypt into vault
  GET    /api/connectors/{connector_id}/credentials  — status only, never the secret
  DELETE /api/connectors/{connector_id}/credentials  — Owner-only, revoke

These verify AC10 at the ROUTE layer: static credentials for Jira / ServiceNow /
databases can be entered per org by an Owner, land Fernet-encrypted in the vault,
and are NEVER readable back through the API (the write-only guarantee) — the vault
layer's own encryption/keying is covered by test_static_credentials_vault.py.

FAKE CREDENTIALS: every value below is a non-real, test-only credential.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from app import db
from app.auth.vault import get_static_credential, revoke_static_credential

# One key for the whole module so every test's store/read uses the same vault key
# (the shared session DB persists rows across tests).
_VAULT_KEY = Fernet.generate_key().decode()

OWNER = {"Authorization": "Bearer dev-token-change-me"}   # seeded owner of 'default'
VIEWER = {"Authorization": "Bearer viewer-token"}          # seeded viewer of 'default'
NO_AUTH: dict = {}

# Fake, non-real credentials. Not live secrets.
_FAKE_USER = "svc-agentiq@example.com"
_FAKE_SECRET = "FAKE-jira-api-token-0123456789abcdef"
_FAKE_URL = "https://example.atlassian.net"


def _seed_member(org_id: str, user_id: str, role: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO workspace_members (org_id, user_id, role, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (org_id, user_id)
            DO UPDATE SET role = EXCLUDED.role, is_deleted = FALSE
            """,
            (org_id, user_id, role, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()


def _raw_enc_secret(org_id: str, connector_id: str) -> str | None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT enc_secret FROM credentials "
            "WHERE org_id=%s AND connector_id=%s AND kind='static' AND is_deleted=FALSE",
            (org_id, connector_id),
        )
        row = cur.fetchone()
    finally:
        con.close()
    return row[0] if row else None


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    """The store path Fernet-encrypts with CREDENTIAL_VAULT_KEY, read at use time."""
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)
    yield


@pytest.fixture(autouse=True, scope="module")
def _seed_roles():
    """dev-token = owner (also done by conftest); viewer-token = a non-owner."""
    from app.rbac import seed_owner

    seed_owner("default", "dev-token-change-me")
    _seed_member("default", "viewer-token", "viewer")


@pytest.fixture
def clean_connector():
    """Yield a static connector id guaranteed clear of any prior credential."""
    connector_id = "jira"
    revoke_static_credential("default", connector_id)
    try:
        yield connector_id
    finally:
        revoke_static_credential("default", connector_id)


# ─────────────────────────────────────────────────────────────────────────────
# Auth + RBAC (AC10 — "by an Owner")
# ─────────────────────────────────────────────────────────────────────────────


def test_post_requires_auth(client):
    r = client.post("/api/connectors/jira/credentials", json={
        "base_url": _FAKE_URL, "username": _FAKE_USER, "secret": _FAKE_SECRET,
    })
    assert r.status_code == 401


def test_get_requires_auth(client):
    assert client.get("/api/connectors/jira/credentials").status_code == 401


def test_delete_requires_auth(client):
    assert client.delete("/api/connectors/jira/credentials").status_code == 401


def test_post_forbidden_for_non_owner(client, clean_connector):
    """A non-owner (viewer) cannot enter credentials — AC10 requires an Owner."""
    r = client.post(
        f"/api/connectors/{clean_connector}/credentials",
        headers=VIEWER,
        json={"base_url": _FAKE_URL, "username": _FAKE_USER, "secret": _FAKE_SECRET},
    )
    assert r.status_code == 403
    # And nothing was written.
    assert get_static_credential("default", clean_connector) is None


def test_delete_forbidden_for_non_owner(client):
    assert client.delete(
        "/api/connectors/jira/credentials", headers=VIEWER
    ).status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Owner store → Fernet-encrypted in the vault, write-only response (AC10)
# ─────────────────────────────────────────────────────────────────────────────


def test_owner_can_store_and_gets_metadata_only(client, clean_connector):
    r = client.post(
        f"/api/connectors/{clean_connector}/credentials",
        headers=OWNER,
        json={"base_url": _FAKE_URL, "username": _FAKE_USER, "secret": _FAKE_SECRET},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["connector_id"] == clean_connector
    assert body["configured"] is True
    assert body["base_url"] == _FAKE_URL          # non-secret instance location
    assert body["has_username"] is True           # presence only
    assert body["updated_at"]
    # Write-only: the response never carries the username or secret value, and no
    # 'username'/'secret' field exists on the metadata shape at all (AC10).
    assert "secret" not in body
    assert "username" not in body


def test_stored_credential_is_fernet_encrypted_in_vault(client, clean_connector):
    """The value lands encrypted at rest and round-trips via the vault (AC10)."""
    client.post(
        f"/api/connectors/{clean_connector}/credentials",
        headers=OWNER,
        json={"base_url": _FAKE_URL, "username": _FAKE_USER, "secret": _FAKE_SECRET},
    )
    # Raw column is ciphertext, not the plaintext secret.
    enc = _raw_enc_secret("default", clean_connector)
    assert enc is not None
    assert enc != _FAKE_SECRET and _FAKE_SECRET not in enc
    # Decrypts with the same vault key the route used.
    assert Fernet(_VAULT_KEY.encode()).decrypt(enc.encode()).decode() == _FAKE_SECRET
    # And the vault round-trips it.
    assert get_static_credential("default", clean_connector).secret == _FAKE_SECRET


def test_response_body_never_contains_the_secret_or_username(client, clean_connector):
    """AC10: values are never readable back through the API — POST or GET."""
    post = client.post(
        f"/api/connectors/{clean_connector}/credentials",
        headers=OWNER,
        json={"base_url": _FAKE_URL, "username": _FAKE_USER, "secret": _FAKE_SECRET},
    )
    assert _FAKE_SECRET not in post.text
    assert _FAKE_USER not in post.text

    get = client.get(f"/api/connectors/{clean_connector}/credentials", headers=OWNER)
    assert get.status_code == 200
    assert _FAKE_SECRET not in get.text
    assert _FAKE_USER not in get.text


def test_get_status_reflects_stored_credential(client, clean_connector):
    client.post(
        f"/api/connectors/{clean_connector}/credentials",
        headers=OWNER,
        json={"base_url": _FAKE_URL, "username": _FAKE_USER, "secret": _FAKE_SECRET},
    )
    body = client.get(
        f"/api/connectors/{clean_connector}/credentials", headers=OWNER
    ).json()
    assert body["configured"] is True
    assert body["base_url"] == _FAKE_URL
    assert body["has_username"] is True


def test_get_status_not_configured_when_absent(client):
    # A static connector that this suite never stores into.
    revoke_static_credential("default", "postgresql")
    body = client.get(
        "/api/connectors/postgresql/credentials", headers=OWNER
    ).json()
    assert body["configured"] is False
    assert body["base_url"] is None
    assert body["has_username"] is False


def test_replacing_a_credential_rotates_in_place(client, clean_connector):
    client.post(
        f"/api/connectors/{clean_connector}/credentials",
        headers=OWNER,
        json={"base_url": _FAKE_URL, "username": _FAKE_USER, "secret": _FAKE_SECRET},
    )
    rotated = "FAKE-rotated-token-fedcba9876543210"
    r = client.post(
        f"/api/connectors/{clean_connector}/credentials",
        headers=OWNER,
        json={"base_url": _FAKE_URL, "username": _FAKE_USER, "secret": rotated},
    )
    assert r.status_code == 200
    assert get_static_credential("default", clean_connector).secret == rotated


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────


def test_post_missing_secret_returns_400(client, clean_connector):
    r = client.post(
        f"/api/connectors/{clean_connector}/credentials",
        headers=OWNER,
        json={"base_url": _FAKE_URL, "username": _FAKE_USER, "secret": ""},
    )
    assert r.status_code == 400
    assert "token/password" in r.json()["detail"]
    assert get_static_credential("default", clean_connector) is None


def test_post_missing_url_and_username_returns_400(client, clean_connector):
    r = client.post(
        f"/api/connectors/{clean_connector}/credentials",
        headers=OWNER,
        json={"base_url": "", "username": "", "secret": _FAKE_SECRET},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "URL" in detail and "username" in detail


def test_post_rejects_oauth_only_connector(client):
    """Salesforce is OAuth-only — the static path must refuse it, not store it."""
    r = client.post(
        "/api/connectors/salesforce/credentials",
        headers=OWNER,
        json={"base_url": _FAKE_URL, "username": _FAKE_USER, "secret": _FAKE_SECRET},
    )
    assert r.status_code == 400
    assert get_static_credential("default", "salesforce") is None


def test_get_rejects_oauth_only_connector(client):
    assert client.get(
        "/api/connectors/salesforce/credentials", headers=OWNER
    ).status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# Revoke
# ─────────────────────────────────────────────────────────────────────────────


def test_owner_can_revoke_credential(client):
    connector_id = "servicenow"
    client.post(
        f"/api/connectors/{connector_id}/credentials",
        headers=OWNER,
        json={"base_url": _FAKE_URL, "username": _FAKE_USER, "secret": _FAKE_SECRET},
    )
    assert get_static_credential("default", connector_id) is not None

    r = client.delete(f"/api/connectors/{connector_id}/credentials", headers=OWNER)
    assert r.status_code == 204
    assert get_static_credential("default", connector_id) is None
    # Idempotent — a second revoke still 204s.
    assert client.delete(
        f"/api/connectors/{connector_id}/credentials", headers=OWNER
    ).status_code == 204
    assert client.get(
        f"/api/connectors/{connector_id}/credentials", headers=OWNER
    ).json()["configured"] is False
