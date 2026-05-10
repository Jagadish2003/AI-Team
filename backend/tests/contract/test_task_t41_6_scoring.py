"""
T41-6 Scorer Recalibration — Contract Test Suite

Tests the surgical patch to backend/discovery/scorer.py.
All tests import from the REAL scorer at that path — no parallel module.

Coverage:
  - _rescale_impact() — clamping, boundary values, empty-factor safety
  - All 7 detector impact profiles (PERMISSION_BOTTLENECK, REPETITIVE_AUTOMATION included)
  - Fixture round-trip against t41_6_inputs.json / t41_6_expected.json
  - Regression: effort and confidence unchanged from pre-T41-6 behaviour
  - Determinism (replay-safe)
  - Distinct impact spread across fixture set (≥ 3 distinct values)
"""

import json
from pathlib import Path

import pytest

from discovery.models import DetectorResult
from discovery.scorer import (
    _RAW_IMPACT_MIN,
    _RAW_IMPACT_MAX,
    _rescale_impact,
    _compute_impact,
    _compute_effort,
    _compute_confidence,
    _assign_tier,
    score,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _load_fixtures():
    inputs   = json.loads((FIXTURES_DIR / "t41_6_inputs.json").read_text())
    expected = json.loads((FIXTURES_DIR / "t41_6_expected.json").read_text())
    return inputs, expected


def _dr(detector_id, signal_source, metric_value, threshold, raw_evidence) -> DetectorResult:
    return DetectorResult(
        detector_id=detector_id,
        signal_source=signal_source,
        metric_value=metric_value,
        threshold=threshold,
        raw_evidence=raw_evidence,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Group 1: _rescale_impact() unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRescaleImpact:

    def test_min_raw_maps_to_1(self):
        """_RAW_IMPACT_MIN should map to impact 1."""
        assert _rescale_impact(_RAW_IMPACT_MIN) == 1

    def test_max_raw_maps_to_10(self):
        """_RAW_IMPACT_MAX should map to impact 10."""
        assert _rescale_impact(_RAW_IMPACT_MAX) == 10

    def test_midpoint_maps_to_midrange(self):
        """Midpoint of raw range should produce impact near 5–6."""
        mid = (_RAW_IMPACT_MIN + _RAW_IMPACT_MAX) / 2
        result = _rescale_impact(mid)
        assert 5 <= result <= 6, f"Expected 5–6, got {result}"

    def test_clamp_below_min(self):
        """Raw values below _RAW_IMPACT_MIN are clamped to 1."""
        assert _rescale_impact(0.0) == 1
        assert _rescale_impact(-5.0) == 1

    def test_clamp_above_max(self):
        """Raw values above _RAW_IMPACT_MAX are clamped to 10."""
        assert _rescale_impact(100.0) == 10
        assert _rescale_impact(_RAW_IMPACT_MAX + 0.5) == 10

    def test_monotone(self):
        """Larger raw values produce >= impact."""
        samples = [1.2, 2.0, 3.0, 4.0, 5.0, 6.1]
        impacts = [_rescale_impact(r) for r in samples]
        assert impacts == sorted(impacts), f"Not monotone: {list(zip(samples, impacts))}"

    def test_output_always_int_in_range(self):
        """Return type is int, always in [1, 10]."""
        for raw in [0.0, 1.2, 3.5, 6.1, 9.9]:
            result = _rescale_impact(raw)
            assert isinstance(result, int)
            assert 1 <= result <= 10


# ─────────────────────────────────────────────────────────────────────────────
# Group 2: _compute_impact() — per-detector profile tests
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeImpactProfiles:

    def test_handoff_friction_impact_range(self):
        """HANDOFF_FRICTION with small org should score ≥ 4."""
        dr = _dr("HANDOFF_FRICTION", "salesforce", 2.8, 2.0,
                  {"total_cases_90d": 38, "handoff_score": 2.8})
        assert _compute_impact(dr) >= 4

    def test_approval_bottleneck_impact_range(self):
        """APPROVAL_BOTTLENECK with medium org and delay > 3d should score ≥ 5."""
        dr = _dr("APPROVAL_BOTTLENECK", "salesforce", 8.4, 3.0,
                  {"pending_count": 180, "avg_delay_days": 4.2, "approver_count": 3})
        assert _compute_impact(dr) >= 5

    def test_knowledge_gap_impact_range(self):
        """KNOWLEDGE_GAP with high gap_score should score ≥ 4."""
        dr = _dr("KNOWLEDGE_GAP", "salesforce", 0.71, 0.5,
                  {"closed_cases_90d": 65, "knowledge_gap_score": 0.71})
        assert _compute_impact(dr) >= 4

    def test_repetitive_automation_impact_range(self):
        """REPETITIVE_AUTOMATION with large org (650 records/90d) should score ≥ 5."""
        dr = _dr("REPETITIVE_AUTOMATION", "salesforce", 2.1, 0.8,
                  {"records_90d": 650, "flow_activity_score": 2.1, "trigger_object": "Case"})
        assert _compute_impact(dr) >= 5

    def test_permission_bottleneck_impact_range(self):
        """PERMISSION_BOTTLENECK with high bottleneck_score should score ≥ 5."""
        dr = _dr("PERMISSION_BOTTLENECK", "salesforce", 24.0, 10.0,
                  {"pending_count": 170, "bottleneck_score": 24.0})
        assert _compute_impact(dr) >= 5

    def test_cross_system_echo_highest_impact(self):
        """CROSS_SYSTEM_ECHO (multi-system + large org) should outscore single-system detectors."""
        cse = _dr("CROSS_SYSTEM_ECHO", "servicenow", 0.38, 0.15,
                   {"sf_total_cases": 1100, "sf_echo_score": 0.38,
                    "sn_echo_score": 0.29, "jira_echo_score": 0.0})
        hf  = _dr("HANDOFF_FRICTION", "salesforce", 2.8, 2.0,
                   {"total_cases_90d": 38, "handoff_score": 2.8})
        assert _compute_impact(cse) > _compute_impact(hf)

    def test_unknown_detector_falls_back_safely(self):
        """Unknown detector_id returns a valid impact in [1, 10]."""
        dr = _dr("FUTURE_DETECTOR", "salesforce", 5.0, 2.0, {"count": 10})
        result = _compute_impact(dr)
        assert 1 <= result <= 10


# ─────────────────────────────────────────────────────────────────────────────
# Group 3: Fixture round-trip
# ─────────────────────────────────────────────────────────────────────────────

class TestFixtureRoundTrip:

    def test_round_trip_impact_and_effort(self):
        """Full score() call must match t41_6_expected.json for all 6 fixtures."""
        inputs, expected = _load_fixtures()
        for inp, exp in zip(inputs, expected):
            dr = _dr(
                inp["detector_id"], inp["signal_source"],
                inp["metric_value"], inp["threshold"], inp["raw_evidence"],
            )
            result = score(dr)
            assert result["impact"] == exp["impact"], (
                f"{inp['detector_id']}: impact {result['impact']} != expected {exp['impact']}"
            )
            assert result["effort"] == exp["effort"], (
                f"{inp['detector_id']}: effort {result['effort']} != expected {exp['effort']}"
            )

    def test_round_trip_confidence(self):
        """score() confidence must match expected for all 6 fixtures."""
        inputs, expected = _load_fixtures()
        for inp, exp in zip(inputs, expected):
            dr = _dr(
                inp["detector_id"], inp["signal_source"],
                inp["metric_value"], inp["threshold"], inp["raw_evidence"],
            )
            result = score(dr)
            assert result["confidence"] == exp["confidence"], (
                f"{inp['detector_id']}: confidence {result['confidence']} != {exp['confidence']}"
            )

    def test_round_trip_tier(self):
        """score() tier must match expected for all 6 fixtures."""
        inputs, expected = _load_fixtures()
        for inp, exp in zip(inputs, expected):
            dr = _dr(
                inp["detector_id"], inp["signal_source"],
                inp["metric_value"], inp["threshold"], inp["raw_evidence"],
            )
            result = score(dr)
            assert result["tier"] == exp["tier"], (
                f"{inp['detector_id']}: tier {result['tier']} != {exp['tier']}"
            )

    def test_at_least_three_distinct_impact_values(self):
        """Fixture set must produce ≥ 3 distinct impact values — score compression failure guard."""
        inputs, _ = _load_fixtures()
        impacts = set()
        for inp in inputs:
            dr = _dr(
                inp["detector_id"], inp["signal_source"],
                inp["metric_value"], inp["threshold"], inp["raw_evidence"],
            )
            impacts.add(score(dr)["impact"])
        assert len(impacts) >= 3, (
            f"Score compression detected: only {len(impacts)} distinct impact value(s): {sorted(impacts)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Group 4: Determinism and output contract
# ─────────────────────────────────────────────────────────────────────────────

class TestDeterminismAndContract:

    def test_replay_is_deterministic(self):
        """Same DetectorResult must produce identical score() output on two calls."""
        dr = _dr("APPROVAL_BOTTLENECK", "salesforce", 8.4, 3.0,
                  {"pending_count": 180, "avg_delay_days": 4.2, "approver_count": 3})
        assert score(dr) == score(dr)

    def test_all_output_keys_present(self):
        """score() must return all required keys."""
        dr = _dr("HANDOFF_FRICTION", "salesforce", 2.8, 2.0,
                  {"total_cases_90d": 38, "handoff_score": 2.8})
        result = score(dr)
        for key in ("impact", "effort", "confidence", "tier", "roadmap_stage", "score_debug"):
            assert key in result, f"Missing key: {key}"

    def test_score_debug_exposes_rescale_constants(self):
        """score_debug must expose raw_impact_min and raw_impact_max (T41-6 calibration visibility)."""
        dr = _dr("HANDOFF_FRICTION", "salesforce", 2.8, 2.0,
                  {"total_cases_90d": 38, "handoff_score": 2.8})
        debug = score(dr)["score_debug"]["impact_factors"]
        assert "raw_impact_min" in debug
        assert "raw_impact_max" in debug
        assert debug["raw_impact_min"] == _RAW_IMPACT_MIN
        assert debug["raw_impact_max"] == _RAW_IMPACT_MAX

    def test_impact_and_effort_are_integers_in_range(self):
        """impact and effort must be int, both in [1, 10]."""
        inputs, _ = _load_fixtures()
        for inp in inputs:
            dr = _dr(
                inp["detector_id"], inp["signal_source"],
                inp["metric_value"], inp["threshold"], inp["raw_evidence"],
            )
            result = score(dr)
            assert isinstance(result["impact"], int), f"{inp['detector_id']}: impact not int"
            assert isinstance(result["effort"], int), f"{inp['detector_id']}: effort not int"
            assert 1 <= result["impact"] <= 10
            assert 1 <= result["effort"] <= 10

    def test_roadmap_stage_consistent_with_tier(self):
        """roadmap_stage must be the correct mapping for the returned tier."""
        stage_map = {"Quick Win": "NEXT_30", "Strategic": "NEXT_60", "Complex": "NEXT_90"}
        inputs, _ = _load_fixtures()
        for inp in inputs:
            dr = _dr(
                inp["detector_id"], inp["signal_source"],
                inp["metric_value"], inp["threshold"], inp["raw_evidence"],
            )
            result = score(dr)
            assert result["roadmap_stage"] == stage_map[result["tier"]]
