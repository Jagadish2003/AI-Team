"""MSP-B2 — native Azure Event Connector wired into the Discovery Run.

The forensic gap this suite closes: the Azure REST clients, mappers, auth, and
``AzureEventIngestor.ingest_all``/``ingest_changes`` were all implemented, but the
Discovery Runner never invoked them, so a run made no Azure REST calls, emitted no
OperationalEvents, and produced no Azure evidence.

These tests prove that during a Discovery Run:

  * ``AzureEventIngestor`` is invoked on the shared change-runner/checkpoint path
    (``runner._ingest_azure_events``), gated on the Azure connector being
    connected+selected AND a cloud_ops pack being selected;
  * Azure Monitor **Alerts**, the Azure **Activity Log**, and Azure **Service
    Health** are each polled;
  * normalised ``OperationalEvent`` records are emitted in the SAME record shape the
    MSP-B8 bridge emits;
  * those records feed the SAME cloud-ops assembly seam as the bridge, where a
    native event and its bridged twin fold to ONE signal (no duplication).

Offline / injected: the three stream clients, the vaulted service principal, and
the ARM token exchange are all injected, so no boto3/network/live credential is
needed. FAKE CREDENTIALS: every value below is a non-real, test-only stub.
"""
from __future__ import annotations

from discovery import runner
from discovery.cloud_ops_runtime import build_cloud_ops_runtime
from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg
from discovery.signals.reference_mappers import map_azure_monitor


ORG = "default"
SUB = "11111111-2222-3333-4444-555555555555"


# ── raw payloads (shaped for the B0 mappers; the shapes the connector tests use) ──

_AZURE_MONITOR = {
    "data": {"essentials": {
        "alertId": f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/az-mon-1",
        "alertRule": "HighCPU", "severity": "Sev2", "firedDateTime": "2026-06-01T15:00:00Z",
        "monitorCondition": "Fired",
        "alertTargetIDs": [
            f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"
        ],
        "description": "CPU consistently above 90%",
    }},
}
_AZURE_ACTIVITY = {
    "eventDataId": "az-act-1",
    "operationName": {"value": "Microsoft.Compute/virtualMachines/write"},
    "resourceId": f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
    "caller": "admin@contoso.com", "level": "Informational",
    "status": {"value": "Succeeded"}, "eventTimestamp": "2026-06-01T16:00:00Z",
    "category": {"value": "Administrative"}, "subscriptionId": SUB,
}
_SERVICE_HEALTH = {
    "eventDataId": "sh-1", "eventTimestamp": "2026-06-02T08:00:00Z", "subscriptionId": SUB,
    "level": "Warning", "category": {"value": "ServiceHealth"}, "status": {"value": "Active"},
    "properties": {"title": "Networking degradation", "service": "Virtual Machines",
                   "region": "East US", "incidentType": "Incident", "stage": "Active",
                   "trackingId": "SH-1"},
}


# ── recording fakes / stubs ─────────────────────────────────────────────────────


class _RecordingAlerts:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def fetch_alerts(self, *, token, subscription_id, environment, since_iso):
        self.calls.append(subscription_id)
        return list(self._rows)


class _RecordingStream:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def fetch(self, *, token, subscription_id, environment, since_iso):
        self.calls.append(subscription_id)
        return list(self._rows)


def _sp_record(org_id, connector_id):
    # A complete (fake) service principal so acquire_arm_token resolves offline.
    return type("R", (), {"username": "app", "secret": "s", "base_url": "tenant"})()


async def _fake_token_fn(*, token_url, client_id, client_secret, scope):
    return {"access_token": "TEST-ARM-TOKEN", "expires_in": 3600}


def _recording_ingestor(org):
    # A distinct org per change-runner test so the (org, "azure_events") checkpoint
    # never carries over between tests (incremental reads would otherwise be empty).
    alerts = _RecordingAlerts([_AZURE_MONITOR])
    activity = _RecordingStream([_AZURE_ACTIVITY])
    health = _RecordingStream([_SERVICE_HEALTH])
    ingestor = ae.AzureEventIngestor(
        org,
        cfg.AzureEventConfig(
            environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
            mode=cfg.MODE_LIGHTHOUSE,
            subscriptions=[SUB],
        ),
        vault_reader=_sp_record,
        token_fn=_fake_token_fn,
        alerts_client=alerts,
        activity_log_client=activity,
        service_health_client=health,
    )
    return ingestor, alerts, activity, health


# ── AzureEventIngestor is invoked; all three streams are polled ──────────────────


class TestNativeAzureIngestionInvoked:
    def test_all_three_streams_polled_and_events_emitted(self, client, monkeypatch):
        org = "org_az_poll"
        ingestor, alerts, activity, health = _recording_ingestor(org)
        monkeypatch.setattr(ae, "build_ingestor", lambda org_id, **kw: ingestor)

        data = runner._ingest_azure_events(org, "run_azure_int_1")

        # Azure Monitor Alerts, Activity Log, and Service Health each polled.
        assert alerts.calls == [SUB]
        assert activity.calls == [SUB]
        assert health.calls == [SUB]

        # OperationalEvents emitted in the bridge-compatible record shape.
        assert data["health"]["status"] == "ok"
        assert len(data["records"]) == 3

        # AC4 transport re-stamp: the EVENT's source_system is the provider FAMILY,
        # so a native event equals its bridged twin in every field but the signature's
        # own inputs. The per-stream MSP-B0 source system is not lost — it moves to the
        # transport wrapper as `surface` (with the stream key alongside it), mirroring
        # the shared AWS cloud-event skeleton's per-surface record metadata.
        assert {r["event"]["source_system"] for r in data["records"]} == {"azure"}
        assert {r["surface"] for r in data["records"]} == {
            "azure_monitor", "azure_activity", "azure_service_health",
        }
        assert {r["stream"] for r in data["records"]} == {
            "alerts", "activity_log", "service_health",
        }
        # Every record carries the provider-agnostic B0 event payload + provenance.
        for record in data["records"]:
            assert record["provider"] == "azure"
            assert record["account_scope"] == SUB
            assert "account_scope" not in record["event"]  # no invented detector field
            # The surface stays on the wrapper only — never invented onto the event.
            assert "surface" not in record["event"]

    def test_not_configured_connector_contributes_nothing(self, client, monkeypatch):
        monkeypatch.setattr(ae, "build_ingestor", lambda org_id, **kw: None)

        data = runner._ingest_azure_events("org_az_notcfg", "run_azure_int_2")

        assert data["records"] == []
        assert data["health"]["status"] == "not_configured"


# ── Native events feed the SAME cloud-ops seam; native + bridge twin dedupe ──────


class TestCloudOpsAssemblySeam:
    def test_native_events_reach_the_assembly_seam(self, client, monkeypatch):
        org = "org_az_seam"
        ingestor, *_ = _recording_ingestor(org)
        monkeypatch.setattr(ae, "build_ingestor", lambda org_id, **kw: ingestor)
        data = runner._ingest_azure_events(org, "run_azure_int_3")

        result = build_cloud_ops_runtime(org, {"org_id": org}, bridge_records=data["records"])
        event_health = result.health["b8_event_bridge"]
        # All three native OperationalEvents were admitted into the B7 OpsEventStream
        # (the detector-input pipeline). active_signals is measured BEFORE the noise
        # floor, so it is independent of per-class suppression.
        assert event_health["bridge_records"] == 3
        assert event_health["active_signals"] >= 1

    def test_native_and_bridge_twin_fold_to_one_signal(self):
        # The same underlying condition arriving via the native transport (azure)
        # and the bridge transport (bridge:azure) shares one event_signature, so the
        # runtime's OpsEventStream folds them into ONE active signal — no double-count.
        event = map_azure_monitor(_AZURE_MONITOR, org_id=ORG)
        native = {
            "event": event.to_dict(),
            "provider": "azure",
            "account_scope": SUB,
            "provider_event_id": "az-mon-1",
        }
        bridge_twin = {
            "event": {**event.to_dict(), "source_system": "bridge:azure"},
            "provider": "azure",
            "account_scope": SUB,
            "provider_event_id": "az-mon-1",
            "batch_id": "b1",
            "staging_row_id": 1,
        }

        merged = build_cloud_ops_runtime(
            ORG, {"org_id": ORG}, bridge_records=[native, bridge_twin]
        )
        native_only = build_cloud_ops_runtime(
            ORG, {"org_id": ORG}, bridge_records=[native]
        )

        merged_health = merged.health["b8_event_bridge"]
        native_health = native_only.health["b8_event_bridge"]
        # Two records admitted (native + bridged twin) but they FOLD to exactly one
        # active signal — the same count as the native event alone. No duplication.
        assert merged_health["bridge_records"] == 2
        assert merged_health["active_signals"] == 1
        assert native_health["active_signals"] == 1


# ── Runner gating: connected+selected AND cloud_ops pack ─────────────────────────


class TestRunnerGating:
    def _record_azure(self, monkeypatch):
        calls = []

        def _fake(org_id, run_id):
            calls.append((org_id, run_id))
            return {"records": [], "health": {"status": "ok", "records": 0}}

        monkeypatch.setattr(runner, "_ingest_azure_events", _fake)
        # Keep the bridge a no-op so the run needs no staging DB rows.
        monkeypatch.setattr(
            runner,
            "_ingest_ops_event_bridge",
            lambda org_id, run_id: {"records": [], "health": {"status": "ok", "records": 0}},
        )
        return calls

    def test_invoked_when_selected_and_cloud_ops_pack(self, monkeypatch):
        calls = self._record_azure(monkeypatch)
        runner.run("offline", systems=["azure_events"], pack="cloud_ops")
        assert calls, "azure ingestion should run when azure_events is selected under a cloud_ops pack"

    def test_skipped_without_cloud_ops_pack(self, monkeypatch):
        calls = self._record_azure(monkeypatch)
        runner.run("offline", systems=["salesforce", "azure_events"], pack="service_cloud")
        assert not calls, "azure ingestion must not run when no cloud_ops pack is selected"

    def test_skipped_when_not_selected(self, monkeypatch):
        calls = self._record_azure(monkeypatch)
        runner.run("offline", systems=["salesforce"], pack="cloud_ops")
        assert not calls, "azure ingestion must not run when azure_events is not selected"


# ── 2.0-D3 T1 — the bounded Application Insights read scope, in run health ───────


_AI_COMPONENT = (
    f"/subscriptions/{SUB}/resourceGroups/prod/providers"
    "/microsoft.insights/components/prod-checkout-api"
)

#: An App Insights availability alert, on the SAME Alerts Management surface the
#: connector already reads (D3 adds no read surface).
_AI_AVAILABILITY_ALERT = {
    "data": {"essentials": {
        "alertId": f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/ai-1",
        "alertRule": "prod-checkout-api-availability",
        "severity": "Sev1",
        "signalType": "Metric",
        "monitorCondition": "Fired",
        "monitoringService": "Platform",
        "alertTargetIDs": [_AI_COMPONENT],
        "firedDateTime": "2026-06-03T09:15:00.000Z",
        "description": "Availability test failed from 3 of 5 locations",
    }, "alertContext": {"conditionType": "WebtestLocationAvailabilityCriteria"}},
}

#: Raw request telemetry. Seeded to prove it is never ingested (D3-AC2).
_AI_RAW_REQUEST_TELEMETRY = {
    "name": "Microsoft.ApplicationInsights.dev.Request",
    "time": "2026-06-03T13:02:11.4410000Z",
    "data": {"baseType": "RequestData", "baseData": {
        "name": "POST /api/checkout", "responseCode": "500", "success": False,
    }},
}


class TestAppInsightsScopeInRunHealth:
    """2.0-D3 T1 through the REAL runner path, not just the connector in isolation."""

    def _ingestor(self, org, alert_rows):
        alerts = _RecordingAlerts(alert_rows)
        ingestor = ae.AzureEventIngestor(
            org,
            cfg.AzureEventConfig(
                environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
                mode=cfg.MODE_LIGHTHOUSE,
                subscriptions=[SUB],
            ),
            vault_reader=_sp_record,
            token_fn=_fake_token_fn,
            alerts_client=alerts,
            activity_log_client=_RecordingStream([]),
            service_health_client=_RecordingStream([]),
        )
        return ingestor, alerts

    def test_app_insights_signal_is_reported_in_run_health(self, client, monkeypatch):
        org = "org_az_d3_health"
        ingestor, _ = self._ingestor(org, [_AI_AVAILABILITY_ALERT])
        monkeypatch.setattr(ae, "build_ingestor", lambda org_id, **kw: ingestor)

        data = runner._ingest_azure_events(org, "run_azure_d3_1")

        block = data["health"]["app_insights"]
        assert block["records"] == 1
        assert block["components"] == [_AI_COMPONENT]
        assert block["by_signal_kind"] == {"availability": 1}
        # The scope rides the record WRAPPER, never the normalised event.
        record = next(r for r in data["records"] if r.get("app_insights"))
        assert "app_insights" not in record["event"]

    def test_seeded_raw_telemetry_is_not_ingested_through_the_runner(
        self, client, monkeypatch
    ):
        """D3-AC2 at the runner boundary: telemetry seeded into a stream produces
        no record and no App Insights health block."""
        org = "org_az_d3_scope"
        ingestor, _ = self._ingestor(org, [_AI_RAW_REQUEST_TELEMETRY])
        monkeypatch.setattr(ae, "build_ingestor", lambda org_id, **kw: ingestor)

        data = runner._ingest_azure_events(org, "run_azure_d3_2")

        assert data["records"] == []
        assert "app_insights" not in data["health"]

    def test_health_block_is_absent_when_no_app_insights_estate(
        self, client, monkeypatch
    ):
        """A run with no App Insights signal reports the pre-D3 health block."""
        org = "org_az_d3_none"
        ingestor, _ = self._ingestor(org, [_AZURE_MONITOR])
        monkeypatch.setattr(ae, "build_ingestor", lambda org_id, **kw: ingestor)

        data = runner._ingest_azure_events(org, "run_azure_d3_3")

        assert data["health"]["records"] == 1          # the plain B2 alert still flows
        assert "app_insights" not in data["health"]


# ── 2.0-D3 T4 — a budget-cut poll must never report a clean run ──────────────────


class TestBudgetDeferralInRunHealth:
    """The gap this closes: the MSP-B7 budget counts deferred EVENTS, which it can
    only do for events it actually saw. When capacity is exhausted the connector
    stops REQUESTING further pages (the desired behaviour), so those polls
    contribute no deferred-event count and `BudgetReport.breached` stays False — and
    the run would report a clean budget while having skipped whole subscriptions."""

    def _ingestor(self, org, *, budget):
        return ae.AzureEventIngestor(
            org,
            cfg.AzureEventConfig(
                environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
                mode=cfg.MODE_LIGHTHOUSE,
                subscriptions=[SUB],
            ),
            vault_reader=_sp_record,
            token_fn=_fake_token_fn,
            alerts_client=_RecordingAlerts([_AI_AVAILABILITY_ALERT]),
            activity_log_client=_RecordingStream([_AZURE_ACTIVITY]),
            service_health_client=_RecordingStream([_SERVICE_HEALTH]),
            budget=budget,
        )

    def test_a_skipped_poll_degrades_the_reported_status(self, client, monkeypatch):
        org = "org_az_d3t4_deferred"
        ingestor = self._ingestor(org, budget=1)
        monkeypatch.setattr(ae, "build_ingestor", lambda org_id, **kw: ingestor)

        data = runner._ingest_azure_events(org, "run_azure_d3t4_1")

        health = data["health"]
        # The budget alone would have reported nothing wrong...
        assert health.get("budget", {}).get("breached") in (False, None)
        # ...so the deferral block is what keeps it honest.
        assert health["status"] == "degraded"
        assert health["reason"] == "run_event_budget_exhausted"
        assert health["deferrals"]["complete"] is False
        assert health["deferrals"]["deferred_polls"] >= 1

    def test_a_complete_poll_reports_ok_with_no_deferral_block(self, client, monkeypatch):
        org = "org_az_d3t4_complete"
        ingestor = self._ingestor(org, budget=None)
        monkeypatch.setattr(ae, "build_ingestor", lambda org_id, **kw: ingestor)

        data = runner._ingest_azure_events(org, "run_azure_d3t4_2")

        assert data["health"]["status"] == "ok"
        assert "deferrals" not in data["health"]
