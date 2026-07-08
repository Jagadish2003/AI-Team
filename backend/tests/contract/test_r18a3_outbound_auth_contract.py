"""R18-A3 T8 (AT-561) — Outbound-Initiated Connector Authentication: full-story
contract suite, run with ALL inbound routes disabled.

Section 4 acceptance criteria (AC1–AC8) for the no-public-inbound posture, driven
against the real APIs. The whole module runs under ``NETWORK_PROFILE=no_public_inbound``,
and the HTTP-facing tests run through the ``no_inbound_client`` fixture, which
strips the *only* provider-inbound route (``GET /api/connectors/oauth/callback``)
off the app — so every outbound-only flow is proven to succeed with no inbound
surface at all (the AC premise).

Why this complements the per-task suites (``test_salesforce_jwt_bearer`` T2,
``test_graph_client_credentials`` T3, ``test_servicenow_client_credentials`` T4,
``test_network_profile`` T5, ``test_scoped_inbound_callback_docs`` T6,
``test_auth2_org_approval`` T7): those prove each unit in isolation. This suite is
the acceptance layer — it maps each AC, and adds what none of them do: it removes
the inbound callback route and shows connect + mode-agnostic credential resolution
still work end to end, and it exercises the TCU deployment profile
(no_public_inbound + customer-tenant model provider) through connect-and-discover
(AC8).

FAKE CREDENTIALS: every credential value below is a generated, test-only value —
no real Salesforce/Graph/ServiceNow credential appears here.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fastapi.testclient import TestClient

from app import db
from app.auth import oauth, vault
from app.auth.auth_modes import (
    AUTH_MODE_AUTHORIZATION_CODE,
    AUTH_MODE_CLIENT_CREDENTIALS,
    AUTH_MODE_JWT_BEARER,
    AUTH_MODE_STATIC,
    resolve_auth_mode,
    set_auth_mode,
)
from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.auth.credentials import get_connector_credentials, get_connector_secret
from app.auth.models import ConnectorAuthConfig, StaticCredentialRecord, TokenRecord

_VAULT_KEY = Fernet.generate_key().decode()
OWNER = {"Authorization": "Bearer dev-token-change-me"}
VIEWER = {"Authorization": "Bearer viewer-token"}  # recognized VIEWER_JWT principal

_INBOUND_CALLBACK_PATH = "/api/connectors/oauth/callback"

# Repo root: backend/tests/contract/<file> → parents[3] == repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]

_FAKE_SF_USER = "svc-agentiq@example.com.sandbox"
_FAKE_LOGIN_URL = "https://test.salesforce.com"
_GRAPH_TOKEN_URL = "https://login.microsoftonline.com/fake-tenant-guid/oauth2/v2.0/token"
_GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------
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
    """Returns a fixed status + JSON body; captures the last outbound request."""

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


def _graph_config(connector_id: str = "teams") -> ConnectorAuthConfig:
    return ConnectorAuthConfig(
        connector_id=connector_id,
        flow="authorization_code",
        client_id=f"FAKE-{connector_id}-client-id",
        secret_key="TEAMS_CLIENT_SECRET",
        token_url=_GRAPH_TOKEN_URL,
        scopes=["offline_access", "Channel.ReadBasic.All"],
        client_credentials_scopes=[_GRAPH_DEFAULT_SCOPE],
        supported_auth_modes=["authorization_code", "client_credentials"],
    )


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


@pytest.fixture(autouse=True)
def _r18a3_env(monkeypatch):
    """The whole suite runs in the no-public-inbound posture, with the vault key and
    the outbound connectors' client secrets present."""
    monkeypatch.setenv("NETWORK_PROFILE", "no_public_inbound")
    monkeypatch.setenv("CREDENTIAL_VAULT_KEY", _VAULT_KEY)
    monkeypatch.setenv("TEAMS_CLIENT_SECRET", "FAKE-teams-client-secret")
    monkeypatch.setenv("SHAREPOINT_CLIENT_SECRET", "FAKE-sharepoint-client-secret")
    monkeypatch.setenv("SERVICENOW_CLIENT_SECRET", "FAKE-servicenow-client-secret")
    yield


@pytest.fixture
def no_inbound_client(client):
    """A client over the same app with EVERY inbound-callback route removed.

    The only provider-inbound route is ``GET /api/connectors/oauth/callback``;
    stripping it proves the outbound-only setup + ingestion paths need no inbound
    surface. Depends on ``client`` so the app lifespan has already run; a plain
    ``TestClient`` (no ``with``) reuses the started app without re-running lifespan.
    Routes are restored after the test.
    """
    from app.main import app

    removed = [r for r in list(app.router.routes) if "callback" in getattr(r, "path", "")]
    for route in removed:
        app.router.routes.remove(route)
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        # Restore routes FIRST — this must always happen so later tests are never
        # left with the inbound route missing, even if closing the client raises.
        for route in removed:
            app.router.routes.append(route)
        try:
            test_client.close()  # release the transport; never leak it into later tests
        except Exception:
            pass


# ===========================================================================
# Premise — the inbound callback route is the only inbound surface, and the
# suite genuinely runs with it disabled.
# ===========================================================================
def test_inbound_callback_route_exists_by_default(client):
    """Sanity: the provider-inbound callback IS registered normally — so the
    no_inbound_client fixture is removing a real route, not a no-op."""
    from app.main import app

    paths = {getattr(r, "path", "") for r in app.router.routes}
    assert _INBOUND_CALLBACK_PATH in paths


def test_all_inbound_routes_disabled(no_inbound_client):
    """With inbound disabled, the callback path is gone (404) — the environment
    the rest of this suite asserts against."""
    from app.main import app

    paths = {getattr(r, "path", "") for r in app.router.routes}
    assert _INBOUND_CALLBACK_PATH not in paths
    assert no_inbound_client.get(_INBOUND_CALLBACK_PATH).status_code == 404


# ===========================================================================
# AC1 — Salesforce connects + ingests via JWT bearer, no callback attempted.
# ===========================================================================
def test_ac1_salesforce_jwt_setup_works_with_inbound_disabled(no_inbound_client):
    """The JWT-bearer 'connect' (cert entry) succeeds with inbound disabled and
    flips the connector to jwt_bearer mode — an outbound-only setup path."""
    vault.revoke_jwt_bearer_credential("default", "salesforce")
    body = {"login_url": _FAKE_LOGIN_URL, "username": _FAKE_SF_USER, "private_key": _PRIVATE_KEY_PEM}
    r = no_inbound_client.post("/api/connectors/salesforce/jwt-credentials", json=body, headers=OWNER)
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True
    # Selecting JWT bearer is what makes ingestion outbound-only for this org.
    assert resolve_auth_mode("default", "salesforce") == AUTH_MODE_JWT_BEARER
    # The cert is write-only — never echoed by the setup path (AC5 tie-in).
    assert _PRIVATE_KEY_PEM not in r.text
    vault.revoke_jwt_bearer_credential("default", "salesforce")
    set_auth_mode("default", "salesforce", AUTH_MODE_AUTHORIZATION_CODE)


@pytest.mark.anyio
async def test_ac1_jwt_bearer_token_exchange_is_outbound_only():
    """The token exchange is a single outbound POST of a signed assertion — no
    redirect_uri, no authorization code, no client secret, no callback."""
    transport = _MockTransport(
        200, {"access_token": "sf-access-token", "instance_url": "https://my.my.salesforce.com"}
    )
    result = await oauth.get_jwt_bearer_token(
        _sf_config(), private_key=_PRIVATE_KEY_PEM, subject=_FAKE_SF_USER, _transport=transport
    )
    assert result["access_token"] == "sf-access-token"

    body = transport.last_request.content.decode()
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in body
    assert "assertion=" in body
    # The hallmarks of the inbound authorization-code flow must all be absent.
    assert "redirect_uri" not in body
    assert "code=" not in body
    assert "client_secret" not in body


@pytest.mark.anyio
async def test_ac1_minted_jwt_token_ingests_via_get_connector_credentials(monkeypatch):
    """After the outbound mint, ingestion resolves a usable token through the ONE
    credential path — 'connects and ingests' with no callback ever touched."""
    org = "org_r18a3_ac1"
    vault.revoke_jwt_bearer_credential(org, "salesforce")
    vault.revoke_static_credential(org, "salesforce")
    vault.store_jwt_bearer_credential(
        org, "salesforce", private_key=_PRIVATE_KEY_PEM, subject=_FAKE_SF_USER, base_url=_FAKE_LOGIN_URL
    )

    async def _fake_exchange(config, **kwargs):
        return {"access_token": "minted-sf-token", "expires_in": 3600}

    monkeypatch.setattr(vault._oauth, "get_jwt_bearer_token", _fake_exchange)

    record = await vault.get_token(org, "salesforce")
    assert isinstance(record, TokenRecord)
    assert record.access_token == "minted-sf-token"

    cred = get_connector_credentials(org, "salesforce")
    assert isinstance(cred, TokenRecord)
    assert cred.access_token == "minted-sf-token"


# ===========================================================================
# AC2 — Teams and SharePoint connect + ingest via Graph client-credentials.
# ===========================================================================
@pytest.mark.parametrize("connector", ["teams", "sharepoint"])
def test_ac2_graph_client_credentials_connect_with_inbound_disabled(
    no_inbound_client, monkeypatch, connector
):
    """The Graph client-credentials connect route succeeds with inbound disabled,
    selects client_credentials mode, and the token resolves for ingestion — no
    browser redirect, no callback."""
    import app.routes_connector_auth as routes

    async def _fake_cc(config, **kwargs):
        return {"access_token": f"FAKE-{connector}-cc-token", "expires_in": 3599}

    monkeypatch.setattr(routes, "get_client_credentials_token", _fake_cc)

    r = no_inbound_client.post(f"/api/connectors/{connector}/client-credentials", headers=OWNER)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["connected"] is True
    assert payload["auth_mode"] == AUTH_MODE_CLIENT_CREDENTIALS
    assert f"FAKE-{connector}-cc-token" not in r.text  # write-only (AC5 tie-in)

    cred = get_connector_credentials("default", connector)
    assert isinstance(cred, TokenRecord)
    assert cred.access_token == f"FAKE-{connector}-cc-token"

    # Cleanup: drop the token and restore the default browser mode.
    no_inbound_client.delete(f"/api/connectors/{connector}/token", headers=OWNER)
    set_auth_mode("default", connector, AUTH_MODE_AUTHORIZATION_CODE)


@pytest.mark.anyio
async def test_ac2_graph_token_request_uses_default_scope_no_callback():
    """The outbound Graph request uses the client_credentials grant and the
    .default resource scope — never a delegated/redirect flow."""
    transport = _MockTransport(200, {"access_token": "FAKE-graph-token", "expires_in": 3599})
    await oauth.get_client_credentials_token(_graph_config("teams"), _transport=transport)

    body = transport.last_request.content.decode("utf-8")
    assert "grant_type=client_credentials" in body
    assert "graph.microsoft.com" in body and "default" in body
    assert "redirect_uri" not in body
    assert "code=" not in body


# ===========================================================================
# AC3 — every auth mode resolves identically through get_connector_credentials().
# ===========================================================================
@pytest.mark.anyio
async def test_ac3_every_mode_resolves_through_one_entrypoint(monkeypatch):
    """authorization_code, client_credentials, jwt_bearer and static all land in
    the vault and resolve through the SAME mode-agnostic call — ingestion never
    branches on mode."""
    org = "org_r18a3_ac3"

    # authorization_code → OAuth TokenRecord.
    await vault.revoke_token(org, "github")
    vault.store_token(org, "github", {"access_token": "authcode-token", "expires_in": 3600})

    # client_credentials → OAuth TokenRecord.
    await vault.revoke_token(org, "teams")
    vault.store_token(org, "teams", {"access_token": "cc-token", "expires_in": 3600})

    # jwt_bearer → minted OAuth TokenRecord.
    vault.revoke_jwt_bearer_credential(org, "salesforce")
    vault.revoke_static_credential(org, "salesforce")
    vault.store_jwt_bearer_credential(
        org, "salesforce", private_key=_PRIVATE_KEY_PEM, subject=_FAKE_SF_USER, base_url=_FAKE_LOGIN_URL
    )

    async def _fake_exchange(config, **kwargs):
        return {"access_token": "jwt-token", "expires_in": 3600}

    monkeypatch.setattr(vault._oauth, "get_jwt_bearer_token", _fake_exchange)
    await vault.get_token(org, "salesforce")

    # static → StaticCredentialRecord.
    vault.revoke_static_credential(org, "jira")
    vault.store_static_credential(
        org, "jira", username="svc", secret="FAKE-jira-api-token", base_url="https://x.atlassian.net"
    )

    expected = {
        "github": ("authcode-token", TokenRecord),
        "teams": ("cc-token", TokenRecord),
        "salesforce": ("jwt-token", TokenRecord),
        "jira": ("FAKE-jira-api-token", StaticCredentialRecord),
    }
    for connector_id, (secret, kind) in expected.items():
        cred = get_connector_credentials(org, connector_id)
        assert isinstance(cred, kind), connector_id
        # The identical secret accessor works regardless of the producing mode.
        assert get_connector_secret(org, connector_id) == secret, connector_id


# ===========================================================================
# AC4 — in no_public_inbound, no authorization_code flow is offered where an
#        outbound-only mode exists (the capability the Integration Hub consumes).
# ===========================================================================
def test_ac4_network_profile_hides_authcode_where_outbound_exists(no_inbound_client):
    resp = no_inbound_client.get("/api/network-profile", headers=OWNER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["network_profile"] == "no_public_inbound"
    assert body["no_public_inbound"] is True

    connectors = body["connectors"]

    # Connectors WITH an outbound-only mode must advertise it (the UI offers that
    # instead of the dead authorization-code button).
    for cid in ("salesforce", "servicenow", "jira", "teams", "sharepoint"):
        cap = connectors[cid]
        assert cap["has_outbound_only_mode"] is True, cid
        assert cap["outbound_only_modes"], cid

    # Connectors with NO outbound-only mode are honestly flagged (browser flow only).
    for cid in ("github", "slack"):
        assert connectors[cid]["has_outbound_only_mode"] is False, cid

    # The load-bearing invariant AC4 depends on, for EVERY connector: the flag is
    # true exactly when a non-authorization_code mode exists to offer instead.
    for cid, cap in connectors.items():
        has_alt = any(m != AUTH_MODE_AUTHORIZATION_CODE for m in cap["supported_auth_modes"])
        assert cap["has_outbound_only_mode"] == has_alt, cid
        assert cap["has_outbound_only_mode"] == bool(cap["outbound_only_modes"]), cid


def test_ac4_network_profile_requires_auth(no_inbound_client):
    assert no_inbound_client.get("/api/network-profile").status_code == 401


# ===========================================================================
# AC5 — outbound-mode tokens/keys keep the same vault hygiene as every credential.
# ===========================================================================
def test_ac5_outbound_credentials_encrypted_at_rest_and_write_only(no_inbound_client):
    org = "org_r18a3_ac5"
    # A JWT bearer cert (jwt_bearer) and a client-credentials token (client_credentials).
    vault.revoke_jwt_bearer_credential(org, "salesforce")
    vault.store_jwt_bearer_credential(
        org, "salesforce", private_key=_PRIVATE_KEY_PEM, subject=_FAKE_SF_USER, base_url=_FAKE_LOGIN_URL
    )
    vault.store_token(org, "teams", {"access_token": "FAKE-cc-secret-token", "expires_in": 3600})

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT enc_secret FROM credentials "
            "WHERE org_id=%s AND connector_id=%s AND kind='static' AND is_deleted=FALSE",
            (org, "salesforce:jwt"),
        )
        enc_key = cur.fetchone()[0]
        cur.execute(
            "SELECT access_token FROM credentials "
            "WHERE org_id=%s AND connector_id=%s AND is_deleted=FALSE",
            (org, "teams"),
        )
        enc_token = cur.fetchone()[0]
    finally:
        con.close()

    fernet = Fernet(_VAULT_KEY.encode())
    # Encrypted at rest — ciphertext, not the plaintext; decrypts back to the secret.
    assert _PRIVATE_KEY_PEM not in enc_key
    assert fernet.decrypt(enc_key.encode()).decode() == _PRIVATE_KEY_PEM
    assert "FAKE-cc-secret-token" not in enc_token
    assert fernet.decrypt(enc_token.encode()).decode() == "FAKE-cc-secret-token"

    # Never-logged: the record repr masks the key material.
    rec = vault.get_jwt_bearer_credential(org, "salesforce")
    assert isinstance(rec, StaticCredentialRecord)
    assert _PRIVATE_KEY_PEM not in repr(rec)
    assert _FAKE_SF_USER not in repr(rec)

    # Write-only status routes return metadata only, never the secret.
    vault.revoke_jwt_bearer_credential("default", "salesforce")
    vault.store_jwt_bearer_credential(
        "default", "salesforce", private_key=_PRIVATE_KEY_PEM, subject=_FAKE_SF_USER, base_url=_FAKE_LOGIN_URL
    )
    status = no_inbound_client.get("/api/connectors/salesforce/jwt-credentials", headers=OWNER)
    assert status.status_code == 200
    assert _PRIVATE_KEY_PEM not in status.text
    assert _FAKE_SF_USER not in status.text
    vault.revoke_jwt_bearer_credential("default", "salesforce")
    set_auth_mode("default", "salesforce", AUTH_MODE_AUTHORIZATION_CODE)


# ===========================================================================
# AC6 — AUTH-2 approval links resolve internally, and the limitation is documented.
# ===========================================================================
def test_ac6_auth2_links_resolve_against_internal_url(monkeypatch):
    from app import email_service

    internal = "https://agentiq.internal.tcu.local"
    monkeypatch.setenv("AGENTIQ_BACKEND_URL", internal)
    # Approve/reject links are built from the internal backend URL — an internal
    # admin clicking them from inside the network reaches the deployment directly,
    # never a public inbound callback.
    assert email_service._backend_url() == internal
    assert "oauth/callback" not in email_service._backend_url()
    # The state-changing POST is a relative, same-host path (proven behaviourally in
    # test_auth2_org_approval.py::test_confirmation_form_posts_to_relative_same_host_path),
    # so it also lands on the internal host.


def test_ac6_internal_link_limitation_is_documented():
    readme = (REPO_ROOT / "deployment" / "README.md").read_text(encoding="utf-8")
    assert "AUTH-2 org-approval email links in no-inbound" in readme
    assert "AGENTIQ_BACKEND_URL" in readme


# ===========================================================================
# AC7 — the scoped-inbound fallback package exists as customer-facing docs.
# ===========================================================================
def test_ac7_scoped_inbound_fallback_package_exists():
    doc = REPO_ROOT / "deployment" / "SCOPED_INBOUND_CALLBACK.md"
    assert doc.exists(), "scoped-inbound fallback package must exist (AC7)"
    text = doc.read_text(encoding="utf-8").lower()
    for needle in ("reverse proxy", "allowlist", "/api/connectors/oauth/callback", "approach c"):
        assert needle in text, needle
    # Approach C (vendor-hosted relay) is explicitly rejected.
    assert "reject" in text and "relay" in text
    # The deployment guide links the package.
    readme = (REPO_ROOT / "deployment" / "README.md").read_text(encoding="utf-8")
    assert "SCOPED_INBOUND_CALLBACK.md" in readme


# ===========================================================================
# AC8 — TCU deployment profile (no_public_inbound + customer-tenant model mode)
#        passes an end-to-end connect-and-discover test, inbound disabled.
# ===========================================================================
def test_ac8_tcu_profile_connect_and_discover(no_inbound_client, monkeypatch):
    import app.model_gateway as model_gateway
    import app.routes_connector_auth as routes

    # --- TCU model posture: the customer's own in-tenant managed model service. -
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "customer_tenant")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "customer_tenant")
    assert model_gateway.get_generation_provider().name == "customer_tenant"
    assert model_gateway.get_embedding_provider().name == "customer_tenant"
    model_gateway.validate_provider_config()  # customer-tenant config must not raise

    # --- The run gate must see a healthy license so discovery may run. ---------
    monkeypatch.setattr(
        "app.middleware.license_gate.get_current_license_status",
        lambda *a, **k: {"status": "valid"},
    )

    # --- Connect two connectors via OUTBOUND-only modes (no callback). ---------
    # Salesforce JWT bearer: enter the cert (mode → jwt_bearer) and represent the
    # minted access token (the mint itself is proven in AC1).
    vault.revoke_jwt_bearer_credential("default", "salesforce")
    sf_body = {"login_url": _FAKE_LOGIN_URL, "username": _FAKE_SF_USER, "private_key": _PRIVATE_KEY_PEM}
    assert no_inbound_client.post(
        "/api/connectors/salesforce/jwt-credentials", json=sf_body, headers=OWNER
    ).status_code == 200
    vault.store_token("default", "salesforce", {"access_token": "tcu-sf-token", "expires_in": 3600})

    # Teams via Graph client-credentials.
    async def _fake_cc(config, **kwargs):
        return {"access_token": "tcu-teams-token", "expires_in": 3599}

    monkeypatch.setattr(routes, "get_client_credentials_token", _fake_cc)
    assert no_inbound_client.post(
        "/api/connectors/teams/client-credentials", headers=OWNER
    ).status_code == 200

    # Both resolve mode-agnostically for ingestion.
    assert isinstance(get_connector_credentials("default", "salesforce"), TokenRecord)
    assert isinstance(get_connector_credentials("default", "teams"), TokenRecord)

    # The hub reports the no-inbound posture with outbound-only paths available.
    profile = no_inbound_client.get("/api/network-profile", headers=OWNER).json()
    assert profile["no_public_inbound"] is True
    assert profile["connectors"]["salesforce"]["has_outbound_only_mode"] is True
    assert profile["connectors"]["teams"]["has_outbound_only_mode"] is True

    # --- Discover: a full offline discovery run completes with inbound disabled. -
    run_body = {
        "connectedSources": [],
        "uploadedFiles": [],
        "sampleWorkspaceEnabled": False,
        "mode": "offline",
        "systems": ["salesforce", "servicenow", "jira"],
    }
    started = no_inbound_client.post("/api/runs/start", json=run_body, headers=OWNER)
    assert started.status_code in (200, 201), started.text
    run_id = started.json()["runId"]

    status = "running"
    for _ in range(60):
        st = no_inbound_client.get(f"/api/runs/{run_id}/status", headers=OWNER)
        if st.status_code == 200:
            status = st.json().get("status", "running")
            if status in ("complete", "partial", "failed"):
                break
        time.sleep(1)
    assert status in ("complete", "partial"), f"TCU-profile discovery reached {status!r}"

    # Cleanup: drop every credential + token + restore default modes so the shared
    # 'default' org is left exactly as found — no lingering connected state or
    # credentials for the run-based suites that execute after this one.
    no_inbound_client.delete("/api/connectors/teams/token", headers=OWNER)
    no_inbound_client.delete("/api/connectors/salesforce/token", headers=OWNER)
    vault.revoke_jwt_bearer_credential("default", "salesforce")
    set_auth_mode("default", "salesforce", AUTH_MODE_AUTHORIZATION_CODE)
    set_auth_mode("default", "teams", AUTH_MODE_AUTHORIZATION_CODE)
