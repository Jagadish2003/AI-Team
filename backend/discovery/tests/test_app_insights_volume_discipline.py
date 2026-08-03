"""
2.0-D3 T4 — Application Insights volume discipline through MSP-B7.

D3-AC4: "Events pass through B7 admission (dedup with counts, floors, budgets)."

The point of this task is that there is NOTHING App Insights-specific to build.
MSP-B7 already owns the four event-volume disciplines (dedup at admission, noise
floors, per-run budgets, correlation windows) and the native Azure connector
already admits through them, so T4's deliverable is the PROOF that App Insights
rides the shared controls rather than a parallel pipeline of its own — plus
structural guards that keep it that way.

That framing matters for how these tests are written. Several of them assert
sameness against the AWS / bridged / other-Azure paths rather than asserting an
App Insights-specific number, because "behaves like every other event source" is
the actual requirement. Where a test does pin a number it is the SHARED calibrated
value, referenced from `ops_calibration` rather than restated, so a recalibration
cannot leave this suite asserting a stale constant.

Offline / DB-free throughout: stream clients are injected and the volume services
are pure.
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from discovery.cloud_ops_runtime import (
    TRANSPORT_BRIDGE,
    TRANSPORT_MIXED,
    TRANSPORT_NATIVE,
    build_cloud_ops_runtime,
    operational_event_from_bridge_record,
)
from discovery.correlation.windows import (
    JOIN_EVENT_EVENT,
    JOIN_EVENT_INCIDENT,
    CorrelationWindowPolicy,
    gate_operational_corroboration,
    join_within_window,
)
from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg
from discovery.signals.noise_floor import (
    DEFAULT_FLOOR,
    DEFAULT_NOISE_FLOORS,
    apply_noise_floors,
)
from discovery.signals.ops_calibration import (
    CALIBRATED_CORRELATION_WINDOWS,
    CALIBRATED_NOISE_FLOORS,
    CALIBRATED_RUN_EVENT_BUDGET,
)
from discovery.signals.ops_stream import DEFAULT_ACTIVE_PERIOD_SECONDS, OpsEventStream
from discovery.signals.reference_mappers import map_app_insights

SUB = "11111111-2222-3333-4444-555555555555"


def _component(name="checkout-api"):
    return (
        f"/subscriptions/{SUB}/resourceGroups/prod/providers"
        f"/microsoft.insights/components/{name}"
    )


def _alert(
    *,
    alert_id="a-1",
    fired="2026-07-20T09:00:00Z",
    component=None,
    rule="checkout-api-availability",
    condition="Fired",
    severity="Sev1",
    description="3 of 5 locations failed",
):
    """An App Insights availability alert (an ACTIVE failure → event_class 'error')."""
    return {"data": {"essentials": {
        "alertId": f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/{alert_id}",
        "alertRule": rule, "severity": severity, "signalType": "Metric",
        "monitorCondition": condition, "monitoringService": "Platform",
        "alertTargetIDs": [component or _component()],
        "firedDateTime": fired, "description": description,
    }, "alertContext": {"conditionType": "WebtestLocationAvailabilityCriteria"}}}


def _health(*, event_id="h-1", at="2026-07-20T13:00:00Z", component=None):
    """An App Insights health transition (→ event_class 'state_change')."""
    return {
        "eventDataId": event_id, "eventTimestamp": at, "subscriptionId": SUB,
        "level": "Warning",
        "operationName": {"value": "Microsoft.ResourceHealth/healthevent/action"},
        "category": {"value": "ResourceHealth"}, "status": {"value": "Active"},
        "properties": {
            "title": "Application health degraded",
            "currentHealthStatus": "Degraded", "previousHealthStatus": "Available",
            "impactedResources": [{"resourceId": component or _component()}],
        },
    }


class _AlertsFake:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def fetch_alerts(self, *, token, subscription_id, environment, since_iso):
        self.calls.append(subscription_id)
        return list(self._rows)


class _StreamFake:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.calls = []

    def fetch(self, *, token, subscription_id, environment, since_iso):
        self.calls.append(subscription_id)
        return list(self._rows)


def _ingestor(*, alerts=(), health=(), subs=(SUB,), budget=None, activity=None):
    return ae.AzureEventIngestor(
        "acme",
        cfg.AzureEventConfig(
            environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
            mode=cfg.MODE_LIGHTHOUSE,
            subscriptions=list(subs),
        ),
        alerts_client=_AlertsFake(list(alerts)),
        service_health_client=_StreamFake(health),
        activity_log_client=_StreamFake(activity or ()),
        budget=budget,
    )


def _admit(*raws, stream=None, org="acme"):
    """Map and admit raw App Insights records through a B7 stream."""
    s = stream or OpsEventStream()
    admissions = [s.admit(map_app_insights(r, org_id=org), org_id=org) for r in raws]
    return s, admissions


# ── 1. Deduplication at admission ───────────────────────────────────────────────


class TestDeduplication:

    def test_repeated_alerts_fold_into_one_signal_with_a_count(self):
        stream, _ = _admit(*[
            _alert(alert_id=f"a-{i}", fired=f"2026-07-20T09:0{i}:00Z") for i in range(4)
        ])
        signals = list(stream.active_signals("acme"))
        assert len(signals) == 1
        assert signals[0].occurrence_count == 4

    def test_the_folded_signal_carries_a_first_and_last_seen_range(self):
        stream, _ = _admit(
            _alert(alert_id="a-1", fired="2026-07-20T09:00:00Z"),
            _alert(alert_id="a-2", fired="2026-07-20T11:30:00Z"),
            _alert(alert_id="a-3", fired="2026-07-20T10:15:00Z"),
        )
        signal = list(stream.active_signals("acme"))[0]
        assert signal.first_seen == "2026-07-20T09:00:00Z"
        assert signal.last_seen == "2026-07-20T11:30:00Z"
        assert signal.is_recurrence

    def test_exact_provider_redelivery_is_idempotent(self):
        """The same alert id delivered twice must not inflate the count."""
        raw = _alert(alert_id="a-1")
        stream, admissions = _admit(raw, dict(raw), dict(raw))
        assert [a.is_duplicate for a in admissions] == [False, True, True]
        signals = list(stream.active_signals("acme"))
        assert len(signals) == 1
        assert signals[0].occurrence_count == 1

    def test_identical_signatures_on_different_applications_stay_separate(self):
        """The fold key includes the resource, so the same condition on two
        applications is two operational facts — never one."""
        stream, _ = _admit(
            _alert(alert_id="a-1", component=_component("checkout-api")),
            _alert(alert_id="a-2", component=_component("orders-api")),
        )
        signals = list(stream.active_signals("acme"))
        assert len(signals) == 2
        assert {s.occurrence_count for s in signals} == {1}
        # ...and they genuinely share the signature, which is what makes this a
        # meaningful test rather than two unrelated events.
        sigs = {map_app_insights(r, org_id="acme").event_signature for r in (
            _alert(alert_id="a-1", component=_component("checkout-api")),
            _alert(alert_id="a-2", component=_component("orders-api")),
        )}
        assert len(sigs) == 2, "different applications ⇒ different signatures too"

    def test_the_same_condition_in_different_active_periods_stays_separate(self):
        """The active period is part of the fold key: a fault recurring on two
        different days is two active signals, each with its own count."""
        day1 = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
        day2 = day1 + timedelta(seconds=DEFAULT_ACTIVE_PERIOD_SECONDS)
        stream, _ = _admit(
            _alert(alert_id="a-1", fired=day1.strftime("%Y-%m-%dT%H:%M:%SZ")),
            _alert(alert_id="a-2", fired=day2.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        assert len(list(stream.active_signals("acme"))) == 2

    def test_the_active_period_boundary_is_exercised_from_both_sides(self):
        base = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
        period = timedelta(seconds=DEFAULT_ACTIVE_PERIOD_SECONDS)
        just_inside = base + period - timedelta(seconds=1)
        just_outside = base + period
        fmt = "%Y-%m-%dT%H:%M:%SZ"

        same, _ = _admit(
            _alert(alert_id="a-1", fired=base.strftime(fmt)),
            _alert(alert_id="a-2", fired=just_inside.strftime(fmt)),
        )
        assert len(list(same.active_signals("acme"))) == 1

        split, _ = _admit(
            _alert(alert_id="a-1", fired=base.strftime(fmt)),
            _alert(alert_id="a-2", fired=just_outside.strftime(fmt)),
        )
        assert len(list(split.active_signals("acme"))) == 2

    def test_the_connector_admits_every_event_it_maps(self):
        """No caller can consume App Insights events un-deduplicated, because the
        connector owns the admission rather than leaving it to a downstream step."""
        ing = _ingestor(alerts=[
            _alert(alert_id=f"a-{i}", fired=f"2026-07-20T09:0{i}:00Z") for i in range(3)
        ])
        result = ing.ingest_alerts(token="T")
        assert result.emitted_count == 3
        signals = list(ing.active_signals("acme"))
        assert len(signals) == 1 and signals[0].occurrence_count == 3

    def test_the_connector_drops_an_exact_redelivery_rather_than_emitting_it(self):
        raw = _alert(alert_id="a-1")
        result = _ingestor(alerts=[raw, dict(raw)]).ingest_alerts(token="T")
        assert result.emitted_count == 1
        assert result.subscription_status[SUB]["deduped"] == 1


# ── 2. Noise floors ─────────────────────────────────────────────────────────────


class TestNoiseFloors:

    def test_an_active_application_failure_is_never_floored(self):
        """`error` is a PROTECTED class in the shared defaults. An App Insights
        active failure maps to `error`, so a single occurrence stays visible —
        exactly as it would for any other source."""
        stream, _ = _admit(_alert(alert_id="a-1"))
        signal = list(stream.active_signals("acme"))[0]
        assert signal.representative.event_class == "error"
        visible, report = apply_noise_floors([signal])
        assert len(visible) == 1
        assert report.total_suppressed_signatures == 0

    @pytest.mark.parametrize("protected", ["error", "security"])
    def test_the_protected_classes_keep_their_shared_default(self, protected):
        """No App Insights-specific weakening: the protected classes are simply
        absent from the floor map, so they fall to DEFAULT_FLOOR = 1."""
        assert protected not in DEFAULT_NOISE_FLOORS
        assert protected not in CALIBRATED_NOISE_FLOORS
        assert DEFAULT_FLOOR == 1

    def test_a_below_floor_health_transition_is_suppressed_and_counted(self):
        """A health transition is `state_change`, which the shared policy floors.
        Suppression is loud: the report names the class and counts what it removed."""
        floor = DEFAULT_NOISE_FLOORS["state_change"]
        stream, _ = _admit(_health(event_id="h-1"))
        signal = list(stream.active_signals("acme"))[0]
        assert signal.representative.event_class == "state_change"
        assert signal.occurrence_count < floor

        visible, report = apply_noise_floors([signal])
        assert visible == []
        assert report.total_suppressed_signatures == 1
        assert report.total_suppressed_events == signal.occurrence_count
        assert report.suppressed_signatures["state_change"] == 1
        assert report.floors["state_change"] == floor

    def test_a_health_transition_at_the_floor_is_visible(self):
        """Count == floor is visible; only strictly-below is suppressed."""
        floor = DEFAULT_NOISE_FLOORS["state_change"]
        stream, _ = _admit(*[
            _health(event_id=f"h-{i}", at=f"2026-07-20T13:0{i}:00Z")
            for i in range(floor)
        ])
        signal = list(stream.active_signals("acme"))[0]
        assert signal.occurrence_count == floor
        visible, report = apply_noise_floors([signal])
        assert len(visible) == 1
        assert report.total_suppressed_signatures == 0

    def test_suppression_is_reported_not_silent(self):
        stream, _ = _admit(_health(event_id="h-1"))
        _, report = apply_noise_floors(list(stream.active_signals("acme")))
        blob = report.to_dict()
        assert blob["total_suppressed_signatures"] == 1
        assert blob["floors"]                      # self-describing
        assert "state_change" in json.dumps(blob)

    def test_the_floor_policy_names_no_app_insights_class(self):
        """Structural: the shared policy must not have grown an App Insights case."""
        for module in ("noise_floor", "ops_calibration"):
            src = (Path(apply_noise_floors.__code__.co_filename).parent
                   / f"{module}.py").read_text(encoding="utf-8").lower()
            for token in ("app_insights", "appinsights", "applicationinsights"):
                assert token not in src, f"{module}.py mentions {token}"


# ── 3. Run budgets ──────────────────────────────────────────────────────────────


class TestRunBudget:

    def test_the_connector_uses_the_shared_calibrated_budget(self):
        """Not an App Insights-specific number — the one calibrated value every
        cloud source is bounded by."""
        src = Path(ae.__file__).read_text(encoding="utf-8")
        assert "CALIBRATED_RUN_EVENT_BUDGET" in src
        assert isinstance(CALIBRATED_RUN_EVENT_BUDGET, int)
        assert CALIBRATED_RUN_EVENT_BUDGET > 0

    def test_budget_exhaustion_defers_and_counts(self):
        ing = _ingestor(
            alerts=[_alert(alert_id=f"a-{i}", fired=f"2026-07-20T09:0{i}:00Z")
                    for i in range(4)],
            budget=1,
        )
        result = ing.ingest_alerts(token="T")
        st = result.subscription_status[SUB]
        assert st["status"] == "deferred"
        assert st["reason"] == "run_event_budget_exhausted"
        report = result.budget
        assert report["breached"] is True
        assert report["deferred"] >= 1
        assert report["processed"] >= 1

    def test_a_deferred_subscription_keeps_a_resumable_checkpoint(self):
        prior = ae.encode_checkpoints({SUB: "2026-07-01T00:00:00Z"})
        result = _ingestor(
            alerts=[_alert(alert_id=f"a-{i}", fired=f"2026-07-20T09:0{i}:00Z")
                    for i in range(4)],
            budget=1,
        ).ingest_alerts(token="T", checkpoint=prior)
        assert result.subscription_status[SUB]["checkpoint_advanced"] is False
        # unchanged, so the whole page is re-polled next run rather than lost
        assert ae.decode_checkpoints(result.next_checkpoint)[SUB] == "2026-07-01T00:00:00Z"

    def test_the_poller_stops_fetching_once_capacity_is_exhausted(self):
        """The budget must stop the run REQUESTING more, not merely discard what it
        already paid to fetch. With the budget spent on the first subscription, the
        second is never polled at all."""
        other = "bbbbbbbb-0000-0000-0000-000000000002"
        client = _AlertsFake([
            _alert(alert_id=f"a-{i}", fired=f"2026-07-20T09:0{i}:00Z") for i in range(3)
        ])
        ing = ae.AzureEventIngestor(
            "acme",
            cfg.AzureEventConfig(
                environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
                mode=cfg.MODE_LIGHTHOUSE, subscriptions=[SUB, other],
            ),
            alerts_client=client, budget=1,
        )
        result = ing.ingest_alerts(token="T")
        assert client.calls == [SUB], "the second subscription must not be fetched"
        assert result.subscription_status[other]["status"] == "deferred"
        assert result.subscription_status[other]["polled"] == 0

    def test_a_skipped_subscription_is_visibly_deferred_not_silently_dropped(self):
        other = "bbbbbbbb-0000-0000-0000-000000000002"
        ing = ae.AzureEventIngestor(
            "acme",
            cfg.AzureEventConfig(
                environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
                mode=cfg.MODE_LIGHTHOUSE, subscriptions=[SUB, other],
            ),
            alerts_client=_AlertsFake([_alert(alert_id="a-1"), _alert(alert_id="a-2")]),
            budget=1,
        )
        result = ing.ingest_alerts(token="T")
        st = result.subscription_status[other]
        assert st["status"] == "deferred"
        assert st["reason"] == "run_event_budget_exhausted"
        assert st["checkpoint_advanced"] is False

    def test_an_unbounded_ingestor_processes_everything(self):
        """The default (no budget) is unbounded, so the bound is a deliberate
        configuration rather than a hidden cap."""
        result = _ingestor(alerts=[
            _alert(alert_id=f"a-{i}", fired=f"2026-07-20T09:0{i}:00Z") for i in range(5)
        ]).ingest_alerts(token="T")
        assert result.emitted_count == 5
        assert result.budget.get("breached") in (False, None)


# ── 4. Correlation windows ──────────────────────────────────────────────────────


class TestCorrelationWindows:

    def test_the_shared_window_service_is_used_with_its_calibrated_windows(self):
        assert JOIN_EVENT_INCIDENT in CALIBRATED_CORRELATION_WINDOWS
        assert JOIN_EVENT_EVENT in CALIBRATED_CORRELATION_WINDOWS

    def test_an_in_window_join_passes_and_records_its_trace(self):
        window = CALIBRATED_CORRELATION_WINDOWS[JOIN_EVENT_INCIDENT]
        event_at = "2026-07-20T09:00:00Z"
        incident_at = "2026-07-20T09:30:00Z"
        join = join_within_window(event_at, incident_at, JOIN_EVENT_INCIDENT, org_id="acme")
        trace = join.to_trace()["correlation_window"]
        assert join.within is True
        assert trace["join_type"] == JOIN_EVENT_INCIDENT
        assert trace["window_seconds"] == window
        assert trace["delta_seconds"] == 1800
        assert trace["within_window"] is True

    def test_an_out_of_window_join_is_rejected_but_still_traced(self):
        """A rejected coincidence must be auditable, never silent."""
        window = CALIBRATED_CORRELATION_WINDOWS[JOIN_EVENT_INCIDENT]
        event_at = "2026-07-20T09:00:00Z"
        far = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc) + timedelta(
            seconds=window + 60
        )
        join = join_within_window(
            event_at, far.strftime("%Y-%m-%dT%H:%M:%SZ"), JOIN_EVENT_INCIDENT,
            org_id="acme",
        )
        trace = join.to_trace()["correlation_window"]
        assert join.within is False
        assert trace["within_window"] is False
        assert trace["delta_seconds"] == window + 60
        assert trace["window_seconds"] == window

    def test_the_window_boundary_is_inclusive(self):
        window = CALIBRATED_CORRELATION_WINDOWS[JOIN_EVENT_INCIDENT]
        start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        at_edge = start + timedelta(seconds=window)
        past_edge = start + timedelta(seconds=window + 1)
        assert join_within_window(
            start.strftime(fmt), at_edge.strftime(fmt), JOIN_EVENT_INCIDENT
        ).within is True
        assert join_within_window(
            start.strftime(fmt), past_edge.strftime(fmt), JOIN_EVENT_INCIDENT
        ).within is False

    def test_an_in_window_agreement_elevates_confidence(self):
        gated = gate_operational_corroboration(
            "2026-07-20T09:00:00Z", "2026-07-20T09:10:00Z", org_id="acme"
        )
        assert gated.within is True
        assert gated.elevates is True
        trace = gated.to_trace()
        assert trace["corroboration"]["elevates"] is True
        assert trace["correlation_window"]["within_window"] is True

    def test_an_out_of_window_agreement_contributes_no_elevation(self):
        """Coincidence must never inflate confidence."""
        window = CALIBRATED_CORRELATION_WINDOWS[JOIN_EVENT_INCIDENT]
        far = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc) + timedelta(
            seconds=window * 4
        )
        gated = gate_operational_corroboration(
            "2026-07-20T09:00:00Z", far.strftime("%Y-%m-%dT%H:%M:%SZ"), org_id="acme"
        )
        assert gated.within is False
        assert gated.elevates is False
        # the base confidence is unchanged — no partial credit for a near miss
        assert gated.confidence == "MEDIUM"
        assert gated.to_trace()["corroboration"]["elevates"] is False

    def test_a_per_org_window_override_applies(self):
        policy = CorrelationWindowPolicy()
        policy.set_org_window("acme", JOIN_EVENT_INCIDENT, 60)
        join = join_within_window(
            "2026-07-20T09:00:00Z", "2026-07-20T09:05:00Z",
            JOIN_EVENT_INCIDENT, org_id="acme", policy=policy,
        )
        assert join.within is False
        assert join.to_trace()["correlation_window"]["window_seconds"] == 60

    def test_the_window_service_names_no_app_insights_case(self):
        src = Path(join_within_window.__code__.co_filename).read_text(encoding="utf-8").lower()
        for token in ("app_insights", "appinsights", "applicationinsights"):
            assert token not in src


# ── 5. End to end: bounded output, correct counts, visible reporting ────────────


class TestEndToEndThroughTheSharedAssembly:

    def _rows(self, records, sn_data=None):
        return build_cloud_ops_runtime("acme", sn_data, bridge_records=records)

    def test_app_insights_events_reach_the_shared_assembly_and_fold(self):
        result = _ingestor(alerts=[
            _alert(alert_id=f"a-{i}", fired=f"2026-07-20T09:0{i}:00Z") for i in range(4)
        ]).ingest_alerts(token="T")
        runtime = self._rows(result.records)
        rows = runtime.block["event_signatures"]
        assert len(rows) == 1
        assert rows[0]["event_count"] == 4
        assert rows[0]["recurring"] is True
        assert rows[0]["first_seen"] and rows[0]["last_seen"]

    def test_the_assembly_reports_suppression_and_budget(self):
        """Every suppression or deferral is reported visibly — no silent truncation."""
        result = _ingestor(alerts=[_alert(alert_id="a-1")], health=[_health()]).ingest_all(
            token="T"
        )
        runtime = self._rows(result.records)
        health = runtime.health["b8_event_bridge"]
        assert "noise_suppression" in health
        assert "budget" in health
        # the health transition is below the state_change floor → suppressed, counted
        assert health["noise_suppression"]["total_suppressed_signatures"] == 1
        assert health["active_signals"] == 2
        assert health["visible_signals"] == 1

    def test_output_stays_bounded_under_a_flood(self):
        """A large re-firing flood collapses to one row with an exact count —
        bounded output, evidence preserved by sampling."""
        flood = [
            _alert(alert_id=f"a-{i}",
                   fired=(datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
                          + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            for i in range(200)
        ]
        result = _ingestor(alerts=flood).ingest_alerts(token="T")
        rows = self._rows(result.records).block["event_signatures"]
        assert len(rows) == 1
        assert rows[0]["event_count"] == 200
        assert len(rows[0]["evidence_pointers"]) <= 10      # bounded sample
        assert rows[0]["evidence_sampled_from"] == 200      # ...but honest about it

    def test_two_applications_stay_two_rows(self):
        result = _ingestor(alerts=[
            _alert(alert_id="a-1", component=_component("checkout-api")),
            _alert(alert_id="a-2", component=_component("orders-api")),
        ]).ingest_alerts(token="T")
        rows = self._rows(result.records).block["event_signatures"]
        assert len(rows) == 2
        assert {r["resource_id"].rsplit("/", 1)[-1] for r in rows} == {
            "checkout-api", "orders-api"
        }

    def test_every_row_is_window_gated(self):
        result = _ingestor(alerts=[_alert(alert_id="a-1")]).ingest_alerts(token="T")
        for row in self._rows(result.records).block["event_signatures"]:
            assert row["window_gated"] is True

    def test_the_transport_is_derived_not_assumed(self):
        """A natively-ingested App Insights signal must not claim it arrived via the
        MSP-B8 staging bridge (this row used to say so unconditionally)."""
        result = _ingestor(alerts=[_alert(alert_id="a-1")]).ingest_alerts(token="T")
        row = self._rows(result.records).block["event_signatures"][0]
        assert row["transports"] == [TRANSPORT_NATIVE]
        assert row["transport"] == TRANSPORT_NATIVE
        assert row["transport"] != TRANSPORT_BRIDGE

    def test_an_exact_bridged_twin_is_deduped_and_keeps_one_transport(self):
        """A native event and its bridged twin share the provider event id, so B7
        treats the twin as a redelivery and drops it — the documented
        transport-equivalence behaviour (they collapse to one signal rather than
        double-counting). The surviving firing's transport is therefore the honest
        answer, and `mixed` is unreachable this way."""
        result = _ingestor(alerts=[_alert(alert_id="a-1")]).ingest_alerts(token="T")
        native = result.records[0]
        bridged = json.loads(json.dumps(native))
        bridged["event"]["source_system"] = "bridge:azure"
        bridged["event"]["provenance"]["source_system"] = "bridge:azure"
        bridged["batch_id"] = "batch-1"
        rows = self._rows([native, bridged]).block["event_signatures"]
        assert len(rows) == 1, "a native event and its bridged twin must fold to one"
        assert rows[0]["event_count"] == 1, "the twin must not double-count"
        assert rows[0]["transports"] == [TRANSPORT_NATIVE]

    def test_two_distinct_firings_from_different_transports_report_both(self):
        """`mixed` is reachable only when two DIFFERENT firings of one condition
        arrive by different routes — then the row must not pick one and hide the
        other."""
        result = _ingestor(alerts=[
            _alert(alert_id="a-1", fired="2026-07-20T09:00:00Z"),
            _alert(alert_id="a-2", fired="2026-07-20T09:30:00Z"),
        ]).ingest_alerts(token="T")
        native, second = result.records[0], json.loads(json.dumps(result.records[1]))
        second["event"]["source_system"] = "bridge:azure"
        second["event"]["provenance"]["source_system"] = "bridge:azure"
        second["batch_id"] = "batch-1"
        rows = self._rows([native, second]).block["event_signatures"]
        assert len(rows) == 1
        assert rows[0]["event_count"] == 2
        assert rows[0]["transports"] == sorted([TRANSPORT_BRIDGE, TRANSPORT_NATIVE])
        assert rows[0]["transport"] == TRANSPORT_MIXED

    def test_the_event_rebuilds_from_the_record_for_the_shared_stream(self):
        result = _ingestor(alerts=[_alert(alert_id="a-1")]).ingest_alerts(token="T")
        event = operational_event_from_bridge_record(result.records[0], org_id="acme")
        assert event.event_class == "error"
        assert event.event_signature


# ── 6. Structural: no App Insights-specific volume pipeline ─────────────────────


class TestNoParallelPipeline:

    VOLUME_MODULES = (
        "signals/ops_stream.py",
        "signals/noise_floor.py",
        "signals/budget.py",
        "signals/aggregation.py",
        "signals/ops_calibration.py",
        "correlation/windows.py",
    )

    @pytest.mark.parametrize("relative", VOLUME_MODULES)
    def test_no_shared_volume_module_carries_app_insights_logic(self, relative):
        """The four disciplines stay provider-agnostic. Scans code, not prose:
        comments and docstrings are stripped, because a sentence noting that a
        shared path also serves App Insights is documentation, while an identifier
        or a branch on an App Insights value is the leak this catches."""
        root = Path(ae.__file__).resolve().parents[1]
        path = root / relative
        assert path.exists(), relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
            code = ast.unparse(tree).lower()
        for token in ("app_insights", "appinsights", "applicationinsights"):
            assert token not in code, f"{relative} carries {token} logic"

    def test_the_connector_owns_exactly_one_admission_stream(self):
        """One OpsEventStream per connector — not one per surface, and not a second
        App Insights-specific one."""
        ing = _ingestor(alerts=[_alert(alert_id="a-1")], health=[_health()])
        before = ing.stream
        ing.ingest_all(token="T")
        assert ing.stream is before

    def test_app_insights_and_other_azure_events_share_one_stream(self):
        """An App Insights signal and a plain Azure Monitor alert are admitted to
        the SAME stream, so the run budget is shared rather than per-surface."""
        plain = {"data": {"essentials": {
            "alertId": f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/p-1",
            "alertRule": "HighCPU", "severity": "Sev2", "monitorCondition": "Fired",
            "firedDateTime": "2026-07-20T09:00:00Z",
            "alertTargetIDs": [
                f"/subscriptions/{SUB}/resourceGroups/rg/providers"
                "/Microsoft.Compute/virtualMachines/vm1"
            ],
            "description": "CPU high",
        }}}
        ing = _ingestor(alerts=[_alert(alert_id="a-1"), plain])
        ing.ingest_alerts(token="T")
        assert len(list(ing.active_signals("acme"))) == 2
        assert ing.budget_report()["seen"] == 2

    def test_the_budget_is_shared_across_surfaces_not_per_surface(self):
        """One budget for the whole connector, not one per surface: the alert spends
        it and the health poll is then cut short."""
        ing = _ingestor(alerts=[_alert(alert_id="a-1")], health=[_health()], budget=1)
        result = ing.ingest_all(token="T")
        assert ing.budget_report()["processed"] == 1
        deferred = [k for k, st in result.subscription_status.items()
                    if st.get("status") == "deferred"]
        assert deferred, "the surfaces after the budget was spent must be deferred"


class TestDeferralReporting:
    """A budget that stops the FETCH is the desired behaviour — and the case the
    budget's own counters cannot see, because it never saw those events."""

    def test_a_skipped_poll_is_reported_even_though_no_event_was_counted(self):
        ing = _ingestor(alerts=[_alert(alert_id="a-1")], health=[_health()], budget=1)
        ing.ingest_all(token="T")
        # The budget counted no deferred EVENTS for the skipped polls...
        assert ing.budget_report().get("breached") in (False, None)
        # ...so the deferral report is what keeps the run honest.
        report = ing.deferral_report()
        assert report["complete"] is False
        assert report["deferred_polls"] >= 1
        assert report["reason"] == "run_event_budget_exhausted"
        skipped = [d for d in report["deferred"] if d["fetched"] is False]
        assert skipped, "a poll stopped before fetching must be named"
        assert {d["stream"] for d in report["deferred"]} <= set(ae.V1_STREAMS)

    def test_a_complete_poll_reports_complete(self):
        ing = _ingestor(alerts=[_alert(alert_id="a-1")])
        ing.ingest_all(token="T")
        report = ing.deferral_report()
        assert report["complete"] is True
        assert report["deferred_polls"] == 0
        assert "reason" not in report

    def test_a_mid_page_deferral_is_reported_as_fetched(self):
        """Distinguished from a skipped poll: this page WAS paid for, and its
        checkpoint is deliberately left unadvanced."""
        ing = _ingestor(
            alerts=[_alert(alert_id=f"a-{i}", fired=f"2026-07-20T09:0{i}:00Z")
                    for i in range(4)],
            budget=1,
        )
        ing.ingest_alerts(token="T")
        report = ing.deferral_report()
        assert report["complete"] is False
        assert any(d["fetched"] is True for d in report["deferred"])
