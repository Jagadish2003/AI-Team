"""
MSP-B2 T3 (AT-650) — Azure Activity Log (administrative) + Service Health polling.

Offline / DB-free: stream clients and the ARM token are injected. Reuses the T2
per-subscription checkpoint + failure-isolation engine; only the fetch, mapper,
timestamp/id accessors, and (for Activity Log) the Administrative scope filter differ.

Acceptance criteria:
  T3-AC1 — Activity Log administrative events are ingested successfully.
  T3-AC2 — Service Health events are ingested successfully.
  T3-AC3 — Events are normalized using the shared B0 mapping contract.
  T3-AC4 — Only supported event classes are processed (Administrative only for the
           Activity Log stream; non-Administrative categories are dropped).
"""
from __future__ import annotations

import pytest

from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg
from discovery.ingest import azure_admin_events as admin


SUB_A = "aaaaaaaa-0000-0000-0000-000000000001"
SUB_B = "bbbbbbbb-0000-0000-0000-000000000002"


# ── fakes / builders ─────────────────────────────────────────────────────────


class FakeStreamClient:
    """Returns configured records per subscription; can fail specific subscriptions."""

    def __init__(self, by_sub=None):
        self.by_sub = dict(by_sub or {})
        self.fail_subs = set()
        self.calls = []

    def fetch(self, *, token, subscription_id, environment, since_iso):
        self.calls.append({"sub": subscription_id, "since": since_iso})
        if subscription_id in self.fail_subs:
            raise RuntimeError("throttled (429)")
        return list(self.by_sub.get(subscription_id, []))


def _activity(sub, eid, ts, *, operation="Microsoft.Compute/virtualMachines/write",
              category="Administrative", status="Succeeded", level="Informational"):
    rec = {
        "eventDataId": eid,
        "correlationId": f"corr-{eid}",
        "eventTimestamp": ts,
        "subscriptionId": sub,
        "level": level,
        "operationName": {"value": operation},
        "status": {"value": status},
        "resourceId": f"/subscriptions/{sub}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
    }
    if category is not None:
        rec["category"] = {"value": category}
    return rec


def _health(sub, tid, ts, *, incident="Incident", stage="Active", service="Virtual Machines",
            region="East US", status="Active", level="Warning"):
    return {
        "eventDataId": f"ed-{tid}",
        "correlationId": f"corr-{tid}",
        "eventTimestamp": ts,
        "subscriptionId": sub,
        "level": level,
        "category": {"value": "ServiceHealth"},
        "status": {"value": status},
        "properties": {
            "title": f"{service} {incident}",
            "service": service,
            "region": region,
            "incidentType": incident,
            "stage": stage,
            "trackingId": tid,
        },
    }


def _config(subs):
    return cfg.AzureEventConfig(
        environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
        mode=cfg.MODE_LIGHTHOUSE,
        subscriptions=list(subs),
    )


def _ingestor(subs, *, activity=None, health=None, alerts=None):
    return ae.AzureEventIngestor(
        "org1", _config(subs),
        alerts_client=alerts,
        activity_log_client=activity,
        service_health_client=health,
    )


# ── T3-AC1 — Activity Log administrative events ──────────────────────────────────


class TestAC1ActivityLog:

    def test_administrative_events_ingested(self):
        client = FakeStreamClient({SUB_A: [
            _activity(SUB_A, "e1", "2026-06-01T12:00:00Z"),
            _activity(SUB_A, "e2", "2026-06-01T13:00:00Z", operation="Microsoft.Network/x/delete"),
        ]})
        result = _ingestor([SUB_A], activity=client).ingest_activity_log(token="T")
        assert result.emitted_count == 2
        assert result.subscription_status[SUB_A]["status"] == "ok"

    def test_activity_record_shape(self):
        client = FakeStreamClient({SUB_A: [_activity(SUB_A, "e1", "2026-06-01T12:00:00Z")]})
        [rec] = _ingestor([SUB_A], activity=client).ingest_activity_log(token="T").records
        # AC4: the event carries the provider family; the stream is on the wrapper.
        assert rec["event"]["source_system"] == "azure"
        assert rec["surface"] == "azure_activity"
        assert rec["event"]["event_class"] == "configuration"   # ...write → configuration
        assert rec["account_scope"] == SUB_A
        assert rec["provider"] == "azure"
        assert "account_scope" not in rec["event"]              # no invented detector field


# ── T3-AC4 — only supported event classes (Administrative only) ──────────────────


class TestAC4ScopeDefence:

    def test_non_administrative_categories_dropped(self):
        client = FakeStreamClient({SUB_A: [
            _activity(SUB_A, "admin1", "2026-06-01T12:00:00Z", category="Administrative"),
            _activity(SUB_A, "sec1", "2026-06-01T12:10:00Z", category="Security"),
            _activity(SUB_A, "pol1", "2026-06-01T12:20:00Z", category="Policy"),
            _activity(SUB_A, "sh1", "2026-06-01T12:30:00Z", category="ServiceHealth"),
        ]})
        result = _ingestor([SUB_A], activity=client).ingest_activity_log(token="T")
        assert result.emitted_count == 1                        # only the administrative one
        assert result.records[0]["provider_event_id"] == "admin1"
        assert result.subscription_status[SUB_A]["in_scope"] == 1

    def test_missing_category_treated_as_administrative(self):
        # The Activity Log management surface is administrative by construction.
        client = FakeStreamClient({SUB_A: [_activity(SUB_A, "e1", "2026-06-01T12:00:00Z", category=None)]})
        result = _ingestor([SUB_A], activity=client).ingest_activity_log(token="T")
        assert result.emitted_count == 1

    def test_is_administrative_helper(self):
        assert admin.is_administrative({"category": {"value": "Administrative"}})
        assert admin.is_administrative({})                       # absent → administrative
        assert not admin.is_administrative({"category": {"value": "Security"}})
        assert not admin.is_administrative({"category": {"value": "ServiceHealth"}})


# ── T3-AC2 — Service Health events ───────────────────────────────────────────────


class TestAC2ServiceHealth:

    def test_service_health_ingested(self):
        client = FakeStreamClient({SUB_A: [
            _health(SUB_A, "SH-1", "2026-06-02T08:00:00Z", incident="Incident", stage="Active"),
            _health(SUB_A, "SH-2", "2026-06-03T09:00:00Z", incident="Maintenance", stage="Resolved", status="Resolved"),
        ]})
        result = _ingestor([SUB_A], health=client).ingest_service_health(token="T")
        assert result.emitted_count == 2
        assert result.subscription_status[SUB_A]["status"] == "ok"

    def test_service_health_record_shape_and_classes(self):
        client = FakeStreamClient({SUB_A: [
            _health(SUB_A, "SH-1", "2026-06-02T08:00:00Z", incident="Incident", stage="Active"),
            _health(SUB_A, "SH-2", "2026-06-03T09:00:00Z", incident="Maintenance", stage="Resolved", status="Resolved"),
        ]})
        recs = {r["provider_event_id"]: r for r in
                _ingestor([SUB_A], health=client).ingest_service_health(token="T").records}
        assert recs["SH-1"]["event"]["source_system"] == "azure"
        assert recs["SH-1"]["surface"] == "azure_service_health"
        assert recs["SH-1"]["event"]["event_class"] == "error"          # active incident
        assert recs["SH-2"]["event"]["event_class"] == "state_change"   # resolved maintenance
        assert recs["SH-1"]["account_scope"] == SUB_A


# ── T3-AC3 — normalization through the shared B0 mappers ─────────────────────────


class TestAC3Normalization:

    def test_activity_uses_map_azure_activity_log(self, monkeypatch):
        calls = {"n": 0}
        import discovery.signals.reference_mappers as rm
        orig = rm.map_azure_activity_log

        def _spy(payload, *, org_id):
            calls["n"] += 1
            return orig(payload, org_id=org_id)

        monkeypatch.setattr(ae, "map_azure_activity_log", _spy)
        client = FakeStreamClient({SUB_A: [_activity(SUB_A, "e1", "2026-06-01T12:00:00Z")]})
        _ingestor([SUB_A], activity=client).ingest_activity_log(token="T")
        assert calls["n"] == 1

    def test_service_health_uses_map_service_health(self, monkeypatch):
        calls = {"n": 0}
        import discovery.signals.reference_mappers as rm
        orig = rm.map_service_health

        def _spy(payload, *, org_id):
            calls["n"] += 1
            return orig(payload, org_id=org_id)

        monkeypatch.setattr(ae, "map_service_health", _spy)
        client = FakeStreamClient({SUB_A: [_health(SUB_A, "SH-1", "2026-06-02T08:00:00Z")]})
        _ingestor([SUB_A], health=client).ingest_service_health(token="T")
        assert calls["n"] == 1

    def test_map_service_health_registered_in_b0(self):
        from discovery.signals.reference_mappers import MAPPERS
        assert "map_service_health" in MAPPERS
        from discovery.signals import map_service_health  # exported at package level
        assert callable(map_service_health)


# ── checkpoints — per subscription, incremental (reused engine) ──────────────────


class TestCheckpoints:

    def test_activity_incremental_no_duplicates(self):
        client = FakeStreamClient({SUB_A: [_activity(SUB_A, "e1", "2026-06-01T12:00:00Z")]})
        ing = _ingestor([SUB_A], activity=client)
        run1 = ing.ingest_activity_log(token="T")
        assert run1.emitted_count == 1
        run2 = ing.ingest_activity_log(token="T", checkpoint=run1.next_checkpoint)
        assert run2.emitted_count == 0

    def test_service_health_only_new_on_rerun(self):
        client = FakeStreamClient({SUB_A: [_health(SUB_A, "SH-1", "2026-06-02T08:00:00Z")]})
        ing = _ingestor([SUB_A], health=client)
        run1 = ing.ingest_service_health(token="T")
        client.by_sub[SUB_A].append(_health(SUB_A, "SH-2", "2026-06-05T08:00:00Z"))
        run2 = ing.ingest_service_health(token="T", checkpoint=run1.next_checkpoint)
        assert run2.emitted_count == 1
        assert run2.records[0]["provider_event_id"] == "SH-2"

    def test_independent_per_subscription(self):
        client = FakeStreamClient({
            SUB_A: [_activity(SUB_A, "a1", "2026-06-01T12:00:00Z")],
            SUB_B: [_activity(SUB_B, "b1", "2026-06-01T12:00:00Z")],
        })
        result = _ingestor([SUB_A, SUB_B], activity=client).ingest_activity_log(token="T")
        cps = ae.decode_checkpoints(result.next_checkpoint)
        assert set(cps) == {SUB_A, SUB_B}


# ── failure isolation (same discipline as T2) ────────────────────────────────────


class TestFailureIsolation:

    def test_activity_one_subscription_fails_others_continue(self):
        client = FakeStreamClient({
            SUB_A: [_activity(SUB_A, "a1", "2026-06-01T12:00:00Z")],
            SUB_B: [_activity(SUB_B, "b1", "2026-06-01T12:00:00Z")],
        })
        client.fail_subs.add(SUB_A)
        result = _ingestor([SUB_A, SUB_B], activity=client).ingest_activity_log(token="T")
        assert result.subscription_status[SUB_A]["status"] == "error"
        assert result.subscription_status[SUB_B]["status"] == "ok"
        assert result.emitted_count == 1

    def test_service_health_failure_does_not_advance_checkpoint(self):
        client = FakeStreamClient({SUB_A: [_health(SUB_A, "SH-1", "2026-06-02T08:00:00Z")]})
        ing = _ingestor([SUB_A], health=client)
        run1 = ing.ingest_service_health(token="T")
        client.fail_subs.add(SUB_A)
        run2 = ing.ingest_service_health(token="T", checkpoint=run1.next_checkpoint)
        assert run2.failed_subscriptions == [SUB_A]
        assert ae.decode_checkpoints(run2.next_checkpoint) == ae.decode_checkpoints(run1.next_checkpoint)


# ── ingest_all — three streams, namespaced checkpoints ──────────────────────────


class TestIngestAll:

    def _clients(self):
        from discovery.ingest.azure_alerts import FixtureAzureAlertsClient  # unused offline default
        alerts = FakeAlertsClient({SUB_A: [_alert(SUB_A, "al1", "2026-06-01T10:00:00Z")]})
        activity = FakeStreamClient({SUB_A: [_activity(SUB_A, "e1", "2026-06-01T12:00:00Z")]})
        health = FakeStreamClient({SUB_A: [_health(SUB_A, "SH-1", "2026-06-02T08:00:00Z")]})
        return alerts, activity, health

    def test_ingest_all_combines_three_streams(self):
        alerts, activity, health = self._clients()
        ing = _ingestor([SUB_A], activity=activity, health=health, alerts=alerts)
        result = ing.ingest_all(token="T")
        # Every event carries the one provider family (AC4); the three streams stay
        # individually visible through the wrapper's `surface`.
        assert {r["event"]["source_system"] for r in result.records} == {"azure"}
        assert {r["surface"] for r in result.records} == {
            "azure_monitor", "azure_activity", "azure_service_health"
        }
        assert result.emitted_count == 3

    def test_ingest_all_status_keyed_by_stream_and_sub(self):
        alerts, activity, health = self._clients()
        result = _ingestor([SUB_A], activity=activity, health=health, alerts=alerts).ingest_all(token="T")
        assert f"alerts:{SUB_A}" in result.subscription_status
        assert f"activity_log:{SUB_A}" in result.subscription_status
        assert f"service_health:{SUB_A}" in result.subscription_status

    def test_ingest_all_second_run_no_duplicates(self):
        alerts, activity, health = self._clients()
        ing = _ingestor([SUB_A], activity=activity, health=health, alerts=alerts)
        run1 = ing.ingest_all(token="T")
        assert run1.emitted_count == 3
        run2 = ing.ingest_all(token="T", checkpoint=run1.next_checkpoint)
        assert run2.emitted_count == 0                          # every stream incremental

    def test_ingest_all_namespaced_checkpoint_shape(self):
        alerts, activity, health = self._clients()
        result = _ingestor([SUB_A], activity=activity, health=health, alerts=alerts).ingest_all(token="T")
        ns = ae.decode_stream_checkpoints(result.next_checkpoint)
        assert set(ns) == {"alerts", "activity_log", "service_health"}
        assert SUB_A in ns["activity_log"] and SUB_A in ns["service_health"]

    def test_legacy_flat_checkpoint_read_as_alerts(self):
        # A T2-era flat {sub: iso} checkpoint is read as the alerts stream position.
        ns = ae.decode_stream_checkpoints('{"%s": "2026-06-01T10:00:00Z"}' % SUB_A)
        assert ns["alerts"][SUB_A] == "2026-06-01T10:00:00Z"
        assert ns["activity_log"] == {}


# ── scope defence — only the T3 streams; no metrics/log analytics ───────────────


class TestScopeDefence:

    def test_admin_module_has_no_out_of_scope_streams(self):
        for forbidden in ("metrics", "log_analytics", "diagnostic", "defender", "sentinel", "resource_graph"):
            assert not any(forbidden in name.lower() for name in dir(admin))


# alerts helpers reused for ingest_all tests
from discovery.ingest.azure_alerts import alert_id as _alert_id_unused  # noqa: E402,F401


def _alert(sub, aid, fired, *, sev="Sev2"):
    return {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {"essentials": {
            "alertId": f"/subscriptions/{sub}/providers/Microsoft.AlertsManagement/alerts/{aid}",
            "alertRule": "rule", "severity": sev, "signalType": "Metric",
            "monitorCondition": "Fired",
            "alertTargetIDs": [f"/subscriptions/{sub}/resourceGroups/rg/providers/Microsoft.Web/sites/app"],
            "firedDateTime": fired, "description": "seeded",
        }},
    }


class FakeAlertsClient:
    def __init__(self, by_sub=None):
        self.by_sub = dict(by_sub or {})
        self.fail_subs = set()
        self.calls = []

    def fetch_alerts(self, *, token, subscription_id, environment, since_iso):
        self.calls.append({"sub": subscription_id, "since": since_iso})
        if subscription_id in self.fail_subs:
            raise RuntimeError("throttled")
        return list(self.by_sub.get(subscription_id, []))


# ── Activity Log $filter — REQUIRED by ARM (regression cover) ────────────────────
# The Activity Log List operation rejects a request with no $filter as HTTP 400.
# The connector previously sent only api-version, so every poll failed and Azure
# ingested 0 events while Alerts and Service Health (which do NOT require it)
# returned 200. These tests pin the query string, not just the parsed result.

import re as _re
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

import pytest as _pytest

from discovery.ingest import azure_admin_events as _admin
from discovery.ingest import azure_events_config as _cfg

_SUB = "11111111-2222-3333-4444-555555555555"
_NOW = _dt(2026, 7, 27, 12, 0, 0, tzinfo=_tz.utc)


def _bounds(flt: str):
    """Extract the (ge, le) bounds from the documented filter form."""
    found = dict(
        (op, val) for op, val in _re.findall(r"eventTimestamp (ge|le) '([^']+)'", flt)
    )
    return found.get("ge"), found.get("le")


def test_filter_contains_both_eventtimestamp_bounds():
    flt = _admin.build_activity_log_filter(None, now=_NOW, lookback_days=7)
    ge, le = _bounds(flt)
    assert ge and le, f"both bounds required, got {flt!r}"
    assert flt == (
        "eventTimestamp ge '2026-07-20T12:00:00.000000Z' "
        "and eventTimestamp le '2026-07-27T12:00:00.000000Z'"
    )


def test_first_sync_uses_the_default_lookback_as_lower_bound():
    ge, le = _bounds(_admin.build_activity_log_filter(None, now=_NOW))
    expected = _NOW - _td(days=_admin.ACTIVITY_LOG_DEFAULT_LOOKBACK_DAYS)
    assert _admin._parse_iso(ge) == expected
    assert _admin._parse_iso(le) == _NOW


def test_incremental_sync_uses_the_checkpoint_as_lower_bound():
    checkpoint = "2026-07-26T09:30:00.123456Z"
    ge, le = _bounds(_admin.build_activity_log_filter(checkpoint, now=_NOW))
    assert _admin._parse_iso(ge) == _admin._parse_iso(checkpoint)
    assert _admin._parse_iso(le) == _NOW


def test_checkpoint_older_than_90_days_is_clamped_to_retention():
    # ARM rejects a start bound beyond the 90-day Activity Log retention window,
    # so an aged checkpoint must not reintroduce the 400.
    ge, _ = _bounds(_admin.build_activity_log_filter("2019-01-01T00:00:00Z", now=_NOW))
    assert _admin._parse_iso(ge) == _NOW - _td(days=_admin.ACTIVITY_LOG_MAX_LOOKBACK_DAYS)


def test_unparseable_checkpoint_still_yields_a_valid_filter():
    flt = _admin.build_activity_log_filter("not-a-timestamp", now=_NOW)
    ge, le = _bounds(flt)
    assert ge and le


def test_checkpoint_ahead_of_clock_keeps_the_window_valid():
    ge, le = _bounds(_admin.build_activity_log_filter("2030-01-01T00:00:00Z", now=_NOW))
    assert _admin._parse_iso(ge) <= _admin._parse_iso(le)


class _Recorder:
    """Captures the outgoing request and returns an empty ARM page."""

    def __init__(self):
        self.urls = []

    def handler(self, request):
        import httpx
        self.urls.append(request.url)
        return httpx.Response(200, json={"value": []})


def _fetch(path, api_version, params_builder, since_iso=None):
    import httpx
    rec = _Recorder()
    client = _admin._HttpStreamClient(
        path=path, api_version=api_version,
        params_builder=params_builder, transport=httpx.MockTransport(rec.handler),
    )
    client.fetch(
        token="T", subscription_id=_SUB,
        environment=_cfg.resolve_environment(_cfg.AZURE_CLOUD), since_iso=since_iso,
    )
    return rec.urls[0]


def test_live_activity_log_request_carries_the_filter():
    url = _fetch(_admin._ACTIVITY_LOG_PATH, _admin.ACTIVITY_LOG_API_VERSION,
                 _admin.activity_log_params)
    assert url.path.endswith("/providers/Microsoft.Insights/eventtypes/management/values")
    assert url.params["api-version"] == _admin.ACTIVITY_LOG_API_VERSION
    flt = url.params["$filter"]                       # the fix: absent → HTTP 400
    ge, le = _bounds(flt)
    assert ge and le


def test_live_activity_log_request_passes_the_checkpoint_through():
    checkpoint = "2026-07-26T09:30:00.123456Z"
    url = _fetch(_admin._ACTIVITY_LOG_PATH, _admin.ACTIVITY_LOG_API_VERSION,
                 _admin.activity_log_params, since_iso=checkpoint)
    ge, _ = _bounds(url.params["$filter"])
    assert _admin._parse_iso(ge) == _admin._parse_iso(checkpoint)


def test_service_health_request_is_unchanged_no_filter():
    # Service Health does NOT require $filter — its request must stay as it was.
    url = _fetch(_admin._SERVICE_HEALTH_PATH, _admin.SERVICE_HEALTH_API_VERSION, None)
    assert "$filter" not in str(url) and "%24filter" not in str(url)
    assert url.params["api-version"] == _admin.SERVICE_HEALTH_API_VERSION


def test_default_live_activity_log_client_is_filtered(monkeypatch):
    monkeypatch.setattr(_admin, "is_live", lambda: True)
    assert _admin.default_activity_log_client()._params_builder is _admin.activity_log_params


def test_default_live_service_health_client_has_no_params_builder(monkeypatch):
    monkeypatch.setattr(_admin, "is_live", lambda: True)
    assert _admin.default_service_health_client()._params_builder is None
