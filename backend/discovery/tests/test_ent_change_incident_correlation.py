"""
ENT-5 / AT-263 (T2) — ENT_CHANGE_INCIDENT_CORRELATION detector unit tests.

Acceptance criteria coverage:
  AC3 — fires when post_change_incident_ratio >= 2.0 AND change_count_30d >= 3
         AND post_change_incidents >= 5, with Confidence = HIGH.
  AC6 (partial) — SIGNAL_METRICS defined; all four metrics numeric, present in
         raw_evidence, at most 8 metrics.

Plus edge cases: exact-threshold firing, below-threshold non-firing on each
condition independently, the 72-hour correlation window join, exclusion of
non-Implemented changes, the insufficient-data / volume guard, degraded signal,
and empty data.
"""
from __future__ import annotations

from discovery.detectors.ent_change_incident_correlation import (
    CONFIDENCE,
    DETECTOR_ID,
    MIN_CHANGE_COUNT,
    MIN_POST_CHANGE_INCIDENTS,
    RATIO_THRESHOLD,
    SIGNAL_METRICS,
    detect,
    evaluate,
)

AS_OF = "2026-06-10 00:00:00"


def _sn(
    changes,
    incidents,
    *,
    as_of: str = AS_OF,
    degraded: bool = False,
    baseline_incident_rate=None,
):
    """Build sn_data with a change_correlation block.

    ``changes``  : list of (state, closed_at) tuples.
    ``incidents``: list of opened_at strings.
    """
    block = {
        "changes": [
            {"change_id": f"CHG{i:07d}", "state": state, "closed_at": closed}
            for i, (state, closed) in enumerate(changes)
        ],
        "incidents": [
            {"incident_id": f"INC{i:07d}", "opened_at": opened}
            for i, opened in enumerate(incidents)
        ],
        "as_of": as_of,
        "degraded_signal": degraded,
    }
    if baseline_incident_rate is not None:
        block["baseline_incident_rate"] = baseline_incident_rate
    return {"change_correlation": block}


# Three implemented changes inside the 30-day window ending 2026-06-10.
_THREE_CHANGES = [
    ("Implemented", "2026-05-20 00:00:00"),
    ("Implemented", "2026-05-25 00:00:00"),
    ("Implemented", "2026-05-30 00:00:00"),
]
# Six incidents that fall within 72h of one of those changes.
_SIX_POST_CHANGE = [
    "2026-05-21 06:00:00", "2026-05-22 06:00:00",   # after change 1
    "2026-05-26 06:00:00", "2026-05-27 06:00:00",   # after change 2
    "2026-05-31 06:00:00", "2026-06-01 06:00:00",   # after change 3
]


# ─── AC6: SIGNAL_METRICS shape ────────────────────────────────────────────────

class TestSignalMetrics:
    def test_signal_metrics_exact_set(self):
        assert SIGNAL_METRICS == [
            "post_change_incident_ratio",
            "change_count_30d",
            "post_change_incidents",
            "baseline_incident_rate",
        ]

    def test_signal_metrics_max_eight(self):
        assert len(SIGNAL_METRICS) <= 8

    def test_each_metric_numeric_and_in_raw_evidence(self):
        ev = evaluate(None, _sn(_THREE_CHANGES, _SIX_POST_CHANGE), None)
        for metric in SIGNAL_METRICS:
            assert metric in ev.raw_evidence, f"{metric} missing from raw_evidence"
            assert isinstance(ev.raw_evidence[metric], (int, float)), (
                f"{metric} must be numeric"
            )


# ─── AC3: fires when all three conditions met ─────────────────────────────────

class TestFires:
    def test_fires_when_all_conditions_met(self):
        # 3 changes, 6 post-change incidents; only 2 baseline-only incidents so
        # baseline_rate = 8/30 = 0.267/day, post-change rate = 6/9 = 0.667/day,
        # ratio = 2.5 >= 2.0.
        incidents = _SIX_POST_CHANGE + ["2026-05-13 00:00:00", "2026-05-15 00:00:00"]
        results = detect(None, _sn(_THREE_CHANGES, incidents), None)
        assert len(results) == 1
        r = results[0]
        assert r.detector_id == DETECTOR_ID
        assert r.signal_source == "servicenow"
        assert r.threshold == RATIO_THRESHOLD
        assert r.metric_value >= RATIO_THRESHOLD
        assert r.raw_evidence["change_count_30d"] == 3
        assert r.raw_evidence["post_change_incidents"] == 6

    def test_confidence_is_high(self):
        ev = evaluate(None, _sn(_THREE_CHANGES, _SIX_POST_CHANGE), None)
        assert ev.fired is True
        assert CONFIDENCE == "HIGH"
        assert ev.raw_evidence["confidence"] == "HIGH"

    def test_fires_at_exact_ratio_threshold(self):
        # Pin baseline so ratio is exactly 2.0: post-change rate = 6/9 = 0.6667,
        # baseline_incident_rate = 0.33333 → ratio == 2.0.
        ev = evaluate(
            None,
            _sn(_THREE_CHANGES, _SIX_POST_CHANGE, baseline_incident_rate=0.33333),
            None,
        )
        assert ev.raw_evidence["post_change_incident_ratio"] == 2.0
        assert ev.fired is True

    def test_only_implemented_changes_counted(self):
        # Two extra changes in non-Implemented states must be ignored.
        changes = _THREE_CHANGES + [
            ("New", "2026-05-21 00:00:00"),
            ("Scheduled", "2026-05-22 00:00:00"),
        ]
        ev = evaluate(None, _sn(changes, _SIX_POST_CHANGE), None)
        assert ev.raw_evidence["change_count_30d"] == 3


# ─── 72-hour correlation window join ──────────────────────────────────────────

class TestCorrelationWindow:
    def test_incidents_outside_72h_window_not_counted(self):
        # Incidents opened well after the 72h window of every change.
        late = ["2026-06-05 00:00:00", "2026-06-06 00:00:00", "2026-06-07 00:00:00"]
        ev = evaluate(None, _sn(_THREE_CHANGES, late), None)
        assert ev.raw_evidence["post_change_incidents"] == 0
        assert ev.fired is False

    def test_incident_before_change_not_counted(self):
        # Incident opened just before the change close is not "post-change".
        ev = evaluate(
            None,
            _sn([("Implemented", "2026-05-20 12:00:00")], ["2026-05-20 06:00:00"]),
            None,
        )
        assert ev.raw_evidence["post_change_incidents"] == 0


# ─── AC3 guards: each firing condition independently ──────────────────────────

class TestDoesNotFire:
    def test_does_not_fire_when_ratio_below_threshold(self):
        # Same post-change incidents, but a flood of baseline incidents pushes
        # the baseline rate up so the ratio drops below 2.0.
        baseline_noise = [f"2026-05-{day:02d} 00:00:00" for day in range(12, 20)] * 6
        ev = evaluate(None, _sn(_THREE_CHANGES, _SIX_POST_CHANGE + baseline_noise), None)
        assert ev.raw_evidence["post_change_incident_ratio"] < RATIO_THRESHOLD
        assert ev.fired is False

    def test_does_not_fire_when_change_count_below_min(self):
        # 2 implemented changes (< 3) → insufficient_data, no fire.
        two_changes = _THREE_CHANGES[:2]
        post = _SIX_POST_CHANGE[:4]
        ev = evaluate(None, _sn(two_changes, post), None)
        assert ev.raw_evidence["change_count_30d"] == 2
        assert ev.raw_evidence["insufficient_data"] is True
        assert ev.fired is False

    def test_does_not_fire_when_post_change_incidents_below_min(self):
        # Only 4 post-change incidents (< 5), even with a high ratio.
        ev = evaluate(None, _sn(_THREE_CHANGES, _SIX_POST_CHANGE[:4]), None)
        assert ev.raw_evidence["post_change_incidents"] == 4
        assert ev.fired is False

    def test_does_not_fire_when_degraded_signal(self):
        ev = evaluate(None, _sn(_THREE_CHANGES, _SIX_POST_CHANGE, degraded=True), None)
        assert ev.raw_evidence["degraded_signal"] is True
        assert ev.fired is False

    def test_does_not_fire_when_no_data(self):
        assert detect(None, {}, None) == []
        assert detect(None, None, None) == []
        ev = evaluate(None, _sn([], []), None)
        assert ev.raw_evidence["insufficient_data"] is True
        assert ev.fired is False


# ─── evaluate() contract ──────────────────────────────────────────────────────

class TestEvaluateContract:
    def test_metric_value_is_ratio(self):
        ev = evaluate(None, _sn(_THREE_CHANGES, _SIX_POST_CHANGE), None)
        assert ev.metric_value == ev.raw_evidence["post_change_incident_ratio"]
        assert ev.detector_id == DETECTOR_ID

    def test_baseline_reliable_flag_tracks_ten_changes(self):
        # < 10 implemented changes → baseline_reliable False (advisory, ENT-5 §6).
        ev = evaluate(None, _sn(_THREE_CHANGES, _SIX_POST_CHANGE), None)
        assert ev.raw_evidence["baseline_reliable"] is False
        # Confirm MIN constants are wired as documented.
        assert MIN_CHANGE_COUNT == 3
        assert MIN_POST_CHANGE_INCIDENTS == 5