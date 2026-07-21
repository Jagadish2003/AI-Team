"""
MSP-B2 T1 (AT-648) — Azure Event Connector authentication + subscription discipline.

Offline / DB-free: the vault reader and the token exchange are injected, so these
tests exercise the auth flow and the pinned-subscription discipline without a live
vault, network, or database.

Acceptance criteria:
  T1-AC1 — Azure auth uses vault-held service principal credentials.
  T1-AC2 — ARM-scoped access tokens are acquired.
  T1-AC3 — Azure Lighthouse and direct subscription modes are supported.
  T1-AC4 — Subscription list stays explicitly configured; it never auto-expands.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg


@pytest.fixture(autouse=True)
def _offline_by_default(monkeypatch):
    """Pin INGEST_MODE=offline per test so the offline-fixture path is deterministic.

    Importing app.auth.* elsewhere in the session runs load_dotenv, which can pull
    an ambient INGEST_MODE from a local backend/.env; the live-mode tests below
    override this in-body via monkeypatch.
    """
    monkeypatch.setenv("INGEST_MODE", "offline")


# ── helpers ──────────────────────────────────────────────────────────────────


def _sp_record(client_id="app-123", secret="sp-secret", tenant="tenant-abc"):
    """A stand-in for the vault's StaticCredentialRecord (username/secret/base_url)."""
    return SimpleNamespace(username=client_id, secret=secret, base_url=tenant)


def _vault_with(record):
    return lambda org_id, connector_id: record


def _config(environment=cfg.AZURE_CLOUD, mode=cfg.MODE_LIGHTHOUSE, subs=None):
    return cfg.AzureEventConfig(
        environment=cfg.resolve_environment(environment),
        mode=mode,
        subscriptions=subs if subs is not None else ["sub-1", "sub-2"],
    )


def _capturing_token_fn(captured):
    async def _fn(*, token_url, client_id, client_secret, scope):
        captured.update(
            token_url=token_url, client_id=client_id,
            client_secret=client_secret, scope=scope,
        )
        return {"access_token": "ARM_TOKEN", "expires_in": 3600}
    return _fn


# ── T1-AC1 — vault-held service principal ───────────────────────────────────────


class TestAC1VaultServicePrincipal:

    def test_resolves_service_principal_from_vault(self):
        sp = ae.get_service_principal("org1", vault_reader=_vault_with(_sp_record()))
        assert sp is not None
        assert sp.client_id == "app-123"
        assert sp.tenant_id == "tenant-abc"
        assert sp.client_secret == "sp-secret"
        assert sp.is_complete()

    def test_none_when_no_credential_stored(self):
        assert ae.get_service_principal("org1", vault_reader=lambda o, c: None) is None

    def test_secret_never_in_repr(self):
        sp = ae.AzureServicePrincipal("app-123", "TOP-SECRET", "tenant-abc")
        assert "TOP-SECRET" not in repr(sp)
        assert "***" in repr(sp)

    def test_missing_sp_raises_on_token_acquire(self):
        with pytest.raises(ae.AzureAuthError):
            asyncio.run(ae.acquire_arm_token(
                "org1", _config(), vault_reader=lambda o, c: None,
                token_fn=_capturing_token_fn({}),
            ))


# ── T1-AC2 — ARM-scoped token acquisition ───────────────────────────────────────


class TestAC2ArmToken:

    def test_acquires_arm_scoped_token(self):
        captured: dict = {}
        token = asyncio.run(ae.acquire_arm_token(
            "org1", _config(), vault_reader=_vault_with(_sp_record()),
            token_fn=_capturing_token_fn(captured),
        ))
        assert token == "ARM_TOKEN"
        # ARM .default scope (not Graph, not a delegated scope).
        assert captured["scope"] == "https://management.azure.com/.default"
        assert captured["client_id"] == "app-123"

    def test_token_minted_against_sp_home_tenant(self):
        captured: dict = {}
        asyncio.run(ae.acquire_arm_token(
            "org1", _config(), vault_reader=_vault_with(_sp_record(tenant="tenant-abc")),
            token_fn=_capturing_token_fn(captured),
        ))
        assert captured["token_url"] == (
            "https://login.microsoftonline.com/tenant-abc/oauth2/v2.0/token"
        )

    def test_empty_access_token_raises(self):
        async def _empty(*, token_url, client_id, client_secret, scope):
            return {"access_token": ""}
        with pytest.raises(ae.AzureAuthError):
            asyncio.run(ae.acquire_arm_token(
                "org1", _config(), vault_reader=_vault_with(_sp_record()), token_fn=_empty,
            ))

    def test_reuses_shared_oauth_client_credentials_exchange(self, monkeypatch):
        """The default token path calls the SHARED oauth primitive — no duplicate auth."""
        calls: dict = {}

        async def _fake(*, connector_id, token_url, client_id, client_secret, scopes):
            calls.update(connector_id=connector_id, token_url=token_url, scopes=scopes)
            return {"access_token": "ARM_TOKEN"}

        import app.auth.oauth as oauth
        monkeypatch.setattr(oauth, "request_client_credentials_token", _fake)
        token = asyncio.run(ae.acquire_arm_token(
            "org1", _config(), vault_reader=_vault_with(_sp_record()),
        ))
        assert token == "ARM_TOKEN"
        assert calls["connector_id"] == "azure_events"
        assert calls["scopes"] == ["https://management.azure.com/.default"]


# ── T1-AC3 — Lighthouse + direct modes ──────────────────────────────────────────


class TestAC3Modes:

    @pytest.mark.parametrize("mode", [cfg.MODE_LIGHTHOUSE, cfg.MODE_DIRECT])
    def test_both_modes_acquire_token(self, mode):
        captured: dict = {}
        token = asyncio.run(ae.acquire_arm_token(
            "org1", _config(mode=mode), vault_reader=_vault_with(_sp_record()),
            token_fn=_capturing_token_fn(captured),
        ))
        assert token == "ARM_TOKEN"

    def test_unknown_mode_rejected(self):
        with pytest.raises(cfg.AzureEventConfigError):
            _config(mode="hybrid")

    def test_lighthouse_token_minted_against_home_tenant_for_many_subs(self):
        """Lighthouse: one SP/home tenant, many pinned subscriptions."""
        captured: dict = {}
        config = _config(mode=cfg.MODE_LIGHTHOUSE, subs=["s1", "s2", "s3"])
        asyncio.run(ae.acquire_arm_token(
            "org1", config, vault_reader=_vault_with(_sp_record(tenant="smx-tenant")),
            token_fn=_capturing_token_fn(captured),
        ))
        assert "smx-tenant" in captured["token_url"]
        assert config.pinned_subscriptions == ["s1", "s2", "s3"]


# ── T1-AC4 — pinned subscriptions, never auto-expanding ──────────────────────────


class TestAC4SubscriptionPinning:

    def test_authorized_subscriptions_are_the_pinned_set(self):
        ing = ae.AzureEventIngestor("org1", _config(subs=["s1", "s2"]))
        assert ing.authorized_subscriptions() == ["s1", "s2"]

    def test_discovery_never_auto_expands_ingested_set(self):
        config = _config(subs=["s1", "s2"])
        # Discovery finds a THIRD, newly delegated subscription.
        discovered = ["s1", "s2", "s3-new"]
        assert config.filter_to_pinned(discovered) == ["s1", "s2"]  # s3 excluded
        assert config.newly_delegated(discovered) == ["s3-new"]     # reported, not ingested

    def test_pending_delegated_reported_not_ingested(self):
        ing = ae.AzureEventIngestor("org1", _config(subs=["s1"]))
        pending = ing.pending_delegated_subscriptions(["s1", "s9", "s10"])
        assert pending == ["s9", "s10"]
        assert "s9" not in ing.authorized_subscriptions()

    def test_discover_delegated_filters_to_pinned_when_lister_injected(self):
        config = _config(subs=["s1"])
        ing = ae.AzureEventIngestor(
            "org1", config, vault_reader=_vault_with(_sp_record()),
            token_fn=_capturing_token_fn({}),
        )

        async def _lister(token, cfg_):
            return ["s1", "s2-new"]

        discovered = asyncio.run(ing.discover_delegated_subscriptions(arm_lister=_lister))
        # discovery returns candidates; the ingested set stays pinned.
        assert set(discovered) == {"s1", "s2-new"}
        assert ing.authorized_subscriptions() == ["s1"]
        assert config.filter_to_pinned(discovered) == ["s1"]

    def test_is_pinned(self):
        config = _config(subs=["s1", "s2"])
        assert config.is_pinned("s1") is True
        assert config.is_pinned("s3") is False


# ── Environment resolution (AzureCloud / AzureUSGovernment) — AC8 foundation ─────


class TestEnvironmentResolution:

    def test_azure_cloud_endpoints(self):
        env = cfg.resolve_environment(cfg.AZURE_CLOUD)
        assert env.authority_host == "login.microsoftonline.com"
        assert env.resource_manager == "https://management.azure.com"
        assert env.arm_scope == "https://management.azure.com/.default"
        assert env.token_endpoint("t1") == "https://login.microsoftonline.com/t1/oauth2/v2.0/token"

    def test_us_government_endpoints(self):
        env = cfg.resolve_environment(cfg.AZURE_US_GOVERNMENT)
        assert env.authority_host == "login.microsoftonline.us"
        assert env.resource_manager == "https://management.usgovcloudapi.net"
        assert env.arm_scope == "https://management.usgovcloudapi.net/.default"
        assert env.token_endpoint("t1") == "https://login.microsoftonline.us/t1/oauth2/v2.0/token"

    def test_default_is_azure_cloud(self):
        assert cfg.resolve_environment(None).name == cfg.AZURE_CLOUD

    def test_unknown_environment_rejected(self):
        with pytest.raises(cfg.AzureEventConfigError):
            cfg.resolve_environment("AzureChinaCloud")

    def test_gov_token_url_used_when_gov_environment(self):
        captured: dict = {}
        config = _config(environment=cfg.AZURE_US_GOVERNMENT, mode=cfg.MODE_DIRECT)
        asyncio.run(ae.acquire_arm_token(
            "gov_org", config, vault_reader=_vault_with(_sp_record(tenant="gov-tenant")),
            token_fn=_capturing_token_fn(captured),
        ))
        assert captured["token_url"].startswith("https://login.microsoftonline.us/gov-tenant/")
        assert captured["scope"] == "https://management.usgovcloudapi.net/.default"


# ── Config loading + no-secret-in-config guard ──────────────────────────────────


class TestConfigLoading:

    def test_offline_fixture_default_org(self):
        config = cfg.load_azure_event_config("some-org")  # offline → fixture default
        assert config is not None
        assert config.environment.name == cfg.AZURE_CLOUD
        assert config.mode == cfg.MODE_LIGHTHOUSE
        assert len(config.subscriptions) == 2

    def test_offline_fixture_gov_org(self):
        config = cfg.load_azure_event_config("gov_org")
        assert config.environment.name == cfg.AZURE_US_GOVERNMENT
        assert config.mode == cfg.MODE_DIRECT

    def test_inline_secret_in_config_rejected(self):
        with pytest.raises(cfg.AzureEventConfigError):
            cfg._coerce_config({
                "environment": "AzureCloud", "mode": "direct",
                "subscriptions": ["s1"], "client_secret": "leaked",  # forbidden
            })

    def test_live_config_env_org_keyed(self, monkeypatch):
        monkeypatch.setenv("INGEST_MODE", "live")
        env = {"AZURE_EVENT_CONFIG": (
            '{"org7": {"environment": "AzureUSGovernment", "mode": "direct", '
            '"subscriptions": ["subA"]}}'
        )}
        config = cfg.load_azure_event_config("org7", env=env)
        assert config is not None
        assert config.environment.name == cfg.AZURE_US_GOVERNMENT
        assert config.subscriptions == ["subA"]

    def test_not_configured_org_returns_none(self, monkeypatch):
        monkeypatch.setenv("INGEST_MODE", "live")
        assert cfg.load_azure_event_config("nobody", env={"AZURE_EVENT_CONFIG": ""}) is None


# ── build_ingestor wiring ────────────────────────────────────────────────────────


class TestBuildIngestor:

    def test_build_ingestor_offline(self):
        ing = ae.build_ingestor("some-org")
        assert isinstance(ing, ae.AzureEventIngestor)
        assert ing.connector_id == "azure_events"
        assert len(ing.authorized_subscriptions()) == 2

    def test_ingest_is_t2_t3_seam(self):
        ing = ae.build_ingestor("some-org")
        with pytest.raises(NotImplementedError):
            asyncio.run(ing.ingest())
