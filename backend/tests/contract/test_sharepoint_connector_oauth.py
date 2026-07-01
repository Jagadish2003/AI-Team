"""Contract tests for R17-A2 / AT-462 (T5) — SharePoint connector Microsoft Graph OAuth wiring.

Covers the OAuth wiring of the Microsoft SharePoint connector on the existing
connector framework (auth-url, callback, token in the vault, minimal read-only
Microsoft Graph scopes) and the stated acceptance criteria:

  AC1 — Connecting SharePoint runs the real Microsoft Graph OAuth flow and stores
        the token in the vault. The catalog tile leads to a working connection,
        not a dead end.
  AC4 — Only sites/document libraries AgentIQ is granted are read; the connector
        requests no write access (enforced here at the OAuth-scope level — the
        scopes requested grant read-only access to granted site collections only).
  Plus the requirement: the requested scopes are surfaced to the admin during the
        consent step (the auth-url response echoes them, and Microsoft's own
        consent screen shows exactly these scopes).

These mirror ``test_teams_connector_oauth.py`` (SharePoint reuses the same Graph
auth plumbing per R17-A2 §6) and reuse the session-scoped ``client`` fixture and
the same PostgreSQL test DB provisioned by ``conftest.py`` (the ``credentials``
table exists there).
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

# Minimal read-only Microsoft Graph scopes — the single source of truth for the
# assertions below mirrors app/auth/configs.py.
_EXPECTED_SHAREPOINT_SCOPES = [
    "offline_access",
    "Sites.Read.All",
]

# Scope fragments that would grant WRITE access to SharePoint, full control, or
# reach beyond the granted site collections. None of these may ever appear in the
# SharePoint connector's scopes (AC4: only granted sites are read, read-only).
_FORBIDDEN_SCOPE_FRAGMENTS = (
    "ReadWrite",        # any write scope (Sites.ReadWrite.All, Files.ReadWrite.All, …)
    "Manage",           # Sites.Manage.All
    "FullControl",      # Sites.FullControl.All
    ".Send",            # message-send scope
    "Files.",           # OneDrive / broad file access beyond the granted sites
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
        "SHAREPOINT_CLIENT_SECRET": "sharepoint-test-secret",
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


def test_sharepoint_scopes_are_minimal_and_read_only():
    """The SharePoint connector requests only the minimal read-only scopes, and
    never any write / full-control scope (AC4)."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    cfg = CONNECTOR_AUTH_CONFIGS["sharepoint"]
    assert cfg.connector_id == "sharepoint"
    assert cfg.flow == "authorization_code"
    assert cfg.scopes == _EXPECTED_SHAREPOINT_SCOPES, (
        "SharePoint must request exactly the minimal read-only Graph scopes"
    )
    for scope in cfg.scopes:
        for forbidden in _FORBIDDEN_SCOPE_FRAGMENTS:
            assert forbidden not in scope, (
                f"SharePoint scope {scope!r} grants write / broad access (AC4 violation)"
            )


def test_sharepoint_oauth_targets_microsoft_identity_endpoints():
    """OAuth wiring points at the real Microsoft identity platform endpoints
    (not a dead end), and the secret is referenced by env var name only."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    cfg = CONNECTOR_AUTH_CONFIGS["sharepoint"]
    auth_parsed = _urlparse(cfg.authorization_url)
    token_parsed = _urlparse(cfg.token_url)
    assert auth_parsed.netloc == "login.microsoftonline.com"
    assert token_parsed.netloc == "login.microsoftonline.com"
    assert auth_parsed.path.endswith("/oauth2/v2.0/authorize")
    assert token_parsed.path.endswith("/oauth2/v2.0/token")
    assert cfg.secret_key == "SHAREPOINT_CLIENT_SECRET"  # env var name, never a raw secret
    # offline_access must be present so the access token can be auto-refreshed.
    assert "offline_access" in cfg.scopes
    # No revocation endpoint for Microsoft identity — vault deletion handles revoke.
    assert cfg.revocation_url is None


# ---------------------------------------------------------------------------
# AC1 / auth-url — real Microsoft OAuth URL with the minimal read-only scopes
# ---------------------------------------------------------------------------


def test_sharepoint_auth_url_is_real_ms_oauth_with_read_only_scopes(client):
    """GET /api/connectors/sharepoint/auth-url returns a real Microsoft authorize
    URL whose scope param is exactly the minimal read-only scopes (AC1, AC4)."""
    with _patch.dict(_os.environ, _vault_env()):
        resp = client.get("/api/connectors/sharepoint/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    assert body["connector_id"] == "sharepoint"

    parsed = _urlparse(body["auth_url"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "login.microsoftonline.com"
    assert parsed.path.endswith("/oauth2/v2.0/authorize")

    qs = _parse_qs(parsed.query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"][0]  # non-empty
    assert qs["state"][0]  # non-empty CSRF nonce

    # The scope param must be exactly the minimal read-only scopes and contain no
    # write / full-control scope.
    assert qs["scope"] == [" ".join(_EXPECTED_SHAREPOINT_SCOPES)]
    scope_str = qs["scope"][0]
    for forbidden in _FORBIDDEN_SCOPE_FRAGMENTS:
        assert forbidden not in scope_str, (
            f"write/broad scope {forbidden!r} leaked into the SharePoint auth URL"
        )


def test_sharepoint_auth_url_surfaces_requested_scopes_to_admin(client):
    """The auth-url response echoes the requested scopes so the admin can see the
    permissions before consenting (R17-A2 / AT-462)."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    with _patch.dict(_os.environ, _vault_env()):
        resp = client.get("/api/connectors/sharepoint/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    assert "scopes" in body, "auth-url response must surface the requested scopes"
    assert body["scopes"] == list(CONNECTOR_AUTH_CONFIGS["sharepoint"].scopes)
    assert body["scopes"] == _EXPECTED_SHAREPOINT_SCOPES


def test_sharepoint_auth_url_requires_authentication(client):
    """Initiating the SharePoint OAuth flow requires a Bearer token (admin-gated)."""
    resp = client.get("/api/connectors/sharepoint/auth-url")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# AC1 / callback — the real flow stores the SharePoint token in the vault
# ---------------------------------------------------------------------------


def test_sharepoint_callback_stores_token_in_vault(client):
    """Completing the SharePoint OAuth callback exchanges the code and stores the
    resulting Microsoft Graph token (encrypted) in the credential vault, keyed by
    org+connector (AC1). Only the network token-exchange is mocked; storage is
    real."""
    org = "sharepoint-oauth-test-org"
    org_headers = {**_AUTH_HEADERS, "X-Org-Id": org}

    # Step 1 — initiate: get a real state nonce bound to this org.
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/sharepoint/auth-url", headers=org_headers)
    assert r.status_code == 200
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    # Step 2 — callback: Microsoft returns the access token + granted scopes. The
    # v2.0 token response carries access_token + scope at the top level, which is
    # exactly what store_token reads.
    granted = {
        "access_token": "ms-graph-sp-fake-access-token",
        "token_type": "Bearer",
        "scope": " ".join(_EXPECTED_SHAREPOINT_SCOPES),
        "refresh_token": "ms-graph-sp-fake-refresh-token",
        "expires_in": 3600,
    }
    try:
        with _patch.dict(_os.environ, _vault_env()), _patch(
            "app.routes_connector_auth.exchange_code",
            new_callable=_AsyncMock,
            return_value=granted,
        ):
            resp = client.get(
                f"/api/connectors/oauth/callback?code=sharepoint-auth-code&state={state}",
                headers=_AUTH_HEADERS,
                follow_redirects=False,
            )

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "connected=sharepoint" in location
        assert "status=success" in location

        # The token is now retrievable from the vault for this org+connector and
        # carries exactly the granted read-only scopes.
        with _patch.dict(_os.environ, _vault_env()):
            record = _asyncio.run(get_token(org, "sharepoint"))
        assert record.access_token == "ms-graph-sp-fake-access-token"
        assert record.scopes == _EXPECTED_SHAREPOINT_SCOPES
    finally:
        _soft_delete_credential(org, "sharepoint")


def test_sharepoint_callback_raw_db_row_is_encrypted(client):
    """The stored SharePoint token must never appear in plaintext in the DB (AC1 —
    "stores the token in the vault", i.e. encrypted at rest)."""
    org = "sharepoint-oauth-enc-org"
    org_headers = {**_AUTH_HEADERS, "X-Org-Id": org}

    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/sharepoint/auth-url", headers=org_headers)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    plain = "ms-graph-sp-super-secret-token"
    granted = {"access_token": plain, "scope": " ".join(_EXPECTED_SHAREPOINT_SCOPES)}
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
                (org, "sharepoint"),
            )
            row = cur.fetchone()
        finally:
            con.close()

        assert row is not None
        assert plain not in (row[0] or ""), "raw DB row must not contain the plaintext token"
    finally:
        _soft_delete_credential(org, "sharepoint")
