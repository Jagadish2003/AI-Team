"""
test_r16_c1_t1_weighting_context.py

R16-C1 T1 — Tests: Stack Builder weighting context is persisted, loaded,
and available to the scorer and corroboration engine during discovery.

Acceptance criteria (from task description):
  AC1 - load_for_run() returns a StackBuilderWeightingContext with correct role
        and priority for every system in the persisted setup_context.
  AC2 - load_for_run() on a run_id with no setup_context returns a neutral
        context (is_neutral=True) without raising.
  AC3 - The scorer receives the weighting context and records it in score_debug.
  AC4 - The corroboration engine accepts weighting_context without raising.
  AC5 - Backward compat: score() called without weighting_context still works
        and score_debug["source_weighting"] is None.
  AC6 - Context covers ALL selected systems, not only the primary.
  AC7 - load_for_run() is non-blocking: any KV read error returns neutral.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import patch

import pytest

from discovery.weighting_context import (
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
    ROLE_PRIMARY,
    ROLE_SUPPORTING,
    StackBuilderWeightingContext,
    SystemWeighting,
    load_for_run,
)
from discovery.scorer import score
from discovery.models import DetectorResult


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

_SETUP_CONTEXT: Dict[str, Any] = {
    "org_id": "test_org_r16c1",
    "pack_id": "service_cloud",
    "focus_id": "approvals_compliance",
    "industry_id": "financial_services",
    "template_id": None,
    "selected_system_ids": ["salesforce", "servicenow", "jira"],
    "weightings": {
        "salesforce": {
            "systemId": "salesforce",
            "role": ROLE_PRIMARY,
            "priority": PRIORITY_HIGH,
            "workflowFocus": ["case_management", "approvals"],
            "confirmed": True,
        },
        "servicenow": {
            "systemId": "servicenow",
            "role": ROLE_SUPPORTING,
            "priority": PRIORITY_MEDIUM,
            "workflowFocus": ["incident_management"],
            "confirmed": True,
        },
        "jira": {
            "systemId": "jira",
            "role": ROLE_SUPPORTING,
            "priority": PRIORITY_MEDIUM,
            "workflowFocus": ["backlog_work_queues"],
            "confirmed": False,
        },
    },
}


def _make_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:8]}"


def _minimal_detector_result(signal_source: str = "salesforce") -> DetectorResult:
    return DetectorResult(
        detector_id="HANDOFF_FRICTION",
        signal_source=signal_source,
        metric_value=3.5,
        threshold=2.0,
        raw_evidence={"total_cases_90d": 500, "handoff_score": 3.0},
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — load_for_run returns correct role/priority per system
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadForRun:

    def test_ac1_returns_correct_salesforce_role_and_priority(self):
        run_id = _make_run_id()
        with patch("discovery.weighting_context.run_kv_get", return_value=_SETUP_CONTEXT):
            ctx = load_for_run(run_id)

        assert not ctx.is_neutral
        sf = ctx.get("salesforce")
        assert sf.role == ROLE_PRIMARY
        assert sf.priority == PRIORITY_HIGH
        assert sf.confirmed is True

    def test_ac1_returns_correct_servicenow_weighting(self):
        run_id = _make_run_id()
        with patch("discovery.weighting_context.run_kv_get", return_value=_SETUP_CONTEXT):
            ctx = load_for_run(run_id)

        sn = ctx.get("servicenow")
        assert sn.role == ROLE_SUPPORTING
        assert sn.priority == PRIORITY_MEDIUM

    def test_ac1_run_id_stored_on_context(self):
        run_id = _make_run_id()
        with patch("discovery.weighting_context.run_kv_get", return_value=_SETUP_CONTEXT):
            ctx = load_for_run(run_id)

        assert ctx.run_id == run_id

    def test_ac1_pack_id_stored_on_context(self):
        run_id = _make_run_id()
        with patch("discovery.weighting_context.run_kv_get", return_value=_SETUP_CONTEXT):
            ctx = load_for_run(run_id)

        assert ctx.pack_id == "service_cloud"

    # AC6 — all selected systems present ─────────────────────────────────────

    def test_ac6_all_systems_in_weightings(self):
        run_id = _make_run_id()
        with patch("discovery.weighting_context.run_kv_get", return_value=_SETUP_CONTEXT):
            ctx = load_for_run(run_id)

        assert "salesforce" in ctx.weightings
        assert "servicenow" in ctx.weightings
        assert "jira" in ctx.weightings
        assert len(ctx.weightings) == 3

    def test_ac6_selected_system_ids_preserved(self):
        run_id = _make_run_id()
        with patch("discovery.weighting_context.run_kv_get", return_value=_SETUP_CONTEXT):
            ctx = load_for_run(run_id)

        assert set(ctx.selected_system_ids) == {"salesforce", "servicenow", "jira"}


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — neutral fallback for runs without setup_context
# ─────────────────────────────────────────────────────────────────────────────

class TestNeutralFallback:

    def test_ac2_missing_context_returns_neutral(self):
        run_id = _make_run_id()
        with patch("discovery.weighting_context.run_kv_get", return_value=None):
            ctx = load_for_run(run_id)

        assert ctx.is_neutral is True
        assert ctx.weightings == {}

    def test_ac2_empty_run_id_returns_neutral(self):
        ctx = load_for_run("")
        assert ctx.is_neutral is True

    def test_ac2_none_run_id_returns_neutral(self):
        ctx = load_for_run(None)  # type: ignore[arg-type]
        assert ctx.is_neutral is True

    def test_ac2_non_dict_context_returns_neutral(self):
        run_id = _make_run_id()
        with patch("discovery.weighting_context.run_kv_get", return_value="bad_value"):
            ctx = load_for_run(run_id)

        assert ctx.is_neutral is True

    def test_ac2_neutral_get_returns_neutral_weighting(self):
        ctx = StackBuilderWeightingContext.neutral()
        w = ctx.get("salesforce")
        assert w.is_neutral is True
        assert w.role == ""
        assert w.priority == ""


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — load_for_run is non-blocking on KV errors
# ─────────────────────────────────────────────────────────────────────────────

class TestNonBlocking:

    def test_ac7_runtime_error_returns_neutral(self):
        run_id = _make_run_id()
        with patch(
            "discovery.weighting_context.run_kv_get",
            side_effect=RuntimeError("DB failed"),
        ):
            ctx = load_for_run(run_id)

        assert ctx.is_neutral is True

    def test_ac7_import_error_returns_neutral(self):
        run_id = _make_run_id()
        with patch(
            "discovery.weighting_context.run_kv_get",
            side_effect=ImportError("module not found"),
        ):
            ctx = load_for_run(run_id)

        assert ctx.is_neutral is True


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — scorer receives weighting context and records it in score_debug
# ─────────────────────────────────────────────────────────────────────────────

class TestScorerWeightingContext:

    def test_ac3_score_debug_contains_source_weighting(self):
        run_id = _make_run_id()
        dr = _minimal_detector_result(signal_source="salesforce")

        with patch("discovery.weighting_context.run_kv_get", return_value=_SETUP_CONTEXT):
            ctx = load_for_run(run_id)

        result = score(dr, weighting_context=ctx)
        sw = result["score_debug"]["source_weighting"]

        assert sw is not None
        assert sw["system_id"] == "salesforce"
        assert sw["role"] == ROLE_PRIMARY
        assert sw["priority"] == PRIORITY_HIGH
        assert sw["confirmed"] is True

    def test_ac3_supporting_system_weighting_captured(self):
        run_id = _make_run_id()
        dr = _minimal_detector_result(signal_source="servicenow")

        with patch("discovery.weighting_context.run_kv_get", return_value=_SETUP_CONTEXT):
            ctx = load_for_run(run_id)

        result = score(dr, weighting_context=ctx)
        sw = result["score_debug"]["source_weighting"]
        assert sw["role"] == ROLE_SUPPORTING
        assert sw["priority"] == PRIORITY_MEDIUM

    # AC5 — backward compat ───────────────────────────────────────────────────

    def test_ac5_no_weighting_context_still_scores(self):
        dr = _minimal_detector_result()
        result = score(dr)

        assert result["impact"] >= 1
        assert result["confidence"] in ("HIGH", "MEDIUM", "LOW")
        assert result["score_debug"]["source_weighting"] is None

    def test_ac5_neutral_context_gives_none_debug(self):
        dr = _minimal_detector_result()
        ctx = StackBuilderWeightingContext.neutral()
        result = score(dr, weighting_context=ctx)

        assert result["score_debug"]["source_weighting"] is None

    def test_ac5_impact_unchanged_with_weighting_context(self):
        """T1 must not change scoring — modulation is T2 work."""
        dr = _minimal_detector_result()
        without = score(dr)
        ctx = StackBuilderWeightingContext.neutral()
        with_ctx = score(dr, weighting_context=ctx)

        assert without["impact"] == with_ctx["impact"]
        assert without["confidence"] == with_ctx["confidence"]


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — corroboration engine accepts weighting_context without raising
# ─────────────────────────────────────────────────────────────────────────────

class TestCorroborationEngineWeightingContext:

    def _make_run_data(self):
        return {
            "connected_systems": ["salesforce", "servicenow"],
            "servicenow": {
                "incidents": [
                    {
                        "sys_created_on": datetime.now(timezone.utc).isoformat(),
                        "state": "Open",
                        "detector_ids": ["HANDOFF_FRICTION"],
                    }
                ]
            },
        }

    def test_ac4_corroboration_accepts_weighting_context(self):
        run_id = _make_run_id()
        with patch("discovery.weighting_context.run_kv_get", return_value=_SETUP_CONTEXT):
            ctx = load_for_run(run_id)

        try:
            from app.corroboration_engine import evaluate_corroboration
        except ModuleNotFoundError:
            from backend.app.corroboration_engine import evaluate_corroboration

        result = evaluate_corroboration(
            detector_id="HANDOFF_FRICTION",
            pack_id="service_cloud",
            run_data=self._make_run_data(),
            run_timestamp=datetime.now(timezone.utc),
            org_id="test_org",
            weighting_context=ctx,
        )

        assert result is not None
        assert result.elevated_confidence in ("HIGH", "MEDIUM", "LOW")

    def test_ac4_corroboration_with_none_weighting_context_unchanged(self):
        try:
            from app.corroboration_engine import evaluate_corroboration
        except ModuleNotFoundError:
            from backend.app.corroboration_engine import evaluate_corroboration

        result = evaluate_corroboration(
            detector_id="HANDOFF_FRICTION",
            pack_id="service_cloud",
            run_data=self._make_run_data(),
            run_timestamp=datetime.now(timezone.utc),
            org_id="test_org",
            weighting_context=None,
        )

        assert result is not None

    def test_ac4_corroboration_with_neutral_context(self):
        try:
            from app.corroboration_engine import evaluate_corroboration
        except ModuleNotFoundError:
            from backend.app.corroboration_engine import evaluate_corroboration

        ctx = StackBuilderWeightingContext.neutral()
        result = evaluate_corroboration(
            detector_id="HANDOFF_FRICTION",
            pack_id="service_cloud",
            run_data=self._make_run_data(),
            run_timestamp=datetime.now(timezone.utc),
            org_id="test_org",
            weighting_context=ctx,
        )

        assert result is not None


# ─────────────────────────────────────────────────────────────────────────────
# SystemWeighting property tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSystemWeightingProperties:

    def test_is_primary_system_true(self):
        w = SystemWeighting(system_id="sf", role=ROLE_PRIMARY, priority=PRIORITY_HIGH)
        assert w.is_primary_system is True

    def test_is_primary_system_false_for_secondary(self):
        w = SystemWeighting(system_id="sn", role=ROLE_SUPPORTING, priority=PRIORITY_MEDIUM)
        assert w.is_primary_system is False

    def test_is_supporting_system_true(self):
        w = SystemWeighting(system_id="sn", role=ROLE_SUPPORTING)
        assert w.is_supporting_system is True

    def test_is_neutral_true_when_empty(self):
        w = SystemWeighting(system_id="unknown")
        assert w.is_neutral is True

    def test_is_neutral_false_when_role_set(self):
        w = SystemWeighting(system_id="sf", role=ROLE_PRIMARY)
        assert w.is_neutral is False


# ─────────────────────────────────────────────────────────────────────────────
# StackBuilderWeightingContext helper method tests
# ─────────────────────────────────────────────────────────────────────────────

class TestWeightingContextHelpers:

    def _make_ctx(self) -> StackBuilderWeightingContext:
        run_id = _make_run_id()
        with patch("discovery.weighting_context.run_kv_get", return_value=_SETUP_CONTEXT):
            return load_for_run(run_id)

    def test_has_weighting_for_known_system(self):
        ctx = self._make_ctx()
        assert ctx.has_weighting_for("salesforce") is True

    def test_has_weighting_for_unknown_system_false(self):
        ctx = self._make_ctx()
        assert ctx.has_weighting_for("slack") is False

    def test_get_unknown_system_returns_neutral(self):
        ctx = self._make_ctx()
        w = ctx.get("slack")
        assert w.is_neutral is True

    def test_to_debug_dict_includes_all_systems(self):
        ctx = self._make_ctx()
        debug = ctx.to_debug_dict()
        assert "salesforce" in debug["system_weightings"]
        assert "servicenow" in debug["system_weightings"]
        assert "jira" in debug["system_weightings"]

    def test_to_debug_dict_not_neutral(self):
        ctx = self._make_ctx()
        assert ctx.to_debug_dict()["is_neutral"] is False

    def test_neutral_to_debug_dict_is_neutral_true(self):
        ctx = StackBuilderWeightingContext.neutral()
        assert ctx.to_debug_dict()["is_neutral"] is True
