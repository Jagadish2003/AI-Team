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
        # servicenow + slack + teams + github + java_app not connected in this scenario.
        if connector_id in ("servicenow", "slack", "teams", "github", "java_app", "dotnet_app"):
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


def test_authenticated_but_unresolvable_url_is_skipped(monkeypatch):
    """A connector with a token but no resolvable URL is not promoted to live.

    URL resolution order is captured → env → OAuth-derived; when all three yield
    nothing, the connector degrades to skipped rather than hard-failing the run.
    (Scoped to the URL-requiring systems of record — Slack is URL-less and is not
    connected in this scenario.)
    """
    monkeypatch.delenv("SF_INSTANCE_URL", raising=False)
    monkeypatch.delenv("SERVICENOW_URL", raising=False)
    monkeypatch.delenv("JIRA_URL", raising=False)

    async def fake_get_token(org_id, connector_id):
        if connector_id in ("slack", "teams", "github", "java_app", "dotnet_app"):
            raise ConnectorNotAuthenticatedError(org_id, connector_id)
        return _token(connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)
    monkeypatch.setattr(lic, "get_connector_instance_url", lambda o, c: None)
    # Simulate every connector being underivable (no captured URL, no config host,
    # Jira gateway lookup fails) so the skip path is what's exercised — and no live
    # accessible-resources network call is made.
    monkeypatch.setattr(lic, "_derive_oauth_instance_url", lambda cid, tok: None)

    assert lic.resolve_live_systems("default") == []


def test_servicenow_derived_from_oauth_config_when_no_url(monkeypatch):
    """ServiceNow ingests live from OAuth alone — its host is derived from the
    connector's OAuth config (SERVICENOW_INSTANCE), needing no SERVICENOW_URL env."""
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    monkeypatch.delenv("SERVICENOW_URL", raising=False)

    async def fake_get_token(org_id, connector_id):
        if connector_id == "servicenow":
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)
    monkeypatch.setattr(lic, "get_connector_instance_url", lambda o, c: None)
    monkeypatch.setattr(lic, "store_connector_instance_url", lambda o, c, u: None)

    live = lic.resolve_live_systems("default")

    assert live == ["servicenow"]
    cred = get_live_connector("servicenow")
    expected = lic.capture_instance_url(
        "servicenow", None, CONNECTOR_AUTH_CONFIGS["servicenow"].token_url
    )
    assert cred["url"] == expected
    assert cred["token"] == "servicenow-access"


def test_jira_derived_on_demand_when_no_url(monkeypatch):
    """Jira ingests live from OAuth alone — the api.atlassian.com gateway base is
    discovered on demand from the token, needing no JIRA_URL env."""
    monkeypatch.delenv("JIRA_URL", raising=False)

    async def fake_get_token(org_id, connector_id):
        if connector_id == "jira":
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    async def fake_gateway(token, **kw):
        assert token == "jira-access"
        return "https://api.atlassian.com/ex/jira/abc-123"

    monkeypatch.setattr(lic, "get_token", fake_get_token)
    monkeypatch.setattr(lic, "get_connector_instance_url", lambda o, c: None)
    monkeypatch.setattr(lic, "store_connector_instance_url", lambda o, c, u: None)
    monkeypatch.setattr(lic, "fetch_jira_gateway_base", fake_gateway)

    live = lic.resolve_live_systems("default")

    assert live == ["jira"]
    cred = get_live_connector("jira")
    assert cred["url"] == "https://api.atlassian.com/ex/jira/abc-123"
    assert cred["token"] == "jira-access"


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
        if connector_id in ("jira", "slack", "teams", "github", "java_app", "dotnet_app"):
            raise ConnectorNotAuthenticatedError(org_id, connector_id)
        return _token(connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    live = lic.resolve_live_systems("default")

    assert live == ["servicenow"]


# ─────────────────────────────────────────────────────────────────────────────
# Slack — URL-less SaaS connector (R16-A2 / live-ingest wiring)
# ─────────────────────────────────────────────────────────────────────────────
def test_slack_resolved_by_token_without_url(monkeypatch):
    """Slack is URL-less: when authenticated it joins the live set keyed by token
    alone (no instance URL), published to the per-run context."""
    async def fake_get_token(org_id, connector_id):
        if connector_id == "slack":
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    live = lic.resolve_live_systems("default")

    assert live == ["slack"]
    cred = get_live_connector("slack")
    assert cred == {"token": "slack-access"}
    assert "url" not in cred  # Slack's Web API host is global — no per-org URL


def test_slack_excluded_when_not_authenticated(monkeypatch):
    """Unconnected Slack is left out and publishes no per-run credential."""
    async def fake_get_token(org_id, connector_id):
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    assert lic.resolve_live_systems("default") == []
    assert get_live_connector("slack") is None


def test_slack_and_servicenow_both_live(monkeypatch):
    """Slack (URL-less) coexists with a system of record so the corroboration
    path can elevate Slack via COR-06 in a live run."""
    monkeypatch.delenv("SERVICENOW_URL", raising=False)

    async def fake_get_token(org_id, connector_id):
        if connector_id in ("servicenow", "slack"):
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)
    monkeypatch.setattr(lic, "get_connector_instance_url", lambda o, c: None)
    monkeypatch.setattr(lic, "store_connector_instance_url", lambda o, c, u: None)

    live = lic.resolve_live_systems("default")

    assert set(live) == {"servicenow", "slack"}
    assert get_live_connector("slack") == {"token": "slack-access"}
    assert get_live_connector("servicenow")["token"] == "servicenow-access"


# ─────────────────────────────────────────────────────────────────────────────
# Microsoft Teams — URL-less SaaS connector (R17-A1). Mirrors Slack: a connected
# Teams must join the live set (token only) so the discovery run ingests it and
# the Discovery Log's "Using authenticated connectors" lists it — previously
# Teams was authenticated but never resolved, so it showed in the catalog/progress
# yet was silently dropped from the live ingest.
# ─────────────────────────────────────────────────────────────────────────────
def test_teams_resolved_by_token_without_url(monkeypatch):
    """Teams is URL-less: when authenticated it joins the live set keyed by token
    alone (no instance URL), published to the per-run context that
    TeamsIngestor._client reads."""
    async def fake_get_token(org_id, connector_id):
        if connector_id == "teams":
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    live = lic.resolve_live_systems("default")

    assert live == ["teams"]
    cred = get_live_connector("teams")
    assert cred == {"token": "teams-access"}
    assert "url" not in cred  # Microsoft Graph host is global — no per-org URL


def test_teams_excluded_when_not_authenticated(monkeypatch):
    """Unconnected Teams is left out and publishes no per-run credential."""
    async def fake_get_token(org_id, connector_id):
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    assert lic.resolve_live_systems("default") == []
    assert get_live_connector("teams") is None


def test_teams_and_salesforce_both_live(monkeypatch):
    """Teams (URL-less) coexists with a system of record so the Discovery Log
    lists both as authenticated connectors and corroboration can run."""
    monkeypatch.setenv("SF_INSTANCE_URL", "https://sf.example.com")

    async def fake_get_token(org_id, connector_id):
        if connector_id in ("salesforce", "teams"):
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    live = lic.resolve_live_systems("default")

    assert set(live) == {"salesforce", "teams"}
    assert get_live_connector("teams") == {"token": "teams-access"}
    assert get_live_connector("salesforce")["token"] == "salesforce-access"


# ─────────────────────────────────────────────────────────────────────────────
# GitHub — URL-less, vault-direct connector (T1-S12 live-path wiring)
# ─────────────────────────────────────────────────────────────────────────────
def test_github_resolved_promotes_run_to_live(monkeypatch):
    """GitHub joins the live set when authenticated so the run is promoted to live
    (INGEST_MODE=live). It reads its token straight from the vault, so — unlike
    Slack — it is NOT published to the per-run context."""
    async def fake_get_token(org_id, connector_id):
        if connector_id == "github":
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    live = lic.resolve_live_systems("default")

    assert live == ["github"]
    # GitHub fetches from the vault itself — nothing in the per-run context.
    assert get_live_connector("github") is None


def test_github_excluded_when_not_authenticated(monkeypatch):
    """Unconnected GitHub is left out — the run is not forced live by it."""
    async def fake_get_token(org_id, connector_id):
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    assert lic.resolve_live_systems("default") == []
    assert get_live_connector("github") is None


def test_github_and_slack_both_live_without_url(monkeypatch):
    """The two URL-less connectors resolve together; Slack is in the per-run
    context (it reads it), GitHub is not (it reads the vault directly)."""
    async def fake_get_token(org_id, connector_id):
        if connector_id in ("github", "slack"):
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    live = lic.resolve_live_systems("default")

    assert set(live) == {"slack", "github"}
    assert get_live_connector("slack") == {"token": "slack-access"}
    assert get_live_connector("github") is None


# ─────────────────────────────────────────────────────────────────────────────
# Java application — URL-less, vault-credentialed source (R17-A3 live-path wiring)
# ─────────────────────────────────────────────────────────────────────────────
def test_java_app_resolved_by_token_without_url(monkeypatch):
    """The Java app source is URL-less here (each target carries its own
    actuator_url/log_source in config); when its credential is in the vault it
    joins the live set, keyed by token in the per-run context so the ingestor
    resolves the secret safely (never from config — AC3)."""
    async def fake_get_token(org_id, connector_id):
        if connector_id == "java_app":
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    live = lic.resolve_live_systems("default")

    assert live == ["java_app"]
    assert get_live_connector("java_app") == {"token": "java_app-access"}


def test_java_app_excluded_when_not_authenticated(monkeypatch):
    """Unconnected Java app is left out and publishes no per-run credential."""
    async def fake_get_token(org_id, connector_id):
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    assert lic.resolve_live_systems("default") == []
    assert get_live_connector("java_app") is None


# ─────────────────────────────────────────────────────────────────────────────
# .NET application — URL-less, vault-credentialed source (R17-A4 live-path wiring)
# ─────────────────────────────────────────────────────────────────────────────
def test_dotnet_app_resolved_by_token_without_url(monkeypatch):
    """The .NET app source is URL-less here (each target carries its own
    diagnostics_url/log_source in config); when its credential is in the vault it
    joins the live set, keyed by token in the per-run context so the ingestor
    resolves the secret safely (never from config)."""
    async def fake_get_token(org_id, connector_id):
        if connector_id == "dotnet_app":
            return _token(connector_id)
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    live = lic.resolve_live_systems("default")

    assert live == ["dotnet_app"]
    assert get_live_connector("dotnet_app") == {"token": "dotnet_app-access"}


def test_dotnet_app_excluded_when_not_authenticated(monkeypatch):
    """Unconnected .NET app is left out and publishes no per-run credential."""
    async def fake_get_token(org_id, connector_id):
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    monkeypatch.setattr(lic, "get_token", fake_get_token)

    assert lic.resolve_live_systems("default") == []
    assert get_live_connector("dotnet_app") is None
