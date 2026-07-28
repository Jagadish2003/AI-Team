"""MSP-B7 / AT-671 — tests for noise floors (loud, per-class suppression).

Covers the T3 acceptance criterion and its supporting guarantees:
  * AC3  — signatures below a configured floor produce no signals, and the run
           report shows the suppressed count per class (suppression is visible,
           never silent).
  * floors are per event class; unlisted classes fall back to the default (1 →
           never suppressed), so error/security are never silently dropped.
  * the report tallies BOTH suppressed signatures and suppressed event volume.
  * deterministic and order-independent; boundary at count == floor is visible.

DB-free: operates on folded active signals from the T1 stream.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from discovery.signals import (
    DEFAULT_FLOOR,
    NoiseFloorPolicy,
    OperationalEvent,
    ResourceRef,
    SuppressionReport,
    apply_noise_floors,
    fold_events,
)

_DAY = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _events(*, event_class, resource_id, count, signature_seed="s", severity="info"):
    """`count` firings of ONE signature (same class/resource/type) → one signal."""
    out = []
    for i in range(count):
        out.append(OperationalEvent.build(
            org_id="acme",
            source_system="aws_cloudtrail",
            signal_id=f"{signature_seed}-{i}",
            event_type=f"Action-{signature_seed}",
            event_class=event_class,
            severity=severity,
            resource=ResourceRef(provider="aws", resource_type="identity", resource_id=resource_id),
            observed_at=(_DAY + timedelta(seconds=30 * i)).isoformat(),
        ))
    return out


# ── AC3: below-floor signatures produce no signals; report shows the counts ──

def test_ac3_below_floor_suppressed_and_counted_per_class():
    # audit floor is 5 by default. One noisy signature (count 3 → below) and one
    # busy signature (count 20 → above). Both are audit class.
    noisy = _events(event_class="audit", resource_id="role/a", count=3, signature_seed="noisy")
    busy = _events(event_class="audit", resource_id="role/b", count=20, signature_seed="busy")

    signals = fold_events(noisy + busy)
    assert len(signals) == 2  # T1 folded them into two signatures

    visible, report = apply_noise_floors(signals)

    # the below-floor signature produces NO detector-visible signal.
    assert len(visible) == 1
    assert visible[0].occurrence_count == 20

    # the run report shows the suppressed count per class — visible, not silent.
    assert report.suppressed_signatures == {"audit": 1}
    assert report.suppressed_events == {"audit": 3}   # the 3 underlying events
    assert report.total_suppressed_signatures == 1
    assert report.total_suppressed_events == 3
    assert report.any_suppressed is True
    # the applied floor is recorded on the report (self-describing).
    assert report.floors["audit"] == 5


def test_ac3_report_is_json_serialisable_run_shape():
    import json

    noisy = _events(event_class="audit", resource_id="r", count=2, signature_seed="n")
    _, report = apply_noise_floors(fold_events(noisy))
    d = report.to_dict()
    json.dumps(d)  # must not raise
    assert d["total_suppressed_signatures"] == 1
    assert d["total_suppressed_events"] == 2
    assert d["suppressed_events"] == {"audit": 2}


def test_ac3_forty_thousand_one_off_signatures_all_suppressed_and_counted():
    # The "we ignored 40k events" scenario: many distinct one-off audit
    # signatures, each below the floor → all suppressed, all counted.
    events = []
    for k in range(400):
        events += _events(event_class="audit", resource_id=f"r{k}", count=1, signature_seed=f"sig{k}")
    signals = fold_events(events)
    assert len(signals) == 400
    visible, report = apply_noise_floors(signals)
    assert visible == []
    assert report.suppressed_signatures["audit"] == 400
    assert report.suppressed_events["audit"] == 400   # 1 event each


# ── per-class floors; safe classes never suppressed ─────────────────────────

def test_unlisted_class_uses_default_floor_never_suppressed():
    # 'error' has no configured floor → default 1 → a single occurrence survives.
    err = _events(event_class="error", resource_id="i-1", count=1, signature_seed="e")
    visible, report = apply_noise_floors(fold_events(err))
    assert len(visible) == 1
    assert not report.any_suppressed
    assert DEFAULT_FLOOR == 1


def test_error_and_security_not_floored_by_default():
    policy = NoiseFloorPolicy()
    assert policy.floor_for("error") == 1
    assert policy.floor_for("security") == 1
    assert policy.floor_for("audit") == 5


def test_configurable_floor_overrides_defaults():
    sig = _events(event_class="lifecycle", resource_id="i-1", count=3, signature_seed="lc")
    signals = fold_events(sig)
    # default: lifecycle floor 1 → visible.
    assert len(apply_noise_floors(signals)[0]) == 1
    # configured: lifecycle floor 5 → the count-3 signature is suppressed.
    policy = NoiseFloorPolicy({"lifecycle": 5})
    visible, report = policy.apply(signals)
    assert visible == []
    assert report.suppressed_signatures == {"lifecycle": 1}   # one signature
    assert report.suppressed_events == {"lifecycle": 3}       # its 3 events


# ── boundary ─────────────────────────────────────────────────────────────────

def test_count_equal_to_floor_is_visible():
    policy = NoiseFloorPolicy({"audit": 5})
    at_floor = _events(event_class="audit", resource_id="r", count=5, signature_seed="atf")
    below = _events(event_class="audit", resource_id="r2", count=4, signature_seed="below")
    signals = fold_events(at_floor + below)
    visible, report = policy.apply(signals)
    assert len(visible) == 1 and visible[0].occurrence_count == 5   # == floor → visible
    assert report.suppressed_events == {"audit": 4}                 # count 4 → suppressed


# ── determinism ──────────────────────────────────────────────────────────────

def test_suppression_is_order_independent():
    a = _events(event_class="audit", resource_id="r1", count=2, signature_seed="a")
    b = _events(event_class="audit", resource_id="r2", count=9, signature_seed="b")
    fwd = apply_noise_floors(fold_events(a + b))
    bwd = apply_noise_floors(fold_events(list(reversed(a + b))))
    assert [s.event_signature for s in fwd[0]] == [s.event_signature for s in bwd[0]]
    assert fwd[1].to_dict() == bwd[1].to_dict()


# ── validation & edges ───────────────────────────────────────────────────────

def test_empty_input_reports_nothing_suppressed():
    visible, report = apply_noise_floors([])
    assert visible == []
    assert report.total_suppressed_events == 0
    assert not report.any_suppressed


def test_invalid_floor_rejected():
    with pytest.raises(ValueError):
        NoiseFloorPolicy({"audit": 0})
    with pytest.raises(ValueError):
        NoiseFloorPolicy(default_floor=0)


def test_multi_class_report_tallies_separately():
    audit = _events(event_class="audit", resource_id="r", count=1, signature_seed="au")
    access = _events(event_class="access", resource_id="r", count=2, signature_seed="ac")
    signals = fold_events(audit + access)
    _, report = apply_noise_floors(signals)
    assert report.suppressed_signatures == {"audit": 1, "access": 1}
    assert report.suppressed_events == {"audit": 1, "access": 2}
