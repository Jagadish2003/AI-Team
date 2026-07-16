"""R18-A3 T2 (AT-555) — Salesforce JWT bearer flow contract tests.

The JWT bearer flow authenticates a connected app with a SIGNED ASSERTION
exchanged outbound for an access token — no client secret, no redirect URI, no
inbound callback. It is Salesforce's standard headless integration path and the
no-public-inbound option for the connector.

Covered:
  * AC1 — connect + ingest via JWT bearer with NO callback: build a signed
    assertion, POST it for a token (mock transport), and mint through get_token()
    without ever touching an OAuth callback route; re-assertion mints again on
    expiry (the "refresh").
  * AC5 — the cert private key is vault-stored per org with the same hygiene as
    every credential: Fernet-encrypted at rest, masked in repr, write-only through
    the entry route (never returned, never logged).
  * AC3 — a minted JWT bearer token resolves through get_connector_credentials()
    exactly like any OAuth token; the private key is never mis-resolved as one.

FAKE CREDENTIALS: the RSA key below is generated per test run and is not a real
Salesforce credential.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import db
from app.auth import oauth, vault
from app.auth.models import ConnectorAuthConfig, StaticCredentialRecord, TokenRecord

_VAULT_KEY = Fernet.generate_key().decode()
OWNER = {"Authorization": "Bearer dev-token-change-me"}
VIEWER = {"Authorization": "Bearer viewer-token"}  # recognized VIEWER_JWT principal

_FAKE_SF_USER = "svc-agentiq@example.com.sandbox"
_FAKE_LOGIN_URL = "https://test.salesforce.com"


# --- test RSA keypair (generated once per module) ---------------------------


def _make_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


_PRIVATE_KEY_PEM, _PUBLIC_KEY_PEM = _make_keypair()


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


def _sf_config() -> ConnectorAuthConfig:
    return ConnectorAuthConfig(
        connector_id="salesforce",
        flow="authorization_code",
        client_id="3MVG9-connected-app-consumer-key",
        secret_key="SALESFORCE_CLIENT_SECRET",
        token_url="https://test.salesforce.com/services/oauth2/token",
        scopes=["api", "refresh_token"],
        supported_auth_modes=["authorization_code", "jwt_bearer"],
    )


@pytest.fixture(autouse=True)
def _vault_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)
    yield


# ---------------------------------------------------------------------------
# Assertion builder — signs a verifiable RS256 JWT (AC1 building block)
# ---------------------------------------------------------------------------


def test_build_assertion_is_verifiable_rs256():
    assertion = oauth.build_jwt_bearer_assertion(
        issuer="consumer-key",
        subject=_FAKE_SF_USER,
        audience=_FAKE_LOGIN_URL,
        private_key=_PRIVATE_KEY_PEM,
    )
    # Verify with the PUBLIC key — proves it was signed by the private key (RS256).
    claims = jwt.decode(
        assertion, _PUBLIC_KEY_PEM, algorithms=["RS256"], audience=_FAKE_LOGIN_URL
    )
    assert claims["iss"] == "consumer-key"
    assert claims["sub"] == _FAKE_SF_USER
    assert claims["aud"] == _FAKE_LOGIN_URL
    assert claims["exp"] > int(datetime.now(timezone.utc).timestamp())


def test_build_assertion_bad_key_raises_without_leaking():
    with pytest.raises(oauth.OAuthError) as ei:
        oauth.build_jwt_bearer_assertion(
            issuer="iss",
            subject="sub",
            audience="aud",
            private_key="-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----",
        )
    # The error must not carry the (attempted) key material.
    assert "not-a-real-key" not in str(ei.value)
    assert ei.value.reason == "jwt_signing_failed"


# ---------------------------------------------------------------------------
# Outbound token exchange — assertion POST, no callback (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_jwt_bearer_token_posts_assertion_no_secret():
    transport = _MockTransport(
        200,
        {"access_token": "sf-access-token", "instance_url": "https://my.my.salesforce.com"},
    )
    result = await oauth.get_jwt_bearer_token(
        _sf_config(),
        private_key=_PRIVATE_KEY_PEM,
        subject=_FAKE_SF_USER,
        _transport=transport,
    )
    assert result["access_token"] == "sf-access-token"
    assert result["instance_url"] == "https://my.my.salesforce.com"

    body = transport.last_request.content.decode()
    # Correct grant, an assertion present, and NO client secret in the body.
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in body
    assert "assertion=" in body
    assert "client_secret" not in body


@pytest.mark.anyio
async def test_get_jwt_bearer_token_error_raises_oauth_error():
    transport = _MockTransport(400, {"error": "invalid_grant", "error_description": "user hasn't approved"})
    with pytest.raises(oauth.OAuthError):
        await oauth.get_jwt_bearer_token(
            _sf_config(),
            private_key=_PRIVATE_KEY_PEM,
            subject=_FAKE_SF_USER,
            _transport=transport,
        )


# ---------------------------------------------------------------------------
# get_token mints from vaulted material + re-asserts on expiry (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_token_mints_from_jwt_material(monkeypatch):
    org = "org_jwt_mint"
    vault.revoke_jwt_bearer_credential(org, "salesforce")
    vault.store_jwt_bearer_credential(
        org, "salesforce", private_key=_PRIVATE_KEY_PEM, subject=_FAKE_SF_USER, base_url=_FAKE_LOGIN_URL
    )

    calls = {"n": 0}

    async def _fake_exchange(config, **kwargs):
        calls["n"] += 1
        # The mint path must pass the per-org key/subject/host to the exchange.
        assert kwargs["private_key"] == _PRIVATE_KEY_PEM
        assert kwargs["subject"] == _FAKE_SF_USER
        assert kwargs["audience"] == _FAKE_LOGIN_URL
        return {
            "access_token": f"minted-token-{calls['n']}",
            "instance_url": "https://acme.my.salesforce.com",
            "expires_in": 3600,
        }

    monkeypatch.setattr(vault._oauth, "get_jwt_bearer_token", _fake_exchange)

    record = await vault.get_token(org, "salesforce")
    assert isinstance(record, TokenRecord)
    assert record.access_token == "minted-token-1"
    assert calls["n"] == 1

    # instance_url from the JWT response is captured for live-ingest URL resolution.
    from app.live_ingest_credentials import get_connector_instance_url

    assert get_connector_instance_url(org, "salesforce") == "https://acme.my.salesforce.com"

    # A second call inside the token's validity window is served from the cached
    # OAuth row — NO re-mint (the assertion is only re-signed when needed).
    record2 = await vault.get_token(org, "salesforce")
    assert record2.access_token == "minted-token-1"
    assert calls["n"] == 1


@pytest.mark.anyio
async def test_get_token_reasserts_on_expiry(monkeypatch):
    org = "org_jwt_reassert"
    vault.revoke_jwt_bearer_credential(org, "salesforce")
    vault.revoke_static_credential(org, "salesforce")  # clear any oauth/static row
    vault.store_jwt_bearer_credential(
        org, "salesforce", private_key=_PRIVATE_KEY_PEM, subject=_FAKE_SF_USER, base_url=_FAKE_LOGIN_URL
    )

    calls = {"n": 0}

    async def _fake_exchange(config, **kwargs):
        calls["n"] += 1
        # expires_in=1 → immediately inside the refresh window, forcing re-assertion
        # on the next get_token (there is no OAuth refresh_token for JWT bearer).
        return {"access_token": f"tok-{calls['n']}", "expires_in": 1}

    monkeypatch.setattr(vault._oauth, "get_jwt_bearer_token", _fake_exchange)

    first = await vault.get_token(org, "salesforce")
    assert first.access_token == "tok-1"
    # Second call: cached token is (near-)expired and carries no refresh token, so
    # get_token re-mints by re-assertion rather than raising.
    second = await vault.get_token(org, "salesforce")
    assert second.access_token == "tok-2"
    assert calls["n"] == 2


@pytest.mark.anyio
async def test_minted_token_resolves_via_get_connector_credentials(monkeypatch):
    """AC3: after minting, the connector resolves through the ONE credential path
    as a TokenRecord — the private key is never returned as the credential."""
    org = "org_jwt_ac3"
    vault.revoke_jwt_bearer_credential(org, "salesforce")
    vault.store_jwt_bearer_credential(
        org, "salesforce", private_key=_PRIVATE_KEY_PEM, subject=_FAKE_SF_USER, base_url=_FAKE_LOGIN_URL
    )

    async def _fake_exchange(config, **kwargs):
        return {"access_token": "resolved-token", "expires_in": 3600}

    monkeypatch.setattr(vault._oauth, "get_jwt_bearer_token", _fake_exchange)
    await vault.get_token(org, "salesforce")  # mint + cache

    from app.auth.credentials import get_connector_credentials

    cred = get_connector_credentials(org, "salesforce")
    assert isinstance(cred, TokenRecord)
    assert cred.access_token == "resolved-token"
    assert _PRIVATE_KEY_PEM not in cred.access_token


# ---------------------------------------------------------------------------
# AC5 — vault hygiene: encrypted at rest, masked in repr
# ---------------------------------------------------------------------------


def test_private_key_encrypted_at_rest():
    org = "org_jwt_enc"
    vault.revoke_jwt_bearer_credential(org, "salesforce")
    vault.store_jwt_bearer_credential(
        org, "salesforce", private_key=_PRIVATE_KEY_PEM, subject=_FAKE_SF_USER, base_url=_FAKE_LOGIN_URL
    )
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT enc_secret FROM credentials "
            "WHERE org_id=%s AND connector_id=%s AND kind='static' AND is_deleted=FALSE",
            (org, "salesforce:jwt"),
        )
        row = cur.fetchone()
    finally:
        con.close()
    assert row is not None
    enc = row[0]
    # The stored column is ciphertext, not the PEM; it decrypts back to the key.
    assert _PRIVATE_KEY_PEM not in enc
    assert Fernet(_VAULT_KEY.encode()).decrypt(enc.encode()).decode() == _PRIVATE_KEY_PEM


def test_static_credential_record_masks_key_in_repr():
    rec = StaticCredentialRecord(
        org_id="o",
        connector_id="salesforce:jwt",
        username=_FAKE_SF_USER,
        secret=_PRIVATE_KEY_PEM,
        base_url=_FAKE_LOGIN_URL,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    text = repr(rec)
    assert _PRIVATE_KEY_PEM not in text
    assert _FAKE_SF_USER not in text


# ---------------------------------------------------------------------------
# Entry route — owner-only, write-only (AC5)
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


def _body() -> dict:
    return {
        "login_url": _FAKE_LOGIN_URL,
        "username": _FAKE_SF_USER,
        "private_key": _PRIVATE_KEY_PEM,
    }


def test_route_requires_auth(client):
    assert client.post("/api/connectors/salesforce/jwt-credentials", json=_body()).status_code == 401


def test_route_owner_only(client):
    r = client.post("/api/connectors/salesforce/jwt-credentials", json=_body(), headers=VIEWER)
    assert r.status_code == 403


def test_route_stores_and_never_returns_key(client):
    vault.revoke_jwt_bearer_credential("default", "salesforce")
    r = client.post("/api/connectors/salesforce/jwt-credentials", json=_body(), headers=OWNER)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["configured"] is True
    assert payload["base_url"] == _FAKE_LOGIN_URL
    # Write-only: the response body must never echo the key or username.
    assert _PRIVATE_KEY_PEM not in r.text
    assert _FAKE_SF_USER not in r.text
    # It really landed in the vault under the reserved id.
    assert vault.get_jwt_bearer_credential("default", "salesforce") is not None
    vault.revoke_jwt_bearer_credential("default", "salesforce")


def test_route_rejects_unsupported_connector(client):
    # Jira does not support jwt_bearer.
    r = client.post("/api/connectors/jira/jwt-credentials", json=_body(), headers=OWNER)
    assert r.status_code == 400


def test_route_missing_fields(client):
    r = client.post(
        "/api/connectors/salesforce/jwt-credentials",
        json={"login_url": _FAKE_LOGIN_URL},
        headers=OWNER,
    )
    assert r.status_code == 400
    assert "username" in r.json()["detail"]
    assert "private key" in r.json()["detail"]


def test_route_status_and_delete(client):
    vault.revoke_jwt_bearer_credential("default", "salesforce")
    client.post("/api/connectors/salesforce/jwt-credentials", json=_body(), headers=OWNER)

    g = client.get("/api/connectors/salesforce/jwt-credentials", headers=OWNER)
    assert g.status_code == 200
    assert g.json()["configured"] is True
    assert _PRIVATE_KEY_PEM not in g.text

    d = client.delete("/api/connectors/salesforce/jwt-credentials", headers=OWNER)
    assert d.status_code == 204

    g2 = client.get("/api/connectors/salesforce/jwt-credentials", headers=OWNER)
    assert g2.json()["configured"] is False


def test_route_normalises_bare_login_host(client):
    # A bare host (no scheme) is accepted and defaulted to https, so the outbound
    # token URL and JWT `aud` claim are well-formed. Fails loudly at setup would be
    # worse than a silent malformed mint.
    vault.revoke_jwt_bearer_credential("default", "salesforce")
    body = _body()
    body["login_url"] = "login.salesforce.com"
    r = client.post(
        "/api/connectors/salesforce/jwt-credentials", json=body, headers=OWNER
    )
    assert r.status_code == 200, r.text
    assert r.json()["base_url"] == "https://login.salesforce.com"
    vault.revoke_jwt_bearer_credential("default", "salesforce")


def test_route_rejects_non_http_login_url(client):
    vault.revoke_jwt_bearer_credential("default", "salesforce")
    body = _body()
    body["login_url"] = "ftp://not-a-web-url"
    r = client.post(
        "/api/connectors/salesforce/jwt-credentials", json=body, headers=OWNER
    )
    assert r.status_code == 400
    assert "login URL" in r.json()["detail"]
