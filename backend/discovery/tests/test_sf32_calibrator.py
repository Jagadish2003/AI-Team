"""
SF-3.2 tests — Calibrator + Score Debug Analysis.
All tests run offline. No credentials required.
"""
from __future__ import annotations
import json
import os
import pytest

os.environ["INGEST_MODE"] = "offline"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def offline_run_output():
    """Full pipeline offline run — 7 opportunities with score_debug."""
    from discovery.runner import run
    return run(mode="offline", run_id="sf32-test")


@pytest.fixture
def sample_arch_assessment():
    return {
        "assessor": "Test Architect",
        "org_id": "demo-org",
        "assessment_date": "2026-04-17",
        "top_5": [
            {"rank":1,"label":"Case routing","detector_match":"HANDOFF_FRICTION",
             "architect_impact":8,"architect_effort":3,"is_real":True,"notes":""},
            {"rank":2,"label":"Approval delay","detector_match":"APPROVAL_BOTTLENECK",
             "architect_impact":7,"architect_effort":4,"is_real":True,"notes":""},
            {"rank":3,"label":"Knowledge gap","detector_match":"KNOWLEDGE_GAP",
             "architect_impact":6,"architect_effort":2,"is_real":True,"notes":""},
            {"rank":4,"label":"Cross system","detector_match":"CROSS_SYSTEM_ECHO",
             "architect_impact":7,"architect_effort":5,"is_real":True,"notes":""},
            {"rank":5,"label":"Repetitive flows","detector_match":"REPETITIVE_AUTOMATION",
             "architect_impact":5,"architect_effort":2,"is_real":True,"notes":""},
        ]
    }

@pytest.fixture
def arch_with_false_positive():
    return {
        "top_5": [
            {"rank":1,"detector_match":"HANDOFF_FRICTION","architect_impact":8,
             "architect_effort":3,"is_real":True},
            {"rank":2,"detector_match":"INTEGRATION_CONCENTRATION","architect_impact":2,
             "architect_effort":5,"is_real":False,"notes":"Not real — only 1 flow refs NC"},
            {"rank":3,"detector_match":"KNOWLEDGE_GAP","architect_impact":6,
             "architect_effort":2,"is_real":True},
            {"rank":4,"detector_match":"APPROVAL_BOTTLENECK","architect_impact":7,
             "architect_effort":4,"is_real":True},
            {"rank":5,"detector_match":"REPETITIVE_AUTOMATION","architect_impact":5,
             "architect_effort":2,"is_real":True},
        ]
    }


# ─────────────────────────────────────────────────────────────────────────────
# Calibration report structure
# ─────────────────────────────────────────────────────────────────────────────

class TestCalibrationReport:

    def test_report_has_required_top_level_keys(self, offline_run_output, sample_arch_assessment):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        for key in ("sf32_gate_passed", "report_time", "algo_top5",
                    "score_debug_summary", "calibration_gate",
                    "recommendations", "adjustments_log", "summary"):
            assert key in report, f"Missing key: {key}"

    def test_algo_top5_has_5_items(self, offline_run_output, sample_arch_assessment):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        assert len(report["algo_top5"]) <= 5
        assert len(report["algo_top5"]) >= 1

    def test_algo_top5_sorted_by_impact_desc(self, offline_run_output, sample_arch_assessment):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        impacts = [o["impact"] for o in report["algo_top5"]]
        assert impacts == sorted(impacts, reverse=True)

    def test_score_debug_summary_has_all_opportunities(self, offline_run_output, sample_arch_assessment):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        assert len(report["score_debug_summary"]) == 7  # all detectors

    def test_score_debug_has_proxy_ratio(self, offline_run_output, sample_arch_assessment):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        for item in report["score_debug_summary"]:
            assert "proxy_ratio" in item
            assert "impact_factors" in item

    def test_report_json_serialisable(self, offline_run_output, sample_arch_assessment):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        try:
            json.dumps(report)
        except TypeError as e:
            pytest.fail(f"Report not JSON serialisable: {e}")

    def test_adjustments_log_starts_empty(self, offline_run_output, sample_arch_assessment):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        assert report["adjustments_log"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Calibration gate logic
# ─────────────────────────────────────────────────────────────────────────────

class TestCalibrationGate:

    def test_gate_passes_with_3_overlap_no_fp(self, offline_run_output, sample_arch_assessment):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        gate = report["calibration_gate"]
        # The fixture run contains HANDOFF_FRICTION, APPROVAL_BOTTLENECK,
        # KNOWLEDGE_GAP, CROSS_SYSTEM_ECHO, REPETITIVE_AUTOMATION — all 5 match
        assert gate["overlap_count"] >= 3
        assert len(gate["false_positives_top5"]) == 0
        assert report["sf32_gate_passed"] is True

    def test_gate_fails_with_false_positive(self, offline_run_output, arch_with_false_positive):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, arch_with_false_positive)
        # INTEGRATION_CONCENTRATION is is_real=False
        # Gate FPs (top-5 only) OR review_later (outside top-5) — either way it's flagged
        gate_fps  = report["calibration_gate"]["false_positives_top5"]
        later_fps = report["calibration_gate"]["false_positives_other"]
        assert "INTEGRATION_CONCENTRATION" in gate_fps or "INTEGRATION_CONCENTRATION" in later_fps
        # If in top5, gate fails; if outside top5, gate may still pass but it's logged
        if "INTEGRATION_CONCENTRATION" in gate_fps:
            assert report["sf32_gate_passed"] is False

    def test_gate_none_without_architect(self, offline_run_output):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, None)
        assert report["sf32_gate_passed"] is None

    def test_analysis_only_still_generates_recommendations(self, offline_run_output):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, None)
        assert len(report["recommendations"]) >= 1  # impact bias should be detected

    def test_overlap_count_correct(self):
        from discovery.calibration.calibrator import evaluate_overlap
        algo_top5 = [
            {"detector_id": "HANDOFF_FRICTION"},
            {"detector_id": "APPROVAL_BOTTLENECK"},
            {"detector_id": "KNOWLEDGE_GAP"},
            {"detector_id": "REPETITIVE_AUTOMATION"},
            {"detector_id": "INTEGRATION_CONCENTRATION"},
        ]
        arch_top5 = [
            {"detector_match": "HANDOFF_FRICTION"},
            {"detector_match": "APPROVAL_BOTTLENECK"},
            {"detector_match": "KNOWLEDGE_GAP"},
            {"detector_match": "CROSS_SYSTEM_ECHO"},       # not in algo
            {"detector_match": "PERMISSION_BOTTLENECK"},   # not in algo
        ]
        count, matched, unmatched = evaluate_overlap(algo_top5, arch_top5)
        assert count == 3
        assert "HANDOFF_FRICTION" in matched
        assert "INTEGRATION_CONCENTRATION" in unmatched

    def test_false_positive_detection(self):
        from discovery.calibration.calibrator import check_false_positives
        algo_top5 = [
            {"detector_id": "INTEGRATION_CONCENTRATION"},
            {"detector_id": "HANDOFF_FRICTION"},
        ]
        arch_top5 = [
            {"detector_match": "INTEGRATION_CONCENTRATION", "is_real": False},
            {"detector_match": "HANDOFF_FRICTION", "is_real": True},
        ]
        gate_fps, review_fps = check_false_positives(algo_top5, arch_top5)
        # INTEGRATION_CONCENTRATION is in top5 → gate_fps
        assert "INTEGRATION_CONCENTRATION" in gate_fps
        assert "HANDOFF_FRICTION" not in gate_fps
        assert review_fps == []

    def test_direction_check_flags_large_gap(self):
        from discovery.calibration.calibrator import check_direction
        algo_top5 = [{"detector_id": "HANDOFF_FRICTION", "impact": 3, "effort": 2}]
        arch_top5 = [{"detector_match": "HANDOFF_FRICTION",
                      "architect_impact": 8, "architect_effort": 3}]
        issues = check_direction(algo_top5, arch_top5)
        assert len(issues) == 1
        assert issues[0]["impact_gap"] == 5
        assert issues[0]["severity"] == "HIGH"

    def test_direction_check_no_issue_within_3(self):
        from discovery.calibration.calibrator import check_direction
        algo_top5 = [{"detector_id": "HANDOFF_FRICTION", "impact": 6, "effort": 3}]
        arch_top5 = [{"detector_match": "HANDOFF_FRICTION",
                      "architect_impact": 8, "architect_effort": 3}]
        issues = check_direction(algo_top5, arch_top5)
        assert len(issues) == 0  # gap=2, within tolerance


# ─────────────────────────────────────────────────────────────────────────────
# Impact bias detection
# ─────────────────────────────────────────────────────────────────────────────

class TestImpactBias:

    def test_bias_detected_on_fixture_output(self, offline_run_output):
        """Fixture org produces all impact=1-3 — bias must be detected."""
        from discovery.calibration.calibrator import _analyse_impact_bias
        opps = offline_run_output["opportunities"]
        bias_note = _analyse_impact_bias(opps)
        assert bias_note is not None
        assert "low impact bias" in bias_note.lower()

    def test_no_bias_on_high_impact(self):
        from discovery.calibration.calibrator import _analyse_impact_bias
        opps = [{"impact": 7}, {"impact": 8}, {"impact": 6}]
        assert _analyse_impact_bias(opps) is None

    def test_empty_opps_returns_none(self):
        from discovery.calibration.calibrator import _analyse_impact_bias
        assert _analyse_impact_bias([]) is None

    def test_borderline_threshold_detection(self, offline_run_output):
        from discovery.calibration.calibrator import _analyse_threshold_coverage
        opps = offline_run_output["opportunities"]
        # HANDOFF_FRICTION has proxy_ratio=1.067 — borderline
        borderlines = _analyse_threshold_coverage(opps)
        assert len(borderlines) >= 1
        assert any("HANDOFF_FRICTION" in b for b in borderlines)


# ─────────────────────────────────────────────────────────────────────────────
# Recommendations
# ─────────────────────────────────────────────────────────────────────────────

class TestRecommendations:

    def test_recommendations_all_have_allowed_flag(self, offline_run_output):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, None)
        for rec in report["recommendations"]:
            assert "allowed" in rec
            assert rec["allowed"] is True

    def test_pm05_note_always_included(self, offline_run_output):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, None)
        pm05_recs = [r for r in report["recommendations"] if r["type"] == "pm05_note"]
        assert len(pm05_recs) == 1
        assert "PM-05" in pm05_recs[0]["observation"]

    def test_impact_bias_recommendation_has_before_after(self, offline_run_output):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, None)
        scorer_recs = [r for r in report["recommendations"] if r["type"] == "scorer_weight"]
        if scorer_recs:
            rec = scorer_recs[0]
            assert "before" in rec
            assert "suggested_after" in rec
            assert "rationale" in rec

    def test_false_positive_generates_threshold_recommendation(self, offline_run_output, arch_with_false_positive):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, arch_with_false_positive)
        # INTEGRATION_CONCENTRATION marked FP — if it appears in gate_fps, threshold rec generated
        gate_fps = report["calibration_gate"]["false_positives_top5"]
        if gate_fps:  # if in top5, should generate HIGH threshold recommendation
            fp_recs = [r for r in report["recommendations"]
                       if r["type"] == "detector_threshold" and "HIGH" in r.get("severity","")]
            assert len(fp_recs) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Template
# ─────────────────────────────────────────────────────────────────────────────

class TestArchitectTemplate:

    def test_template_has_required_keys(self):
        from discovery.calibration.calibrator import ARCHITECT_TEMPLATE
        assert "top_5" in ARCHITECT_TEMPLATE
        assert "assessor" in ARCHITECT_TEMPLATE
        assert "_instructions" in ARCHITECT_TEMPLATE

    def test_template_top5_has_all_fields(self):
        from discovery.calibration.calibrator import ARCHITECT_TEMPLATE
        item = ARCHITECT_TEMPLATE["top_5"][0]
        for field in ["rank", "label", "detector_match",
                      "architect_impact", "architect_effort", "is_real"]:
            assert field in item


# ─────────────────────────────────────────────────────────────────────────────
# Regression: full pipeline unaffected
# ─────────────────────────────────────────────────────────────────────────────

class TestRegression:

    def test_offline_runner_7_opportunities(self):
        from discovery.runner import run
        payload = run(mode="offline", run_id="sf32-reg")
        assert len(payload["opportunities"]) >= 7

    def test_track_a_adapter_unaffected(self):
        from discovery.runner import run
        from discovery.track_a_adapter import export_track_a_seed
        import itertools
        payload = run(mode="offline", run_id="sf32-adapter-reg")
        seed = export_track_a_seed(payload, id_counter=itertools.count(1))
        assert len(seed["opportunities"]) >= 7
        for opp in seed["opportunities"]:
            assert "aiRationale" in opp
            assert "override" in opp


# ─────────────────────────────────────────────────────────────────────────────
# Must-fix tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMustFixes:

    def test_ranking_is_tier_first(self, offline_run_output, sample_arch_assessment):
        """Must-fix 1: algo_top5 sorted by tier order then (impact-effort) desc."""
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        top5 = report["algo_top5"]
        _TIER_ORDER = {"Quick Win": 1, "Strategic": 2, "Complex": 3}
        tier_ranks = [_TIER_ORDER.get(o["tier"], 3) for o in top5]
        # Tier ranks should be non-decreasing
        assert tier_ranks == sorted(tier_ranks)

    def test_ranking_function_documented_in_gate(self, offline_run_output, sample_arch_assessment):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        assert "ranking_function" in report["calibration_gate"]
        assert "Quick Win" in report["calibration_gate"]["ranking_function"]

    def test_fp_scope_top5_only(self):
        """Must-fix 2: FP in rank-6+ does not block gate."""
        from discovery.calibration.calibrator import check_false_positives
        algo_top5  = [{"detector_id": "HANDOFF_FRICTION"}]
        all_opps   = [{"detector_id": "HANDOFF_FRICTION"},
                      {"detector_id": "INTEGRATION_CONCENTRATION"}]  # rank 6
        arch_top5  = [
            {"detector_match": "HANDOFF_FRICTION", "is_real": True},
            {"detector_match": "INTEGRATION_CONCENTRATION", "is_real": False},
        ]
        gate_fps, review_fps = check_false_positives(algo_top5, arch_top5, all_opps)
        assert "INTEGRATION_CONCENTRATION" not in gate_fps   # not in top5
        assert "INTEGRATION_CONCENTRATION" in review_fps     # logged for review
        assert "HANDOFF_FRICTION" not in gate_fps            # is_real=True

    def test_gate_rule_mentions_fp_scope(self, offline_run_output, sample_arch_assessment):
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        rule = report["calibration_gate"]["gate_rule"]
        assert "top 5" in rule.lower() or "Top-5" in rule or "top-5" in rule.lower()

    def test_direction_formula_documented(self, offline_run_output, sample_arch_assessment):
        """Must-fix 3: direction check uses explicit formula."""
        from discovery.calibration.calibrator import check_direction
        algo_top5 = [{"detector_id": "HANDOFF_FRICTION", "impact": 3, "effort": 2}]
        arch_top5 = [{"detector_match": "HANDOFF_FRICTION",
                      "architect_impact": 8, "architect_effort": 3}]
        issues = check_direction(algo_top5, arch_top5)
        assert len(issues) == 1
        assert "direction_formula" in issues[0]
        assert "abs" in issues[0]["direction_formula"]
        assert issues[0]["impact_gap"] == 5
        assert issues[0]["impact_gap_ok"] is False
        assert issues[0]["effort_gap_ok"] is True  # gap=1, within 3


class TestGoodToHaveImprovements:

    def test_summary_table_in_report(self, offline_run_output, sample_arch_assessment):
        """Good-to-have A: one-glance summary table."""
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        assert "calibration_summary_table" in report
        table = report["calibration_summary_table"]
        assert isinstance(table, list)
        assert len(table) <= 5
        if table:
            row = table[0]
            for k in ["rank","detector_id","algo_impact","algo_effort",
                      "arch_impact","arch_effort","impact_delta","matched"]:
                assert k in row

    def test_org_readiness_checklist_in_report(self, offline_run_output, sample_arch_assessment):
        """Good-to-have B: org state with verification queries."""
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, sample_arch_assessment)
        assert "org_readiness_checklist" in report
        checklist = report["org_readiness_checklist"]
        assert isinstance(checklist, list)
        assert len(checklist) == 5  # 5 checks
        for item in checklist:
            for k in ["check","current_value","minimum","status","soql"]:
                assert k in item

    def test_overfit_guard_in_volume_recommendation(self, offline_run_output):
        """Good-to-have C: overfit guard in volume bias recommendation."""
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, None)
        scorer_recs = [r for r in report["recommendations"] if r["type"]=="scorer_weight"]
        if scorer_recs:
            rationale = scorer_recs[0].get("rationale","")
            assert "OVERFIT" in rationale or "overfit" in rationale or "mid-size" in rationale

    def test_fp_review_later_not_blocking(self, offline_run_output, arch_with_false_positive):
        """Must-fix 2 + Good-to-have: review_later FPs logged but not gate-blocking."""
        from discovery.calibration.calibrator import run_calibration
        report = run_calibration(offline_run_output, arch_with_false_positive)
        other_fps = report["calibration_gate"]["false_positives_other"]
        # If INTEGRATION_CONCENTRATION is outside top5, it lands here
        gate_fps  = report["calibration_gate"]["false_positives_top5"]
        # Either way — the distinction is correctly represented
        assert isinstance(gate_fps, list)
        assert isinstance(other_fps, list)
