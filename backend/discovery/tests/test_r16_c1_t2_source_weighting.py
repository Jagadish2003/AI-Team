"""
test_r16_c1_t2_source_weighting.py

R16-C1 T2 — Tests: deterministic ROLE_WEIGHT and bounded priority modulation
applied to each source's evidence contribution per Section 2 of the spec.

Acceptance criteria covered:
  AC2 - Changing a system's role (e.g. System of Record → Supporting) and
        re-running discovery on identical data produces visibly different
        scores or rankings in the expected direction.
  AC3 - Changing a system's priority produces a bounded, expected shift
        in emphasis.
  AC4 - Weighting never breaches a hard rule: a Supporting-only signal
        cannot reach HIGH regardless of priority. (Full ceiling clamp is T3;
        this test verifies the weight alone pushes supporting away from HIGH.)
  AC6 - Two runs with identical configuration and data produce identical
        results — the weighting is deterministic.

Additional unit tests cover:
  - ROLE_WEIGHT table values match spec
  - PRIORITY_NUDGE table values match spec
  - compute_source_weight clamps output to [WEIGHT_MIN, WEIGHT_MAX]
  - source_weight property on SystemWeighting
  - get_source_weight on StackBuilderWeightingContext
  - score_debug exposes weighted_raw, effective_proxy_ratio, source_weight
"""
from __future__ import annotations

import pytest
from discovery.weighting_context import (
    ROLE_WEIGHT,
    PRIORITY_NUDGE,
    WEIGHT_MIN,
    WEIGHT_MAX,
    WEIGHT_NEUTRAL,
    compute_source_weight,
    SystemWeighting,
    StackBuilderWeightingContext,
    ROLE_PRIMARY,
    ROLE_SUPPORTING,
    ROLE_SUPPLEMENTARY,
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
    PRIORITY_LOW,
)
from discovery.scorer import score, _compute_impact, _compute_confidence
from discovery.models import DetectorResult


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _handoff_dr(signal_source: str = "salesforce") -> DetectorResult:
    """HANDOFF_FRICTION detector — high volume, high proxy_ratio → normally HIGH."""
    return DetectorResult(
        detector_id="HANDOFF_FRICTION",
        signal_source=signal_source,
        metric_value=3.5,
        threshold=2.0,
        raw_evidence={"total_cases_90d": 800, "handoff_score": 4.0},
    )


def _approval_dr(signal_source: str = "salesforce") -> DetectorResult:
    """APPROVAL_BOTTLENECK — moderate signal."""
    return DetectorResult(
        detector_id="APPROVAL_BOTTLENECK",
        signal_source=signal_source,
        metric_value=2.5,
        threshold=2.0,
        raw_evidence={"pending_count": 150, "avg_delay_days": 4.0},
    )


def _ctx_with(
    system_id: str,
    role: str,
    priority: str,
) -> StackBuilderWeightingContext:
    """Build a minimal weighting context with a single configured system."""
    w = SystemWeighting(
        system_id=system_id,
        role=role,
        priority=priority,
        confirmed=True,
    )
    return StackBuilderWeightingContext(
        weightings={system_id: w},
        selected_system_ids=[system_id],
        pack_id="service_cloud",
        run_id="run_test",
        is_neutral=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ROLE_WEIGHT table tests (spec compliance)
# ─────────────────────────────────────────────────────────────────────────────

class TestRoleWeightTable:

    def test_system_of_record_is_1_0(self):
        assert ROLE_WEIGHT["system_of_record"] == 1.0

    def test_workflow_system_is_0_8(self):
        assert ROLE_WEIGHT["workflow_system"] == 0.8

    def test_operational_signal_source_is_0_6(self):
        assert ROLE_WEIGHT["operational_signal_source"] == 0.6

    def test_supporting_is_0_6(self):
        assert ROLE_WEIGHT["supporting"] == 0.6

    def test_supplementary_is_0_6(self):
        assert ROLE_WEIGHT["supplementary"] == 0.6

    def test_no_role_in_table_returns_neutral_weight(self):
        result = compute_source_weight("unknown_role", "secondary")
        assert result == WEIGHT_NEUTRAL


# ─────────────────────────────────────────────────────────────────────────────
# PRIORITY_NUDGE table tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPriorityNudgeTable:

    def test_primary_nudge_is_1_1(self):
        assert PRIORITY_NUDGE["primary"] == 1.10

    def test_secondary_nudge_is_1_0(self):
        assert PRIORITY_NUDGE["secondary"] == 1.00

    def test_tertiary_nudge_is_0_9(self):
        assert PRIORITY_NUDGE["tertiary"] == 0.90

    def test_no_priority_returns_neutral_nudge(self):
        result = compute_source_weight("system_of_record", "unknown_priority")
        assert result == 1.0  # role_w=1.0 × default_nudge=1.0


# ─────────────────────────────────────────────────────────────────────────────
# compute_source_weight — deterministic and bounded
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeSourceWeight:

    def test_system_of_record_secondary_equals_1_0(self):
        assert compute_source_weight("system_of_record", "secondary") == pytest.approx(1.0)

    def test_system_of_record_primary_equals_1_1(self):
        assert compute_source_weight("system_of_record", "primary") == pytest.approx(1.1)

    def test_system_of_record_tertiary(self):
        # 1.0 × 0.9 = 0.90
        assert compute_source_weight("system_of_record", "tertiary") == pytest.approx(0.9)

    def test_supporting_secondary(self):
        # 0.6 × 1.0 = 0.60
        assert compute_source_weight("operational_signal_source", "secondary") == pytest.approx(0.6)

    def test_supporting_primary(self):
        # 0.6 × 1.1 = 0.66
        assert compute_source_weight("operational_signal_source", "primary") == pytest.approx(0.66)

    def test_supporting_tertiary(self):
        # 0.6 × 0.9 = 0.54
        assert compute_source_weight("operational_signal_source", "tertiary") == pytest.approx(0.54)

    def test_workflow_system_secondary(self):
        # 0.8 × 1.0 = 0.80
        assert compute_source_weight("workflow_system", "secondary") == pytest.approx(0.8)

    def test_clamp_floor_at_weight_min(self):
        # artificially low scenario: even if raw < WEIGHT_MIN, result >= WEIGHT_MIN
        result = compute_source_weight("supporting", "tertiary")
        assert result >= WEIGHT_MIN

    def test_clamp_ceiling_at_weight_max(self):
        result = compute_source_weight("system_of_record", "primary")
        assert result <= WEIGHT_MAX

    def test_all_valid_combinations_in_range(self):
        for role in ["system_of_record", "workflow_system", "operational_signal_source",
                     "supporting", "supplementary"]:
            for priority in ["primary", "secondary", "tertiary"]:
                w = compute_source_weight(role, priority)
                assert WEIGHT_MIN <= w <= WEIGHT_MAX, \
                    f"Out of range: role={role}, priority={priority}, weight={w}"

    def test_deterministic_same_inputs_same_output(self):
        """AC6: identical inputs always produce identical output."""
        w1 = compute_source_weight("operational_signal_source", "primary")
        w2 = compute_source_weight("operational_signal_source", "primary")
        assert w1 == w2


# ─────────────────────────────────────────────────────────────────────────────
# SystemWeighting.source_weight property
# ─────────────────────────────────────────────────────────────────────────────

class TestSystemWeightingSourceWeight:

    def test_system_of_record_primary(self):
        w = SystemWeighting(system_id="sf", role=ROLE_PRIMARY, priority=PRIORITY_HIGH)
        assert w.source_weight == pytest.approx(1.1)

    def test_supporting_secondary(self):
        w = SystemWeighting(system_id="sn", role=ROLE_SUPPORTING, priority=PRIORITY_MEDIUM)
        assert w.source_weight == pytest.approx(0.6)

    def test_neutral_system_weighting_returns_1_0(self):
        w = SystemWeighting(system_id="unknown")
        assert w.source_weight == WEIGHT_NEUTRAL

    def test_supplementary_tertiary(self):
        w = SystemWeighting(system_id="slack", role=ROLE_SUPPLEMENTARY, priority=PRIORITY_LOW)
        assert w.source_weight == pytest.approx(0.54)


# ─────────────────────────────────────────────────────────────────────────────
# StackBuilderWeightingContext.get_source_weight
# ─────────────────────────────────────────────────────────────────────────────

class TestContextGetSourceWeight:

    def test_neutral_context_returns_1_0(self):
        ctx = StackBuilderWeightingContext.neutral()
        assert ctx.get_source_weight("salesforce") == WEIGHT_NEUTRAL

    def test_configured_system_returns_correct_weight(self):
        ctx = _ctx_with("salesforce", ROLE_PRIMARY, PRIORITY_HIGH)
        assert ctx.get_source_weight("salesforce") == pytest.approx(1.1)

    def test_unconfigured_system_returns_neutral(self):
        ctx = _ctx_with("salesforce", ROLE_PRIMARY, PRIORITY_HIGH)
        assert ctx.get_source_weight("servicenow") == WEIGHT_NEUTRAL

    def test_supporting_system_weight(self):
        ctx = _ctx_with("servicenow", ROLE_SUPPORTING, PRIORITY_MEDIUM)
        assert ctx.get_source_weight("servicenow") == pytest.approx(0.6)


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — Changing role produces visible score shift in expected direction
# ─────────────────────────────────────────────────────────────────────────────

class TestAC2RoleChangesScore:

    def test_system_of_record_impact_higher_than_supporting(self):
        dr = _handoff_dr(signal_source="salesforce")
        ctx_sor = _ctx_with("salesforce", ROLE_PRIMARY, PRIORITY_MEDIUM)
        ctx_sup = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_MEDIUM)

        result_sor = score(dr, weighting_context=ctx_sor)
        result_sup = score(dr, weighting_context=ctx_sup)

        assert result_sor["impact"] >= result_sup["impact"], (
            f"system_of_record impact ({result_sor['impact']}) should be >= "
            f"supporting impact ({result_sup['impact']})"
        )

    def test_system_of_record_impact_strictly_higher_for_strong_signal(self):
        """A strong signal should show a strict difference across role bands."""
        dr = _handoff_dr(signal_source="salesforce")
        ctx_sor = _ctx_with("salesforce", "system_of_record", PRIORITY_MEDIUM)
        ctx_sup = _ctx_with("salesforce", "operational_signal_source", PRIORITY_MEDIUM)

        sor = score(dr, weighting_context=ctx_sor)
        sup = score(dr, weighting_context=ctx_sup)

        # System of record weight=1.0 vs supporting weight=0.6 → visible drop
        assert sor["impact"] > sup["impact"], (
            f"Expected strict: sor={sor['impact']} > sup={sup['impact']}"
        )

    def test_workflow_system_impact_between_sor_and_supporting(self):
        dr = _handoff_dr(signal_source="salesforce")
        ctx_sor  = _ctx_with("salesforce", "system_of_record", PRIORITY_MEDIUM)
        ctx_wfs  = _ctx_with("salesforce", "workflow_system", PRIORITY_MEDIUM)
        ctx_sup  = _ctx_with("salesforce", "operational_signal_source", PRIORITY_MEDIUM)

        sor_impact = score(dr, weighting_context=ctx_sor)["impact"]
        wfs_impact = score(dr, weighting_context=ctx_wfs)["impact"]
        sup_impact = score(dr, weighting_context=ctx_sup)["impact"]

        # SoR ≥ WFS ≥ Supporting
        assert sor_impact >= wfs_impact >= sup_impact, (
            f"Expected sor={sor_impact} >= wfs={wfs_impact} >= sup={sup_impact}"
        )

    def test_no_context_equals_system_of_record_secondary(self):
        """Neutral/no-context should equal the weight-1.0 configuration."""
        dr = _handoff_dr()
        ctx_neutral = None
        ctx_sor_sec = _ctx_with("salesforce", "system_of_record", PRIORITY_MEDIUM)

        result_neutral = score(dr, weighting_context=ctx_neutral)
        result_sor     = score(dr, weighting_context=ctx_sor_sec)

        assert result_neutral["impact"] == result_sor["impact"]
        assert result_neutral["confidence"] == result_sor["confidence"]

    def test_changing_role_changes_weighted_raw_in_debug(self):
        dr = _handoff_dr()
        sor = score(dr, weighting_context=_ctx_with("salesforce", "system_of_record", PRIORITY_MEDIUM))
        sup = score(dr, weighting_context=_ctx_with("salesforce", "operational_signal_source", PRIORITY_MEDIUM))

        sor_wr = sor["score_debug"]["impact_factors"]["weighted_raw"]
        sup_wr = sup["score_debug"]["impact_factors"]["weighted_raw"]
        assert sor_wr > sup_wr


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — Priority produces bounded shift in expected direction
# ─────────────────────────────────────────────────────────────────────────────

class TestAC3PriorityBoundedNudge:

    def test_primary_priority_higher_impact_than_secondary(self):
        dr = _handoff_dr()
        ctx_pri = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_HIGH)
        ctx_sec = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_MEDIUM)

        pri = score(dr, weighting_context=ctx_pri)["impact"]
        sec = score(dr, weighting_context=ctx_sec)["impact"]
        assert pri >= sec

    def test_tertiary_priority_lower_impact_than_secondary(self):
        dr = _handoff_dr()
        ctx_sec = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_MEDIUM)
        ctx_ter = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_LOW)

        sec = score(dr, weighting_context=ctx_sec)["impact"]
        ter = score(dr, weighting_context=ctx_ter)["impact"]
        assert sec >= ter

    def test_priority_nudge_bounded_below_weight_max(self):
        """A high-priority system_of_record cannot exceed WEIGHT_MAX."""
        w = compute_source_weight("system_of_record", "primary")
        assert w <= WEIGHT_MAX

    def test_priority_nudge_bounded_above_weight_min(self):
        """A low-priority supporting system cannot fall below WEIGHT_MIN."""
        w = compute_source_weight("supplementary", "tertiary")
        assert w >= WEIGHT_MIN

    def test_primary_supporting_weight_less_than_secondary_sor(self):
        """Priority cannot let a supporting system outweigh a system-of-record."""
        w_supporting_primary = compute_source_weight("operational_signal_source", "primary")
        w_sor_secondary      = compute_source_weight("system_of_record", "secondary")
        assert w_supporting_primary < w_sor_secondary, (
            f"supporting+primary ({w_supporting_primary}) should be < "
            f"sor+secondary ({w_sor_secondary})"
        )

    def test_priority_shift_visible_in_effective_proxy_ratio(self):
        dr = _approval_dr()
        ctx_pri = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_HIGH)
        ctx_ter = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_LOW)

        pri_epr = score(dr, weighting_context=ctx_pri)["score_debug"]["effective_proxy_ratio"]
        ter_epr = score(dr, weighting_context=ctx_ter)["score_debug"]["effective_proxy_ratio"]
        assert pri_epr > ter_epr


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — Supporting system cannot be boosted to HIGH by priority alone
# ─────────────────────────────────────────────────────────────────────────────

class TestAC4SupportingCannotReachHighViaWeight:

    def _strong_dr(self, signal_source: str = "servicenow") -> DetectorResult:
        """Near-borderline HIGH signal: proxy_ratio=2.1, volume=150."""
        return DetectorResult(
            detector_id="HANDOFF_FRICTION",
            signal_source=signal_source,
            metric_value=4.21,   # 4.21 / 2.0 = 2.105 proxy_ratio
            threshold=2.0,
            raw_evidence={"total_cases_90d": 150, "handoff_score": 4.0},
        )

    def test_system_of_record_secondary_can_reach_high(self):
        """Baseline: sor+secondary should score HIGH on a strong signal."""
        dr = self._strong_dr(signal_source="salesforce")
        ctx = _ctx_with("salesforce", "system_of_record", "secondary")
        result = score(dr, weighting_context=ctx)
        # weight=1.0 → effective_proxy_ratio=2.105 → HIGH
        assert result["confidence"] == "HIGH"

    def test_supporting_primary_with_borderline_signal_does_not_reach_high(self):
        """Supporting+primary weight=0.66; effective_proxy_ratio = 2.105×0.66=1.39 → MEDIUM."""
        dr = self._strong_dr(signal_source="servicenow")
        ctx = _ctx_with("servicenow", "operational_signal_source", "primary")
        result = score(dr, weighting_context=ctx)
        # effective_proxy_ratio ≈ 1.39 < 2.0 → cannot reach HIGH
        assert result["confidence"] in ("MEDIUM", "LOW"), (
            f"Supporting+primary should not reach HIGH, got {result['confidence']}"
        )

    def test_supporting_primary_effective_proxy_below_2(self):
        dr = self._strong_dr()
        ctx = _ctx_with("servicenow", "operational_signal_source", "primary")
        epr = score(dr, weighting_context=ctx)["score_debug"]["effective_proxy_ratio"]
        assert epr < 2.0, f"effective_proxy_ratio={epr} should be < 2.0 for supporting"

    def test_supplementary_tertiary_stays_low_or_medium(self):
        dr = self._strong_dr()
        ctx = _ctx_with("servicenow", ROLE_SUPPLEMENTARY, PRIORITY_LOW)
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] in ("LOW", "MEDIUM")


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — Determinism: same inputs always produce same output
# ─────────────────────────────────────────────────────────────────────────────

class TestAC6Determinism:

    def test_same_role_priority_same_weight(self):
        for _ in range(5):
            w = compute_source_weight("operational_signal_source", "primary")
            assert w == pytest.approx(0.66)

    def test_same_context_same_score(self):
        dr = _handoff_dr()
        ctx = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_HIGH)

        results = [score(dr, weighting_context=ctx) for _ in range(5)]
        impacts = [r["impact"] for r in results]
        confidences = [r["confidence"] for r in results]

        assert len(set(impacts)) == 1, f"Non-deterministic impact: {impacts}"
        assert len(set(confidences)) == 1, f"Non-deterministic confidence: {confidences}"

    def test_same_weight_table_stable_across_calls(self):
        """The ROLE_WEIGHT and PRIORITY_NUDGE tables must not mutate."""
        import discovery.weighting_context as wc
        sor_before = wc.ROLE_WEIGHT["system_of_record"]
        _ = compute_source_weight("system_of_record", "primary")
        _ = compute_source_weight("operational_signal_source", "tertiary")
        assert wc.ROLE_WEIGHT["system_of_record"] == sor_before


# ─────────────────────────────────────────────────────────────────────────────
# score_debug transparency tests (explainability requirement)
# ─────────────────────────────────────────────────────────────────────────────

class TestScoreDebugTransparency:

    def test_source_weighting_contains_source_weight(self):
        dr = _handoff_dr()
        ctx = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_HIGH)
        result = score(dr, weighting_context=ctx)
        sw = result["score_debug"]["source_weighting"]
        assert "source_weight" in sw
        assert sw["source_weight"] == pytest.approx(0.66)

    def test_weighted_raw_in_impact_factors(self):
        dr = _handoff_dr()
        ctx = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_MEDIUM)
        result = score(dr, weighting_context=ctx)
        debug = result["score_debug"]["impact_factors"]
        assert "weighted_raw" in debug
        # weighted_raw = raw_sum × 0.6
        assert debug["weighted_raw"] == pytest.approx(debug["raw_sum"] * 0.6, rel=1e-4)

    def test_effective_proxy_ratio_in_debug(self):
        dr = _handoff_dr()
        ctx = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_MEDIUM)
        result = score(dr, weighting_context=ctx)
        debug = result["score_debug"]
        assert "effective_proxy_ratio" in debug
        assert debug["effective_proxy_ratio"] == pytest.approx(
            debug["proxy_ratio"] * 0.6, rel=1e-4
        )

    def test_no_context_source_weighting_is_none(self):
        dr = _handoff_dr()
        result = score(dr)
        assert result["score_debug"]["source_weighting"] is None

    def test_no_context_weighted_raw_equals_raw_sum(self):
        dr = _handoff_dr()
        result = score(dr)
        debug = result["score_debug"]["impact_factors"]
        assert debug["weighted_raw"] == debug["raw_sum"]

    def test_no_context_effective_proxy_ratio_equals_proxy_ratio(self):
        dr = _handoff_dr()
        result = score(dr)
        debug = result["score_debug"]
        assert debug["effective_proxy_ratio"] == debug["proxy_ratio"]

    def test_role_visible_in_source_weighting(self):
        dr = _handoff_dr()
        ctx = _ctx_with("salesforce", ROLE_PRIMARY, PRIORITY_HIGH)
        result = score(dr, weighting_context=ctx)
        sw = result["score_debug"]["source_weighting"]
        assert sw["role"] == ROLE_PRIMARY
        assert sw["priority"] == PRIORITY_HIGH

    def test_to_debug_dict_includes_source_weight(self):
        ctx = _ctx_with("salesforce", ROLE_SUPPORTING, PRIORITY_HIGH)
        debug = ctx.to_debug_dict()
        sf = debug["system_weightings"]["salesforce"]
        assert "source_weight" in sf
        assert sf["source_weight"] == pytest.approx(0.66)


# ─────────────────────────────────────────────────────────────────────────────
# Backward compatibility — no context / neutral context
# ─────────────────────────────────────────────────────────────────────────────

class TestBackwardCompatibility:

    def test_score_without_context_unchanged_from_pre_t2(self):
        """Existing tests that call score(dr) with no context must still pass."""
        dr = DetectorResult(
            detector_id="HANDOFF_FRICTION",
            signal_source="salesforce",
            metric_value=3.5,
            threshold=2.0,
            raw_evidence={"total_cases_90d": 500, "handoff_score": 3.0},
        )
        result = score(dr)
        assert result["impact"] >= 1
        assert result["confidence"] in ("HIGH", "MEDIUM", "LOW")
        assert result["effort"] >= 1

    def test_neutral_context_same_as_no_context(self):
        dr = _handoff_dr()
        without = score(dr)
        with_neutral = score(dr, weighting_context=StackBuilderWeightingContext.neutral())
        assert without["impact"] == with_neutral["impact"]
        assert without["confidence"] == with_neutral["confidence"]
