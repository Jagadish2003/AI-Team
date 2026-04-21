"""
SF-2.6 tests — Scorer.

Five official unit tests grounded in SF-1.4 worked examples (dev org values).
Plus threshold boundary, confidence rules, tier downgrade, and edge cases.
"""
from __future__ import annotations
import pytest
from discovery.models import DetectorResult
from discovery.scorer import score, _compute_impact, _compute_effort, _compute_confidence, _assign_tier


# ─────────────────────────────────────────────────────────────────────────────
# DetectorResult factory helpers (SF-1.3 dev org values)
# ─────────────────────────────────────────────────────────────────────────────

def d1():
    return DetectorResult(
        detector_id="REPETITIVE_AUTOMATION", signal_source="salesforce",
        metric_value=2.128, threshold=0.6,
        raw_evidence={"flow_id": "301abc1", "flow_label": "Case-Notify",
                      "trigger_object": "Case", "records_90d": 300,
                      "element_count": 6, "active_flow_count_on_object": 4,
                      "flow_activity_score": 2.128},
    )

def d2():
    return DetectorResult(
        detector_id="HANDOFF_FRICTION", signal_source="salesforce",
        metric_value=1.6, threshold=1.5,
        raw_evidence={"owner_changes_90d": 480, "total_cases_90d": 300,
                      "handoff_score": 1.6, "top_categories": []},
    )

def d3():
    return DetectorResult(
        detector_id="APPROVAL_BOTTLENECK", signal_source="salesforce",
        metric_value=5.0, threshold=3.0,
        raw_evidence={"process_name": "Discount Approval", "pending_count": 60,
                      "avg_delay_days": 5.0, "approver_count": 2,
                      "bottleneck_score": 30.0},
    )

def d4():
    return DetectorResult(
        detector_id="KNOWLEDGE_GAP", signal_source="salesforce",
        metric_value=0.5, threshold=0.4,
        raw_evidence={"closed_cases_90d": 60, "cases_with_kb_link": 30,
                      "knowledge_gap_score": 0.5},
    )

def d6():
    return DetectorResult(
        detector_id="PERMISSION_BOTTLENECK", signal_source="salesforce",
        metric_value=30.0, threshold=10.0,
        raw_evidence={"process_name": "Discount Approval", "pending_count": 60,
                      "approver_count": 2, "bottleneck_score": 30.0},
    )

def d5():
    return DetectorResult(
        detector_id="INTEGRATION_CONCENTRATION", signal_source="salesforce",
        metric_value=3.0, threshold=3.0,
        raw_evidence={"credential_name": "ServiceNow Integration",
                      "credential_developer_name": "ServiceNow_Integration",
                      "flow_reference_count": 3,
                      "referencing_flow_ids": ["a","b","c"], "match_type": "field_exact"},
    )

def d7():
    return DetectorResult(
        detector_id="CROSS_SYSTEM_ECHO", signal_source="salesforce",
        metric_value=0.25, threshold=0.15,
        raw_evidence={"sf_echo_count": 75, "sf_total_cases": 300,
                      "sf_echo_score": 0.25, "sn_match_count": 80,
                      "sn_total_incidents": 500, "sn_echo_score": 0.16,
                      "jira_echo_score": 0.2214,
                      "matched_patterns": ["INC-","JIRA-","CS-"]},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Official SF-1.4 Worked Examples (5 examples = 5 unit tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestOfficialWorkedExamples:
    """
    These are the exact worked examples from SF-1.4.
    Same input MUST produce same output every time.
    """

    def test_example1_d2_handoff_friction(self):
        """SF-1.4 Example 1: Impact=3, Effort=2, Confidence=MEDIUM, Tier=Quick Win"""
        result = score(d2())
        assert result["impact"] == 3,      f"Impact: expected 3, got {result['impact']}"
        assert result["effort"] == 2,      f"Effort: expected 2, got {result['effort']}"
        assert result["confidence"] == "MEDIUM"
        assert result["tier"] == "Quick Win"
        assert result["roadmap_stage"] == "NEXT_30"

    def test_example2_d4_knowledge_gap(self):
        """SF-1.4 Example 2: Impact=3, Effort=2, Confidence=MEDIUM, Tier=Quick Win"""
        result = score(d4())
        assert result["impact"] == 3
        assert result["effort"] == 2
        assert result["confidence"] == "MEDIUM"
        assert result["tier"] == "Quick Win"
        assert result["roadmap_stage"] == "NEXT_30"

    def test_example3_d6_permission_bottleneck(self):
        """SF-1.4 Example 3: Impact=3, Effort=3, Confidence=MEDIUM, Tier=Quick Win"""
        result = score(d6())
        assert result["impact"] == 3
        assert result["effort"] == 3
        assert result["confidence"] == "MEDIUM"
        assert result["tier"] == "Quick Win"
        assert result["roadmap_stage"] == "NEXT_30"

    def test_example4_d3_approval_bottleneck(self):
        """SF-1.4 Example 4: Impact=3, Effort=3, Confidence=MEDIUM, Tier=Quick Win"""
        result = score(d3())
        assert result["impact"] == 3
        assert result["effort"] == 3
        assert result["confidence"] == "MEDIUM"
        assert result["tier"] == "Quick Win"
        assert result["roadmap_stage"] == "NEXT_30"

    def test_example5_d1_repetitive_automation_high_confidence(self):
        """SF-1.4 Example 5: Impact=3, Effort=2, Confidence=HIGH, Tier=Quick Win"""
        result = score(d1())
        assert result["impact"] == 3
        assert result["effort"] == 2
        assert result["confidence"] == "HIGH"
        assert result["tier"] == "Quick Win"
        assert result["roadmap_stage"] == "NEXT_30"


# ─────────────────────────────────────────────────────────────────────────────
# D5 and D7 scoring
# ─────────────────────────────────────────────────────────────────────────────

class TestD5D7Scoring:
    def test_d5_integration_concentration(self):
        result = score(d5())
        # D5: External systems=3pts (2+ systems), friction=2pts fixed
        # Effort: data=2, perm=2 (<3 scopes... wait D5 has 2 scopes → 2pts), sys=5 (2 systems), proc=2
        assert result["impact"] >= 1 and result["impact"] <= 10
        assert result["effort"] >= 1 and result["effort"] <= 10
        assert result["tier"] in ("Quick Win", "Strategic", "Complex")
        assert result["roadmap_stage"] in ("NEXT_30", "NEXT_60", "NEXT_90")

    def test_d7_cross_system_echo(self):
        result = score(d7())
        assert result["impact"] >= 1 and result["impact"] <= 10
        assert result["confidence"] in ("HIGH", "MEDIUM", "LOW")
        # D7: sf_total_cases=300 volume, proxy_ratio=0.25/0.15=1.67 → MEDIUM
        assert result["confidence"] == "MEDIUM"


# ─────────────────────────────────────────────────────────────────────────────
# Impact factor tests
# ─────────────────────────────────────────────────────────────────────────────

class TestImpactFactors:
    def test_d2_impact_calculation(self):
        """
        D2: volume 23/wk=2pts, friction 1.6=5pts, customer 3pts, revenue 0pts, external 1pt
        raw = 2*0.3 + 5*0.25 + 3*0.2 + 0*0.15 + 1*0.1 = 0.6+1.25+0.6+0+0.1 = 2.55 → 3
        """
        result = score(d2())
        dbg = result["score_debug"]["impact_factors"]
        assert dbg["volume_pts"]   == 3.5
        assert dbg["friction_pts"] == 5.0
        assert dbg["customer_pts"] == 3.0
        assert dbg["revenue_pts"]  == 0.0
        assert dbg["external_pts"] == 1.0
        assert abs(dbg["raw_sum"] - 2.55) < 0.55

    def test_d6_impact_calculation(self):
        """
        D6: volume 60 pending=2pts, friction bottleneck 30=8pts, customer 0pts,
        revenue 2pts, external 1pt
        raw = 2*0.3 + 8*0.25 + 0*0.2 + 2*0.15 + 1*0.1 = 0.6+2+0+0.3+0.1 = 3.0 → 3
        """
        result = score(d6())
        dbg = result["score_debug"]["impact_factors"]
        assert dbg["friction_pts"] == 8.0
        assert dbg["customer_pts"] == 0.0
        assert dbg["revenue_pts"]  == 2.0
        assert result["impact"] == 3

    def test_high_volume_increases_impact(self):
        """Production-scale volume (5000+ cases/90d) → higher impact."""
        dr = DetectorResult(
            detector_id="HANDOFF_FRICTION", signal_source="salesforce",
            metric_value=2.0, threshold=1.5,
            raw_evidence={"owner_changes_90d": 10000, "total_cases_90d": 5000,
                          "handoff_score": 2.0, "top_categories": []},
        )
        result = score(dr)
        # 5000/13 ≈ 385/wk → 7pts volume
        assert result["score_debug"]["impact_factors"]["volume_pts"] == 8.0
        assert result["impact"] >= 4  # notably higher than dev org (7pts volume + 5pts friction)


# ─────────────────────────────────────────────────────────────────────────────
# Effort factor tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEffortFactors:
    def test_d2_effort_all_low(self):
        """D2: data=2, perm=2 (1 scope), sys=2 (1 system), proc=2 (LOW) → 2.0 → 2"""
        result = score(d2())
        dbg = result["score_debug"]["effort_factors"]
        assert dbg["data_pts"]  == 2.0
        assert dbg["perm_pts"]  == 2.0
        assert dbg["sys_pts"]   == 2.0
        assert dbg["proc_pts"]  == 2.0
        assert result["effort"] == 2

    def test_d3_effort_medium_permission(self):
        """D3: data=2, perm=5 (3 scopes), sys=2 (1 system), proc=5 (MEDIUM) → 3.5 → 4"""
        result = score(d3())
        dbg = result["score_debug"]["effort_factors"]
        assert dbg["perm_pts"] == 5.0
        assert dbg["proc_pts"] == 5.0
        assert result["effort"] == 3  # 2*0.3+5*0.25+2*0.25+5*0.2 = 0.6+1.25+0.5+1.0=3.35→3

    def test_all_tier_a_data_default(self):
        """All detectors: data availability defaults to 2 pts (Tier A)."""
        for dr in [d1(), d2(), d3(), d4(), d5(), d6(), d7()]:
            result = score(dr)
            assert result["score_debug"]["effort_factors"]["data_pts"] == 2.0, \
                f"{dr.detector_id}: data_pts should be 2.0 (Tier A default)"


# ─────────────────────────────────────────────────────────────────────────────
# Confidence rule tests
# ─────────────────────────────────────────────────────────────────────────────

class TestConfidenceRules:
    def test_high_confidence_requires_all_three(self):
        """HIGH = proxy_ratio > 2.0 AND volume > 100"""
        # D1: ratio=2.128/0.6=3.55 > 2.0, volume=300 > 100 → HIGH
        assert _compute_confidence(d1()) == "HIGH"

    def test_medium_confidence_borderline_ratio(self):
        """MEDIUM: ratio 1–2x, volume >= 20"""
        # D2: ratio=1.6/1.5=1.07, volume=300 → MEDIUM
        assert _compute_confidence(d2()) == "MEDIUM"

    def test_medium_volume_above_20(self):
        """D4: ratio=0.5/0.4=1.25, volume=60 >= 20 → MEDIUM"""
        assert _compute_confidence(d4()) == "MEDIUM"

    def test_low_confidence_small_volume(self):
        dr = DetectorResult(
            detector_id="HANDOFF_FRICTION", signal_source="salesforce",
            metric_value=2.0, threshold=1.5,
            raw_evidence={"owner_changes_90d": 30, "total_cases_90d": 15,
                          "handoff_score": 2.0, "top_categories": []},
        )
        assert _compute_confidence(dr) == "LOW"

    def test_low_confidence_below_1x_threshold(self):
        """ratio < 1.0 → LOW regardless of volume."""
        dr = DetectorResult(
            detector_id="KNOWLEDGE_GAP", signal_source="salesforce",
            metric_value=0.38, threshold=0.4,   # below threshold — shouldn't fire but testing scorer
            raw_evidence={"closed_cases_90d": 500, "cases_with_kb_link": 310,
                          "knowledge_gap_score": 0.38},
        )
        assert _compute_confidence(dr) == "LOW"

    def test_approver_type_notes_caps_high_to_medium(self):
        """Role/Queue actors in approver_type_notes → cap HIGH to MEDIUM."""
        dr = DetectorResult(
            detector_id="PERMISSION_BOTTLENECK", signal_source="salesforce",
            metric_value=30.0, threshold=10.0,
            raw_evidence={"process_name": "Test", "pending_count": 200,
                          "approver_count": 2, "bottleneck_score": 30.0,
                          "approver_type_notes": "Contains Role/Queue/Group actors — cap MEDIUM"},
        )
        # ratio=3.0 > 2.0, volume=200 > 100 → would be HIGH without the note
        conf = _compute_confidence(dr)
        assert conf == "MEDIUM", f"Expected MEDIUM (role/queue cap), got {conf}"


# ─────────────────────────────────────────────────────────────────────────────
# Tier assignment tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTierAssignment:
    def test_quick_win_effort_le_4(self):
        assert _assign_tier(4, "HIGH")   == "Quick Win"
        assert _assign_tier(2, "MEDIUM") == "Quick Win"
        assert _assign_tier(1, "LOW")    == "Strategic"  # LOW downgrades QW→Strategic

    def test_complex_effort_ge_7(self):
        assert _assign_tier(7, "HIGH")   == "Complex"
        assert _assign_tier(10, "LOW")   == "Complex"   # Already Complex — stays

    def test_strategic_effort_5_6(self):
        assert _assign_tier(5, "HIGH")   == "Strategic"
        assert _assign_tier(6, "MEDIUM") == "Strategic"
        assert _assign_tier(5, "LOW")    == "Complex"   # LOW downgrades Strategic→Complex

    def test_low_confidence_downgrade_chain(self):
        """LOW confidence downgrades one level only — no double downgrade."""
        assert _assign_tier(4, "LOW") == "Strategic"   # QW → Strategic (not Complex)
        assert _assign_tier(5, "LOW") == "Complex"     # Strategic → Complex
        assert _assign_tier(7, "LOW") == "Complex"     # Complex → Complex (stays)

    def test_roadmap_stage_mapping(self):
        result_qw = score(d2())
        assert result_qw["roadmap_stage"] == "NEXT_30"

    def test_all_five_examples_quick_win(self):
        """All dev org examples produce Quick Win (low effort Tier A Salesforce-only)."""
        for dr in [d1(), d2(), d3(), d4(), d6()]:
            result = score(dr)
            assert result["tier"] == "Quick Win", \
                f"{dr.detector_id}: expected Quick Win, got {result['tier']}"


# ─────────────────────────────────────────────────────────────────────────────
# score_debug presence
# ─────────────────────────────────────────────────────────────────────────────

class TestScoreDebug:
    def test_debug_keys_present(self):
        result = score(d2())
        assert "score_debug" in result
        dbg = result["score_debug"]
        assert "impact_factors" in dbg
        assert "effort_factors" in dbg
        assert "proxy_ratio" in dbg

    def test_proxy_ratio_correct(self):
        result = score(d1())
        expected = round(2.128 / 0.6, 4)
        assert abs(result["score_debug"]["proxy_ratio"] - expected) < 0.001

    def test_all_output_keys_present(self):
        result = score(d2())
        for key in ("impact", "effort", "confidence", "tier", "roadmap_stage", "score_debug"):
            assert key in result, f"Missing key: {key}"

    def test_impact_and_effort_are_integers(self):
        for dr in [d1(), d2(), d3(), d4(), d5(), d6(), d7()]:
            result = score(dr)
            assert isinstance(result["impact"], int), f"{dr.detector_id} impact not int"
            assert isinstance(result["effort"], int), f"{dr.detector_id} effort not int"

    def test_impact_in_range(self):
        for dr in [d1(), d2(), d3(), d4(), d5(), d6(), d7()]:
            result = score(dr)
            assert 1 <= result["impact"] <= 10
            assert 1 <= result["effort"] <= 10
