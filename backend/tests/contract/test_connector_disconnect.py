"""R18-C0 P4 / AT-566 — unified per-tile connector disconnect contract tests.

Each connected Integration Hub tile gains a Disconnect action (with confirm). It
maps to ONE backend route regardless of how the connector was authenticated:

  DELETE /api/connectors/{connector_id}   — analyst+

These verify the AC4 contract at the ROUTE layer:
  * disconnecting clears WHICHEVER credential kind the org holds (OAuth token or
    static credential),
  * the org's connector connection state flips to "disconnected",
  * the action is org-scoped (never another org's credential),
  * it is idempotent (disconnecting an already-/never-connected connector 204s),
  * it is role-gated analyst+ (a viewer is 403; unauthenticated is 401).

FAKE CREDENTIALS: every value below is a non-real, test-only credential.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from app import db
from app.auth.vault import (
    get_static_credential,
    revoke_static_credential,
    store_static_credential,
    store_token,
)

# One key for the whole module so every store/read uses the same vault key
# (the shared session DB persists rows across tests).
_VAULT_KEY = Fernet.generate_key().decode()

OWNER = {"Authorization": "Bearer dev-token-change-me"}   # seeded owner of 'default'
VIEWER = {"Authorization": "Bearer viewer-token"}          # seeded viewer of 'default'

# Fake, non-real credentials. Not live secrets.
_FAKE_USER = "svc-agentiq@example.com"
_FAKE_SECRET = "FAKE-jira-api-token-0123456789abcdef"
_FAKE_URL = "https://example.atlassian.net"


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
            ("default", "viewer-token", "viewer", datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()


def _connector_status(org_id: str, connector_id: str):
    """The org's stored connection-state status for this connector, or None."""
    record = db.org_connector_get(org_id, connector_id)
    if record is None or record.get("org_id") != org_id:
        return None
    return record.get("status")


def _has_oauth_token(org_id: str, connector_id: str) -> bool:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT 1 FROM credentials "
            "WHERE org_id=%s AND connector_id=%s AND is_deleted=FALSE",
            (org_id, connector_id),
        )
        return cur.fetchone() is not None
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# Auth + RBAC — analyst+ (AC4: the disconnect action is a connector write)
# ─────────────────────────────────────────────────────────────────────────────


def test_disconnect_requires_auth(client):
    assert client.delete("/api/connectors/jira").status_code == 401


def test_disconnect_forbidden_for_viewer(client):
    assert client.delete("/api/connectors/jira", headers=VIEWER).status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Static-credential connector — the vault row is cleared, tile disconnected
# ─────────────────────────────────────────────────────────────────────────────


def test_disconnect_clears_static_credential_and_state(client):
    connector_id = "servicenow"
    revoke_static_credential("default", connector_id)
    store_static_credential(
        "default", connector_id,
        username=_FAKE_USER, secret=_FAKE_SECRET, base_url=_FAKE_URL,
    )
    db.org_connector_set("default", connector_id, {"status": "connected"})
    assert get_static_credential("default", connector_id) is not None

    r = client.delete(f"/api/connectors/{connector_id}", headers=OWNER)
    assert r.status_code == 204

    # Credential cleared and the tile returned to disconnected (AC4).
    assert get_static_credential("default", connector_id) is None
    assert _connector_status("default", connector_id) == "disconnected"


# ─────────────────────────────────────────────────────────────────────────────
# OAuth connector — the token row is cleared, tile disconnected
# ─────────────────────────────────────────────────────────────────────────────


def test_disconnect_clears_oauth_token_and_state(client):
    # github has revocation_url=None, so revoke_token performs no external call.
    connector_id = "github"
    store_token(
        "default", connector_id,
        {"access_token": "FAKE-access", "refresh_token": "FAKE-refresh", "expires_in": 3600},
    )
    db.org_connector_set("default", connector_id, {"status": "connected"})
    assert _has_oauth_token("default", connector_id) is True

    r = client.delete(f"/api/connectors/{connector_id}", headers=OWNER)
    assert r.status_code == 204

    assert _has_oauth_token("default", connector_id) is False
    assert _connector_status("default", connector_id) == "disconnected"


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency — disconnecting a never-connected connector still 204s
# ─────────────────────────────────────────────────────────────────────────────


def test_disconnect_is_idempotent(client):
    # A connector with revocation_url=None so no external call is attempted, and
    # nothing stored so both revokes are no-ops — the route must still 204 twice.
    connector_id = "github"
    revoke_static_credential("default", connector_id)

    first = client.delete(f"/api/connectors/{connector_id}", headers=OWNER)
    assert first.status_code == 204
    second = client.delete(f"/api/connectors/{connector_id}", headers=OWNER)
    assert second.status_code == 204


# ─────────────────────────────────────────────────────────────────────────────
# Org isolation — one org disconnecting never touches another org's credential
# ─────────────────────────────────────────────────────────────────────────────


def test_disconnect_is_org_scoped(client):
    """The default org disconnecting servicenow must leave another org's credential."""
    connector_id = "servicenow"
    other_org = "other_org_disconnect"
    revoke_static_credential(other_org, connector_id)
    store_static_credential(
        other_org, connector_id,
        username=_FAKE_USER, secret=_FAKE_SECRET, base_url=_FAKE_URL,
    )

    # Disconnect as the default org (dev-token owner of 'default').
    r = client.delete(f"/api/connectors/{connector_id}", headers=OWNER)
    assert r.status_code == 204

    # The other org's credential is untouched.
    assert get_static_credential(other_org, connector_id) is not None
    revoke_static_credential(other_org, connector_id)
