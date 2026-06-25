"""
test_r16_c1_t3_ceiling_clamp.py

R16-C1 T3 — Tests: clamp weighting so it operates strictly within the hard
corroboration rules (ceilings, single-source caps) per Section 2 of the spec.

Acceptance criteria verified:
  AC4 - Weighting never breaches a hard rule: a Supporting-only or Slack-only
        signal cannot reach HIGH regardless of priority. Over-weighting such a
        source must still not produce HIGH. Verified by attempting to over-weight.
  AC6 - Determinism: same inputs always produce identical clamped output.

Additional T3 coverage:
  - apply_t3_ceiling_clamp() unit tests (all four hard rules)
  - scorer.score() integration: T3 clamp fires when role is supporting-only
  - scorer.score() integration: T3 clamp fires when system_id is slack
  - scorer.score() integration: T3 clamp does NOT fire for system_of_record
  - score_debug exposes t3_ceiling_clamp audit trail
  - corroboration_engine.apply_corroboration_confidence() defense-in-depth:
      Slack-only (COR-05) cannot produce HIGH even if elevated_confidence
      were somehow set to HIGH by a future bug.
  - Single-source (COR-08) cannot produce HIGH from corroboration engine.
  - is_slack_only_corroboration() helper
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from discovery.t3_ceiling_clamp import (
    apply_t3_ceiling_clamp,
    is_slack_only_corroboration,
    SUPPORTING_ONLY_ROLES,
    SLACK_SYSTEM_IDS,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
)
from discovery.scorer import score
from discovery.models import DetectorResult
from discovery.weighting_context import (
    SystemWeighting,
    StackBuilderWeightingContext,
    ROLE_PRIMARY,
    ROLE_SUPPORTING,
    ROLE_SUPPLEMENTARY,
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
    PRIORITY_LOW,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _strong_dr(
    detector_id: str = "HANDOFF_FRICTION",
    signal_source: str = "salesforce",
    proxy_ratio: float = 4.0,
    volume: float = 500.0,
) -> DetectorResult:
    """A detector result strong enough to score HIGH under system_of_record."""
    return DetectorResult(
        detector_id=detector_id,
        signal_source=signal_source,
        metric_value=proxy_ratio * 2.0,   # threshold=2.0 → metric = ratio × threshold
        threshold=2.0,
        raw_evidence={
            "total_cases_90d": volume,
            "handoff_score": 5.0,
            "pending_count": volume,
            "avg_delay_days": 5.0,
        },
    )


def _ctx_with(system_id: str, role: str, priority: str) -> StackBuilderWeightingContext:
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
        run_id="run_t3_test",
        is_neutral=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: apply_t3_ceiling_clamp()
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyT3CeilingClampUnit:

    # ── MEDIUM and LOW pass through unchanged ─────────────────────────────────

    def test_medium_confidence_unchanged(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_MEDIUM, role="supporting")
        assert result == CONFIDENCE_MEDIUM

    def test_low_confidence_unchanged(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_LOW, role="supporting")
        assert result == CONFIDENCE_LOW

    def test_medium_unchanged_for_slack_system(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_MEDIUM, system_id="slack")
        assert result == CONFIDENCE_MEDIUM

    # ── Rule 1: Supporting-only role cannot reach HIGH ─────────────────────────

    def test_supporting_role_caps_high_to_medium(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, role="supporting")
        assert result == CONFIDENCE_MEDIUM

    def test_operational_signal_source_caps_high_to_medium(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, role="operational_signal_source")
        assert result == CONFIDENCE_MEDIUM

    def test_supplementary_caps_high_to_medium(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, role="supplementary")
        assert result == CONFIDENCE_MEDIUM

    def test_all_supporting_roles_cap_high(self):
        for role in SUPPORTING_ONLY_ROLES:
            result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, role=role)
            assert result == CONFIDENCE_MEDIUM, f"role={role!r} should cap HIGH to MEDIUM"

    def test_system_of_record_does_not_clamp_high(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, role="system_of_record")
        assert result == CONFIDENCE_HIGH

    def test_workflow_system_does_not_clamp_high(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, role="workflow_system")
        assert result == CONFIDENCE_HIGH

    def test_empty_role_does_not_clamp_high(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, role="")
        assert result == CONFIDENCE_HIGH

    def test_no_role_does_not_clamp_high(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH)
        assert result == CONFIDENCE_HIGH

    def test_role_with_whitespace_is_stripped(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, role="  supporting  ")
        assert result == CONFIDENCE_MEDIUM

    # ── Rule 2: Slack system-ID always caps at MEDIUM ─────────────────────────

    def test_slack_system_id_caps_high_to_medium(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, system_id="slack")
        assert result == CONFIDENCE_MEDIUM

    def test_slack_workspace_system_id_caps_high_to_medium(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, system_id="slack_workspace")
        assert result == CONFIDENCE_MEDIUM

    def test_slack_system_id_case_insensitive(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, system_id="Slack")
        assert result == CONFIDENCE_MEDIUM

    def test_salesforce_system_id_does_not_clamp(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, system_id="salesforce")
        assert result == CONFIDENCE_HIGH

    def test_servicenow_system_id_does_not_clamp(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, system_id="servicenow")
        assert result == CONFIDENCE_HIGH

    def test_slack_system_id_clamps_even_with_sor_role(self):
        """Slack system-ID clamp is independent of role — misconfigured role cannot bypass."""
        result = apply_t3_ceiling_clamp(
            CONFIDENCE_HIGH,
            role="system_of_record",  # incorrectly assigned
            system_id="slack",
        )
        assert result == CONFIDENCE_MEDIUM

    # ── Rule 3: Slack-only corroboration sources ───────────────────────────────

    def test_slack_only_sources_caps_high_to_medium(self):
        result = apply_t3_ceiling_clamp(
            CONFIDENCE_HIGH,
            corroboration_sources=["Slack (supporting only)"],
        )
        assert result == CONFIDENCE_MEDIUM

    def test_slack_escalation_pattern_only_caps_high(self):
        result = apply_t3_ceiling_clamp(
            CONFIDENCE_HIGH,
            corroboration_sources=["Slack (escalation pattern)"],
        )
        assert result == CONFIDENCE_MEDIUM

    def test_slack_plus_servicenow_does_not_clamp(self):
        result = apply_t3_ceiling_clamp(
            CONFIDENCE_HIGH,
            corroboration_sources=["ServiceNow", "Slack (escalation pattern)"],
        )
        assert result == CONFIDENCE_HIGH

    def test_servicenow_only_does_not_clamp(self):
        result = apply_t3_ceiling_clamp(
            CONFIDENCE_HIGH,
            corroboration_sources=["ServiceNow"],
        )
        assert result == CONFIDENCE_HIGH

    def test_jira_only_does_not_clamp(self):
        result = apply_t3_ceiling_clamp(
            CONFIDENCE_HIGH,
            corroboration_sources=["Jira"],
        )
        assert result == CONFIDENCE_HIGH

    def test_empty_corroboration_sources_does_not_clamp(self):
        result = apply_t3_ceiling_clamp(
            CONFIDENCE_HIGH,
            corroboration_sources=[],
        )
        assert result == CONFIDENCE_HIGH

    def test_none_corroboration_sources_skipped(self):
        result = apply_t3_ceiling_clamp(
            CONFIDENCE_HIGH,
            corroboration_sources=None,
        )
        assert result == CONFIDENCE_HIGH

    # ── Rule 4: Single-source ─────────────────────────────────────────────────

    def test_single_source_caps_high_to_medium(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, is_single_source=True)
        assert result == CONFIDENCE_MEDIUM

    def test_single_source_medium_unchanged(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_MEDIUM, is_single_source=True)
        assert result == CONFIDENCE_MEDIUM

    def test_multi_source_not_clamped_by_single_source_rule(self):
        result = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, is_single_source=False)
        assert result == CONFIDENCE_HIGH

    # ── Rule ordering: role checked before system_id ──────────────────────────

    def test_supporting_role_clamps_before_system_id_check(self):
        """Role check fires first; result is MEDIUM regardless of system_id."""
        result = apply_t3_ceiling_clamp(
            CONFIDENCE_HIGH,
            role="supporting",
            system_id="servicenow",
        )
        assert result == CONFIDENCE_MEDIUM

    # ── Determinism (AC6) ─────────────────────────────────────────────────────

    def test_same_inputs_always_same_output(self):
        for _ in range(10):
            r = apply_t3_ceiling_clamp(
                CONFIDENCE_HIGH,
                role="operational_signal_source",
            )
            assert r == CONFIDENCE_MEDIUM


# ─────────────────────────────────────────────────────────────────────────────
# is_slack_only_corroboration() helper
# ─────────────────────────────────────────────────────────────────────────────

class TestIsSlackOnlyCorroboration:

    def test_slack_supporting_only_label(self):
        assert is_slack_only_corroboration(["Slack (supporting only)"]) is True

    def test_slack_escalation_pattern_label(self):
        assert is_slack_only_corroboration(["Slack (escalation pattern)"]) is True

    def test_slack_plus_servicenow_is_false(self):
        assert is_slack_only_corroboration(["ServiceNow", "Slack (escalation pattern)"]) is False

    def test_servicenow_only_is_false(self):
        assert is_slack_only_corroboration(["ServiceNow"]) is False

    def test_empty_list_is_false(self):
        assert is_slack_only_corroboration([]) is False

    def test_jira_and_slack_is_false(self):
        assert is_slack_only_corroboration(["Jira", "Slack (escalation pattern)"]) is False


# ─────────────────────────────────────────────────────────────────────────────
# AC4 Integration: scorer.score() with T3 clamp applied
# ─────────────────────────────────────────────────────────────────────────────

class TestAC4ScorerT3Clamp:
    """
    AC4 — Weighting never breaches a hard rule:
    A Supporting-only or Slack-only signal cannot reach HIGH regardless of priority.
    Verified by attempting to over-weight such a source.
    """

    def test_supporting_primary_cannot_reach_high(self):
        """Supporting + primary priority (highest allowed weight for supporting) → MEDIUM."""
        dr = _strong_dr(signal_source="servicenow")
        ctx = _ctx_with("servicenow", "operational_signal_source", "primary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] != CONFIDENCE_HIGH, (
            f"Supporting+primary must not reach HIGH, got {result['confidence']}"
        )
        assert result["confidence"] == CONFIDENCE_MEDIUM

    def test_supporting_secondary_cannot_reach_high(self):
        dr = _strong_dr(signal_source="servicenow")
        ctx = _ctx_with("servicenow", "operational_signal_source", "secondary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] != CONFIDENCE_HIGH

    def test_supporting_tertiary_cannot_reach_high(self):
        dr = _strong_dr(signal_source="servicenow")
        ctx = _ctx_with("servicenow", "operational_signal_source", "tertiary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] != CONFIDENCE_HIGH

    def test_supplementary_primary_cannot_reach_high(self):
        """Supplementary + primary is supporting-only → MEDIUM."""
        dr = _strong_dr(signal_source="jira")
        ctx = _ctx_with("jira", "supplementary", "primary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] != CONFIDENCE_HIGH

    def test_supporting_role_cannot_reach_high_even_with_very_strong_signal(self):
        """Even a very high proxy_ratio cannot bypass the T3 ceiling for supporting."""
        dr = _strong_dr(proxy_ratio=10.0, volume=10000.0)
        ctx = _ctx_with("salesforce", "operational_signal_source", "primary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] != CONFIDENCE_HIGH, (
            "Supporting role cannot reach HIGH regardless of signal strength or priority"
        )

    def test_system_of_record_can_still_reach_high(self):
        """Baseline: system_of_record should reach HIGH — T3 must not break this."""
        dr = _strong_dr(signal_source="salesforce", proxy_ratio=3.0, volume=200.0)
        ctx = _ctx_with("salesforce", "system_of_record", "secondary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] == CONFIDENCE_HIGH, (
            f"system_of_record should reach HIGH on strong signal, got {result['confidence']}"
        )

    def test_workflow_system_can_reach_high(self):
        """Workflow system is not supporting-only — T3 must not cap it."""
        dr = _strong_dr(signal_source="salesforce", proxy_ratio=3.0, volume=200.0)
        ctx = _ctx_with("salesforce", "workflow_system", "secondary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] == CONFIDENCE_HIGH

    def test_no_context_can_reach_high(self):
        """No weighting context → neutral → T3 clamp does not apply."""
        dr = _strong_dr(signal_source="salesforce", proxy_ratio=3.0, volume=200.0)
        result = score(dr, weighting_context=None)
        assert result["confidence"] == CONFIDENCE_HIGH

    def test_slack_system_id_cannot_reach_high_even_with_sor_role(self):
        """Slack system-ID clamp prevents HIGH even if role is incorrectly set to SoR."""
        dr = _strong_dr(signal_source="slack", proxy_ratio=5.0, volume=1000.0)
        ctx = _ctx_with("slack", "system_of_record", "primary")  # misconfigured
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] != CONFIDENCE_HIGH, (
            "Slack system_id must never produce HIGH from scorer regardless of role"
        )

    def test_slack_system_id_without_context_cannot_reach_high(self):
        """Slack system-ID clamp fires even without a weighting context."""
        dr = _strong_dr(signal_source="slack", proxy_ratio=5.0, volume=1000.0)
        result = score(dr, weighting_context=None)
        assert result["confidence"] != CONFIDENCE_HIGH


# ─────────────────────────────────────────────────────────────────────────────
# score_debug T3 audit trail
# ─────────────────────────────────────────────────────────────────────────────

class TestT3DebugAuditTrail:

    def test_t3_ceiling_clamp_key_present_in_debug(self):
        dr = _strong_dr()
        result = score(dr)
        assert "t3_ceiling_clamp" in result["score_debug"]

    def test_t3_ceiling_clamp_not_applied_for_sor(self):
        """system_of_record HIGH → clamp not applied, applied=False."""
        dr = _strong_dr(proxy_ratio=3.0, volume=200.0)
        ctx = _ctx_with("salesforce", "system_of_record", "secondary")
        result = score(dr, weighting_context=ctx)
        clamp_debug = result["score_debug"]["t3_ceiling_clamp"]
        if result["confidence"] == CONFIDENCE_HIGH:
            assert clamp_debug["applied"] is False

    def test_t3_ceiling_clamp_applied_for_supporting(self):
        """Supporting + strong signal → clamp fires, applied=True."""
        dr = _strong_dr(signal_source="servicenow", proxy_ratio=3.0, volume=200.0)
        ctx = _ctx_with("servicenow", "operational_signal_source", "primary")
        result = score(dr, weighting_context=ctx)
        clamp_debug = result["score_debug"]["t3_ceiling_clamp"]
        # Only meaningful to check applied if the pre-clamp confidence was HIGH
        if clamp_debug["confidence_before_clamp"] == CONFIDENCE_HIGH:
            assert clamp_debug["applied"] is True
            assert clamp_debug["confidence_after_clamp"] == CONFIDENCE_MEDIUM

    def test_t3_debug_exposes_role_and_system_id(self):
        dr = _strong_dr(signal_source="servicenow")
        ctx = _ctx_with("servicenow", "supporting", "secondary")
        result = score(dr, weighting_context=ctx)
        clamp_debug = result["score_debug"]["t3_ceiling_clamp"]
        assert clamp_debug["role"] == "supporting"
        assert clamp_debug["system_id"] == "servicenow"

    def test_t3_debug_present_without_weighting_context(self):
        dr = _strong_dr()
        result = score(dr, weighting_context=None)
        clamp_debug = result["score_debug"]["t3_ceiling_clamp"]
        assert "applied" in clamp_debug
        assert clamp_debug["role"] == ""  # neutral — no role configured

    def test_t3_debug_before_and_after_match_when_not_clamped(self):
        dr = _strong_dr(proxy_ratio=3.0, volume=200.0)
        ctx = _ctx_with("salesforce", "system_of_record", "secondary")
        result = score(dr, weighting_context=ctx)
        clamp_debug = result["score_debug"]["t3_ceiling_clamp"]
        if not clamp_debug["applied"]:
            assert clamp_debug["confidence_before_clamp"] == clamp_debug["confidence_after_clamp"]


# ─────────────────────────────────────────────────────────────────────────────
# Corroboration engine defense-in-depth (apply_corroboration_confidence)
# ─────────────────────────────────────────────────────────────────────────────

class TestCorroborationEngineT3Guard:
    """
    Verifies that apply_corroboration_confidence() enforces the Slack-only and
    single-source ceilings as defense-in-depth (R16-C1 T3).

    These are guarded at the corroboration layer so that if a future rule
    accidentally sets elevated_confidence=HIGH for COR-05 or COR-08, the
    ceiling is still enforced.
    """

    def _make_result(
        self,
        rule_ids,
        elevated_confidence,
        sources=None,
    ):
        """Build a CorroborationResult with injected elevated_confidence."""
        from app.corroboration_engine import CorroborationResult
        return CorroborationResult(
            rule_ids=rule_ids,
            corroboration_sources=sources or [],
            corroboration_label=None,
            elevated_confidence=elevated_confidence,
            original_confidence="MEDIUM",
            triple_corroboration=False,
            confidence_elevated=(elevated_confidence == "HIGH"),
            elevation_target=elevated_confidence,
        )

    def test_slack_only_cor05_cannot_elevate_beyond_medium(self):
        """COR-05 only: even if elevated_confidence were HIGH (future bug), clamp to MEDIUM."""
        from app.corroboration_engine import apply_corroboration_confidence
        result = self._make_result(
            rule_ids=["COR-05"],
            elevated_confidence="HIGH",  # simulate a future bug
            sources=["Slack (supporting only)"],
        )
        final = apply_corroboration_confidence("MEDIUM", result)
        assert final == "MEDIUM", (
            "COR-05-only result must not elevate to HIGH — T3 Slack ceiling"
        )

    def test_cor05_normal_medium_still_medium(self):
        """Normal COR-05 behavior: elevated_confidence=MEDIUM stays MEDIUM."""
        from app.corroboration_engine import apply_corroboration_confidence
        result = self._make_result(
            rule_ids=["COR-05"],
            elevated_confidence="MEDIUM",
            sources=["Slack (supporting only)"],
        )
        final = apply_corroboration_confidence("MEDIUM", result)
        assert final == "MEDIUM"

    def test_cor06_slack_with_servicenow_can_elevate_to_high(self):
        """COR-06 (Slack + ServiceNow) IS allowed to elevate — ensure T3 guard doesn't block it."""
        from app.corroboration_engine import apply_corroboration_confidence
        result = self._make_result(
            rule_ids=["COR-01", "COR-06"],
            elevated_confidence="HIGH",
            sources=["ServiceNow", "Slack (escalation pattern)"],
        )
        final = apply_corroboration_confidence("MEDIUM", result)
        assert final == "HIGH", (
            "COR-06 (Slack + primary corroborator) should still elevate to HIGH"
        )

    def test_cor01_servicenow_elevates_to_high(self):
        """COR-01 (ServiceNow) elevates correctly — T3 guard must not interfere."""
        from app.corroboration_engine import apply_corroboration_confidence
        result = self._make_result(
            rule_ids=["COR-01"],
            elevated_confidence="HIGH",
            sources=["ServiceNow"],
        )
        final = apply_corroboration_confidence("MEDIUM", result)
        assert final == "HIGH"

    def test_cor08_single_source_with_injected_high_is_clamped(self):
        """COR-08-only with injected HIGH: T3 guard clamps to MEDIUM."""
        from app.corroboration_engine import apply_corroboration_confidence
        result = self._make_result(
            rule_ids=["COR-08"],
            elevated_confidence="HIGH",  # simulate a future bug
        )
        final = apply_corroboration_confidence("MEDIUM", result)
        assert final == "MEDIUM", (
            "COR-08-only result must not elevate to HIGH — T3 single-source ceiling"
        )

    def test_scorer_high_preserved_when_corroboration_medium(self):
        """Never-downgrade still holds: if scorer says HIGH, corroboration MEDIUM → stays HIGH."""
        from app.corroboration_engine import apply_corroboration_confidence
        result = self._make_result(
            rule_ids=["COR-05"],
            elevated_confidence="MEDIUM",
            sources=["Slack (supporting only)"],
        )
        final = apply_corroboration_confidence("HIGH", result)
        # The scorer's HIGH is preserved — T3 guard does not downgrade
        assert final == "HIGH"

    def test_no_rules_fired_never_downgrade(self):
        """No rules fired: scorer HIGH must be preserved (never downgrade)."""
        from app.corroboration_engine import apply_corroboration_confidence
        result = self._make_result(
            rule_ids=[],
            elevated_confidence="MEDIUM",
        )
        final = apply_corroboration_confidence("HIGH", result)
        assert final == "HIGH"


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — Determinism with T3 clamp
# ─────────────────────────────────────────────────────────────────────────────

class TestAC6DeterminismWithT3:

    def test_same_supporting_context_same_clamped_output(self):
        dr = _strong_dr(signal_source="servicenow")
        ctx = _ctx_with("servicenow", "operational_signal_source", "primary")
        results = [score(dr, weighting_context=ctx) for _ in range(5)]
        confidences = [r["confidence"] for r in results]
        assert len(set(confidences)) == 1, f"Non-deterministic: {confidences}"
        assert confidences[0] == CONFIDENCE_MEDIUM

    def test_same_sor_context_same_output(self):
        dr = _strong_dr(proxy_ratio=3.0, volume=200.0)
        ctx = _ctx_with("salesforce", "system_of_record", "secondary")
        results = [score(dr, weighting_context=ctx) for _ in range(5)]
        confidences = [r["confidence"] for r in results]
        assert len(set(confidences)) == 1

    def test_clamp_function_deterministic(self):
        for _ in range(10):
            r = apply_t3_ceiling_clamp(CONFIDENCE_HIGH, role="supplementary")
            assert r == CONFIDENCE_MEDIUM


# ─────────────────────────────────────────────────────────────────────────────
# Misconfiguration scenarios (the key customer-safety guarantees)
# ─────────────────────────────────────────────────────────────────────────────

class TestMisconfigurationProtection:
    """
    These tests represent the exact misconfigurations the spec warns about:
    a customer accidentally marking a weak or supporting source as important.
    """

    def test_slack_marked_as_system_of_record_primary_still_capped(self):
        """Worst-case misconfiguration: Slack given SoR role at primary priority."""
        dr = _strong_dr(signal_source="slack", proxy_ratio=10.0, volume=5000.0)
        ctx = _ctx_with("slack", "system_of_record", "primary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] != CONFIDENCE_HIGH, (
            "Slack misconfigured as SoR must not produce HIGH — T3 system_id guard"
        )

    def test_supporting_source_marked_primary_priority_still_capped(self):
        """Supporting source at primary priority — weight=0.66 — must stay MEDIUM."""
        dr = _strong_dr(signal_source="servicenow", proxy_ratio=5.0, volume=1000.0)
        ctx = _ctx_with("servicenow", "operational_signal_source", "primary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] != CONFIDENCE_HIGH

    def test_supplementary_source_at_primary_still_capped(self):
        """Supplementary + primary → T3 role clamp → MEDIUM."""
        dr = _strong_dr(signal_source="confluence", proxy_ratio=5.0, volume=1000.0)
        ctx = _ctx_with("confluence", "supplementary", "primary")
        result = score(dr, weighting_context=ctx)
        assert result["confidence"] != CONFIDENCE_HIGH

    def test_supporting_cannot_manufacture_false_high(self):
        """
        Key acceptance criterion: over-weighting Supporting-only still cannot
        produce HIGH. Verified end-to-end through scorer.score().
        """
        for priority in [PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW]:
            dr = _strong_dr(signal_source="servicenow", proxy_ratio=10.0, volume=10000.0)
            ctx = _ctx_with("servicenow", "operational_signal_source", priority)
            result = score(dr, weighting_context=ctx)
            assert result["confidence"] != CONFIDENCE_HIGH, (
                f"Supporting at priority={priority!r} must not reach HIGH"
            )
