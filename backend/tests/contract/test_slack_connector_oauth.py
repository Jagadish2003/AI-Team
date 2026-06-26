"""Contract tests for R16-A2 / AT-420 (T5) — Slack connector OAuth wiring.

Covers the OAuth wiring of the Slack connector on the existing connector
framework (auth-url, callback, token in the vault, minimal public-channels-only
scopes) and the stated acceptance criteria:

  AC1 — Connecting Slack runs the real OAuth flow and stores the token in the
        vault. The catalog tile leads to a working connection, not a dead end.
  AC4 — Only public channels AgentIQ has been invited to are read; private
        channels and DMs are never accessed (enforced here at the OAuth-scope
        level — the scopes requested grant no private-channel / DM access).
  Plus the requirement: the requested scopes are surfaced to the admin during the
        consent step (the auth-url response echoes them, and Slack's own consent
        screen shows exactly these scopes).

These reuse the session-scoped ``client`` fixture and the same PostgreSQL test DB
provisioned by ``conftest.py`` (the ``credentials`` table exists there).
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

# Minimal public-channels-only read scopes — the single source of truth for the
# assertions below mirrors app/auth/configs.py.
_EXPECTED_SLACK_SCOPES = ["channels:read", "channels:history"]

# Scope prefixes that would grant access to private channels, DMs, group DMs, or
# write access. None of these may ever appear in the Slack connector's scopes
# (AC4: private channels and DMs are never accessed).
_FORBIDDEN_SCOPE_PREFIXES = ("groups:", "im:", "mpim:", "chat:write", "channels:write")


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
    return {"CREDENTIAL_VAULT_KEY": _VAULT_KEY, "SLACK_CLIENT_SECRET": "slack-test-secret"}


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


def test_slack_scopes_are_minimal_and_public_only():
    """The Slack connector requests only the minimal public-channel read scopes,
    and never any private-channel / DM / write scope (AC4)."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    cfg = CONNECTOR_AUTH_CONFIGS["slack"]
    assert cfg.connector_id == "slack"
    assert cfg.flow == "authorization_code"
    assert cfg.scopes == _EXPECTED_SLACK_SCOPES, (
        "Slack must request exactly the minimal public-channel read scopes"
    )
    for scope in cfg.scopes:
        for forbidden in _FORBIDDEN_SCOPE_PREFIXES:
            assert not scope.startswith(forbidden), (
                f"Slack scope {scope!r} grants private/DM/write access (AC4 violation)"
            )


def test_slack_oauth_targets_the_real_slack_endpoints():
    """OAuth wiring points at the real Slack OAuth endpoints (not a dead end)."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    cfg = CONNECTOR_AUTH_CONFIGS["slack"]
    assert cfg.authorization_url == "https://slack.com/oauth/v2/authorize"
    assert cfg.token_url == "https://slack.com/api/oauth.v2.access"
    assert cfg.secret_key == "SLACK_CLIENT_SECRET"  # env var name, never a raw secret


# ---------------------------------------------------------------------------
# AC1 / auth-url — real Slack OAuth URL with the minimal public-only scopes
# ---------------------------------------------------------------------------


def test_slack_auth_url_is_real_slack_oauth_with_public_only_scopes(client):
    """GET /api/connectors/slack/auth-url returns a real Slack authorize URL whose
    scope param is exactly the minimal public-channel read scopes (AC1, AC4)."""
    with _patch.dict(_os.environ, _vault_env()):
        resp = client.get("/api/connectors/slack/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    assert body["connector_id"] == "slack"

    parsed = _urlparse(body["auth_url"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "slack.com"
    assert parsed.path == "/oauth/v2/authorize"

    qs = _parse_qs(parsed.query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"][0]  # non-empty
    assert qs["state"][0]  # non-empty CSRF nonce

    # The scope param must be exactly the minimal public-channel read scopes and
    # contain no private/DM/write scope.
    assert qs["scope"] == [" ".join(_EXPECTED_SLACK_SCOPES)]
    scope_str = qs["scope"][0]
    for forbidden in _FORBIDDEN_SCOPE_PREFIXES:
        assert forbidden not in scope_str, (
            f"private/DM/write scope {forbidden!r} leaked into the Slack auth URL"
        )


def test_slack_auth_url_surfaces_requested_scopes_to_admin(client):
    """The auth-url response echoes the requested scopes so the admin can see the
    permissions before consenting (R16-A2 §3 / AT-420)."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    with _patch.dict(_os.environ, _vault_env()):
        resp = client.get("/api/connectors/slack/auth-url", headers=_AUTH_HEADERS)
    assert resp.status_code == 200

    body = resp.json()
    assert "scopes" in body, "auth-url response must surface the requested scopes"
    assert body["scopes"] == list(CONNECTOR_AUTH_CONFIGS["slack"].scopes)
    assert body["scopes"] == _EXPECTED_SLACK_SCOPES


def test_slack_auth_url_requires_authentication(client):
    """Initiating the Slack OAuth flow requires a Bearer token (admin-gated)."""
    resp = client.get("/api/connectors/slack/auth-url")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# AC1 / callback — the real flow stores the Slack token in the vault
# ---------------------------------------------------------------------------


def test_slack_callback_stores_token_in_vault(client):
    """Completing the Slack OAuth callback exchanges the code and stores the
    resulting token (encrypted) in the credential vault, keyed by org+connector
    (AC1). Only the network token-exchange is mocked; storage is real."""
    org = "slack-oauth-test-org"
    org_headers = {**_AUTH_HEADERS, "X-Org-Id": org}

    # Step 1 — initiate: get a real state nonce bound to this org.
    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/slack/auth-url", headers=org_headers)
    assert r.status_code == 200
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    # Step 2 — callback: Slack returns the bot token + granted scopes. The
    # oauth.v2.access bot-token shape carries access_token + scope at the top
    # level, which is exactly what store_token reads.
    granted = {
        "access_token": "xoxb-slack-fake-bot-token",
        "token_type": "bot",
        "scope": " ".join(_EXPECTED_SLACK_SCOPES),
        "expires_in": 3600,
    }
    try:
        with _patch.dict(_os.environ, _vault_env()), _patch(
            "app.routes_connector_auth.exchange_code",
            new_callable=_AsyncMock,
            return_value=granted,
        ):
            resp = client.get(
                f"/api/connectors/oauth/callback?code=slack-auth-code&state={state}",
                headers=_AUTH_HEADERS,
                follow_redirects=False,
            )

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "connected=slack" in location
        assert "status=success" in location

        # The token is now retrievable from the vault for this org+connector and
        # carries exactly the granted public-channel scopes.
        with _patch.dict(_os.environ, _vault_env()):
            record = _asyncio.run(get_token(org, "slack"))
        assert record.access_token == "xoxb-slack-fake-bot-token"
        assert record.scopes == _EXPECTED_SLACK_SCOPES
    finally:
        _soft_delete_credential(org, "slack")


def test_slack_callback_raw_db_row_is_encrypted(client):
    """The stored Slack token must never appear in plaintext in the DB (AC1 —
    "stores the token in the vault", i.e. encrypted at rest)."""
    org = "slack-oauth-enc-org"
    org_headers = {**_AUTH_HEADERS, "X-Org-Id": org}

    with _patch.dict(_os.environ, _vault_env()):
        r = client.get("/api/connectors/slack/auth-url", headers=org_headers)
    state = _parse_qs(_urlparse(r.json()["auth_url"]).query)["state"][0]

    plain = "xoxb-super-secret-slack-token"
    granted = {"access_token": plain, "scope": " ".join(_EXPECTED_SLACK_SCOPES)}
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
                (org, "slack"),
            )
            row = cur.fetchone()
        finally:
            con.close()

        assert row is not None
        assert plain not in (row[0] or ""), "raw DB row must not contain the plaintext token"
    finally:
        _soft_delete_credential(org, "slack")
