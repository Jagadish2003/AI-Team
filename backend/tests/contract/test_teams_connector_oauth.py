"""Contract tests for R17-A1 / AT-434 (T5) — Teams connector Microsoft Graph OAuth wiring.

Covers the OAuth wiring of the Microsoft Teams connector on the existing
connector framework (auth-url, callback, token in the vault, minimal
channels-only Graph scopes) and the stated acceptance criteria:

  AC1 — Connecting Teams runs the real Microsoft Graph OAuth flow and stores the
        token in the vault. The catalog tile leads to a working connection, not a
        dead end.
  AC4 — Only channels AgentIQ is granted are read; private chats and DMs are
        never accessed (enforced here at the OAuth-scope level — the scopes
        requested grant no chat / DM / write access).
  Plus the requirement: the requested scopes are surfaced to the admin during the
        consent step (the auth-url response echoes them, and Microsoft's own
        consent screen shows exactly these scopes).

These mirror ``test_slack_connector_oauth.py`` and reuse the session-scoped
``client`` fixture and the same PostgreSQL test DB provisioned by ``conftest.py``
(the ``credentials`` table exists there).
"""
from __future__ import annotations

import asyncio as _asyncio
import os as _os
from unittest.mock import AsyncMock as _AsyncMock
from unittest.mock import patch as _patch
from urllib.parse import parse_qs as _parse_qs
from urllib.parse import urlparse as _urlparse

from app.auth.vault import get_token
from app.rbac import seed_owner

_AUTH_HEADERS = {"Authorization": "Bearer dev-token-change-me"}

# Minimal channels-only read scopes — the single source of truth for the
# assertions below mirrors app/auth/configs.py.
_EXPECTED_TEAMS_SCOPES = [
    "offline_access",
    "Team.ReadBasic.All",
    "Channel.ReadBasic.All",
    "ChannelMessage.Read.All",
]

# Scope fragments that would grant access to private chats / DMs (the Graph
# ``Chat.*`` / ``ChatMessage.*`` surface), mail/files, or any write access. None
# of these may ever appear in the Teams connector's scopes (AC4: private chats and
# DMs are never accessed; the connector only reads channels).
_FORBIDDEN_SCOPE_FRAGMENTS = (
    "Chat.",          # 1:1 / group-DM read (Chat.Read, Chat.ReadWrite, …)
    "ChatMessage.",   # DM message content
    "ReadWrite",      # any write scope
    ".Send",          # message-send scope
    "Mail.",          # mailbox access
    "Files.",         # OneDrive / SharePoint file access
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
    return {"CREDENTIAL_VAULT_KEY": _VAULT_KEY, "TEAMS_CLIENT_SECRET": "teams-test-secret"}


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


def test_teams_scopes_are_minimal_and_channel_only():
    """The Teams connector requests only the minimal channel read scopes, and
    never any private-chat / DM / write scope (AC4)."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    cfg = CONNECTOR_AUTH_CONFIGS["teams"]
    assert cfg.connector_id == "teams"
    assert cfg.flow == "authorization_code"
    assert cfg.scopes == _EXPECTED_TEAMS_SCOPES, (
        "Teams must request exactly the minimal channel read scopes"
    )
    for scope in cfg.scopes:
        for forbidden in _FORBIDDEN_SCOPE_FRAGMENTS:
            assert forbidden not in scope, (
                f"Teams scope {scope!r} grants chat/DM/write access (AC4 violation)"
            )


def test_teams_oauth_targets_microsoft_identity_endpoints():
    """OAuth wiring points at the real Microsoft identity platform endpoints
    (not a dead end), and the secret is referenced by env var name only."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    cfg = CONNECTOR_AUTH_CONFIGS["teams"]
    auth_parsed = _urlparse(cfg.authorization_url)
    token_parsed = _urlparse(cfg.token_url)
    assert auth_parsed.netloc == "login.microsoftonline.com"
    assert token_parsed.netloc == "login.microsoftonline.com"
    assert auth_parsed.path.endswith("/oauth2/v2.0/authorize")
    assert token_parsed.path.endswith("/oauth2/v2.0/token")
    assert cfg.secret_key == "TEAMS_CLIENT_SECRET"  # env var name, never a raw secret
    # offline_access must be present so the access token can be auto-refreshed.
    assert "offline_access" in cfg.scopes
    # No revocation endpoint for Microsoft identity — vault deletion handles revoke.
    assert cfg.revocation_url is None


# ---------------------------------------------------------------------------
# AC1 / auth-url — real Microsoft OAuth URL with the minimal channels-only scopes
# ---------------------------------------------------------------------------


def test_teams_auth_url_is_real_ms_oauth_with_channel_only_scopes(client):
    """GET /api/connectors/teams/auth-url returns a real Microsoft authorize URL
    whose scope param is exactly the minimal channel read scopes (AC1, AC4)."""
    with _patch.dict(_os.environ, _vault_env()):
        resp = client.get("/api/connectors/teams/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    assert body["connector_id"] == "teams"

    parsed = _urlparse(body["auth_url"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "login.microsoftonline.com"
    assert parsed.path.endswith("/oauth2/v2.0/authorize")

    qs = _parse_qs(parsed.query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"][0]  # non-empty
    assert qs["state"][0]  # non-empty CSRF nonce

    # The scope param must be exactly the minimal channel read scopes and contain
    # no chat/DM/write scope.
    assert qs["scope"] == [" ".join(_EXPECTED_TEAMS_SCOPES)]
    scope_str = qs["scope"][0]
    for forbidden in _FORBIDDEN_SCOPE_FRAGMENTS:
        assert forbidden not in scope_str, (
            f"chat/DM/write scope {forbidden!r} leaked into the Teams auth URL"
        )


def test_teams_auth_url_surfaces_requested_scopes_to_admin(client):
    """The auth-url response echoes the requested scopes so the admin can see the
    permissions before consenting (R17-A1 / AT-434)."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    with _patch.dict(_os.environ, _vault_env()):
        resp = client.get("/api/connectors/teams/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    assert "scopes" in body, "auth-url response must surface the requested scopes"
    assert body["scopes"] == list(CONNECTOR_AUTH_CONFIGS["teams"].scopes)
    assert body["scopes"] == _EXPECTED_TEAMS_SCOPES


def test_teams_auth_url_requires_authentication(client):
    """Initiating the Teams OAuth flow requires a Bearer token (admin-gated)."""
    resp = client.get("/api/connectors/teams/auth-url")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# AC1 / callback — the real flow stores the Teams token in the vault
# ---------------------------------------------------------------------------


def test_teams_callback_stores_token_in_vault(client):
    """Completing the Teams OAuth callback exchanges the code and stores the
    resulting Microsoft Graph token (encrypted) in the credential vault, keyed by
    org+connector (AC1). Only the network token-exchange is mocked; storage is
    real."""
    org = "teams-oauth-test-org"
    seed_owner(org, "dev-token-change-me")  # csc RBAC: role-gated auth-url needs a role; seed owner to test tenancy, not RBAC
    org_headers = {**_AUTH_HEADERS, "X-Org-Id": org}

    # Step 1 — initiate: get a real state nonce bound to this org.
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/teams/auth-url", headers=org_headers)
    assert r.status_code == 200
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    # Step 2 — callback: Microsoft returns the access token + granted scopes. The
    # v2.0 token response carries access_token + scope at the top level, which is
    # exactly what store_token reads.
    granted = {
        "access_token": "ms-graph-fake-access-token",
        "token_type": "Bearer",
        "scope": " ".join(_EXPECTED_TEAMS_SCOPES),
        "refresh_token": "ms-graph-fake-refresh-token",
        "expires_in": 3600,
    }
    try:
        with _patch.dict(_os.environ, _vault_env()), _patch(
            "app.routes_connector_auth.exchange_code",
            new_callable=_AsyncMock,
            return_value=granted,
        ):
            resp = client.get(
                f"/api/connectors/oauth/callback?code=teams-auth-code&state={state}",
                headers=_AUTH_HEADERS,
                follow_redirects=False,
            )

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "connected=teams" in location
        assert "status=success" in location

        # The token is now retrievable from the vault for this org+connector and
        # carries exactly the granted channel scopes.
        with _patch.dict(_os.environ, _vault_env()):
            record = _asyncio.run(get_token(org, "teams"))
        assert record.access_token == "ms-graph-fake-access-token"
        assert record.scopes == _EXPECTED_TEAMS_SCOPES
    finally:
        _soft_delete_credential(org, "teams")


def test_teams_callback_raw_db_row_is_encrypted(client):
    """The stored Teams token must never appear in plaintext in the DB (AC1 —
    "stores the token in the vault", i.e. encrypted at rest)."""
    org = "teams-oauth-enc-org"
    seed_owner(org, "dev-token-change-me")  # csc RBAC: role-gated auth-url needs a role; seed owner to test tenancy, not RBAC
    org_headers = {**_AUTH_HEADERS, "X-Org-Id": org}

    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/teams/auth-url", headers=org_headers)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    plain = "ms-graph-super-secret-token"
    granted = {"access_token": plain, "scope": " ".join(_EXPECTED_TEAMS_SCOPES)}
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
                (org, "teams"),
            )
            row = cur.fetchone()
        finally:
            con.close()

        assert row is not None
        assert plain not in (row[0] or ""), "raw DB row must not contain the plaintext token"
    finally:
        _soft_delete_credential(org, "teams")
