"""
MSP-B2 T4 (AT-651) — Azure environment-aware endpoint configuration.

Config-level tests only — no live Azure dependency. Verify the SINGLE shared Azure
environment map (``app.azure_environments``) resolves Commercial and US Government
endpoints, is configuration-driven with a Commercial default, rejects unknown
clouds, carries no credentials, and is the ONE map the Azure Event Connector reuses
(no duplicate endpoint definitions).

Acceptance criteria:
  T4-AC1 — Azure Commercial endpoints resolve correctly.
  T4-AC2 — Azure US Government endpoints resolve correctly.
  T4-AC3 — Environment selection is configuration-driven (default Commercial).
  T4-AC4 — Configuration tests validate endpoint resolution (this file).
"""
from __future__ import annotations

import pytest

from app import azure_environments as az
from discovery.ingest import azure_events_config as cfg


# ── T4-AC1 — Azure Commercial endpoints ─────────────────────────────────────────


class TestAC1Commercial:

    def test_authority_and_arm(self):
        env = az.resolve_environment(az.AZURE_CLOUD)
        assert env.name == "AzureCloud"
        assert env.authority_host == "login.microsoftonline.com"
        assert env.resource_manager == "https://management.azure.com"

    def test_arm_scope(self):
        env = az.resolve_environment(az.AZURE_CLOUD)
        assert env.arm_scope == "https://management.azure.com/.default"

    def test_token_endpoint(self):
        env = az.resolve_environment(az.AZURE_CLOUD)
        assert env.token_endpoint("tenant-1") == (
            "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
        )

    def test_subscriptions_url(self):
        env = az.resolve_environment(az.AZURE_CLOUD)
        assert env.subscriptions_url().startswith("https://management.azure.com/subscriptions?api-version=")


# ── T4-AC2 — Azure US Government endpoints ───────────────────────────────────────


class TestAC2UsGovernment:

    def test_authority_and_arm(self):
        env = az.resolve_environment(az.AZURE_US_GOVERNMENT)
        assert env.name == "AzureUSGovernment"
        assert env.authority_host == "login.microsoftonline.us"
        assert env.resource_manager == "https://management.usgovcloudapi.net"

    def test_arm_scope(self):
        env = az.resolve_environment(az.AZURE_US_GOVERNMENT)
        assert env.arm_scope == "https://management.usgovcloudapi.net/.default"

    def test_token_endpoint(self):
        env = az.resolve_environment(az.AZURE_US_GOVERNMENT)
        assert env.token_endpoint("t9") == (
            "https://login.microsoftonline.us/t9/oauth2/v2.0/token"
        )

    def test_gov_differs_from_commercial(self):
        gov = az.resolve_environment(az.AZURE_US_GOVERNMENT)
        com = az.resolve_environment(az.AZURE_CLOUD)
        assert gov.authority_host != com.authority_host
        assert gov.resource_manager != com.resource_manager
        assert gov.arm_scope != com.arm_scope


# ── T4-AC3 — configuration-driven selection ─────────────────────────────────────


class TestAC3ConfigDriven:

    def test_default_is_commercial(self):
        assert az.DEFAULT_ENVIRONMENT == az.AZURE_CLOUD
        assert az.resolve_environment(None).name == "AzureCloud"
        assert az.resolve_environment("").name == "AzureCloud"
        assert az.resolve_environment("   ").name == "AzureCloud"

    def test_selection_by_configured_name(self):
        assert az.resolve_environment("AzureCloud").name == "AzureCloud"
        assert az.resolve_environment("AzureUSGovernment").name == "AzureUSGovernment"

    def test_unknown_environment_raises(self):
        with pytest.raises(az.UnknownAzureEnvironmentError):
            az.resolve_environment("AzureChinaCloud")

    def test_list_environments(self):
        assert set(az.list_environments()) == {"AzureCloud", "AzureUSGovernment"}

    def test_no_conditional_endpoint_logic_needed(self):
        """Resolution is table-driven: every supported env yields complete endpoints."""
        for name in az.list_environments():
            env = az.resolve_environment(name)
            assert env.authority_host and env.resource_manager and env.arm_scope


# ── Security — environment config carries no credentials ────────────────────────


class TestNoCredentialsInEnvironment:

    def test_environment_has_only_endpoint_metadata(self):
        env = az.resolve_environment(az.AZURE_CLOUD)
        fields = set(vars(env).keys())
        # Only endpoint/metadata fields — nothing secret-shaped.
        assert fields == {"name", "authority_host", "resource_manager"}
        secretish = {"secret", "key", "password", "token", "credential", "client_secret"}
        assert not (secretish & {f.lower() for f in fields})


# ── Reuse — the connector consumes the SHARED map (no duplicate) ─────────────────


class TestSingleSharedMap:

    def test_connector_reuses_shared_environment_map(self):
        # Same objects, not a parallel copy — one source of truth.
        assert cfg.ENVIRONMENTS is az.ENVIRONMENTS
        assert cfg.AzureEnvironment is az.AzureEnvironment
        assert cfg.AZURE_CLOUD == az.AZURE_CLOUD
        assert cfg.AZURE_US_GOVERNMENT == az.AZURE_US_GOVERNMENT
        assert cfg.DEFAULT_ENVIRONMENT == az.DEFAULT_ENVIRONMENT

    def test_connector_resolver_delegates_to_shared_map(self):
        assert cfg.resolve_environment("AzureUSGovernment") is az.ENVIRONMENTS["AzureUSGovernment"]

    def test_connector_resolver_wraps_error_as_config_error(self):
        # The connector surfaces a bad environment as its own config error type.
        with pytest.raises(cfg.AzureEventConfigError):
            cfg.resolve_environment("NotACloud")

    def test_config_selects_environment_from_configuration(self):
        """An AzureEventConfig built from configuration resolves the right endpoints."""
        c = cfg.AzureEventConfig(
            environment=cfg.resolve_environment("AzureUSGovernment"),
            mode=cfg.MODE_DIRECT,
            subscriptions=["s1"],
        )
        assert c.environment.arm_scope == "https://management.usgovcloudapi.net/.default"
