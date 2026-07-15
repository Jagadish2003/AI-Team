"""MSP-B7 / AT-670 — tests for aggregation roll-ups (high-cardinality classes).

Covers the T2 acceptance criterion and its supporting guarantees:
  * AC2  — an aggregate signal carries member count, span, and sample pointers,
           each resolving to a stored raw payload (the traceable-aggregate rule).
  * roll-up targets high-cardinality classes (audit floods, state-change storms)
           and leaves low-cardinality signals alone by default.
  * severity profile preserves the spread the signature ignores.
  * evidence sampling is bounded, span-anchored, deterministic — and raw
           retention is unchanged (every raw payload stays stored).
  * org-scoped — cross-org sample resolution is refused.

DB-free: everything runs against the in-memory raw-event store.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from discovery.signals import (
    HIGH_CARDINALITY_CLASSES,
    InMemoryRawEventStore,
    OperationalEvent,
    OrgScopeError,
    ResourceRef,
    aggregate_events,
    fold_events,
    roll_up,
    store_raw_event,
)
from discovery.signals.aggregation import _sample_indices

_AUDIT_RESOURCE = ResourceRef(provider="aws", resource_type="identity", resource_id="role/admin")
_DAY_START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _audit_flood_event(n, *, org_id="acme", severity="info", store=None,
                       principal="svc-account", when=None, spacing_seconds=30):
    """One firing of the SAME audit action (→ one signature) by one principal."""
    ts = (when or (_DAY_START + timedelta(seconds=spacing_seconds * n))).isoformat()
    raw = {"eventName": "AssumeRole", "eventID": f"evt-{n}", "seq": n,
           "userIdentity": {"arn": principal}}
    ev = OperationalEvent.build(
        org_id=org_id,
        source_system="aws_cloudtrail",
        signal_id=f"audit-{n}",
        event_type="AssumeRole",
        event_class="audit",
        severity=severity,
        resource=_AUDIT_RESOURCE,
        observed_at=ts,
        payload={"principal": principal},
    )
    if store is not None:
        store_raw_event(store, org_id, ev, raw)
    return ev


# ── AC2: traceable aggregate — count, span, sample pointers resolve to raw ───

def test_ac2_aggregate_carries_count_span_and_resolvable_sample():
    store = InMemoryRawEventStore()
    n = 9_000
    # 8s spacing keeps all 9 000 firings inside one active period (day).
    events = [_audit_flood_event(i, store=store, spacing_seconds=8) for i in range(n)]

    aggregates = aggregate_events(events)
    assert len(aggregates) == 1, "one signature/resource/window → one aggregate"
    agg = aggregates[0]

    # member count is exact (never compressed).
    assert agg.member_count == n
    assert agg.sampled_from == n
    # span reflects the real first/last firing.
    assert agg.first_seen == events[0].observed_at
    assert agg.last_seen == events[-1].observed_at

    # pointers are a bounded sample...
    assert 0 < len(agg.sample_pointers) <= 10
    assert agg.is_sampled is True
    # ...and EACH sample pointer resolves to a stored raw payload.
    raws = agg.resolve_sample_raw(store)
    assert len(raws) == len(agg.sample_pointers)
    assert all(r["eventName"] == "AssumeRole" for r in raws)


def test_ac2_sample_includes_span_endpoints():
    store = InMemoryRawEventStore()
    events = [_audit_flood_event(i, store=store) for i in range(500)]
    agg = aggregate_events(events)[0]
    raws = agg.resolve_sample_raw(store)
    seqs = {r["seq"] for r in raws}
    # the earliest (0) and latest (499) instance are always reachable.
    assert 0 in seqs
    assert 499 in seqs


def test_ac2_raw_retention_unchanged_all_members_still_stored():
    store = InMemoryRawEventStore()
    events = [_audit_flood_event(i, store=store) for i in range(200)]
    # sampling bounds the aggregate's pointers, but every raw payload remains
    # stored and independently resolvable (raw retention unchanged).
    sig = fold_events(events)[0]
    all_raws = sig.resolve_raw_instances(store)
    assert len(all_raws) == 200


# ── severity profile ─────────────────────────────────────────────────────────

def test_severity_profile_captures_the_spread():
    # Same audit action recorded at mixed severities (signature ignores severity).
    events = []
    for i in range(10):
        sev = "high" if i % 5 == 0 else "info"   # 2 high, 8 info
        events.append(_audit_flood_event(i, severity=sev))
    agg = aggregate_events(events)[0]
    assert agg.member_count == 10
    assert agg.severity_profile == {"high": 2, "info": 8}
    # serialised profile is ordered most-severe-first.
    assert list(agg.to_dict()["severity_profile"].keys()) == ["high", "info"]


# ── targeting: high-cardinality classes only, by default ────────────────────

def test_only_high_cardinality_classes_rolled_up_by_default():
    audit = _audit_flood_event(0)                       # audit → high-cardinality
    alarm = OperationalEvent.build(                     # state_change → high-card
        org_id="acme", source_system="aws_cloudwatch", signal_id="al-1",
        event_type="ALARM State Change", event_class="state_change", severity="high",
        resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-1"),
        observed_at=_DAY_START.isoformat(),
    )
    lifecycle = OperationalEvent.build(                 # lifecycle → NOT high-card
        org_id="acme", source_system="aws", signal_id="lc-1",
        event_type="RunInstances", event_class="lifecycle", severity="info",
        resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-2"),
        observed_at=_DAY_START.isoformat(),
    )
    aggregates = aggregate_events([audit, alarm, lifecycle])
    classes = {a.event_class for a in aggregates}
    assert classes == {"audit", "state_change"}   # lifecycle excluded by default
    assert "audit" in HIGH_CARDINALITY_CLASSES
    assert "state_change" in HIGH_CARDINALITY_CLASSES
    assert "lifecycle" not in HIGH_CARDINALITY_CLASSES


def test_only_high_cardinality_false_aggregates_everything():
    signals = fold_events([
        OperationalEvent.build(
            org_id="acme", source_system="aws", signal_id="lc-1",
            event_type="RunInstances", event_class="lifecycle", severity="info",
            resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-2"),
            observed_at=_DAY_START.isoformat(),
        )
    ])
    assert roll_up(signals) == []                          # excluded by default
    assert len(roll_up(signals, only_high_cardinality=False)) == 1


def test_configurable_class_set():
    signals = fold_events([
        OperationalEvent.build(
            org_id="acme", source_system="aws", signal_id="lc-1",
            event_type="RunInstances", event_class="lifecycle", severity="info",
            resource=ResourceRef(provider="aws", resource_type="compute", resource_id="i-2"),
            observed_at=_DAY_START.isoformat(),
        )
    ])
    out = roll_up(signals, high_cardinality_classes=frozenset({"lifecycle"}))
    assert len(out) == 1 and out[0].event_class == "lifecycle"


# ── determinism ──────────────────────────────────────────────────────────────

def test_aggregate_is_order_independent():
    store = InMemoryRawEventStore()
    events = [_audit_flood_event(i, store=store) for i in range(300)]
    fwd = aggregate_events(list(events))[0]
    bwd = aggregate_events(list(reversed(events)))[0]
    assert fwd.member_count == bwd.member_count
    assert fwd.first_seen == bwd.first_seen and fwd.last_seen == bwd.last_seen
    assert fwd.severity_profile == bwd.severity_profile
    # the sampled pointer set is identical regardless of arrival order.
    fwd_ids = [p["source_artifact"] for p in fwd.sample_pointers]
    bwd_ids = [p["source_artifact"] for p in bwd.sample_pointers]
    assert fwd_ids == bwd_ids


def test_sample_indices_bounds_and_endpoints():
    assert _sample_indices(5, 10) == [0, 1, 2, 3, 4]      # n <= k → all
    assert _sample_indices(0, 5) == []
    idx = _sample_indices(1000, 10)
    assert len(idx) == 10
    assert idx[0] == 0 and idx[-1] == 999                 # endpoints included
    assert idx == sorted(set(idx))                        # sorted, unique


# ── sampling boundary: small floods keep everything ─────────────────────────

def test_small_flood_keeps_all_pointers_not_sampled():
    store = InMemoryRawEventStore()
    events = [_audit_flood_event(i, store=store) for i in range(4)]  # < sample cap
    agg = aggregate_events(events)[0]
    assert agg.member_count == 4
    assert len(agg.sample_pointers) == 4
    assert agg.is_sampled is False
    assert len(agg.resolve_sample_raw(store)) == 4


# ── org scoping ──────────────────────────────────────────────────────────────

def test_resolve_sample_across_org_boundary_raises():
    store = InMemoryRawEventStore()
    events = [_audit_flood_event(i, org_id="org-a", store=store) for i in range(20)]
    agg = aggregate_events(events, org_id="org-a")[0]
    with pytest.raises(OrgScopeError):
        agg.resolve_sample_raw(store, org_id="org-b")


def test_two_orgs_never_share_an_aggregate():
    a = [_audit_flood_event(i, org_id="org-a") for i in range(5)]
    b = [_audit_flood_event(i, org_id="org-b") for i in range(5)]
    from discovery.signals import OpsEventStream

    stream = OpsEventStream()
    for ev in a + b:
        stream.admit(ev)
    assert len(roll_up(stream.active_signals("org-a"))) == 1
    assert len(roll_up(stream.active_signals("org-b"))) == 1
