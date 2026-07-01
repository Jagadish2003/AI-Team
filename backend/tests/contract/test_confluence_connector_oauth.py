"""Contract tests for R17-A2 / AT-462 (T5) — Confluence connector Atlassian OAuth wiring.

Covers the OAuth wiring of the Atlassian Confluence connector on the existing
connector framework (auth-url, callback, token in the vault, minimal read-only
Confluence scopes) and the stated acceptance criteria:

  AC1 — Connecting Confluence runs the real Atlassian OAuth (3LO) flow and stores
        the token in the vault. The catalog tile leads to a working connection,
        not a dead end.
  AC4 — Only the Confluence spaces AgentIQ is granted are read; the connector
        requests no write access (enforced here at the OAuth-scope level — the
        scopes requested grant read-only access, and Confluence honours the
        granting principal's own space permissions).
  Plus the requirement: the requested scopes are surfaced to the admin during the
        consent step (the auth-url response echoes them, and Atlassian's own
        consent screen shows exactly these scopes).

These mirror ``test_teams_connector_oauth.py`` and reuse the session-scoped
``client`` fixture and the same PostgreSQL test DB provisioned by ``conftest.py``
(the ``credentials`` table exists there). Confluence shares its Atlassian OAuth
app with Jira (auth.atlassian.com) but uses distinct read-only Confluence scopes.
"""
from __future__ import annotations

import asyncio as _asyncio
import os as _os
from unittest.mock import AsyncMock as _AsyncMock
from unittest.mock import patch as _patch
from urllib.parse import parse_qs as _parse_qs
from urllib.parse import urlparse as _urlparse

from app.auth.vault import get_token

_AUTH_HEADERS = {"Authorization": "Bearer dev-token-change-me"}

# Minimal read-only Confluence scopes — the single source of truth for the
# assertions below mirrors app/auth/configs.py.
_EXPECTED_CONFLUENCE_SCOPES = [
    "read:confluence-content.all",
    "read:confluence-space.summary",
    "offline_access",
]

# Scope fragments that would grant WRITE / admin access to Confluence. None of
# these may ever appear in the Confluence connector's scopes (AC4: only granted
# spaces are read, read-only).
_FORBIDDEN_SCOPE_FRAGMENTS = (
    "write:",   # any write scope
    "delete:",  # any delete scope
    "manage:",  # space/app management
    "admin",    # admin scopes
)


# ---------------------------------------------------------------------------
# Vault key helper (Fernet) — one key per test process, consistent across the
# store (callback) and the read-back (get_token) within a test.
# ---------------------------------------------------------------------------
_VAULT_KEY = None


def _vault_env() -> dict:
    global _VAULT_KEY
    if _VAULT_KEY is None:
        from cryptography.fernet import Fernet

        _VAULT_KEY = Fernet.generate_key().decode()
    _os.environ["CREDENTIAL_VAULT_KEY"] = _VAULT_KEY
    return {
        "CREDENTIAL_VAULT_KEY": _VAULT_KEY,
        "CONFLUENCE_CLIENT_SECRET": "confluence-test-secret",
    }


def _soft_delete_credential(org_id: str, connector_id: str) -> None:
    """Soft-delete a credential row so the test leaves no residue (the app DB
    role has no DELETE; revoke/cleanup mark is_deleted=TRUE)."""
    from app import db as _db

    con = _db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE credentials SET is_deleted = TRUE "
            "WHERE org_id = %s AND connector_id = %s",
            (org_id, connector_id),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# AC4 / minimal scopes — config level
# ---------------------------------------------------------------------------


def test_confluence_scopes_are_minimal_and_read_only():
    """The Confluence connector requests only the minimal read-only scopes, and
    never any write / admin scope (AC4)."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    cfg = CONNECTOR_AUTH_CONFIGS["confluence"]
    assert cfg.connector_id == "confluence"
    assert cfg.flow == "authorization_code"
    assert cfg.scopes == _EXPECTED_CONFLUENCE_SCOPES, (
        "Confluence must request exactly the minimal read-only Confluence scopes"
    )
    for scope in cfg.scopes:
        for forbidden in _FORBIDDEN_SCOPE_FRAGMENTS:
            assert forbidden not in scope, (
                f"Confluence scope {scope!r} grants write / admin access (AC4 violation)"
            )


def test_confluence_oauth_targets_atlassian_endpoints():
    """OAuth wiring points at the real Atlassian identity endpoints (not a dead
    end), and the secret is referenced by env var name only."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    cfg = CONNECTOR_AUTH_CONFIGS["confluence"]
    auth_parsed = _urlparse(cfg.authorization_url)
    token_parsed = _urlparse(cfg.token_url)
    assert auth_parsed.netloc == "auth.atlassian.com"
    assert token_parsed.netloc == "auth.atlassian.com"
    assert cfg.secret_key == "CONFLUENCE_CLIENT_SECRET"  # env var name, never a raw secret
    # offline_access must be present so the access token can be auto-refreshed.
    assert "offline_access" in cfg.scopes
    # Atlassian exposes an RFC-7009 revocation endpoint.
    assert cfg.revocation_url is not None
    assert _urlparse(cfg.revocation_url).netloc == "auth.atlassian.com"


# ---------------------------------------------------------------------------
# AC1 / auth-url — real Atlassian OAuth URL with the minimal read-only scopes
# ---------------------------------------------------------------------------


def test_confluence_auth_url_is_real_atlassian_oauth_with_read_only_scopes(client):
    """GET /api/connectors/confluence/auth-url returns a real Atlassian authorize
    URL whose scope param is exactly the minimal read-only scopes (AC1, AC4)."""
    with _patch.dict(_os.environ, _vault_env()):
        resp = client.get("/api/connectors/confluence/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    assert body["connector_id"] == "confluence"

    parsed = _urlparse(body["auth_url"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.atlassian.com"

    qs = _parse_qs(parsed.query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"][0]  # non-empty
    assert qs["state"][0]  # non-empty CSRF nonce

    # The scope param must be exactly the minimal read-only scopes and contain no
    # write / admin scope.
    assert qs["scope"] == [" ".join(_EXPECTED_CONFLUENCE_SCOPES)]
    scope_str = qs["scope"][0]
    for forbidden in _FORBIDDEN_SCOPE_FRAGMENTS:
        assert forbidden not in scope_str, (
            f"write/admin scope {forbidden!r} leaked into the Confluence auth URL"
        )


def test_confluence_auth_url_surfaces_requested_scopes_to_admin(client):
    """The auth-url response echoes the requested scopes so the admin can see the
    permissions before consenting (R17-A2 / AT-462)."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    with _patch.dict(_os.environ, _vault_env()):
        resp = client.get("/api/connectors/confluence/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    assert "scopes" in body, "auth-url response must surface the requested scopes"
    assert body["scopes"] == list(CONNECTOR_AUTH_CONFIGS["confluence"].scopes)
    assert body["scopes"] == _EXPECTED_CONFLUENCE_SCOPES


def test_confluence_auth_url_requires_authentication(client):
    """Initiating the Confluence OAuth flow requires a Bearer token (admin-gated)."""
    resp = client.get("/api/connectors/confluence/auth-url")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# AC1 / callback — the real flow stores the Confluence token in the vault
# ---------------------------------------------------------------------------


def test_confluence_callback_stores_token_in_vault(client):
    """Completing the Confluence OAuth callback exchanges the code and stores the
    resulting Atlassian token (encrypted) in the credential vault, keyed by
    org+connector (AC1). Only the network token-exchange is mocked; storage is
    real."""
    org = "confluence-oauth-test-org"
    org_headers = {**_AUTH_HEADERS, "X-Org-Id": org}

    # Step 1 — initiate: get a real state nonce bound to this org.
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/confluence/auth-url", headers=org_headers)
    assert r.status_code == 200
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    # Step 2 — callback: Atlassian returns the access token + granted scopes.
    granted = {
        "access_token": "atlassian-conf-fake-access-token",
        "token_type": "Bearer",
        "scope": " ".join(_EXPECTED_CONFLUENCE_SCOPES),
        "refresh_token": "atlassian-conf-fake-refresh-token",
        "expires_in": 3600,
    }
    try:
        with _patch.dict(_os.environ, _vault_env()), _patch(
            "app.routes_connector_auth.exchange_code",
            new_callable=_AsyncMock,
            return_value=granted,
        ):
            resp = client.get(
                f"/api/connectors/oauth/callback?code=confluence-auth-code&state={state}",
                headers=_AUTH_HEADERS,
                follow_redirects=False,
            )

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "connected=confluence" in location
        assert "status=success" in location

        # The token is now retrievable from the vault for this org+connector and
        # carries exactly the granted read-only scopes.
        with _patch.dict(_os.environ, _vault_env()):
            record = _asyncio.run(get_token(org, "confluence"))
        assert record.access_token == "atlassian-conf-fake-access-token"
        assert record.scopes == _EXPECTED_CONFLUENCE_SCOPES
    finally:
        _soft_delete_credential(org, "confluence")


def test_confluence_callback_raw_db_row_is_encrypted(client):
    """The stored Confluence token must never appear in plaintext in the DB (AC1 —
    "stores the token in the vault", i.e. encrypted at rest)."""
    org = "confluence-oauth-enc-org"
    org_headers = {**_AUTH_HEADERS, "X-Org-Id": org}

    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/confluence/auth-url", headers=org_headers)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    plain = "atlassian-conf-super-secret-token"
    granted = {"access_token": plain, "scope": " ".join(_EXPECTED_CONFLUENCE_SCOPES)}
    try:
        with _patch.dict(_os.environ, _vault_env()), _patch(
            "app.routes_connector_auth.exchange_code",
            new_callable=_AsyncMock,
            return_value=granted,
        ):
            resp = client.get(
                f"/api/connectors/oauth/callback?code=c&state={state}",
                headers=_AUTH_HEADERS,
                follow_redirects=False,
            )
        assert resp.status_code == 302

        from app import db as _db

        con = _db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT access_token FROM credentials "
                "WHERE org_id = %s AND connector_id = %s AND is_deleted = FALSE",
                (org, "confluence"),
            )
            row = cur.fetchone()
        finally:
            con.close()

        assert row is not None
        assert plain not in (row[0] or ""), "raw DB row must not contain the plaintext token"
    finally:
        _soft_delete_credential(org, "confluence")
