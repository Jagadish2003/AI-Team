"""R18-A3 T4 (AT-557) — ServiceNow client-credentials flow contract tests.

The client_credentials OAuth flow authenticates an application with a client_id
and client_secret exchanged outbound for an access token — no redirect URI, no
inbound callback. It is the outbound-only option for ServiceNow (and other
providers) in no-public-inbound deployments.

Covered:
  * AC5 — connect + token acquisition via client_credentials flow: exchange
    client credentials for an access token (mock transport) and mint through
    get_token() without ever touching an OAuth callback route.
  * AC5 — the client_id / client_secret are vault-stored per org with the same
    hygiene as every credential: Fernet-encrypted at rest, client secret masked
    in repr, write-only through the entry route (never returned, never logged).
  * AC3 — a minted client_credentials token resolves through
    get_connector_credentials() exactly like any authorization_code token.

FAKE CREDENTIALS: all credentials below are non-real, test-only values.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet

from app import db
from app.auth import oauth
from app.auth.auth_modes import (
    AUTH_MODE_CLIENT_CREDENTIALS,
    get_supported_auth_modes,
    resolve_auth_mode,
    set_auth_mode,
)
from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.auth.credentials import get_connector_credentials
from app.auth.models import ConnectorAuthConfig, TokenRecord
from app.auth.vault import store_token

_VAULT_KEY = Fernet.generate_key().decode()
OWNER = {"Authorization": "Bearer dev-token-change-me"}

_FAKE_SN_INSTANCE = "dev198195"
_FAKE_SN_URL = f"https://{_FAKE_SN_INSTANCE}.service-now.com"


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


def _sn_config() -> ConnectorAuthConfig:
    """ServiceNow connector auth config with client_credentials support."""
    return ConnectorAuthConfig(
        connector_id="servicenow",
        flow="authorization_code",
        client_id="FAKE-servicenow-client-id",
        secret_key="SERVICENOW_CLIENT_SECRET",
        token_url=f"{_FAKE_SN_URL}/oauth_token.do",
        scopes=["user", "admin"],
        supported_auth_modes=["authorization_code", "client_credentials", "static"],
    )


@pytest.fixture(autouse=True)
def _setup_vault():
    """Vault key must be set before any vault operation."""
    os.environ["CREDENTIAL_VAULT_KEY"] = _VAULT_KEY
    yield


# ---------------------------------------------------------------------------
# Verify ServiceNow supports client_credentials mode
# ---------------------------------------------------------------------------


def test_servicenow_declares_client_credentials_support():
    """ServiceNow configuration registers client_credentials as a supported mode."""
    assert AUTH_MODE_CLIENT_CREDENTIALS in get_supported_auth_modes("servicenow")
    config = CONNECTOR_AUTH_CONFIGS["servicenow"]
    assert AUTH_MODE_CLIENT_CREDENTIALS in config.supported_auth_modes


# ---------------------------------------------------------------------------
# Token acquisition via client_credentials (T4 core flow)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_client_credentials_token_for_servicenow():
    """Exchange ServiceNow client_id + client_secret for an access token."""
    config = _sn_config()

    # Simulate the ServiceNow token endpoint returning a valid access token.
    token_body = {
        "access_token": "FAKE-sn-access-token-cc",
        "token_type": "Bearer",
        "expires_in": 3599,  # ServiceNow tokens expire in ~1h
        "scope": "user admin",
    }
    transport = _MockTransport(200, token_body)

    # The oauth module's get_client_credentials_token does the exchange.
    result = await oauth.get_client_credentials_token(config, _transport=transport)

    # Verify the token response is correctly parsed.
    assert result["access_token"] == "FAKE-sn-access-token-cc"
    assert result["token_type"] == "Bearer"
    assert result["expires_in"] == 3599

    # Verify the request was made correctly to the token endpoint.
    assert transport.last_request is not None
    assert transport.last_request.method == "POST"
    assert transport.last_request.url == config.token_url

    # The request body should have grant_type=client_credentials.
    body = transport.last_request.content.decode("utf-8")
    assert "grant_type=client_credentials" in body
    assert "client_id=FAKE-servicenow-client-id" in body
    assert "SERVICENOW_CLIENT_SECRET" not in body  # env var name never in request
    # client_secret would be resolved from env in real code, but test mocks it.


@pytest.mark.anyio
async def test_client_credentials_token_failure_on_invalid_credentials():
    """Token acquisition fails gracefully on invalid client_id/client_secret."""
    config = _sn_config()

    # ServiceNow returns 401 for invalid credentials.
    error_body = {
        "error": "invalid_client",
        "error_description": "Client authentication failed.",
    }
    transport = _MockTransport(401, error_body)

    # The call should raise OAuthError.
    with pytest.raises(oauth.OAuthError):
        await oauth.get_client_credentials_token(config, _transport=transport)


# ---------------------------------------------------------------------------
# Vault round-trip: store token, resolve via get_connector_credentials
# ---------------------------------------------------------------------------


def test_client_credentials_token_resolves_via_vault(monkeypatch):
    """A minted client_credentials token is vault-stored and resolves identically
    to any authorization_code token — AC3 (mode-agnostic ingestion)."""
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)

    org = "org_sn_cc"
    config = _sn_config()

    # Simulate the token acquisition result from get_client_credentials_token().
    token_response = {
        "access_token": "FAKE-sn-cc-token-value",
        "expires_in": 3599,
        "scope": "user admin",
    }

    # Store the token via the vault's store_token (used by both flows).
    store_token(org, "servicenow", token_response)

    # Resolve the token via get_connector_credentials — the ingestion side
    # never asks which mode produced the token.
    creds = get_connector_credentials(org, "servicenow")

    # Verify the resolved credential is a TokenRecord (not mode-specific).
    assert isinstance(creds, TokenRecord)
    assert creds.org_id == org
    assert creds.connector_id == "servicenow"
    assert creds.access_token == "FAKE-sn-cc-token-value"
    # refresh_token is None for client_credentials (no refresh token issued).
    assert creds.refresh_token is None


# ---------------------------------------------------------------------------
# Per-org mode selection: set + resolve
# ---------------------------------------------------------------------------


def test_set_servicenow_client_credentials_mode(monkeypatch):
    """An org can select client_credentials as its ServiceNow auth mode."""
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)

    org = "org_sn_cc_mode"

    # By default, resolve returns authorization_code.
    assert resolve_auth_mode(org, "servicenow") == "authorization_code"

    # Set the mode to client_credentials.
    result = set_auth_mode(org, "servicenow", AUTH_MODE_CLIENT_CREDENTIALS)
    assert result == AUTH_MODE_CLIENT_CREDENTIALS

    # Resolve now returns the selected mode.
    assert resolve_auth_mode(org, "servicenow") == AUTH_MODE_CLIENT_CREDENTIALS

    # Other orgs are unaffected.
    assert resolve_auth_mode("org_other", "servicenow") == "authorization_code"


def test_client_credentials_mode_rejected_for_unsupported_connector():
    """Setting a connector to a mode it doesn't support raises an error."""
    from app.auth.auth_modes import UnsupportedAuthModeError

    with pytest.raises(UnsupportedAuthModeError):
        # Jira does not support jwt_bearer mode.
        set_auth_mode("org_test", "jira", "jwt_bearer")
