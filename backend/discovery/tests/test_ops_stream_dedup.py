"""MSP-B7 / AT-669 — tests for dedup at admission (active-signal folding).

Covers the T1 acceptance criterion and its supporting guarantees:
  * AC1  — a seeded alarm re-firing 200 times over a day yields exactly one
           detector-visible signal with count 200 and correct first/last
           timestamps; its evidence resolves to raw instances.
  * dedup key is (event_signature, resource, active period) — distinct
           signatures / resources / periods never fold together.
  * deterministic — folding is independent of admission order.
  * org-scoped — orgs sharing a signature never fold, and cross-org admit /
           resolve is refused.
  * idempotent — an at-least-once redelivery of a firing is not double-counted.

DB-free: everything runs against the in-memory raw-event store.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from discovery.signals import (
    InMemoryRawEventStore,
    OperationalEvent,
    OrgScopeError,
    ResourceRef,
    fold_events,
    store_raw_event,
)
from discovery.signals.ops_stream import OpsEventStream

# A stuck CloudWatch alarm on one EC2 instance. Every firing shares the same
# signature (severity/timestamp/signal_id are excluded from the signature).
_RESOURCE = ResourceRef(provider="aws", resource_type="compute", resource_id="i-stuck-1")
_DAY_START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _firing(n: int, *, org_id="acme", when: datetime | None = None, store=None):
    """Build (and optionally store the raw payload of) one alarm firing."""
    ts = (when or (_DAY_START + timedelta(minutes=7 * n))).isoformat()
    raw = {"alarmName": "cpu-high", "instance": "i-stuck-1", "firing": n, "state": "ALARM"}
    ev = OperationalEvent.build(
        org_id=org_id,
        source_system="aws_cloudwatch",
        signal_id=f"alarm-firing-{n}",
        event_type="ALARM State Change",
        event_class="state_change",
        severity="high",
        resource=_RESOURCE,
        observed_at=ts,
        message="CPU high",
    )
    if store is not None:
        store_raw_event(store, org_id, ev, raw)
    return ev


# ── AC1: 200 re-fires over a day → one signal, count 200, correct span ───────

def test_ac1_alarm_refiring_200_times_folds_to_one_signal():
    store = InMemoryRawEventStore()
    firings = [_firing(n, store=store) for n in range(200)]

    stream = OpsEventStream()
    for ev in firings:
        stream.admit(ev)

    signals = stream.active_signals()
    assert len(signals) == 1, "200 re-fires must collapse to exactly one signal"

    sig = signals[0]
    assert sig.occurrence_count == 200
    # first/last span the real observation window.
    assert sig.first_seen == firings[0].observed_at
    assert sig.last_seen == firings[-1].observed_at
    assert sig.is_recurrence is True
    # every firing's provider event id is retained for traceability.
    assert len(sig.provider_event_ids) == 200
    assert set(sig.provider_event_ids) == {f"alarm-firing-{n}" for n in range(200)}


def test_ac1_evidence_resolves_to_raw_instances():
    store = InMemoryRawEventStore()
    firings = [_firing(n, store=store) for n in range(200)]
    signals = fold_events(firings)
    sig = signals[0]

    raws = sig.resolve_raw_instances(store)
    # the aggregate opens back to all 200 real raw payloads.
    assert len(raws) == 200
    assert {r["firing"] for r in raws} == set(range(200))
    assert all(r["alarmName"] == "cpu-high" for r in raws)


def test_ac1_detector_visible_dict_carries_the_proof():
    firings = [_firing(n) for n in range(200)]
    sig = fold_events(firings)[0]
    d = sig.to_dict()
    assert d["occurrence_count"] == 200
    assert d["first_seen"] == firings[0].observed_at
    assert d["last_seen"] == firings[-1].observed_at
    assert d["is_recurrence"] is True
    # normalised event shape is still present (detectors reason over it).
    assert d["resource_type"] == "compute"
    assert d["event_class"] == "state_change"
    assert len(d["provider_event_ids"]) == 200


# ── fold key: (signature, resource, active period) ──────────────────────────

def test_distinct_resources_do_not_fold():
    a = _firing(0)
    b = OperationalEvent.build(
        org_id="acme", source_system="aws_cloudwatch", signal_id="other",
        event_type="ALARM State Change", event_class="state_change", severity="high",
        resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-other"),
        observed_at=_DAY_START.isoformat(),
    )
    signals = fold_events([a, b])
    assert len(signals) == 2  # different resource → different signal


def test_distinct_signatures_do_not_fold():
    a = _firing(0)  # state_change
    b = OperationalEvent.build(
        org_id="acme", source_system="aws_cloudwatch", signal_id="err-1",
        event_type="Some Error", event_class="error", severity="high",
        resource=_RESOURCE, observed_at=_DAY_START.isoformat(),
    )
    assert a.event_signature != b.event_signature
    assert len(fold_events([a, b])) == 2


def test_different_active_periods_do_not_fold():
    # Same alarm on two different days → two active signals (one per period).
    day1 = _firing(0, when=_DAY_START)
    day2 = _firing(0, when=_DAY_START + timedelta(days=1))
    signals = fold_events([day1, day2])
    assert len(signals) == 2
    assert {s.occurrence_count for s in signals} == {1}


def test_same_day_many_firings_one_period():
    # 288 firings every 5 min stay within one day → one signal, count 288.
    firings = [
        _firing(n, when=_DAY_START + timedelta(minutes=5 * n))
        for n in range(288)
    ]
    signals = fold_events(firings)
    assert len(signals) == 1
    assert signals[0].occurrence_count == 288


# ── determinism ──────────────────────────────────────────────────────────────

def test_folding_is_order_independent():
    firings = [_firing(n) for n in range(50)]
    forward = fold_events(list(firings))[0]
    backward = fold_events(list(reversed(firings)))[0]
    assert forward.occurrence_count == backward.occurrence_count == 50
    assert forward.first_seen == backward.first_seen
    assert forward.last_seen == backward.last_seen
    # representative + retained ids are identical regardless of arrival order.
    assert forward.representative.signal_id == backward.representative.signal_id
    assert forward.to_dict()["provider_event_ids"] == backward.to_dict()["provider_event_ids"]


def test_representative_is_earliest_firing():
    firings = [_firing(n) for n in range(10)]
    sig = fold_events(list(reversed(firings)))[0]
    # earliest by (observed_at, signal_id) is firing 0, whatever the order.
    assert sig.representative.signal_id == "alarm-firing-0"


# ── org scoping ──────────────────────────────────────────────────────────────

def test_two_orgs_same_signature_never_fold():
    a = _firing(0, org_id="org-a")
    b = _firing(0, org_id="org-b")
    stream = OpsEventStream()
    stream.admit(a)
    stream.admit(b)
    assert len(stream.active_signals("org-a")) == 1
    assert len(stream.active_signals("org-b")) == 1
    assert len(stream.active_signals()) == 2


def test_admit_under_wrong_org_raises():
    stream = OpsEventStream()
    with pytest.raises(OrgScopeError):
        stream.admit(_firing(0, org_id="org-a"), org_id="org-b")


def test_resolve_across_org_boundary_raises():
    store = InMemoryRawEventStore()
    sig = fold_events([_firing(0, org_id="org-a", store=store)])[0]
    with pytest.raises(OrgScopeError):
        sig.resolve_raw_instances(store, org_id="org-b")


# ── idempotency (at-least-once redelivery) ──────────────────────────────────

def test_redelivered_firing_is_not_double_counted():
    ev = _firing(0)
    stream = OpsEventStream()
    a = stream.admit(ev)
    b = stream.admit(ev)  # same signal_id delivered again
    assert a.is_new
    assert b.is_duplicate
    assert stream.active_signals()[0].occurrence_count == 1


def test_admission_dispositions():
    stream = OpsEventStream()
    first = stream.admit(_firing(0))
    second = stream.admit(_firing(1))
    assert first.is_new
    assert second.folded
    assert stream.active_signals()[0].occurrence_count == 2


# ── robustness ───────────────────────────────────────────────────────────────

def test_unparseable_timestamp_still_counts_never_crashes():
    good = _firing(0, when=_DAY_START)
    bad = OperationalEvent.build(
        org_id="acme", source_system="aws_cloudwatch", signal_id="bad-ts",
        event_type="ALARM State Change", event_class="state_change", severity="high",
        resource=_RESOURCE, observed_at="not-a-timestamp",
    )
    # bad timestamp buckets separately (unbucketed) but never raises.
    signals = fold_events([good, bad])
    counts = sorted(s.occurrence_count for s in signals)
    assert counts == [1, 1]


def test_zero_period_rejected():
    with pytest.raises(ValueError):
        OpsEventStream(active_period_seconds=0)


def test_admit_rejects_non_event():
    stream = OpsEventStream()
    with pytest.raises(TypeError):
        stream.admit({"not": "an event"})  # type: ignore[arg-type]
