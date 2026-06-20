"""Unit tests for CS-2 live ingest wiring (app.live_ingest_credentials).

resolve_live_systems() must promote a connector to live ingest only when it is
both authenticated in the vault AND has an instance URL (captured at connect, or
env fallback), and it must publish the credentials to the per-run ingest context
(DB-sourced, isolated per run) — NOT to process-global os.environ.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app import live_ingest_credentials as lic
from app.auth.models import ConnectorNotAuthenticatedError, TokenRecord
from discovery.ingest import clear_live_connectors, get_live_connector


@pytest.fixture(autouse=True)
def _isolate_run_context():
    """The per-run credential context is a contextvar shared across tests in the
    main thread; clear it before and after each test so state never leaks."""
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


def test_resolves_only_authenticated_connectors_with_url(monkeypatch):
    """Authenticated + URL-configured connectors are returned; others excluded."""
    # salesforce + jira have URL config; servicenow's URL is absent.
    monkeypatch.setenv("SF_INSTANCE_URL", "https://sf.example.com")
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.delenv("SERVICENOW_URL", raising=False)
    for var in ("SF_ACCESS_TOKEN", "JIRA_TOKEN", "SERVICENOW_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    async def fake_get_token(org_id, connector_id):
        if connector_id == "servicenow":
            raise ConnectorNotAuthenticatedError(org_id, connector_id)
        return _token(connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    live = lic.resolve_live_systems("default")

    assert live == ["salesforce", "jira"]
    # Credentials published to the per-run context (not os.environ).
    assert get_live_connector("salesforce")["token"] == "salesforce-access"
    assert get_live_connector("jira")["token"] == "jira-access"
    # The excluded connector has no per-run credentials.
    assert get_live_connector("servicenow") is None


def test_authenticated_but_missing_instance_url_is_skipped(monkeypatch):
    """A connector with a token but no URL config is not promoted to live."""
    monkeypatch.delenv("SF_INSTANCE_URL", raising=False)
    monkeypatch.delenv("SERVICENOW_URL", raising=False)
    monkeypatch.delenv("JIRA_URL", raising=False)

    async def fake_get_token(org_id, connector_id):
        return _token(connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    assert lic.resolve_live_systems("default") == []


def test_no_authenticated_connectors_returns_empty(monkeypatch):
    """When nothing is authenticated, the run falls back to offline (empty list)."""
    monkeypatch.setenv("SF_INSTANCE_URL", "https://sf.example.com")
    monkeypatch.setenv("SERVICENOW_URL", "https://sn.example.com")
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")

    async def fake_get_token(org_id, connector_id):
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    assert lic.resolve_live_systems("default") == []


def test_capture_instance_url_salesforce_from_token_response():
    """Salesforce returns instance_url in the token response."""
    url = lic.capture_instance_url(
        "salesforce",
        {"access_token": "x", "instance_url": "https://acme.my.salesforce.com/"},
    )
    assert url == "https://acme.my.salesforce.com"


def test_capture_instance_url_servicenow_from_config_host():
    """ServiceNow host is derived from the connector's configured token URL."""
    url = lic.capture_instance_url(
        "servicenow",
        {"access_token": "x"},
        token_url="https://dev198195.service-now.com/oauth_token.do",
    )
    assert url == "https://dev198195.service-now.com"


def test_capture_instance_url_jira_returns_none():
    """Jira's site URL is not derivable from the token response."""
    assert lic.capture_instance_url("jira", {"access_token": "x"}) is None


def test_captured_url_used_when_env_absent(monkeypatch):
    """A connect-time captured instance URL removes the need for env config."""
    monkeypatch.delenv("SF_INSTANCE_URL", raising=False)
    monkeypatch.delenv("SF_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SERVICENOW_URL", raising=False)
    monkeypatch.delenv("JIRA_URL", raising=False)

    async def fake_get_token(org_id, connector_id):
        if connector_id == "salesforce":
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)
    monkeypatch.setattr(
        lic,
        "get_connector_instance_url",
        lambda org_id, cid: "https://acme.my.salesforce.com"
        if cid == "salesforce"
        else None,
    )

    live = lic.resolve_live_systems("default")

    assert live == ["salesforce"]
    cred = get_live_connector("salesforce")
    assert cred["url"] == "https://acme.my.salesforce.com"
    assert cred["token"] == "salesforce-access"


def test_fetch_jira_gateway_base_builds_gateway_url():
    """accessible-resources cloudId is turned into the api.atlassian.com gateway base."""

    def handler(request):
        assert request.headers["Authorization"] == "Bearer jira-tok"
        return httpx.Response(
            200, json=[{"id": "abc-123", "url": "https://acme.atlassian.net"}]
        )

    transport = httpx.MockTransport(handler)
    base = asyncio.run(lic.fetch_jira_gateway_base("jira-tok", _transport=transport))
    assert base == "https://api.atlassian.com/ex/jira/abc-123"


def test_fetch_jira_gateway_base_no_sites_returns_none():
    """No accessible sites → None (Jira excluded from live ingest)."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=[]))
    assert asyncio.run(lic.fetch_jira_gateway_base("t", _transport=transport)) is None


def test_fetch_jira_gateway_base_empty_token_returns_none():
    assert asyncio.run(lic.fetch_jira_gateway_base("")) is None


def test_jira_resolution_hydrates_gateway_url_and_bearer_token(monkeypatch):
    """Jira live ingest hydrates the captured gateway URL + OAuth Bearer token."""
    monkeypatch.delenv("JIRA_URL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)

    async def fake_get_token(org_id, connector_id):
        if connector_id == "jira":
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)
    monkeypatch.setattr(
        lic,
        "get_connector_instance_url",
        lambda org_id, cid: "https://api.atlassian.com/ex/jira/abc-123"
        if cid == "jira"
        else None,
    )

    live = lic.resolve_live_systems("default")

    assert live == ["jira"]
    cred = get_live_connector("jira")
    assert cred["url"] == "https://api.atlassian.com/ex/jira/abc-123"
    assert cred["token"] == "jira-access"


def test_unexpected_vault_error_excludes_connector(monkeypatch):
    """A vault error for one connector excludes it without raising."""
    monkeypatch.setenv("SF_INSTANCE_URL", "https://sf.example.com")
    monkeypatch.setenv("SERVICENOW_URL", "https://sn.example.com")
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.delenv("SERVICENOW_TOKEN", raising=False)

    async def fake_get_token(org_id, connector_id):
        if connector_id == "salesforce":
            raise RuntimeError("vault boom")
        if connector_id == "jira":
            raise ConnectorNotAuthenticatedError(org_id, connector_id)
        return _token(connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    live = lic.resolve_live_systems("default")

    assert live == ["servicenow"]
