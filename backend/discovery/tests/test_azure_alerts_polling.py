"""
MSP-B2 T2 (AT-649) — Azure Monitor Alerts polling with per-subscription checkpoints.

Offline / DB-free: the alerts client and the ARM token are injected, so the poll →
filter → map → emit → checkpoint flow is exercised without a live Azure or a network.

Acceptance criteria:
  T2-AC1 — Azure Monitor Alerts are successfully polled.
  T2-AC2 — Per-subscription checkpoints prevent duplicate processing.
  T2-AC3 — Alerts are normalized using map_azure_monitor().
  T2-AC4 — Only newly generated alerts are ingested during subsequent runs.
"""
from __future__ import annotations

import pytest

from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg
from discovery.ingest import azure_alerts
from discovery.ingest.base import Checkpoint


# ── fakes / builders ─────────────────────────────────────────────────────────


def _alert(sub, aid, fired, *, rule="prod-latency", sev="Sev2", condition="Fired"):
    """A minimal Azure common-alert-schema alert for subscription ``sub``."""
    return {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {"essentials": {
            "alertId": f"/subscriptions/{sub}/providers/Microsoft.AlertsManagement/alerts/{aid}",
            "alertRule": rule,
            "severity": sev,
            "signalType": "Metric",
            "monitorCondition": condition,
            "alertTargetIDs": [
                f"/subscriptions/{sub}/resourceGroups/rg/providers/Microsoft.Web/sites/app"
            ],
            "firedDateTime": fired,
            "description": "seeded alert",
        }},
    }


class FakeAlertsClient:
    """Returns configured alerts per subscription; can fail specific subscriptions."""

    def __init__(self, by_sub=None):
        self.by_sub = dict(by_sub or {})
        self.fail_subs = set()
        self.calls = []

    def fetch_alerts(self, *, token, subscription_id, environment, since_iso):
        self.calls.append({"sub": subscription_id, "since": since_iso, "token": token})
        if subscription_id in self.fail_subs:
            raise RuntimeError("throttled (429)")
        return list(self.by_sub.get(subscription_id, []))


def _config(subs, environment=cfg.AZURE_CLOUD):
    return cfg.AzureEventConfig(
        environment=cfg.resolve_environment(environment),
        mode=cfg.MODE_LIGHTHOUSE,
        subscriptions=list(subs),
    )


def _ingestor(subs, client):
    return ae.AzureEventIngestor("org1", _config(subs), alerts_client=client)


SUB_A = "aaaaaaaa-0000-0000-0000-000000000001"
SUB_B = "bbbbbbbb-0000-0000-0000-000000000002"


# ── T2-AC1 — alerts are polled ───────────────────────────────────────────────────


class TestAC1AlertsPolled:

    def test_alerts_are_polled_and_emitted(self):
        client = FakeAlertsClient({SUB_A: [
            _alert(SUB_A, "a1", "2026-06-01T12:00:00Z"),
            _alert(SUB_A, "a2", "2026-06-01T13:00:00Z"),
        ]})
        result = _ingestor([SUB_A], client).ingest_alerts(token="T")
        assert result.emitted_count == 2
        assert result.subscription_status[SUB_A]["status"] == "ok"
        assert result.subscription_status[SUB_A]["polled"] == 2

    def test_polls_each_pinned_subscription(self):
        client = FakeAlertsClient({SUB_A: [], SUB_B: []})
        _ingestor([SUB_A, SUB_B], client).ingest_alerts(token="T")
        assert {c["sub"] for c in client.calls} == {SUB_A, SUB_B}

    def test_empty_poll_is_ok_and_emits_nothing(self):
        client = FakeAlertsClient({SUB_A: []})
        result = _ingestor([SUB_A], client).ingest_alerts(token="T")
        assert result.emitted_count == 0
        assert result.all_ok


# ── T2-AC3 — normalization through map_azure_monitor ─────────────────────────────


class TestAC3Normalization:

    def test_record_carries_normalized_operational_event(self):
        client = FakeAlertsClient({SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")]})
        [rec] = _ingestor([SUB_A], client).ingest_alerts(token="T").records
        event = rec["event"]
        # Shape produced by map_azure_monitor (B0 OperationalEvent). The event's
        # source_system is the PROVIDER FAMILY (shared cloud-event skeleton AC4); the
        # mapper's per-stream source system rides the wrapper as `surface`.
        assert event["source_system"] == "azure"
        assert rec["surface"] == "azure_monitor"
        assert event["event_class"] == "state_change"
        assert event["org_id"] == "org1"
        assert event["event_signature"]              # deterministic fingerprint present
        assert "provenance" in event and event["provenance"]

    def test_map_azure_monitor_is_the_normaliser(self, monkeypatch):
        calls = {"n": 0}
        real = azure_alerts  # noqa: F841 — keep reference
        import discovery.signals.reference_mappers as rm
        orig = rm.map_azure_monitor

        def _spy(payload, *, org_id):
            calls["n"] += 1
            return orig(payload, org_id=org_id)

        monkeypatch.setattr(ae, "map_azure_monitor", _spy)
        client = FakeAlertsClient({SUB_A: [
            _alert(SUB_A, "a1", "2026-06-01T12:00:00Z"),
            _alert(SUB_A, "a2", "2026-06-01T13:00:00Z"),
        ]})
        _ingestor([SUB_A], client).ingest_alerts(token="T")
        assert calls["n"] == 2

    def test_account_scope_is_the_subscription(self):
        client = FakeAlertsClient({
            SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")],
            SUB_B: [_alert(SUB_B, "b1", "2026-06-01T12:30:00Z")],
        })
        result = _ingestor([SUB_A, SUB_B], client).ingest_alerts(token="T")
        scopes = {rec["account_scope"] for rec in result.records}
        assert scopes == {SUB_A, SUB_B}
        for rec in result.records:
            assert rec["provider"] == "azure"
            assert rec["provider_event_id"].endswith(("a1", "b1"))

    def test_no_invented_detector_fields(self):
        """The subscription (account scope) lives on the wrapper, not the event."""
        client = FakeAlertsClient({SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")]})
        [rec] = _ingestor([SUB_A], client).ingest_alerts(token="T").records
        assert "account_scope" not in rec["event"]
        assert "subscription_id" not in rec["event"]


# ── T2-AC2 / AC4 — per-subscription checkpoints, no duplicates, only new ─────────


class TestAC2AC4Checkpoints:

    def test_checkpoint_advances_to_newest_per_subscription(self):
        client = FakeAlertsClient({
            SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z"),
                    _alert(SUB_A, "a2", "2026-06-01T13:00:00Z")],
            SUB_B: [_alert(SUB_B, "b1", "2026-06-02T09:00:00Z")],
        })
        result = _ingestor([SUB_A, SUB_B], client).ingest_alerts(token="T")
        cps = ae.decode_checkpoints(result.next_checkpoint)
        assert cps[SUB_A] == "2026-06-01T13:00:00Z"   # newest for A
        assert cps[SUB_B] == "2026-06-02T09:00:00Z"   # independent, newest for B

    def test_second_run_ingests_no_duplicates(self):
        alerts = {SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z"),
                          _alert(SUB_A, "a2", "2026-06-01T13:00:00Z")]}
        ing = _ingestor([SUB_A], FakeAlertsClient(alerts))
        run1 = ing.ingest_alerts(token="T")
        assert run1.emitted_count == 2
        run2 = ing.ingest_alerts(token="T", checkpoint=run1.next_checkpoint)
        assert run2.emitted_count == 0                       # nothing re-read (AC2)
        assert ae.decode_checkpoints(run2.next_checkpoint) == ae.decode_checkpoints(run1.next_checkpoint)

    def test_only_newly_generated_alerts_on_subsequent_run(self):
        client = FakeAlertsClient({SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")]})
        ing = _ingestor([SUB_A], client)
        run1 = ing.ingest_alerts(token="T")
        assert run1.emitted_count == 1
        # A newer alert appears for the same subscription.
        client.by_sub[SUB_A].append(_alert(SUB_A, "a2", "2026-06-01T15:00:00Z"))
        run2 = ing.ingest_alerts(token="T", checkpoint=run1.next_checkpoint)
        assert run2.emitted_count == 1                       # only the new one (AC4)
        assert run2.records[0]["provider_event_id"].endswith("a2")

    def test_independent_checkpoints_across_subscriptions(self):
        client = FakeAlertsClient({
            SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")],
            SUB_B: [_alert(SUB_B, "b1", "2026-06-01T12:00:00Z")],
        })
        ing = _ingestor([SUB_A, SUB_B], client)
        run1 = ing.ingest_alerts(token="T")
        # Only B gets a new alert next round.
        client.by_sub[SUB_B].append(_alert(SUB_B, "b2", "2026-06-03T00:00:00Z"))
        run2 = ing.ingest_alerts(token="T", checkpoint=run1.next_checkpoint)
        assert run2.emitted_count == 1
        assert run2.records[0]["account_scope"] == SUB_B

    def test_unparseable_checkpoint_is_safe_full_reread(self):
        client = FakeAlertsClient({SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")]})
        result = _ingestor([SUB_A], client).ingest_alerts(token="T", checkpoint="not-json")
        assert result.emitted_count == 1                     # degraded to full re-read


# ── failure isolation + checkpoint safety ────────────────────────────────────────


class TestFailureIsolation:

    def test_one_failing_subscription_does_not_stop_others(self):
        client = FakeAlertsClient({
            SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")],
            SUB_B: [_alert(SUB_B, "b1", "2026-06-01T12:00:00Z")],
        })
        client.fail_subs.add(SUB_A)
        result = _ingestor([SUB_A, SUB_B], client).ingest_alerts(token="T")
        assert result.subscription_status[SUB_A]["status"] == "error"
        assert "throttled" in result.subscription_status[SUB_A]["error"]
        assert result.subscription_status[SUB_B]["status"] == "ok"
        assert result.emitted_count == 1                     # B still ingested
        assert result.records[0]["account_scope"] == SUB_B

    def test_failed_subscription_checkpoint_not_advanced(self):
        client = FakeAlertsClient({
            SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")],
            SUB_B: [_alert(SUB_B, "b1", "2026-06-01T12:00:00Z")],
        })
        ing = _ingestor([SUB_A, SUB_B], client)
        run1 = ing.ingest_alerts(token="T")            # both ok, both advanced
        prior = ae.decode_checkpoints(run1.next_checkpoint)
        client.fail_subs.add(SUB_A)
        client.by_sub[SUB_A].append(_alert(SUB_A, "a2", "2026-06-05T00:00:00Z"))
        run2 = ing.ingest_alerts(token="T", checkpoint=run1.next_checkpoint)
        after = ae.decode_checkpoints(run2.next_checkpoint)
        # A failed → its checkpoint is unchanged (will retry a2 next run).
        assert after[SUB_A] == prior[SUB_A]
        assert run2.failed_subscriptions == [SUB_A]

    def test_malformed_alert_is_skipped_not_fatal(self, monkeypatch):
        # A single alert whose normalisation raises must be loud-skipped without
        # failing the whole subscription; the good alert is still emitted.
        import discovery.signals.reference_mappers as rm
        orig = rm.map_azure_monitor

        def _selective(payload, *, org_id):
            if "a2" in azure_alerts.alert_id(payload):
                raise ValueError("boom")
            return orig(payload, org_id=org_id)

        monkeypatch.setattr(ae, "map_azure_monitor", _selective)
        client = FakeAlertsClient({SUB_A: [
            _alert(SUB_A, "a1", "2026-06-01T12:00:00Z"),
            _alert(SUB_A, "a2", "2026-06-01T13:00:00Z"),
        ]})
        result = _ingestor([SUB_A], client).ingest_alerts(token="T")
        assert result.subscription_status[SUB_A]["status"] == "ok"
        assert result.subscription_status[SUB_A]["skipped"] == 1
        assert result.emitted_count == 1
        # The checkpoint still advances past the skipped alert (not re-read forever).
        assert ae.decode_checkpoints(result.next_checkpoint)[SUB_A] == "2026-06-01T13:00:00Z"


# ── scope defence — Alerts ONLY ─────────────────────────────────────────────────


class TestScopeDefence:

    def test_only_alerts_client_is_used(self):
        client = FakeAlertsClient({SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")]})
        _ingestor([SUB_A], client).ingest_alerts(token="T")
        # Only fetch_alerts is the transport call; no other stream is polled here.
        assert client.calls and all("sub" in c for c in client.calls)

    def test_no_activity_log_or_service_health_yet(self):
        # T3 adds these; T2 must not implement them.
        ing = _ingestor([SUB_A], FakeAlertsClient())
        assert not hasattr(ing, "poll_activity_log")
        assert not hasattr(ing, "poll_service_health")
        assert not hasattr(azure_alerts, "map_azure_activity_log")


# ── ChangeBasedIngestor pipeline entrypoint ─────────────────────────────────────


class TestPipelineEntrypoint:

    def test_ingest_changes_yields_delta_batch(self):
        client = FakeAlertsClient({SUB_A: [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")]})
        ing = _ingestor([SUB_A], client)
        # Inject a token via token_fn-free path: ingest_changes acquires a token,
        # so give it a pre-seeded one by monkeypatching acquire path via token arg
        # is not available here; instead use a client that ignores the token and
        # stub the blocking token acquisition.
        import discovery.ingest.azure_events as mod
        original = mod.acquire_arm_token_blocking
        mod.acquire_arm_token_blocking = lambda org_id, config, **kw: "TESTTOKEN"
        try:
            batches = list(ing.ingest_changes("org1"))
        finally:
            mod.acquire_arm_token_blocking = original
        assert len(batches) == 1
        assert batches[0].records and batches[0].is_complete
        assert batches[0].next_checkpoint

    def test_ingest_changes_rejects_org_mismatch(self):
        ing = _ingestor([SUB_A], FakeAlertsClient())
        with pytest.raises(ValueError):
            list(ing.ingest_changes("other-org"))

    def test_connector_is_change_based_ingestor(self):
        from discovery.ingest.base import ChangeBasedIngestor
        ing = _ingestor([SUB_A], FakeAlertsClient())
        assert isinstance(ing, ChangeBasedIngestor)
        assert ing.connector_id == "azure_events"
        assert ing.reports_deletes is False
