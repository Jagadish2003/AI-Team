"""R18-A3 — static vault credentials must ingest LIVE (AC3).

In a no-public-inbound deployment, Jira's only outbound-only auth mode is a
static API token and ServiceNow may use static user/password. A static
credential is a first-class connection: ``resolve_live_systems`` must promote
the connector to live ingest exactly as an OAuth token does, and the ingest
clients must authenticate Basic (username present) instead of Bearer.

Covers:
  * resolve_live_systems: static fallback fires only when get_token finds no
    OAuth row, only for jira/servicenow, publishing {url, token, username}.
  * OAuth stays preferred: a token row wins; static is never consulted.
  * Failure posture: no URL → skipped; vault decrypt error → skipped (never
    raises into the run).
  * JiraClient / ServiceNowClient: username selects Basic auth; token-only
    keeps the OAuth Bearer header byte-identical to before.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import live_ingest_credentials as lic
from app.auth.models import (
    ConnectorNotAuthenticatedError,
    StaticCredentialRecord,
    TokenRecord,
)
from discovery.ingest import clear_live_connectors, get_live_connector
from discovery.ingest.jira import JiraClient
from discovery.ingest.servicenow import ServiceNowClient


@pytest.fixture(autouse=True)
def _isolate_run_context():
    clear_live_connectors()
    yield
    clear_live_connectors()


def _token(connector_id: str) -> TokenRecord:
    now = datetime.now(timezone.utc)
    return TokenRecord(
        org_id="default",
        connector_id=connector_id,
        access_token=f"{connector_id}-access",
        expires_at=now,
        scopes=[],
        created_at=now,
        updated_at=now,
    )


def _static(connector_id: str, base_url: str = "") -> StaticCredentialRecord:
    now = datetime.now(timezone.utc)
    return StaticCredentialRecord(
        org_id="default",
        connector_id=connector_id,
        username=f"{connector_id}-user",
        secret=f"{connector_id}-secret",
        base_url=base_url,
        created_at=now,
        updated_at=now,
    )


def _no_oauth(monkeypatch):
    """Every connector's get_token finds no OAuth row."""

    async def fake_get_token(org_id, connector_id):
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)


def _clear_url_env(monkeypatch):
    for var in ("SF_INSTANCE_URL", "SERVICENOW_URL", "JIRA_URL"):
        monkeypatch.delenv(var, raising=False)


# ─────────────────────────────────────────────────────────────────────────────
# resolve_live_systems — static fallback
# ─────────────────────────────────────────────────────────────────────────────


def test_jira_static_credential_promotes_live(monkeypatch):
    """A Jira static API-token credential ingests live: promoted with the site
    URL from the credential's own base_url and username published so the client
    authenticates Basic."""
    _no_oauth(monkeypatch)
    _clear_url_env(monkeypatch)

    def fake_static(org_id, connector_id):
        if connector_id == "jira":
            return _static("jira", base_url="https://yourco.atlassian.net/")
        return None

    monkeypatch.setattr(lic, "get_static_credential", fake_static)

    live = lic.resolve_live_systems("default")

    assert live == ["jira"]
    cred = get_live_connector("jira")
    assert cred == {
        "url": "https://yourco.atlassian.net",
        "token": "jira-secret",
        "username": "jira-user",
    }


def test_servicenow_static_credential_promotes_live(monkeypatch):
    _no_oauth(monkeypatch)
    _clear_url_env(monkeypatch)

    def fake_static(org_id, connector_id):
        if connector_id == "servicenow":
            return _static("servicenow", base_url="https://acme.service-now.com")
        return None

    monkeypatch.setattr(lic, "get_static_credential", fake_static)

    live = lic.resolve_live_systems("default")

    assert live == ["servicenow"]
    cred = get_live_connector("servicenow")
    assert cred["url"] == "https://acme.service-now.com"
    assert cred["token"] == "servicenow-secret"
    assert cred["username"] == "servicenow-user"


def test_salesforce_never_falls_back_to_static(monkeypatch):
    """Salesforce has no static mode — the fallback must not even consult the
    vault for it (its outbound path is JWT bearer, minted inside get_token)."""
    _no_oauth(monkeypatch)
    _clear_url_env(monkeypatch)
    consulted = []

    def fake_static(org_id, connector_id):
        consulted.append(connector_id)
        return None

    monkeypatch.setattr(lic, "get_static_credential", fake_static)

    assert lic.resolve_live_systems("default") == []
    assert "salesforce" not in consulted
    assert set(consulted) <= {"jira", "servicenow"}


def test_oauth_token_preferred_over_static(monkeypatch):
    """When an OAuth row exists the static fallback never fires (one credential
    per connector per org: the vault enforces either/or, and get_token wins)."""
    monkeypatch.setenv("JIRA_URL", "https://api.atlassian.com/ex/jira/cid")
    monkeypatch.delenv("SF_INSTANCE_URL", raising=False)
    monkeypatch.delenv("SERVICENOW_URL", raising=False)

    async def fake_get_token(org_id, connector_id):
        if connector_id == "jira":
            return _token("jira")
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    def fail_static(org_id, connector_id):  # pragma: no cover - must not run for jira
        assert connector_id != "jira", "static fallback consulted despite OAuth row"
        return None

    monkeypatch.setattr(lic, "get_token", fake_get_token)
    monkeypatch.setattr(lic, "get_static_credential", fail_static)

    live = lic.resolve_live_systems("default")

    assert live == ["jira"]
    cred = get_live_connector("jira")
    assert cred["token"] == "jira-access"
    assert "username" not in cred  # OAuth cred → Bearer path in the client


def test_static_credential_without_url_is_skipped(monkeypatch):
    """base_url empty, nothing captured, no env fallback → degrade to skipped."""
    _no_oauth(monkeypatch)
    _clear_url_env(monkeypatch)
    monkeypatch.setattr(lic, "get_connector_instance_url", lambda o, c: None)
    monkeypatch.setattr(
        lic, "get_static_credential", lambda o, c: _static(c) if c == "jira" else None
    )

    assert lic.resolve_live_systems("default") == []
    assert get_live_connector("jira") is None


def test_static_vault_error_degrades_to_skipped(monkeypatch):
    """A vault decrypt failure skips the connector — never raises into the run."""
    _no_oauth(monkeypatch)
    _clear_url_env(monkeypatch)

    def boom(org_id, connector_id):
        raise RuntimeError("decrypt failed")

    monkeypatch.setattr(lic, "get_static_credential", boom)

    assert lic.resolve_live_systems("default") == []


# ─────────────────────────────────────────────────────────────────────────────
# Ingest clients — Basic vs Bearer selection
# ─────────────────────────────────────────────────────────────────────────────


def test_jira_client_basic_auth_for_static_credential():
    client = JiraClient(
        "https://yourco.atlassian.net", token="api-token", username="me@co.com"
    )
    session = client._get_session()
    assert session.auth == ("me@co.com", "api-token")
    assert "Authorization" not in session.headers


def test_jira_client_bearer_auth_unchanged_for_oauth():
    client = JiraClient("https://api.atlassian.com/ex/jira/cid", token="oauth-access")
    session = client._get_session()
    assert session.headers["Authorization"] == "Bearer oauth-access"
    assert session.auth is None


def test_servicenow_client_basic_auth_for_static_credential():
    client = ServiceNowClient(
        "https://acme.service-now.com", token="pw", username="sn-user"
    )
    session = client._get_session()
    assert session.auth == ("sn-user", "pw")
    assert "Authorization" not in session.headers


def test_servicenow_client_bearer_auth_unchanged_for_oauth():
    client = ServiceNowClient("https://acme.service-now.com", token="oauth-access")
    session = client._get_session()
    assert session.headers["Authorization"] == "Bearer oauth-access"
    assert session.auth is None
