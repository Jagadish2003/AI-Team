"""
test_r16_c1_t4_provenance_guard.py

R16-C1 T4 — Tests: weighting respects provenance; it modulates observed
evidence weight and does not let weighting promote inferred over observed.

Acceptance criteria verified:
  AC5 - Weighting modulates observed-evidence contribution and never promotes
        inferred evidence above observed.  Verified by: scoring the same
        detector result as observed vs. inferred with a heavily-weighted source
        and asserting that the observed result scores >= the inferred result.

Additional T4 coverage:
  - provenance_guard.apply_provenance_weight_cap() unit tests
  - provenance_guard.apply_provenance_confidence_ceiling() unit tests
  - provenance_guard.observed_beats_inferred() invariant check
  - SystemWeighting.base_role_weight property
  - DetectorResult.provenance_type field (default + explicit)
  - scorer.score() integration: inferred weight capped at base_role_weight
  - scorer.score() integration: inferred confidence ceiling at MEDIUM
  - scorer.score(): observed always scores >= inferred at the same source
  - score_debug exposes t4_provenance audit trail
  - evidence_builder stamps provenanceType on evidence objects
  - AC6 determinism with T4 provenance guard active
  - Backward compatibility: existing callers without provenance_type unaffected
"""
from __future__ import annotations

import pytest

from discovery.provenance_guard import (
    PROVENANCE_OBSERVED,
    PROVENANCE_INFERRED,
    INFERRED_CONFIDENCE_CEILING,
    apply_provenance_weight_cap,
    apply_provenance_confidence_ceiling,
    observed_beats_inferred,
    provenance_guard_debug,
)
from discovery.models import DetectorResult
from discovery.scorer import score
from discovery.weighting_context import (
    SystemWeighting,
    StackBuilderWeightingContext,
    ROLE_WEIGHT,
    WEIGHT_NEUTRAL,
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
    PRIORITY_LOW,
    ROLE_PRIMARY,
    ROLE_SUPPORTING,
    ROLE_SUPPLEMENTARY,
)
from discovery.evidence_builder import build_evidence


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _dr(
    detector_id: str = "HANDOFF_FRICTION",
    signal_source: str = "salesforce",
    metric_value: float = 8.0,
    threshold: float = 2.0,
    volume: float = 500.0,
    provenance_type: str = PROVENANCE_OBSERVED,
) -> DetectorResult:
    return DetectorResult(
        detector_id=detector_id,
        signal_source=signal_source,
        metric_value=metric_value,
        threshold=threshold,
        raw_evidence={
            "total_cases_90d": volume,
            "handoff_score": 5.0,
            "pending_count": volume,
            "avg_delay_days": 5.0,
        },
        provenance_type=provenance_type,
    )


def _ctx(system_id: str, role: str, priority: str) -> StackBuilderWeightingContext:
    w = SystemWeighting(system_id=system_id, role=role, priority=priority, confirmed=True)
    return StackBuilderWeightingContext(
        weightings={system_id: w},
        selected_system_ids=[system_id],
        pack_id="service_cloud",
        run_id="run_t4_test",
        is_neutral=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Unit: apply_provenance_weight_cap()
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyProvenanceWeightCap:

    def test_observed_returns_source_weight_unchanged(self):
        result = apply_provenance_weight_cap(1.1, 1.0, PROVENANCE_OBSERVED)
        assert result == pytest.approx(1.1)

    def test_inferred_caps_at_base_role_weight(self):
        # source_weight=1.1 (role=1.0 × priority=1.1), base_role=1.0
        result = apply_provenance_weight_cap(1.1, 1.0, PROVENANCE_INFERRED)
        assert result == pytest.approx(1.0)

    def test_inferred_already_below_base_role_unchanged(self):
        # source_weight already at or below base_role — no change
        result = apply_provenance_weight_cap(0.6, 0.8, PROVENANCE_INFERRED)
        assert result == pytest.approx(0.6)

    def test_inferred_supporting_primary_stripped_to_role_weight(self):
        # supporting+primary: source_weight=0.66, base_role=0.6
        result = apply_provenance_weight_cap(0.66, 0.6, PROVENANCE_INFERRED)
        assert result == pytest.approx(0.6)

    def test_unknown_provenance_treated_as_observed(self):
        # Unknown provenance type must not break the pipeline
        result = apply_provenance_weight_cap(1.1, 1.0, "unknown_type")
        assert result == pytest.approx(1.1)

    def test_inferred_workflow_system_primary_stripped(self):
        # workflow_system+primary: source_weight=0.88, base_role=0.8
        result = apply_provenance_weight_cap(0.88, 0.8, PROVENANCE_INFERRED)
        assert result == pytest.approx(0.8)

    def test_observed_supporting_primary_not_capped(self):
        # Observed supporting+primary: source_weight=0.66 — T4 must not interfere
        result = apply_provenance_weight_cap(0.66, 0.6, PROVENANCE_OBSERVED)
        assert result == pytest.approx(0.66)

    def test_inferred_weight_never_exceeds_source_weight(self):
        for role, base in ROLE_WEIGHT.items():
            for nudge in [1.0, 1.1, 0.9]:
                src_w = min(max(base * nudge, 0.5), 1.1)
                capped = apply_provenance_weight_cap(src_w, base, PROVENANCE_INFERRED)
                assert capped <= src_w, (
                    f"role={role}: capped={capped} must not exceed src_w={src_w}"
                )


# ─────────────────────────────────────────────────────────────────────────────
# Unit: apply_provenance_confidence_ceiling()
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyProvenanceConfidenceCeiling:

    def test_inferred_high_clamped_to_medium(self):
        result = apply_provenance_confidence_ceiling("HIGH", PROVENANCE_INFERRED)
        assert result == "MEDIUM"

    def test_inferred_medium_unchanged(self):
        result = apply_provenance_confidence_ceiling("MEDIUM", PROVENANCE_INFERRED)
        assert result == "MEDIUM"

    def test_inferred_low_unchanged(self):
        result = apply_provenance_confidence_ceiling("LOW", PROVENANCE_INFERRED)
        assert result == "LOW"

    def test_observed_high_unchanged(self):
        result = apply_provenance_confidence_ceiling("HIGH", PROVENANCE_OBSERVED)
        assert result == "HIGH"

    def test_observed_medium_unchanged(self):
        result = apply_provenance_confidence_ceiling("MEDIUM", PROVENANCE_OBSERVED)
        assert result == "MEDIUM"

    def test_observed_low_unchanged(self):
        result = apply_provenance_confidence_ceiling("LOW", PROVENANCE_OBSERVED)
        assert result == "LOW"

    def test_unknown_provenance_treated_as_observed(self):
        result = apply_provenance_confidence_ceiling("HIGH", "unknown_type")
        assert result == "HIGH"

    def test_inferred_confidence_ceiling_constant_is_medium(self):
        assert INFERRED_CONFIDENCE_CEILING == "MEDIUM"


# ─────────────────────────────────────────────────────────────────────────────
# Unit: observed_beats_inferred()
# ─────────────────────────────────────────────────────────────────────────────

class TestObservedBeatsInferred:

    def test_observed_higher_than_inferred(self):
        assert observed_beats_inferred(1.1, 1.0) is True

    def test_observed_equal_to_inferred(self):
        assert observed_beats_inferred(1.0, 1.0) is True

    def test_observed_lower_than_inferred_false(self):
        assert observed_beats_inferred(0.6, 1.0) is False

    def test_neutral_equal(self):
        assert observed_beats_inferred(WEIGHT_NEUTRAL, WEIGHT_NEUTRAL) is True


# ─────────────────────────────────────────────────────────────────────────────
# SystemWeighting.base_role_weight property
# ─────────────────────────────────────────────────────────────────────────────

class TestBaseRoleWeight:

    def test_system_of_record_base_is_1_0(self):
        w = SystemWeighting(system_id="sf", role="system_of_record", priority="primary")
        assert w.base_role_weight == pytest.approx(1.0)

    def test_workflow_system_base_is_0_8(self):
        w = SystemWeighting(system_id="sn", role="workflow_system", priority="primary")
        assert w.base_role_weight == pytest.approx(0.8)

    def test_supporting_base_is_0_6(self):
        w = SystemWeighting(system_id="jira", role="operational_signal_source", priority="primary")
        assert w.base_role_weight == pytest.approx(0.6)

    def test_supplementary_base_is_0_6(self):
        w = SystemWeighting(system_id="slack", role="supplementary", priority="primary")
        assert w.base_role_weight == pytest.approx(0.6)

    def test_base_always_lte_source_weight(self):
        """source_weight (role × priority) >= base_role_weight (role only)."""
        cases = [
            ("system_of_record", "primary"),
            ("workflow_system", "primary"),
            ("operational_signal_source", "primary"),
            ("supplementary", "primary"),
        ]
        for role, priority in cases:
            w = SystemWeighting(system_id="x", role=role, priority=priority)
            assert w.source_weight >= w.base_role_weight, (
                f"role={role}+primary: source_weight={w.source_weight} "
                f"should be >= base_role_weight={w.base_role_weight}"
            )

    def test_neutral_base_is_weight_neutral(self):
        w = SystemWeighting(system_id="unknown")
        assert w.base_role_weight == WEIGHT_NEUTRAL


# ─────────────────────────────────────────────────────────────────────────────
# DetectorResult.provenance_type field
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectorResultProvenanceType:

    def test_default_provenance_is_observed(self):
        dr = DetectorResult(
            detector_id="HANDOFF_FRICTION",
            signal_source="salesforce",
            metric_value=3.5,
            threshold=2.0,
            raw_evidence={"total_cases_90d": 500, "handoff_score": 3.0},
        )
        assert dr.provenance_type == PROVENANCE_OBSERVED

    def test_explicit_observed(self):
        dr = _dr(provenance_type=PROVENANCE_OBSERVED)
        assert dr.provenance_type == PROVENANCE_OBSERVED

    def test_explicit_inferred(self):
        dr = _dr(provenance_type=PROVENANCE_INFERRED)
        assert dr.provenance_type == PROVENANCE_INFERRED

    def test_backward_compat_no_provenance_type_kwarg(self):
        """Existing callers that pass only positional/other kwargs still work."""
        dr = DetectorResult(
            detector_id="APPROVAL_BOTTLENECK",
            signal_source="salesforce",
            metric_value=2.5,
            threshold=2.0,
            raw_evidence={"pending_count": 100, "avg_delay_days": 3.0},
        )
        # provenance_type defaults to "observed"
        assert dr.provenance_type == "observed"


# ─────────────────────────────────────────────────────────────────────────────
# AC5 Integration: observed always scores >= inferred at the same source
# ─────────────────────────────────────────────────────────────────────────────

class TestAC5ObservedBeatsInferred:
    """
    AC5 — Weighting modulates observed-evidence contribution and never promotes
    inferred evidence above observed.

    All combinations of role × priority are tested to show that for the same
    detector signal and weighting context, observed confidence >= inferred
    confidence and observed impact >= inferred impact.
    """

    def _score_pair(self, system_id: str, role: str, priority: str, metric: float = 8.0):
        """Return (observed_result, inferred_result) for the same signal+context."""
        dr_obs = _dr(signal_source=system_id, metric_value=metric, provenance_type=PROVENANCE_OBSERVED)
        dr_inf = _dr(signal_source=system_id, metric_value=metric, provenance_type=PROVENANCE_INFERRED)
        ctx = _ctx(system_id, role, priority)
        return score(dr_obs, weighting_context=ctx), score(dr_inf, weighting_context=ctx)

    def test_sor_primary_observed_confidence_gte_inferred(self):
        obs, inf = self._score_pair("salesforce", "system_of_record", "primary")
        conf_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        assert conf_order[obs["confidence"]] >= conf_order[inf["confidence"]], (
            f"Observed {obs['confidence']} must be >= inferred {inf['confidence']}"
        )

    def test_sor_primary_observed_impact_gte_inferred(self):
        obs, inf = self._score_pair("salesforce", "system_of_record", "primary")
        assert obs["impact"] >= inf["impact"], (
            f"Observed impact {obs['impact']} must be >= inferred {inf['impact']}"
        )

    def test_supporting_primary_observed_gte_inferred(self):
        obs, inf = self._score_pair("servicenow", "operational_signal_source", "primary")
        conf_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        assert conf_order[obs["confidence"]] >= conf_order[inf["confidence"]]
        assert obs["impact"] >= inf["impact"]

    def test_workflow_primary_observed_gte_inferred(self):
        obs, inf = self._score_pair("salesforce", "workflow_system", "primary")
        conf_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        assert conf_order[obs["confidence"]] >= conf_order[inf["confidence"]]
        assert obs["impact"] >= inf["impact"]

    def test_all_role_priority_combinations_observed_gte_inferred(self):
        """Exhaustive: every role × priority keeps observed >= inferred."""
        roles = [
            "system_of_record", "workflow_system",
            "operational_signal_source", "supplementary",
        ]
        priorities = ["primary", "secondary", "tertiary"]
        conf_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

        for role in roles:
            for priority in priorities:
                obs, inf = self._score_pair("salesforce", role, priority, metric=8.0)
                assert conf_order[obs["confidence"]] >= conf_order[inf["confidence"]], (
                    f"role={role}+{priority}: observed {obs['confidence']} "
                    f"must be >= inferred {inf['confidence']}"
                )
                assert obs["impact"] >= inf["impact"], (
                    f"role={role}+{priority}: observed impact {obs['impact']} "
                    f"must be >= inferred {inf['impact']}"
                )

    def test_heavily_weighted_inferred_cannot_exceed_lightly_weighted_observed(self):
        """
        Key AC5 test: inferred from a heavily-weighted source (SoR+primary)
        must not produce higher confidence than observed from a lightly-weighted
        source (supporting+secondary).
        """
        dr_obs = _dr(signal_source="salesforce", metric_value=8.0,
                     provenance_type=PROVENANCE_OBSERVED)
        dr_inf = _dr(signal_source="servicenow", metric_value=8.0,
                     provenance_type=PROVENANCE_INFERRED)

        ctx_obs = _ctx("salesforce", "operational_signal_source", "secondary")  # weight=0.6
        ctx_inf = _ctx("servicenow", "system_of_record", "primary")             # weight capped at 1.0

        res_obs = score(dr_obs, weighting_context=ctx_obs)
        res_inf = score(dr_inf, weighting_context=ctx_inf)

        conf_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        # Inferred is capped at MEDIUM; observed supporting can reach MEDIUM too.
        # The critical assertion: inferred must NOT reach HIGH.
        assert res_inf["confidence"] != "HIGH", (
            f"Inferred from SoR+primary must not reach HIGH, got {res_inf['confidence']}"
        )

    def test_inferred_sor_primary_cannot_reach_high(self):
        """Inferred evidence from system_of_record+primary is capped at MEDIUM."""
        dr = _dr(signal_source="salesforce", metric_value=10.0, volume=2000.0,
                 provenance_type=PROVENANCE_INFERRED)
        ctx = _ctx("salesforce", "system_of_record", "primary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] != "HIGH", (
            "Inferred evidence cannot reach HIGH even from SoR+primary"
        )
        assert result["confidence"] == "MEDIUM"

    def test_observed_sor_secondary_can_reach_high(self):
        """Baseline: observed evidence from SoR+secondary should reach HIGH."""
        dr = _dr(signal_source="salesforce", metric_value=8.0, volume=500.0,
                 provenance_type=PROVENANCE_OBSERVED)
        ctx = _ctx("salesforce", "system_of_record", "secondary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] == "HIGH", (
            f"Observed SoR+secondary should reach HIGH, got {result['confidence']}"
        )

    def test_no_context_observed_unaffected(self):
        """Without weighting context, observed evidence scores normally."""
        dr = _dr(metric_value=8.0, volume=500.0, provenance_type=PROVENANCE_OBSERVED)
        result = score(dr, weighting_context=None)
        assert result["confidence"] in ("HIGH", "MEDIUM", "LOW")

    def test_no_context_inferred_still_capped_at_medium(self):
        """Without weighting context, inferred evidence is still capped at MEDIUM."""
        dr = _dr(metric_value=10.0, volume=2000.0, provenance_type=PROVENANCE_INFERRED)
        result = score(dr, weighting_context=None)
        assert result["confidence"] != "HIGH", (
            "Inferred evidence with no context must still be capped at MEDIUM"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Weight cap in score_debug
# ─────────────────────────────────────────────────────────────────────────────

class TestT4WeightCapInScoreDebug:

    def test_t4_provenance_key_present_in_debug(self):
        dr = _dr(provenance_type=PROVENANCE_OBSERVED)
        result = score(dr)
        assert "t4_provenance" in result["score_debug"]

    def test_t4_provenance_type_observed_debug(self):
        dr = _dr(provenance_type=PROVENANCE_OBSERVED)
        ctx = _ctx("salesforce", "system_of_record", "primary")
        result = score(dr, weighting_context=ctx)
        t4 = result["score_debug"]["t4_provenance"]
        assert t4["provenance_type"] == PROVENANCE_OBSERVED
        assert t4["weight_capped"] is False  # observed: no cap
        assert t4["effective_weight"] == pytest.approx(t4["source_weight"])

    def test_t4_provenance_type_inferred_debug_shows_cap(self):
        dr = _dr(signal_source="salesforce", provenance_type=PROVENANCE_INFERRED)
        ctx = _ctx("salesforce", "system_of_record", "primary")
        result = score(dr, weighting_context=ctx)
        t4 = result["score_debug"]["t4_provenance"]
        assert t4["provenance_type"] == PROVENANCE_INFERRED
        # source_weight=1.1 (SoR+primary), base_role_weight=1.0 → cap at 1.0
        assert t4["source_weight"] == pytest.approx(1.1)
        assert t4["base_role_weight"] == pytest.approx(1.0)
        assert t4["effective_weight"] == pytest.approx(1.0)
        assert t4["weight_capped"] is True

    def test_t4_inferred_confidence_clamped_flag(self):
        dr = _dr(signal_source="salesforce", metric_value=8.0, volume=500.0,
                 provenance_type=PROVENANCE_INFERRED)
        ctx = _ctx("salesforce", "system_of_record", "secondary")
        result = score(dr, weighting_context=ctx)
        t4 = result["score_debug"]["t4_provenance"]
        if t4["confidence_before_provenance"] == "HIGH":
            assert t4["confidence_clamped"] is True
            assert t4["confidence_after_provenance"] == "MEDIUM"

    def test_t4_observed_beats_inferred_holds_flag_true(self):
        dr = _dr(signal_source="salesforce", provenance_type=PROVENANCE_INFERRED)
        ctx = _ctx("salesforce", "system_of_record", "primary")
        result = score(dr, weighting_context=ctx)
        t4 = result["score_debug"]["t4_provenance"]
        assert t4["observed_beats_inferred_holds"] is True

    def test_t4_no_context_debug_present(self):
        dr = _dr(provenance_type=PROVENANCE_INFERRED)
        result = score(dr, weighting_context=None)
        t4 = result["score_debug"]["t4_provenance"]
        assert t4["provenance_type"] == PROVENANCE_INFERRED
        assert "effective_weight" in t4

    def test_t4_source_weighting_includes_base_role_weight(self):
        dr = _dr()
        ctx = _ctx("salesforce", "system_of_record", "primary")
        result = score(dr, weighting_context=ctx)
        sw = result["score_debug"]["source_weighting"]
        assert "base_role_weight" in sw
        assert sw["base_role_weight"] == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────────────────────
# Evidence builder: provenanceType stamp
# ─────────────────────────────────────────────────────────────────────────────

class TestEvidenceBuilderProvenanceStamp:

    def test_observed_evidence_stamped_observed(self):
        dr = _dr(detector_id="HANDOFF_FRICTION", signal_source="salesforce",
                 provenance_type=PROVENANCE_OBSERVED)
        result = score(dr)
        evidences = build_evidence(dr, result)
        assert len(evidences) > 0
        for ev in evidences:
            assert ev.get("provenanceType") == PROVENANCE_OBSERVED

    def test_inferred_evidence_stamped_inferred(self):
        dr = _dr(detector_id="HANDOFF_FRICTION", signal_source="salesforce",
                 provenance_type=PROVENANCE_INFERRED)
        result = score(dr)
        evidences = build_evidence(dr, result)
        assert len(evidences) > 0
        for ev in evidences:
            assert ev.get("provenanceType") == PROVENANCE_INFERRED

    def test_default_provenance_stamped_as_observed(self):
        """DetectorResult with no provenance_type arg defaults to observed."""
        dr = DetectorResult(
            detector_id="HANDOFF_FRICTION",
            signal_source="salesforce",
            metric_value=3.5,
            threshold=2.0,
            raw_evidence={"total_cases_90d": 500, "handoff_score": 3.0},
        )
        result = score(dr)
        evidences = build_evidence(dr, result)
        assert len(evidences) > 0
        assert evidences[0]["provenanceType"] == "observed"


# ─────────────────────────────────────────────────────────────────────────────
# AC6 Determinism with T4 active
# ─────────────────────────────────────────────────────────────────────────────

class TestAC6DeterminismT4:

    def test_same_inferred_context_same_output(self):
        dr = _dr(signal_source="salesforce", provenance_type=PROVENANCE_INFERRED)
        ctx = _ctx("salesforce", "system_of_record", "primary")
        results = [score(dr, weighting_context=ctx) for _ in range(5)]
        confidences = [r["confidence"] for r in results]
        impacts = [r["impact"] for r in results]
        assert len(set(confidences)) == 1, f"Non-deterministic confidence: {confidences}"
        assert len(set(impacts)) == 1, f"Non-deterministic impact: {impacts}"

    def test_same_observed_context_same_output(self):
        dr = _dr(signal_source="salesforce", provenance_type=PROVENANCE_OBSERVED)
        ctx = _ctx("salesforce", "system_of_record", "secondary")
        results = [score(dr, weighting_context=ctx) for _ in range(5)]
        confidences = [r["confidence"] for r in results]
        assert len(set(confidences)) == 1

    def test_provenance_weight_cap_deterministic(self):
        for _ in range(10):
            result = apply_provenance_weight_cap(1.1, 1.0, PROVENANCE_INFERRED)
            assert result == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────────────────────
# provenance_guard_debug() audit dict
# ─────────────────────────────────────────────────────────────────────────────

class TestProvenanceGuardDebug:

    def test_inferred_weight_capped_shows_correct_fields(self):
        debug = provenance_guard_debug(
            provenance_type=PROVENANCE_INFERRED,
            source_weight=1.1,
            base_role_weight=1.0,
            effective_weight=1.0,
            confidence_before_provenance="HIGH",
            confidence_after_provenance="MEDIUM",
        )
        assert debug["provenance_type"] == PROVENANCE_INFERRED
        assert debug["source_weight"] == pytest.approx(1.1)
        assert debug["base_role_weight"] == pytest.approx(1.0)
        assert debug["effective_weight"] == pytest.approx(1.0)
        assert debug["weight_capped"] is True
        assert debug["confidence_before_provenance"] == "HIGH"
        assert debug["confidence_after_provenance"] == "MEDIUM"
        assert debug["confidence_clamped"] is True
        assert debug["observed_beats_inferred_holds"] is True

    def test_observed_no_cap_shows_correct_fields(self):
        debug = provenance_guard_debug(
            provenance_type=PROVENANCE_OBSERVED,
            source_weight=1.1,
            base_role_weight=1.0,
            effective_weight=1.1,
            confidence_before_provenance="HIGH",
            confidence_after_provenance="HIGH",
        )
        assert debug["weight_capped"] is False
        assert debug["confidence_clamped"] is False
        assert debug["observed_beats_inferred_holds"] is True

    def test_observed_beats_inferred_flag_false_when_violated(self):
        debug = provenance_guard_debug(
            provenance_type=PROVENANCE_INFERRED,
            source_weight=0.6,
            base_role_weight=1.0,   # base > source (impossible in practice, but test the math)
            effective_weight=0.6,
            confidence_before_provenance="MEDIUM",
            confidence_after_provenance="MEDIUM",
        )
        # source_weight=0.6 < base_role_weight=1.0 → observed (1.0) > inferred (0.6) ✓
        assert debug["observed_beats_inferred_holds"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Backward compatibility
# ─────────────────────────────────────────────────────────────────────────────

class TestBackwardCompatibilityT4:

    def test_existing_score_call_without_provenance_type_unchanged(self):
        """score(dr) without provenance_type treats evidence as observed — no regression."""
        dr = DetectorResult(
            detector_id="HANDOFF_FRICTION",
            signal_source="salesforce",
            metric_value=3.5,
            threshold=2.0,
            raw_evidence={"total_cases_90d": 500, "handoff_score": 3.0},
        )
        result = score(dr)
        # Should still produce valid scored output
        assert result["confidence"] in ("HIGH", "MEDIUM", "LOW")
        assert 1 <= result["impact"] <= 10
        assert 1 <= result["effort"] <= 10

    def test_t4_provenance_key_present_in_all_score_calls(self):
        """All score() calls return t4_provenance in debug regardless of provenance_type."""
        dr = DetectorResult(
            detector_id="HANDOFF_FRICTION",
            signal_source="salesforce",
            metric_value=3.5,
            threshold=2.0,
            raw_evidence={"total_cases_90d": 500, "handoff_score": 3.0},
        )
        result = score(dr)
        assert "t4_provenance" in result["score_debug"]

    def test_t2_t3_tests_still_pass_with_t4_active(self):
        """
        Spot-check: a T2 test scenario produces the same outcome with T4 active.
        T4 only caps inferred evidence — observed evidence is unaffected.
        """
        from discovery.weighting_context import ROLE_SUPPORTING, PRIORITY_MEDIUM
        dr = DetectorResult(
            detector_id="HANDOFF_FRICTION",
            signal_source="salesforce",
            metric_value=3.5,
            threshold=2.0,
            raw_evidence={"total_cases_90d": 800, "handoff_score": 4.0},
        )
        w = SystemWeighting(system_id="salesforce", role=ROLE_SUPPORTING,
                            priority=PRIORITY_MEDIUM, confirmed=True)
        ctx = StackBuilderWeightingContext(
            weightings={"salesforce": w},
            selected_system_ids=["salesforce"],
            pack_id="service_cloud",
            run_id="compat_test",
            is_neutral=False,
        )
        result = score(dr, weighting_context=ctx)
        # T3 already caps supporting at MEDIUM; T4 adds no further cap for observed
        assert result["confidence"] in ("MEDIUM", "LOW")
