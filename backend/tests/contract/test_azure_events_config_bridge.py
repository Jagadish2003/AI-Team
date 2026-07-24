"""MSP-B2 — Integration Hub → AzureEventConfig bridge.

The forensic gap this suite closes: an Owner could connect Azure through the
Integration Hub (Test Connection → Create → pin subscriptions), which vaults the
service principal and writes ``environment``/``mode``/``scopes`` onto the
``azure_events`` connector record — yet the two gates that decide whether Azure
ingests (``live_ingest_credentials._resolve_azure_events`` and
``azure_events.build_ingestor``) read config ONLY from the ``AZURE_EVENT_CONFIG``
env var / offline fixture, never the connector record. So a UI-connected connector
never entered ``_systems`` and no poller was ever built.

These tests prove the bridge:

  * ``config_from_connector_record`` turns a connected Hub record's pinned
    subscriptions into an ``AzureEventConfig`` (and refuses non-connected / empty /
    candidate-only records — the pinned-only, never-auto-growing discipline holds);
  * ``resolve_azure_event_config`` prefers the explicit env/fixture config and falls
    back to the connector record;
  * ``_resolve_azure_events`` promotes ``azure_events`` into the live systems set
    from the record + a vaulted service principal;
  * ``build_ingestor`` builds a real poller from the record.

FAKE CREDENTIALS: every value below is a non-real, test-only stub.
"""
from __future__ import annotations

import pytest

from app import live_ingest_credentials as lic
from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg


SUB_A = "11111111-2222-3333-4444-555555555555"
SUB_B = "66666666-7777-8888-9999-000000000000"


def _connected_record(*, scopes, environment="AzureCloud", mode="lighthouse", status="connected"):
    return {
        "status": status,
        "environment": environment,
        "mode": mode,
        "scopes": [{"scope_id": s, "kind": "azure_subscription"} for s in scopes],
        # candidate_scopes must NEVER be ingested — included here to prove exclusion.
        "candidate_scopes": ["cccccccc-dddd-eeee-ffff-000000000000"],
    }


# ── config_from_connector_record (pure) ──────────────────────────────────────────


class TestConfigFromConnectorRecord:
    def test_connected_record_with_pinned_subscriptions_bridges(self):
        record = _connected_record(scopes=[SUB_A, SUB_B])
        config = cfg.config_from_connector_record(record, org_id="default")
        assert config is not None
        # Pinned subscriptions in pinned order; candidate_scopes excluded.
        assert config.pinned_subscriptions == [SUB_A, SUB_B]
        assert config.mode == cfg.MODE_LIGHTHOUSE
        assert config.environment.name == "AzureCloud"
        assert config.credential_ref == cfg.CONNECTOR_ID
        assert config.metadata.get("source") == "integration_hub"

    def test_candidate_scopes_are_never_ingested(self):
        # A record with ONLY candidate (delegated-but-unapproved) scopes has no
        # pinned set, so it bridges to nothing — forward-only activation (AC4/AC7).
        record = {"status": "connected", "scopes": [], "candidate_scopes": [SUB_A]}
        assert cfg.config_from_connector_record(record, org_id="default") is None

    def test_not_connected_record_does_not_bridge(self):
        record = _connected_record(scopes=[SUB_A], status="needs_auth")
        assert cfg.config_from_connector_record(record, org_id="default") is None

    def test_connected_but_no_scopes_does_not_bridge(self):
        record = _connected_record(scopes=[])
        assert cfg.config_from_connector_record(record, org_id="default") is None

    def test_none_and_empty_record_bridge_to_none(self):
        assert cfg.config_from_connector_record(None, org_id="default") is None
        assert cfg.config_from_connector_record({}, org_id="default") is None

    def test_duplicate_scope_ids_deduplicated_in_order(self):
        record = _connected_record(scopes=[SUB_A, SUB_A, SUB_B])
        config = cfg.config_from_connector_record(record, org_id="default")
        assert config.pinned_subscriptions == [SUB_A, SUB_B]


# ── resolve_azure_event_config (env/fixture first, then the record) ───────────────


class TestResolvePrecedence:
    def test_falls_back_to_connector_record_when_no_env_config(self, monkeypatch):
        # Force the explicit path to yield nothing: live mode + no AZURE_EVENT_CONFIG.
        monkeypatch.setattr(cfg, "is_live", lambda: True)
        record = _connected_record(scopes=[SUB_A])
        config = cfg.resolve_azure_event_config(
            "default", env={}, record_loader=lambda org: record
        )
        assert config is not None
        assert config.pinned_subscriptions == [SUB_A]
        assert config.metadata.get("source") == "integration_hub"

    def test_explicit_env_config_wins_over_record(self, monkeypatch):
        monkeypatch.setattr(cfg, "is_live", lambda: True)
        env = {
            cfg._CONFIG_ENV: (
                '{"default": {"environment": "AzureCloud", "mode": "direct",'
                f' "subscriptions": ["{SUB_B}"]}}}}'
            )
        }
        # The record would bridge to SUB_A, but the explicit env config (SUB_B) wins.
        record = _connected_record(scopes=[SUB_A])
        config = cfg.resolve_azure_event_config(
            "default", env=env, record_loader=lambda org: record
        )
        assert config.pinned_subscriptions == [SUB_B]
        assert config.metadata.get("source") != "integration_hub"

    def test_no_env_and_no_record_yields_none(self, monkeypatch):
        monkeypatch.setattr(cfg, "is_live", lambda: True)
        config = cfg.resolve_azure_event_config(
            "default", env={}, record_loader=lambda org: None
        )
        assert config is None


# ── _resolve_azure_events promotes azure_events from the Hub record ───────────────


class TestLiveSystemsPromotion:
    def _arrange(self, monkeypatch, *, record, sp_complete):
        monkeypatch.setattr(cfg, "is_live", lambda: True)
        monkeypatch.delenv(cfg._CONFIG_ENV, raising=False)
        monkeypatch.setattr(cfg, "_default_record_loader", lambda org: record)

        sp = ae.AzureServicePrincipal(
            client_id="app" if sp_complete else "",
            client_secret="secret" if sp_complete else "",
            tenant_id="tenant" if sp_complete else "",
        )
        monkeypatch.setattr(ae, "get_service_principal", lambda org_id, **kw: sp)

    def test_promoted_when_record_bridges_and_sp_present(self, monkeypatch):
        self._arrange(
            monkeypatch, record=_connected_record(scopes=[SUB_A]), sp_complete=True
        )
        live: list[str] = []
        lic._resolve_azure_events("default", live)
        assert "azure_events" in live

    def test_not_promoted_when_no_pinned_subscriptions(self, monkeypatch):
        self._arrange(
            monkeypatch, record=_connected_record(scopes=[]), sp_complete=True
        )
        live: list[str] = []
        lic._resolve_azure_events("default", live)
        assert "azure_events" not in live

    def test_not_promoted_when_service_principal_incomplete(self, monkeypatch):
        self._arrange(
            monkeypatch, record=_connected_record(scopes=[SUB_A]), sp_complete=False
        )
        live: list[str] = []
        lic._resolve_azure_events("default", live)
        assert "azure_events" not in live


# ── build_ingestor builds a poller from the Hub record ────────────────────────────


class TestBuildIngestorFromRecord:
    def test_ingestor_built_from_connector_record(self, monkeypatch):
        monkeypatch.setattr(cfg, "is_live", lambda: True)
        monkeypatch.delenv(cfg._CONFIG_ENV, raising=False)
        monkeypatch.setattr(
            cfg, "_default_record_loader", lambda org: _connected_record(scopes=[SUB_A])
        )
        ingestor = ae.build_ingestor("default")
        assert ingestor is not None
        assert ingestor.config.pinned_subscriptions == [SUB_A]

    def test_no_ingestor_when_record_absent(self, monkeypatch):
        monkeypatch.setattr(cfg, "is_live", lambda: True)
        monkeypatch.delenv(cfg._CONFIG_ENV, raising=False)
        monkeypatch.setattr(cfg, "_default_record_loader", lambda org: None)
        assert ae.build_ingestor("default") is None
