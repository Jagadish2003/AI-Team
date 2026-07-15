"""MSP-B7 / AT-675 (T7) — the Event Volume Discipline contract suite (Section 3).

This is the consolidated contract for the five volume disciplines — the
demo-blocking guarantees that let the MSP pack claim both scale and honesty:
re-fires fold, aggregates carry their proof, noise is suppressed loudly, budgets
degrade loudly, and coincidence never inflates confidence. Each test is labelled
with the Section-3 acceptance criterion it discharges and reproduces that
criterion's scenario as stated.

  * AC1 — a seeded alarm re-firing 200×/day → one signal, count 200, correct
          first/last, evidence resolves to raw instances.               (T1)
  * AC2 — an aggregate carries member count, span, and sample pointers each
          resolving to a stored raw payload.                            (T2)
  * AC3 — signatures below a configured floor produce no signals; the run report
          shows the suppressed count per class.                         (T3)
  * AC4 — a run over its event budget completes on the budgeted window and reports
          the deferred volume; never silent truncation.                 (T4)
  * AC5 — two facts inside the window join, the same two outside do not, with the
          window and delta on the joined claim's evidence trace.        (T5)
  * AC6 — an event↔incident agreement inside the window raises confidence; the
          identical agreement outside contributes zero.                 (T5)
  * AC7 — floors, budgets, and window defaults are set from B8's measured
          month-scale sample and documented with their rationale.       (T6)

Pure-Python (in-memory evidence store, no DB), so it runs alongside the other MSP
signal tests without the contract DB.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from discovery.correlation import (
    JOIN_EVENT_INCIDENT,
    gate_operational_corroboration,
    join_within_window,
)
from discovery.signals import (
    B8_MEASUREMENTS,
    CALIBRATED_CORRELATION_WINDOWS,
    CALIBRATED_NOISE_FLOORS,
    CALIBRATED_RUN_EVENT_BUDGET,
    InMemoryRawEventStore,
    NoiseFloorPolicy,
    OperationalEvent,
    OpsEventStream,
    ResourceRef,
    aggregate_events,
    apply_noise_floors,
    calibration_summary,
    fold_events,
    store_raw_event,
)

_HERE = os.path.dirname(__file__)
_DOC = os.path.join(_HERE, "..", "..", "..", "docs", "msp_operational_event_schema.md")
_B8_DOC = os.path.join(_HERE, "..", "..", "..", "docs", "MSP-B8_VOLUME_VALIDATION.md")

_DAY = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)


def _alarm_firing(n, *, org_id="msp_contract", store=None):
    """One firing of a stuck CloudWatch alarm (same signature every time)."""
    raw = {"alarmName": "cpu-high", "eventID": f"cw-{n}", "state": "ALARM", "firing": n}
    ev = OperationalEvent.build(
        org_id=org_id, source_system="aws_cloudwatch", signal_id=f"alarm-{n}",
        event_type="ALARM State Change", event_class="state_change", severity="high",
        resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-app-1"),
        observed_at=(_DAY + timedelta(minutes=7 * n)).isoformat(), message="CPU high",
    )
    if store is not None:
        store_raw_event(store, org_id, ev, raw)
    return ev


def _audit_flood(n, *, org_id="msp_contract", store=None, severity="info"):
    """One firing of one audit action (one signature); an audit flood at volume."""
    raw = {"eventName": "AssumeRole", "eventID": f"ct-{n}", "seq": n,
           "userIdentity": {"arn": "svc"}}
    ev = OperationalEvent.build(
        org_id=org_id, source_system="aws_cloudtrail", signal_id=f"audit-{n}",
        event_type="AssumeRole", event_class="audit", severity=severity,
        resource=ResourceRef(provider="aws", resource_type="identity", resource_id="role/admin"),
        observed_at=(_DAY + timedelta(seconds=8 * n)).isoformat(),
        payload={"principal": "svc"},
    )
    if store is not None:
        store_raw_event(store, org_id, ev, raw)
    return ev


# ── AC1: dedup at the door ───────────────────────────────────────────────────

def test_ac1_alarm_refiring_200_times_a_day_is_one_signal():
    store = InMemoryRawEventStore()
    firings = [_alarm_firing(n, store=store) for n in range(200)]

    signals = fold_events(firings)
    assert len(signals) == 1                      # exactly one detector-visible signal
    sig = signals[0]
    assert sig.occurrence_count == 200            # count 200
    assert sig.first_seen == firings[0].observed_at   # correct first
    assert sig.last_seen == firings[-1].observed_at   # correct last

    raws = sig.resolve_raw_instances(store)        # evidence resolves to raw instances
    assert len(raws) == 200
    assert {r["firing"] for r in raws} == set(range(200))


# ── AC2: traceable aggregate ─────────────────────────────────────────────────

def test_ac2_aggregate_carries_count_span_and_resolvable_sample():
    store = InMemoryRawEventStore()
    events = [_audit_flood(n, store=store) for n in range(6_000)]

    aggregates = aggregate_events(events)          # audit is high-cardinality → rolled up
    assert len(aggregates) == 1
    agg = aggregates[0]

    assert agg.member_count == 6_000               # member count (exact)
    assert agg.first_seen == events[0].observed_at  # span
    assert agg.last_seen == events[-1].observed_at
    assert 0 < len(agg.sample_pointers) <= 10       # bounded sample of pointers

    raws = agg.resolve_sample_raw(store)            # each resolves to a stored raw payload
    assert len(raws) == len(agg.sample_pointers)
    assert all(r["eventName"] == "AssumeRole" for r in raws)


# ── AC3: noise floor — suppression is visible, never silent ─────────────────

def test_ac3_below_floor_produces_no_signal_and_reports_suppressed_count():
    # audit floor is 5 (calibrated). One below-floor signature (count 3) + one busy.
    noisy = [_audit_flood(n) for n in range(3)]                       # signature A, count 3
    busy = [
        OperationalEvent.build(
            org_id="msp_contract", source_system="aws_cloudtrail", signal_id=f"busy-{n}",
            event_type="DeleteBucket", event_class="audit", severity="high",
            resource=ResourceRef(provider="aws", resource_type="storage", resource_id="bkt"),
            observed_at=(_DAY + timedelta(seconds=n)).isoformat(), payload={"principal": "svc"},
        )
        for n in range(20)                                            # signature B, count 20
    ]
    signals = fold_events(noisy + busy)
    visible, report = apply_noise_floors(signals)

    assert len(visible) == 1                        # below-floor signature → no signal
    assert visible[0].occurrence_count == 20
    assert report.suppressed_signatures == {"audit": 1}   # suppressed count per class
    assert report.suppressed_events == {"audit": 3}       # the 3 underlying events
    assert report.any_suppressed and report.floors["audit"] == 5
    json.dumps(report.to_dict())                    # reportable per run (never silent)


# ── AC4: per-run budget — loud degradation, never silent truncation ─────────

def test_ac4_run_over_budget_completes_and_reports_deferred_volume():
    stream = OpsEventStream(budget=100)
    outcomes = [stream.admit(_audit_flood(n)) for n in range(250)]

    assert sum(1 for o in outcomes if not o.is_deferred) == 100   # budgeted window
    assert sum(1 for o in outcomes if o.is_deferred) == 150       # deferred
    assert len(stream.active_signals()) >= 1                      # run completed

    report = stream.budget_report()
    assert report.processed == 100 and report.deferred == 150     # deferred volume reported
    assert report.seen == 250 and report.breached is True
    assert "150" in report.reason                                 # never silent
    json.dumps(report.to_dict())


# ── AC5: correlation window — inside joins, outside does not ────────────────

def test_ac5_facts_join_inside_window_not_outside_with_trace():
    ev = {"observed_at": _DAY.isoformat()}
    inside = {"opened_at": (_DAY + timedelta(minutes=30)).isoformat()}
    outside = {"opened_at": (_DAY + timedelta(days=3)).isoformat()}

    j_in = join_within_window(ev, inside, JOIN_EVENT_INCIDENT)
    j_out = join_within_window(ev, outside, JOIN_EVENT_INCIDENT)
    assert j_in.within is True                       # inside the window → join
    assert j_out.within is False                     # same facts, moved outside → no join

    # window and delta recorded in the joined claim's evidence trace (both cases).
    for j, within in ((j_in, True), (j_out, False)):
        cw = j.to_trace()["correlation_window"]
        assert cw["join_type"] == JOIN_EVENT_INCIDENT
        assert cw["window_seconds"] == 2 * 3600
        assert cw["delta_seconds"] is not None
        assert cw["within_window"] is within


# ── AC6: coincidence never inflates confidence ──────────────────────────────

def test_ac6_in_window_agreement_raises_confidence_out_of_window_zero():
    ev = {"observed_at": _DAY.isoformat()}
    near = {"opened_at": (_DAY + timedelta(minutes=20)).isoformat()}
    far = {"opened_at": (_DAY + timedelta(days=3)).isoformat()}

    inside = gate_operational_corroboration(ev, near)
    outside = gate_operational_corroboration(ev, far)

    assert inside.within and inside.elevates and inside.confidence == "HIGH"
    assert (not outside.within) and (not outside.elevates)
    assert outside.confidence == "MEDIUM"            # identical agreement → zero contribution


# ── AC7: calibration is evidence-based and documented ───────────────────────

def test_ac7_defaults_are_calibrated_from_b8_and_documented():
    # sourced from B8's measured month-scale sample.
    assert B8_MEASUREMENTS["source"] == "docs/MSP-B8_VOLUME_VALIDATION.md"
    assert os.path.isfile(_B8_DOC)

    # floors, budget, and windows are the calibrated values.
    assert CALIBRATED_NOISE_FLOORS == {"audit": 5, "state_change": 5, "access": 5}
    assert CALIBRATED_RUN_EVENT_BUDGET == 250_000
    assert CALIBRATED_CORRELATION_WINDOWS["event_incident"] == 2 * 3600

    # budget is derived from the measured month (never below the raw derivation).
    assert CALIBRATED_RUN_EVENT_BUDGET >= 8 * B8_MEASUREMENTS["month_events_generated"]

    # the derivation is exposed for audit, and documented with rationale.
    summary = calibration_summary()
    assert summary["measured_input"]["source"] == "docs/MSP-B8_VOLUME_VALIDATION.md"
    assert "derivation" in summary["budget"]
    with open(_DOC, "r", encoding="utf-8") as fh:
        doc = fh.read()
    assert "Calibration from B8" in doc and "MSP-B8_VOLUME_VALIDATION" in doc


# ── the full pipeline composes: dedup → floor → aggregate, with budget ──────

def test_disciplines_compose_end_to_end():
    # A realistic mixed run: a stuck alarm + an audit flood + low-count noise,
    # admitted under a budget, then floored and aggregated.
    store = InMemoryRawEventStore()
    events = [_alarm_firing(n, store=store) for n in range(50)]
    events += [_audit_flood(n, store=store) for n in range(500)]

    stream = OpsEventStream(budget=10_000)
    for ev in events:
        stream.admit(ev)
    assert not stream.budget_report().breached      # within budget

    visible, suppression = apply_noise_floors(stream.active_signals())
    # both signatures recur well above the floor → nothing suppressed here.
    assert not suppression.any_suppressed
    assert len(visible) == 2

    aggregates = aggregate_events(events)            # only high-cardinality classes
    classes = {a.event_class for a in aggregates}
    assert classes == {"state_change", "audit"}
