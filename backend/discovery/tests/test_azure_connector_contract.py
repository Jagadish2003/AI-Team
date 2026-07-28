"""
MSP-B2 T7 (AT-654) — Azure Event Connector contract tests.

Comprehensive, offline, fixture-based contract suite proving the connector satisfies
the MSP-B2 specification (Section 3):

  * TRANSPORT EQUIVALENCE (T7-AC1) — the native connector's normalised event matches
    the Azure Event History Bridge's event field-for-field EXCEPT source_system (and
    its transport-derived provenance); the recurrence signature is identical.
  * CHECKPOINT CONTRACT (T7-AC2) — first run emits + advances; second run re-reads
    nothing; failed subscriptions preserve their checkpoint; healthy ones advance;
    per-subscription checkpoints are independent.
  * SKELETON REUSE (T7-AC3) — the connector reuses the SAME shared change-based
    ingestion skeleton the bridge uses (no forked framework).
  * B0 SCHEMA COMPLIANCE — every emitted event satisfies the OperationalEvent
    closed vocabularies and carries a valid OBSERVED provenance spine.
  * B7 ADMISSION (AC5), SCOPE DEFENCE (AC2), OUTBOUND-ONLY (AC6), LIGHTHOUSE
    PINNING (AC7).

Reuses the B0 mappers, the B8 bridge machinery, and the B7 admission stream exactly
as they ship — no test framework is invented.
"""
from __future__ import annotations

import pytest

from database.models.ops_event_staging import OpsEventStagingRow
from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg
from discovery.ingest import azure_admin_events as admin
from discovery.ingest import azure_alerts
from discovery.ingest import base as ingest_base
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint
from discovery.ingest.ops_event_bridge import (
    OpsEventBridgeIngestor,
    bridge_source_system,
)
from discovery.ingest.ops_event_staging_store import InMemoryStagingSink
from discovery.signals.operational_event import (
    EVENT_CLASSES,
    RESOURCE_TYPES,
    SEVERITY_LEVELS,
)
from discovery.signals.ops_stream import OpsEventStream
from discovery.signals.reference_mappers import (
    map_azure_activity_log,
    map_azure_monitor,
    map_service_health,
)

ORG = "default"
SUB = "11111111-2222-3333-4444-555555555555"


# ── raw payloads (shaped for the B0 mappers; same shapes the bridge test uses) ──

_AZURE_MONITOR = {
    "data": {"essentials": {
        "alertId": f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/az-mon-1",
        "alertRule": "HighCPU", "severity": "Sev2", "firedDateTime": "2026-06-01T15:00:00Z",
        "monitorCondition": "Fired",
        "alertTargetIDs": [f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"],
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


# ── fakes / drivers ─────────────────────────────────────────────────────────────


class _AlertsFake:
    def __init__(self, rows):
        self._rows = rows

    def fetch_alerts(self, *, token, subscription_id, environment, since_iso):
        return list(self._rows)


class _StreamFake:
    def __init__(self, rows):
        self._rows = rows

    def fetch(self, *, token, subscription_id, environment, since_iso):
        return list(self._rows)


# A vaulted service principal + token exchange stubbed offline, so the
# ingest_changes/ingest_all path resolves an ARM token without any network call.
def _sp_record(o, c):
    return type("R", (), {"username": "app", "secret": "s", "base_url": "tenant"})()


async def _fake_token_fn(*, token_url, client_id, client_secret, scope):
    return {"access_token": "TEST-ARM-TOKEN", "expires_in": 3600}


def _connector(*, alerts=None, activity=None, health=None, subs=(SUB,)):
    return ae.AzureEventIngestor(
        ORG,
        cfg.AzureEventConfig(
            environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
            mode=cfg.MODE_LIGHTHOUSE, subscriptions=list(subs),
        ),
        alerts_client=_AlertsFake(alerts or []),
        activity_log_client=_StreamFake(activity or []),
        service_health_client=_StreamFake(health or []),
        vault_reader=_sp_record,
        token_fn=_fake_token_fn,
        sleep_fn=lambda s: None,
    )


def _native_event(stream, raw):
    """The event dict the connector emits for one raw record on ``stream``."""
    if stream == "alerts":
        res = _connector(alerts=[raw]).ingest_alerts(token="T")
    elif stream == "activity_log":
        res = _connector(activity=[raw]).ingest_activity_log(token="T")
    else:
        res = _connector(health=[raw]).ingest_service_health(token="T")
    assert res.records, f"connector emitted no record for {stream}"
    return res.records[0]["event"]


def _bridge_event(source_format, peid, raw):
    sink = InMemoryStagingSink()
    sink.insert_rows([OpsEventStagingRow(
        org_id=ORG, provider="azure", source_format=source_format,
        batch_id="b", provider_event_id=peid, raw=raw,
    )])
    records = []
    for batch in OpsEventBridgeIngestor(sink).ingest_changes(ORG, None):
        records.extend(batch.records)
    assert records, "bridge emitted no record"
    return records[0]["event"]


# ── T7-AC1 — transport equivalence with the Azure Event History Bridge ──────────


class TestTransportEquivalence:

    @pytest.mark.parametrize("stream,source_format,peid,raw", [
        ("alerts", "azure_monitor", "az-mon-1", _AZURE_MONITOR),
        ("activity_log", "azure_activity_log", "az-act-1", _AZURE_ACTIVITY),
    ])
    def test_native_matches_bridge_except_source_system(self, stream, source_format, peid, raw):
        native = _native_event(stream, raw)
        bridged = _bridge_event(source_format, peid, raw)
        differing = {k for k in native if native[k] != bridged.get(k)}
        # The ONLY acceptable differences: source_system and its transport-derived
        # provenance. Every detector-visible field matches field-for-field.
        assert differing == {"source_system", "provenance"}, differing

    @pytest.mark.parametrize("stream,source_format,peid,raw", [
        ("alerts", "azure_monitor", "az-mon-1", _AZURE_MONITOR),
        ("activity_log", "azure_activity_log", "az-act-1", _AZURE_ACTIVITY),
    ])
    def test_source_system_is_the_only_stamped_difference(self, stream, source_format, peid, raw):
        native = _native_event(stream, raw)
        bridged = _bridge_event(source_format, peid, raw)
        assert bridged["source_system"] == bridge_source_system("azure") == "bridge:azure"
        assert native["source_system"] in ("azure_monitor", "azure_activity")
        assert native["source_system"] != bridged["source_system"]

    @pytest.mark.parametrize("stream,source_format,peid,raw", [
        ("alerts", "azure_monitor", "az-mon-1", _AZURE_MONITOR),
        ("activity_log", "azure_activity_log", "az-act-1", _AZURE_ACTIVITY),
    ])
    def test_event_signature_identical(self, stream, source_format, peid, raw):
        native = _native_event(stream, raw)
        bridged = _bridge_event(source_format, peid, raw)
        # The recurrence identity must be identical across transports (the whole
        # point of the equivalence guarantee) and non-empty.
        assert native["event_signature"] == bridged["event_signature"]
        assert native["event_signature"]

    def test_native_event_is_exactly_the_mapper_output(self):
        # The native path adds NO transport mutation to the event itself (the
        # subscription/account_scope lives on the record wrapper, not the event).
        assert _native_event("alerts", _AZURE_MONITOR) == map_azure_monitor(_AZURE_MONITOR, org_id=ORG).to_dict()
        assert _native_event("activity_log", _AZURE_ACTIVITY) == map_azure_activity_log(_AZURE_ACTIVITY, org_id=ORG).to_dict()

    def test_service_health_equivalent_to_shared_b0_mapper(self):
        # The bridge does not yet route Service Health (a B8 follow-up), so
        # equivalence is proven against the SAME shared B0 mapper the bridge would
        # use — the native path emits exactly map_service_health's output.
        assert _native_event("service_health", _SERVICE_HEALTH) == map_service_health(_SERVICE_HEALTH, org_id=ORG).to_dict()


# ── B0 schema compliance ────────────────────────────────────────────────────────


class TestB0SchemaCompliance:

    @pytest.mark.parametrize("stream,raw", [
        ("alerts", _AZURE_MONITOR),
        ("activity_log", _AZURE_ACTIVITY),
        ("service_health", _SERVICE_HEALTH),
    ])
    def test_event_satisfies_closed_vocabularies(self, stream, raw):
        ev = _native_event(stream, raw)
        assert ev["event_class"] in EVENT_CLASSES
        assert ev["severity"] in SEVERITY_LEVELS
        assert ev["resource_type"] in RESOURCE_TYPES
        assert ev["org_id"] == ORG
        assert ev["event_signature"]
        # OBSERVED provenance spine present + well-formed.
        prov = ev["provenance"]
        assert prov and prov.get("origin") == "observed"
        assert prov.get("source_system") and prov.get("source_artifact")


# ── B7 admission (AC5) — native-path dedupe with a count ─────────────────────────


class TestB7Admission:

    def _refire(self, alert_id):
        raw = {"data": {"essentials": dict(_AZURE_MONITOR["data"]["essentials"])}}
        raw["data"]["essentials"]["alertId"] = (
            f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/{alert_id}"
        )
        return raw

    def test_refiring_alert_folds_with_a_count_on_native_path(self):
        # Two DISTINCT alert instances of the SAME recurring condition (same rule /
        # target / class → same event_signature, different alertId).
        raws = [self._refire("fire-1"), self._refire("fire-2")]
        res = _connector(alerts=raws).ingest_alerts(token="T")
        assert res.emitted_count == 2

        # The connector's events are exactly the mapper's events; admit them to B7.
        stream = OpsEventStream()
        for raw in raws:
            ev = map_azure_monitor(raw, org_id=ORG)
            assert ev.to_dict() in [r["event"] for r in res.records]  # native == mapper
            stream.admit(ev)
        signals = stream.active_signals()
        assert len(signals) == 1                       # folded into one active signal
        assert signals[0].occurrence_count == 2        # …with a count (dedupe + count)

    def test_idempotent_redelivery_does_not_double_count(self):
        stream = OpsEventStream()
        ev = map_azure_monitor(self._refire("fire-1"), org_id=ORG)
        stream.admit(ev)
        stream.admit(ev)                                # same signal_id redelivered
        assert stream.active_signals()[0].occurrence_count == 1


# ── T7-AC3 — shared skeleton reuse (no forked framework) ─────────────────────────


class TestSkeletonReuse:

    def test_connector_reuses_shared_change_based_skeleton(self):
        # B1's CloudEventIngestorBase is not present on this branch; the connector
        # reuses the existing shared change-based ingestion skeleton
        # (ChangeBasedIngestor) — the SAME one the bridge uses. If B1 later lands a
        # CloudEventIngestorBase, the connector should inherit it; assert that too
        # when present so this test stays correct across that migration.
        assert issubclass(ae.AzureEventIngestor, ChangeBasedIngestor)
        base_mod = __import__("discovery.ingest.base", fromlist=["x"])
        cloud_base = getattr(base_mod, "CloudEventIngestorBase", None)
        if cloud_base is not None:
            assert issubclass(ae.AzureEventIngestor, cloud_base)

    def test_same_base_as_bridge_not_a_fork(self):
        # Both connectors ride the identical shared base + delta primitives — proof
        # there is no copied/forked ingestion framework.
        assert issubclass(OpsEventBridgeIngestor, ChangeBasedIngestor)
        assert ae.ChangeBasedIngestor is ingest_base.ChangeBasedIngestor
        assert ae.DeltaBatch is ingest_base.DeltaBatch
        assert ae.Checkpoint is ingest_base.Checkpoint

    def test_connector_declares_the_contract_attributes(self):
        ing = _connector()
        assert ing.connector_id == "azure_events"
        assert ing.reports_deletes is False


# ── T7-AC2 — checkpoint contract across runs (pipeline path) ─────────────────────


class TestCheckpointContract:

    def _run(self, ing, since_value=None):
        since = Checkpoint.create("azure_events", ORG, since_value) if since_value else None
        records, ckpt = [], None
        for batch in ing.ingest_changes(ORG, since):
            records.extend(batch.records)
            ckpt = batch.next_checkpoint
        return records, ckpt

    def test_first_run_emits_then_second_run_no_duplicates(self):
        ing = _connector(alerts=[_AZURE_MONITOR], activity=[_AZURE_ACTIVITY], health=[_SERVICE_HEALTH])
        records, ckpt = self._run(ing)
        assert len(records) == 3                       # all three streams emitted
        records2, ckpt2 = self._run(ing, since_value=ckpt)
        assert records2 == []                          # nothing re-read (AC3/T7-AC2)
        assert ae.decode_stream_checkpoints(ckpt2) == ae.decode_stream_checkpoints(ckpt)

    def test_only_new_events_on_subsequent_run(self):
        ing = _connector(alerts=[_AZURE_MONITOR])
        _, ckpt = self._run(ing)
        # A newer alert appears; re-run from checkpoint yields only the new one.
        newer = {"data": {"essentials": dict(_AZURE_MONITOR["data"]["essentials"],
                 alertId=f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/az-mon-2",
                 firedDateTime="2026-06-01T18:00:00Z")}}
        ing._alerts_client = _AlertsFake([_AZURE_MONITOR, newer])
        records2, _ = self._run(ing, since_value=ckpt)
        assert len(records2) == 1
        assert records2[0]["provider_event_id"].endswith("az-mon-2")

    def test_multi_subscription_checkpoints_are_independent(self):
        sub_b = "99999999-2222-3333-4444-555555555555"
        ing = _connector(alerts=[_AZURE_MONITOR], subs=(SUB, sub_b))
        _, ckpt = self._run(ing)
        ns = ae.decode_stream_checkpoints(ckpt)
        # Both pinned subs tracked independently in the alerts stream.
        assert set(ns["alerts"]) == {SUB, sub_b}

    def test_failed_subscription_checkpoint_preserved_healthy_advances(self):
        sub_b = "99999999-2222-3333-4444-555555555555"

        class _HalfBrokenAlerts:
            def fetch_alerts(self, *, token, subscription_id, environment, since_iso):
                if subscription_id == SUB:
                    raise RuntimeError("boom")            # non-transient → immediate error
                return [_AZURE_MONITOR]

        ing = ae.AzureEventIngestor(
            ORG, cfg.AzureEventConfig(
                environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
                mode=cfg.MODE_LIGHTHOUSE, subscriptions=[SUB, sub_b]),
            alerts_client=_HalfBrokenAlerts(),
            activity_log_client=_StreamFake([]), service_health_client=_StreamFake([]),
            sleep_fn=lambda s: None,
        )
        res = ing.ingest_alerts(token="T")
        cps = ae.decode_checkpoints(res.next_checkpoint)
        assert SUB not in cps                             # failed → preserved (absent)
        assert sub_b in cps                               # healthy → advanced
        assert res.subscription_status[SUB]["status"] == "error"
        assert res.subscription_status[sub_b]["status"] == "ok"


# ── scope defence (AC2) ──────────────────────────────────────────────────────────


class TestScopeDefence:

    def test_activity_log_ingests_administrative_only(self):
        mixed = [
            dict(_AZURE_ACTIVITY, eventDataId="admin-1", category={"value": "Administrative"}),
            dict(_AZURE_ACTIVITY, eventDataId="sec-1", category={"value": "Security"}),
            dict(_AZURE_ACTIVITY, eventDataId="pol-1", category={"value": "Policy"}),
        ]
        res = _connector(activity=mixed).ingest_activity_log(token="T")
        assert res.emitted_count == 1
        assert res.records[0]["provider_event_id"] == "admin-1"

    def test_only_the_three_inscope_arm_paths_are_wired(self):
        # Structural guard: the ONLY ARM surfaces the connector polls are the three
        # in-scope ones. No metrics / Log Analytics / diagnostic / Defender /
        # Sentinel / Resource Graph endpoint is wired anywhere.
        assert azure_alerts._ALERTS_PATH == "providers/Microsoft.AlertsManagement/alerts"
        assert admin._ACTIVITY_LOG_PATH == "providers/Microsoft.Insights/eventtypes/management/values"
        assert admin._SERVICE_HEALTH_PATH == "providers/Microsoft.ResourceHealth/events"
        wired_paths = [azure_alerts._ALERTS_PATH, admin._ACTIVITY_LOG_PATH, admin._SERVICE_HEALTH_PATH]
        forbidden_fragments = (
            "/metrics", "microsoft.insights/metrics", "operationalinsights",
            "diagnosticsettings", "microsoft.security", "securityinsights", "resourcegraph",
        )
        for path in wired_paths:
            low = path.lower()
            for frag in forbidden_fragments:
                assert frag not in low


# ── outbound-only (AC6) ──────────────────────────────────────────────────────────


class TestOutboundOnly:

    def test_no_server_framework_imported(self):
        # Outbound-only: the connector imports NO inbound server / webhook / Event
        # Grid framework — a listener would be needed to receive pushed events.
        import ast
        import inspect
        server_frameworks = {
            "flask", "fastapi", "uvicorn", "aiohttp", "tornado", "bottle",
            "socketserver", "http.server", "azure.eventgrid", "eventgrid",
        }
        for mod in (ae, azure_alerts, admin):
            tree = ast.parse(inspect.getsource(mod))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            leaked = {name for name in imported
                      if any(name == fw or name.startswith(fw + ".") for fw in server_frameworks)}
            assert not leaked, f"{mod.__name__} imports inbound framework(s): {leaked}"

    def test_live_clients_only_issue_get(self):
        import inspect
        # The only HTTP verb the live clients use is GET (outbound poll).
        for mod in (azure_alerts, admin):
            src = inspect.getsource(mod)
            assert "client.get(" in src
            for verb in (".post(", ".put(", ".delete(", ".patch("):
                assert verb not in src, f"{mod.__name__} issues non-GET {verb}"


# ── Lighthouse pinning (AC7) ─────────────────────────────────────────────────────


class TestLighthousePinning:

    def test_newly_delegated_subscription_not_ingested_until_pinned(self):
        sub_new = "77777777-2222-3333-4444-555555555555"
        ing = _connector(alerts=[_AZURE_MONITOR], subs=(SUB,))   # only SUB pinned
        # Discovery surfaces a newly delegated subscription…
        discovered = [SUB, sub_new]
        assert ing.authorized_subscriptions() == [SUB]           # ingested set unchanged
        assert ing.pending_delegated_subscriptions(discovered) == [sub_new]  # reported only
        # …and a poll reads only the pinned set.
        res = ing.ingest_alerts(token="T")
        assert set(res.subscription_status) == {SUB}
        assert sub_new not in res.subscription_status
