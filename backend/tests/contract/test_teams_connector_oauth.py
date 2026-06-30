"""Contract tests for R17-A1 / AT-436 (T7) — Teams connector OAuth wiring.

Covers the OAuth wiring that makes the Microsoft Teams catalog tile lead to a
working connection end to end (the frontend enables the tile; this verifies the
backend it drives), and the stated acceptance criterion:

  AC1 — Connecting Teams runs the real Microsoft Graph OAuth flow; the auth-url
        the tile redirects to is a real Microsoft identity-platform authorize URL
        (not a dead end), and the requested scopes are surfaced to the admin.
  AC4 — Only granted channels are read; private chats and DMs are never accessed
        — enforced here at the OAuth-scope level (no Chat.* / write / send scopes
        are ever requested).

The Teams Microsoft Graph OAuth config is the AT-434 dependency
(``app/auth/configs.py`` ``CONNECTOR_AUTH_CONFIGS['teams']``).
"""
from __future__ import annotations

import os as _os
from urllib.parse import parse_qs as _parse_qs
from urllib.parse import urlparse as _urlparse

_AUTH_HEADERS = {"Authorization": "Bearer dev-token-change-me"}

# Least-privilege, read-only Microsoft Graph scopes — the single source of truth
# for the assertions below mirrors app/auth/configs.py.
_EXPECTED_TEAMS_SCOPES = [
    "offline_access",
    "https://graph.microsoft.com/Team.ReadBasic.All",
    "https://graph.microsoft.com/Channel.ReadBasic.All",
    "https://graph.microsoft.com/ChannelMessage.Read.All",
]

# Scope fragments that would grant access to private chats / DMs or write access.
# None of these may ever appear in the Teams connector's scopes (AC4: private
# chats and DMs are never accessed; the connector is read-only).
_FORBIDDEN_SCOPE_FRAGMENTS = (
    "Chat.",            # 1:1 and group direct messages
    "ChannelMessage.Send",
    "ReadWrite",
    ".Send",
    "Mail.",
    "Files.",
)


# ---------------------------------------------------------------------------
# AC4 / minimal scopes — config level
# ---------------------------------------------------------------------------


def test_teams_scopes_are_minimal_and_no_dm_or_write():
    """The Teams connector requests only the minimal read-only channel scopes,
    and never any private-chat/DM or write scope (AC4)."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    cfg = CONNECTOR_AUTH_CONFIGS["teams"]
    assert cfg.connector_id == "teams"
    assert cfg.flow == "authorization_code"
    assert cfg.scopes == _EXPECTED_TEAMS_SCOPES, (
        "Teams must request exactly the minimal read-only channel scopes"
    )
    for scope in cfg.scopes:
        for forbidden in _FORBIDDEN_SCOPE_FRAGMENTS:
            assert forbidden not in scope, (
                f"Teams scope {scope!r} grants private-chat/DM/write access (AC4 violation)"
            )


def test_teams_oauth_targets_microsoft_identity_endpoints():
    """OAuth wiring points at the real Microsoft identity-platform endpoints
    (Microsoft Graph), not a dead end."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    cfg = CONNECTOR_AUTH_CONFIGS["teams"]
    assert cfg.authorization_url.startswith("https://login.microsoftonline.com/")
    assert cfg.authorization_url.endswith("/oauth2/v2.0/authorize")
    assert cfg.token_url.startswith("https://login.microsoftonline.com/")
    assert cfg.token_url.endswith("/oauth2/v2.0/token")
    # env var name, never a raw secret; follows the {CONNECTOR}_CLIENT_SECRET rule.
    assert cfg.secret_key == "TEAMS_CLIENT_SECRET"


# ---------------------------------------------------------------------------
# AC1 / auth-url — real Microsoft authorize URL with the least-privilege scopes
# ---------------------------------------------------------------------------


def test_teams_auth_url_is_real_microsoft_oauth_with_readonly_scopes(client):
    """GET /api/connectors/teams/auth-url returns a real Microsoft authorize URL
    whose scope param is exactly the least-privilege read-only channel scopes,
    with no private-chat/DM/write scope (AC1, AC4)."""
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

    # The scope param must be exactly the least-privilege scopes (space-joined)
    # and contain no private-chat/DM/write scope.
    assert qs["scope"] == [" ".join(_EXPECTED_TEAMS_SCOPES)]
    scope_str = qs["scope"][0]
    for forbidden in _FORBIDDEN_SCOPE_FRAGMENTS:
        assert forbidden not in scope_str, (
            f"private-chat/DM/write scope {forbidden!r} leaked into the Teams auth URL"
        )


def test_teams_auth_url_surfaces_requested_scopes_to_admin(client):
    """The auth-url response echoes the requested scopes so the admin can see the
    permissions before consenting (R17-A1 §3)."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

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
