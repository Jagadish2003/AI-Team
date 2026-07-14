"""R18-A3 T3 (AT-556) — Microsoft Graph client-credentials contract tests.

Teams and SharePoint gain a client_credentials auth mode: the Graph app
registration's own client_id + client_secret are exchanged OUTBOUND for a
service-identity access token — no browser redirect, no inbound callback. It is
the no-public-inbound option for the two Microsoft Graph connectors and the
correct grant for reading Graph data under a service identity.

Covered:
  * AC2 — connect + ingest via client-credentials with NO callback: request a
    Graph token with the .default resource scope (mock transport) and mint it
    through get_token() without ever touching an OAuth callback route; the token
    re-mints on expiry (client-credentials issues no refresh token), so ingestion
    continues.
  * AC5 — the minted token is vault-stored per org with the same hygiene as every
    credential: Fernet-encrypted at rest, and the connect route/response never
    return or log the token or the client secret.
  * AC3 — a minted client-credentials token resolves through
    get_connector_credentials() exactly like any authorization_code token.

FAKE CREDENTIALS: all credential values below are non-real, test-only values.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.fernet import Fernet

from app import db
from app.auth import oauth, vault
from app.auth.auth_modes import (
    AUTH_MODE_AUTHORIZATION_CODE,
    AUTH_MODE_CLIENT_CREDENTIALS,
    get_default_auth_mode,
    get_supported_auth_modes,
    resolve_auth_mode,
    set_auth_mode,
)
from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.auth.credentials import get_connector_credentials
from app.auth.models import ConnectorAuthConfig, ConnectorNotAuthenticatedError, TokenRecord
from app.auth.vault import store_token

_VAULT_KEY = Fernet.generate_key().decode()
OWNER = {"Authorization": "Bearer dev-token-change-me"}
VIEWER = {"Authorization": "Bearer viewer-token"}  # recognized VIEWER_JWT principal

_GRAPH_TOKEN_URL = "https://login.microsoftonline.com/fake-tenant-guid/oauth2/v2.0/token"
_GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


class _MockTransport(httpx.AsyncBaseTransport):
    """Returns a fixed status + JSON body; captures the last request."""

    def __init__(self, status_code: int, body: dict):
        self.last_request: httpx.Request | None = None
        self._status_code = status_code
        self._body = body

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        return httpx.Response(
            self._status_code,
            content=json.dumps(self._body).encode("utf-8"),
            headers={"content-type": "application/json"},
        )


def _graph_config(connector_id: str = "teams") -> ConnectorAuthConfig:
    """A Graph connector auth config with client_credentials support + .default scope."""
    return ConnectorAuthConfig(
        connector_id=connector_id,
        flow="authorization_code",
        client_id=f"FAKE-{connector_id}-client-id",
        secret_key="TEAMS_CLIENT_SECRET",
        token_url=_GRAPH_TOKEN_URL,
        # Delegated scopes (used by the browser flow) — deliberately DIFFERENT from
        # the client-credentials scope, to prove the .default override is applied.
        scopes=["offline_access", "Channel.ReadBasic.All", "ChannelMessage.Read.All"],
        client_credentials_scopes=[_GRAPH_DEFAULT_SCOPE],
        supported_auth_modes=["authorization_code", "client_credentials"],
    )


@pytest.fixture(autouse=True)
def _setup_vault(monkeypatch):
    """Vault key + connector secret must be set before any vault/token operation."""
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)
    monkeypatch.setenv("TEAMS_CLIENT_SECRET", "FAKE-teams-client-secret")
    monkeypatch.setenv("SHAREPOINT_CLIENT_SECRET", "FAKE-sharepoint-client-secret")
    yield


# ---------------------------------------------------------------------------
# Mode registration (Teams + SharePoint declare client_credentials)
# ---------------------------------------------------------------------------


def test_teams_sharepoint_declare_client_credentials_support():
    for connector_id in ("teams", "sharepoint"):
        modes = get_supported_auth_modes(connector_id)
        assert AUTH_MODE_CLIENT_CREDENTIALS in modes, connector_id
        # Default stays the browser flow — selecting nothing preserves it.
        assert get_default_auth_mode(connector_id) == AUTH_MODE_AUTHORIZATION_CODE
        config = CONNECTOR_AUTH_CONFIGS[connector_id]
        assert config.client_credentials_scopes == [_GRAPH_DEFAULT_SCOPE]


# ---------------------------------------------------------------------------
# Token acquisition — outbound, .default scope, no callback (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_client_credentials_token_uses_graph_default_scope():
    """The Graph client-credentials request uses the .default resource scope, the
    client_credentials grant, and never puts the secret env var name in the body."""
    config = _graph_config("teams")
    transport = _MockTransport(
        200,
        {"access_token": "FAKE-graph-token", "token_type": "Bearer", "expires_in": 3599},
    )

    result = await oauth.get_client_credentials_token(config, _transport=transport)
    assert result["access_token"] == "FAKE-graph-token"

    assert transport.last_request is not None
    assert transport.last_request.method == "POST"
    assert str(transport.last_request.url) == _GRAPH_TOKEN_URL

    body = transport.last_request.content.decode("utf-8")
    assert "grant_type=client_credentials" in body
    # The .default resource scope is sent — NOT the delegated scopes.
    assert "graph.microsoft.com" in body and "default" in body
    assert "Channel.ReadBasic.All" not in body
    # The env var NAME never appears in the request body (the value is resolved
    # from env; the name is not sent).
    assert "TEAMS_CLIENT_SECRET" not in body


@pytest.mark.anyio
async def test_client_credentials_token_failure_raises_oauth_error():
    """Token acquisition fails gracefully (OAuthError) on rejected credentials."""
    config = _graph_config("teams")
    transport = _MockTransport(
        401,
        {"error": "invalid_client", "error_description": "AADSTS7000215: bad secret"},
    )
    with pytest.raises(oauth.OAuthError):
        await oauth.get_client_credentials_token(config, _transport=transport)


@pytest.mark.anyio
async def test_default_scope_falls_back_to_scopes_when_unset():
    """A config WITHOUT client_credentials_scopes falls back to scopes (ServiceNow/
    SAP behaviour) — proves the .default override is additive, not a hard-coded rule."""
    config = _graph_config("teams")
    config.client_credentials_scopes = None
    transport = _MockTransport(200, {"access_token": "t", "expires_in": 3599})
    await oauth.get_client_credentials_token(config, _transport=transport)
    body = transport.last_request.content.decode("utf-8")
    # Now the delegated scopes are what gets sent.
    assert "Channel.ReadBasic.All" in body


# ---------------------------------------------------------------------------
# Vault round-trip: encrypted at rest + resolves mode-agnostically (AC5 + AC3)
# ---------------------------------------------------------------------------


def test_graph_token_encrypted_at_rest_and_resolves():
    """A minted Graph token is Fernet-encrypted at rest and resolves through the ONE
    credential path as a TokenRecord — AC5 (encrypted, write-only) + AC3."""
    org = "org_graph_cc"
    token_response = {
        "access_token": "FAKE-graph-cc-token-value",
        "token_type": "Bearer",
        "expires_in": 3599,
        # Microsoft client-credentials responses carry NO refresh_token.
    }
    store_token(org, "teams", token_response)

    # Stored column is ciphertext, not the plaintext token (AC5 — encrypted at rest).
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT access_token, refresh_token FROM credentials "
            "WHERE org_id=%s AND connector_id=%s AND is_deleted=FALSE",
            (org, "teams"),
        )
        row = cur.fetchone()
    finally:
        con.close()
    assert row is not None
    enc_access, enc_refresh = row
    assert "FAKE-graph-cc-token-value" not in enc_access
    assert Fernet(_VAULT_KEY.encode()).decrypt(enc_access.encode()).decode() == (
        "FAKE-graph-cc-token-value"
    )
    # client-credentials → no refresh token stored.
    assert enc_refresh is None

    # Mode-agnostic resolution (AC3): ingestion asks for the credential, not the mode.
    creds = get_connector_credentials(org, "teams")
    assert isinstance(creds, TokenRecord)
    assert creds.access_token == "FAKE-graph-cc-token-value"
    assert creds.refresh_token is None


# ---------------------------------------------------------------------------
# get_token mints + re-mints from the client-credentials grant (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_token_mints_when_client_credentials_mode_selected(monkeypatch):
    """With the org in client_credentials mode and no cached token, get_token mints
    one outbound — the client-credentials "connect" — and caches it (AC2)."""
    org = "org_graph_mint"
    await vault.revoke_token(org, "teams")
    set_auth_mode(org, "teams", AUTH_MODE_CLIENT_CREDENTIALS)

    calls = {"n": 0}

    async def _fake_cc(config, **kwargs):
        calls["n"] += 1
        return {"access_token": f"minted-graph-{calls['n']}", "expires_in": 3600}

    monkeypatch.setattr(vault._oauth, "get_client_credentials_token", _fake_cc)

    record = await vault.get_token(org, "teams")
    assert isinstance(record, TokenRecord)
    assert record.access_token == "minted-graph-1"
    assert calls["n"] == 1

    # A second read inside the validity window is served from cache — NO re-mint.
    record2 = await vault.get_token(org, "teams")
    assert record2.access_token == "minted-graph-1"
    assert calls["n"] == 1


@pytest.mark.anyio
async def test_get_token_remints_on_expiry(monkeypatch):
    """A cached client-credentials token near expiry carries no refresh token, so
    get_token re-mints outbound rather than raising (AC2 — ingest continues)."""
    org = "org_graph_remint"
    await vault.revoke_token(org, "sharepoint")
    set_auth_mode(org, "sharepoint", AUTH_MODE_CLIENT_CREDENTIALS)

    calls = {"n": 0}

    async def _fake_cc(config, **kwargs):
        calls["n"] += 1
        # expires_in=1 → immediately inside the refresh window, forcing a re-mint
        # on the next get_token (there is no refresh_token for client-credentials).
        return {"access_token": f"tok-{calls['n']}", "expires_in": 1}

    monkeypatch.setattr(vault._oauth, "get_client_credentials_token", _fake_cc)

    first = await vault.get_token(org, "sharepoint")
    assert first.access_token == "tok-1"
    second = await vault.get_token(org, "sharepoint")
    assert second.access_token == "tok-2"
    assert calls["n"] == 2


@pytest.mark.anyio
async def test_get_token_does_not_mint_for_authorization_code_mode(monkeypatch):
    """An org left in the default authorization_code mode is NOT silently minted a
    client-credentials token — an expired/absent browser token needs a reconnect."""
    org = "org_graph_authcode"
    await vault.revoke_token(org, "teams")
    # Default mode is authorization_code (nothing selected).
    assert resolve_auth_mode(org, "teams") == AUTH_MODE_AUTHORIZATION_CODE

    async def _fail(config, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("client-credentials mint must not fire in authorization_code mode")

    monkeypatch.setattr(vault._oauth, "get_client_credentials_token", _fail)

    with pytest.raises(ConnectorNotAuthenticatedError):
        await vault.get_token(org, "teams")


# ---------------------------------------------------------------------------
# Connect route — owner-only, outbound, no token leak (AC2/AC5)
# ---------------------------------------------------------------------------


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


@pytest.fixture(autouse=True, scope="module")
def _seed_roles():
    from app.rbac import seed_owner

    seed_owner("default", "dev-token-change-me")
    _seed_member("default", "viewer-token", "viewer")


def test_connect_route_requires_auth(client):
    assert client.post("/api/connectors/teams/client-credentials").status_code == 401


def test_connect_route_owner_only(client):
    r = client.post("/api/connectors/teams/client-credentials", headers=VIEWER)
    assert r.status_code == 403


def test_connect_route_rejects_unsupported_connector(client):
    # GitHub has no client_credentials mode.
    r = client.post("/api/connectors/github/client-credentials", headers=OWNER)
    assert r.status_code == 400


def test_connect_route_connects_and_never_returns_token(client, monkeypatch):
    import app.routes_connector_auth as routes

    async def _fake_cc(config, **kwargs):
        return {"access_token": "FAKE-route-graph-token", "expires_in": 3599}

    monkeypatch.setattr(routes, "get_client_credentials_token", _fake_cc)

    r = client.post("/api/connectors/teams/client-credentials", headers=OWNER)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["connected"] is True
    assert payload["connector_id"] == "teams"
    assert payload["auth_mode"] == "client_credentials"
    # The token must never be echoed in the response (AC5 — write-only).
    assert "FAKE-route-graph-token" not in r.text

    # It really landed in the vault and resolves as the connector credential (AC3/AC5).
    creds = get_connector_credentials("default", "teams")
    assert isinstance(creds, TokenRecord)
    assert creds.access_token == "FAKE-route-graph-token"

    # Cleanup so re-runs and other tests start clean: drop the token and reset the
    # org's Teams mode back to the default browser flow.
    cleanup = client.delete("/api/connectors/teams/token", headers=OWNER)
    assert cleanup.status_code in (200, 204)
    set_auth_mode("default", "teams", AUTH_MODE_AUTHORIZATION_CODE)


def test_connect_route_provider_failure_is_502(client, monkeypatch):
    import app.routes_connector_auth as routes

    async def _fail(config, **kwargs):
        raise oauth.OAuthError("teams", 401, detail="invalid_client")

    monkeypatch.setattr(routes, "get_client_credentials_token", _fail)

    r = client.post("/api/connectors/teams/client-credentials", headers=OWNER)
    assert r.status_code == 502
    # The provider error code must not leak the (absent) secret; generic detail only.
    assert "secret" not in r.text.lower()
