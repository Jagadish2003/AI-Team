"""MSP-B7 / AT-673 — tests for the correlation-window service.

Covers the T5 acceptance criteria and supporting guarantees:
  * AC5 — two facts inside the configured window join; the SAME two facts moved
          outside do not — with the window and delta recorded in the joined
          claim's evidence trace.
  * AC6 — an event↔incident agreement inside the window raises confidence; the
          identical agreement outside contributes zero (coincidence never
          inflates confidence).
  * per-join-type + per-org configurable windows; window/delta on the trace;
          tolerant timestamp handling.

DB-free.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from discovery.correlation import (
    DEFAULT_CORRELATION_WINDOWS,
    JOIN_EVENT_EVENT,
    JOIN_EVENT_INCIDENT,
    CorrelationWindowPolicy,
    gate_operational_corroboration,
    join_within_window,
    window_for,
    within_window,
)
from discovery.signals import OperationalEvent, ResourceRef

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _event(when):
    return OperationalEvent.build(
        org_id="acme", source_system="aws_cloudwatch", signal_id="alarm-1",
        event_type="ALARM State Change", event_class="state_change", severity="high",
        resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-1"),
        observed_at=when.isoformat(),
    )


def _incident(when):
    return {"incident_id": "INC001", "opened_at": when.isoformat()}


# ── AC5: inside joins, outside does not; window + delta on the trace ─────────

def test_ac5_two_facts_inside_window_join():
    # event↔incident default window is 2h. Facts 30 min apart → join.
    ev = _event(_T0)
    inc = _incident(_T0 + timedelta(minutes=30))
    j = join_within_window(ev, inc, JOIN_EVENT_INCIDENT)
    assert j.within is True
    assert j.delta_seconds == 1800.0
    assert j.window_seconds == 2 * 3600


def test_ac5_same_facts_moved_outside_window_do_not_join():
    ev = _event(_T0)
    inc = _incident(_T0 + timedelta(hours=3))   # 3h apart, window is 2h
    j = join_within_window(ev, inc, JOIN_EVENT_INCIDENT)
    assert j.within is False
    assert j.delta_seconds == 3 * 3600.0
    assert j.window_seconds == 2 * 3600


def test_ac5_window_and_delta_recorded_in_evidence_trace():
    ev = _event(_T0)
    inc = _incident(_T0 + timedelta(minutes=45))
    trace = join_within_window(ev, inc, JOIN_EVENT_INCIDENT).to_trace()
    cw = trace["correlation_window"]
    assert cw["join_type"] == JOIN_EVENT_INCIDENT
    assert cw["window_seconds"] == 2 * 3600
    assert cw["delta_seconds"] == 2700.0
    assert cw["within_window"] is True
    assert cw["a_at"] == ev.observed_at
    assert cw["b_at"] == inc["opened_at"]


def test_ac5_out_of_window_join_still_records_delta_on_trace():
    # rejection is auditable too — the delta that failed is recorded.
    ev = _event(_T0)
    inc = _incident(_T0 + timedelta(days=3))     # the "AWS event and incident 3 days apart"
    trace = join_within_window(ev, inc, JOIN_EVENT_INCIDENT).to_trace()
    cw = trace["correlation_window"]
    assert cw["within_window"] is False
    assert cw["delta_seconds"] == 3 * 86400.0


def test_within_window_boundary_inclusive():
    ev = _event(_T0)
    at_edge = _incident(_T0 + timedelta(hours=2))        # exactly 2h → inside
    just_over = _incident(_T0 + timedelta(hours=2, seconds=1))
    assert within_window(ev, at_edge, JOIN_EVENT_INCIDENT) is True
    assert within_window(ev, just_over, JOIN_EVENT_INCIDENT) is False


# ── AC6: coincidence never inflates confidence ──────────────────────────────

def test_ac6_in_window_agreement_raises_confidence():
    ev = _event(_T0)
    inc = _incident(_T0 + timedelta(minutes=20))
    gate = gate_operational_corroboration(ev, inc)
    assert gate.within is True
    assert gate.elevates is True
    assert gate.confidence == "HIGH"          # MEDIUM → HIGH inside the window


def test_ac6_out_of_window_agreement_contributes_zero():
    ev = _event(_T0)
    inc = _incident(_T0 + timedelta(days=3))   # identical agreement, 3 days apart
    gate = gate_operational_corroboration(ev, inc)
    assert gate.within is False
    assert gate.elevates is False
    assert gate.confidence == "MEDIUM"         # stays at base — zero contribution


def test_ac6_identical_agreement_differs_only_by_window():
    # THE test: same two facts, only the time gap changes the confidence effect.
    ev = _event(_T0)
    near = _incident(_T0 + timedelta(minutes=10))
    far = _incident(_T0 + timedelta(hours=5))
    assert gate_operational_corroboration(ev, near).confidence == "HIGH"
    assert gate_operational_corroboration(ev, far).confidence == "MEDIUM"


def test_ac6_gate_trace_records_the_decision():
    ev = _event(_T0)
    far = _incident(_T0 + timedelta(hours=6))
    trace = gate_operational_corroboration(ev, far).to_trace()
    assert trace["correlation_window"]["within_window"] is False
    assert trace["corroboration"]["elevates"] is False
    assert trace["corroboration"]["confidence"] == "MEDIUM"


def test_ac6_no_double_elevation_when_base_already_high():
    ev = _event(_T0)
    inc = _incident(_T0 + timedelta(minutes=5))
    gate = gate_operational_corroboration(ev, inc, base_confidence="HIGH")
    assert gate.within is True
    assert gate.confidence == "HIGH"
    assert gate.elevates is False   # already HIGH → within-window but no raise


# ── configurable windows: per join type & per org ──────────────────────────

def test_event_event_has_its_own_window():
    assert window_for(JOIN_EVENT_EVENT) == DEFAULT_CORRELATION_WINDOWS[JOIN_EVENT_EVENT]
    assert window_for(JOIN_EVENT_INCIDENT) == DEFAULT_CORRELATION_WINDOWS[JOIN_EVENT_INCIDENT]


def test_unknown_join_type_uses_default_window():
    assert window_for("event_change") == 3600   # DEFAULT_WINDOW_SECONDS


def test_per_org_window_override():
    policy = CorrelationWindowPolicy()
    policy.set_org_window("tight-org", JOIN_EVENT_INCIDENT, 600)   # 10 min
    ev = _event(_T0)
    inc = _incident(_T0 + timedelta(minutes=30))
    # default org: 30 min < 2h → within.
    assert within_window(ev, inc, JOIN_EVENT_INCIDENT, policy=policy) is True
    # tight org: 30 min > 10 min → NOT within.
    assert within_window(ev, inc, JOIN_EVENT_INCIDENT, org_id="tight-org", policy=policy) is False


def test_custom_policy_windows():
    policy = CorrelationWindowPolicy({JOIN_EVENT_INCIDENT: 60})
    ev = _event(_T0)
    inc = _incident(_T0 + timedelta(minutes=2))
    assert within_window(ev, inc, JOIN_EVENT_INCIDENT, policy=policy) is False


# ── tolerant timestamp handling & validation ───────────────────────────────

def test_accepts_raw_iso_strings_and_z_suffix():
    j = join_within_window("2026-01-01T12:00:00Z", "2026-01-01T13:00:00Z", JOIN_EVENT_INCIDENT)
    assert j.within is True and j.delta_seconds == 3600.0


def test_unparseable_timestamp_cannot_join():
    ev = _event(_T0)
    j = join_within_window(ev, {"opened_at": "not-a-time"}, JOIN_EVENT_INCIDENT)
    assert j.within is False
    assert j.delta_seconds is None


def test_event_to_event_join_across_providers():
    a = _event(_T0)
    b = OperationalEvent.build(
        org_id="acme", source_system="azure_monitor", signal_id="az-1",
        event_type="Alert", event_class="state_change", severity="high",
        resource=ResourceRef(provider="azure", resource_type="compute", resource_id="/vm/1"),
        observed_at=(_T0 + timedelta(minutes=5)).isoformat(),
    )
    # event↔event window is 15 min; 5 min apart → within.
    assert within_window(a, b, JOIN_EVENT_EVENT) is True


def test_invalid_window_rejected():
    with pytest.raises(ValueError):
        CorrelationWindowPolicy({JOIN_EVENT_INCIDENT: 0})
    with pytest.raises(ValueError):
        CorrelationWindowPolicy(default_window_seconds=-1)
    with pytest.raises(ValueError):
        CorrelationWindowPolicy().set_org_window("o", JOIN_EVENT_INCIDENT, 0)
