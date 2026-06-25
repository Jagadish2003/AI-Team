"""
test_r16_c1_corroboration_authority.py

R16-C1 — Unit tests for the corroboration *lead authority* gate
(corroboration_engine._has_lead_authority).

Background
----------
A single corroborating source may only drive a MEDIUM → HIGH corroboration
elevation when its weighted authority reaches the lead threshold
``_CORROBORATION_LEAD_WEIGHT_MIN`` (0.80). The T6 contract suite exercises this
end-to-end through workflow_system (weight 0.80) vs operational_signal_source
(weight 0.60), but never pins the boundary itself: that exactly-0.80 passes,
that 0.79 does NOT, and that the comparison is ``>=`` (inclusive) rather than
``>`` (exclusive).

A boundary off-by-one here silently shifts *who* can drive a HIGH corroboration
— exactly the customer-visible wrong-data class this system was built to
prevent. These unit tests lock the boundary directly.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from discovery.weighting_context import (
    StackBuilderWeightingContext,
    SystemWeighting,
)

try:
    from app.corroboration_engine import (
        _CORROBORATION_LEAD_WEIGHT_MIN,
        _has_lead_authority,
        _weighting_info_for_system,
    )
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.corroboration_engine import (  # type: ignore[no-redef]
        _CORROBORATION_LEAD_WEIGHT_MIN,
        _has_lead_authority,
        _weighting_info_for_system,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stubs — let us put a source_weight EXACTLY on / around the boundary, which no
# real role/priority combination produces (the real grid is discrete: 0.54,
# 0.60, 0.66, 0.72, 0.80, 0.88, 0.90, 1.0, 1.10).
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _StubWeighting:
    source_weight: float
    role: str = "stub_role"
    priority: str = "stub_priority"
    is_neutral: bool = False


@dataclass
class _StubContext:
    """Minimal weighting context whose .get() returns a fixed-weight stub."""
    weight: float
    is_neutral: bool = False

    def get(self, system_id: str) -> _StubWeighting:  # noqa: ARG002 - id irrelevant
        return _StubWeighting(source_weight=self.weight)


def _ctx_with_weight(weight: float) -> _StubContext:
    return _StubContext(weight=weight)


# ─────────────────────────────────────────────────────────────────────────────
# The threshold itself
# ─────────────────────────────────────────────────────────────────────────────

class TestLeadWeightThresholdConstant:

    def test_threshold_is_080(self):
        assert _CORROBORATION_LEAD_WEIGHT_MIN == 0.80


# ─────────────────────────────────────────────────────────────────────────────
# Exact boundary — 0.79 / 0.80 / 0.81, proving the >= (inclusive) operator
# ─────────────────────────────────────────────────────────────────────────────

class TestLeadAuthorityBoundary:

    def test_weight_exactly_080_has_lead_authority(self):
        # Inclusive boundary: exactly the threshold must PASS (>= not >).
        assert _has_lead_authority(_ctx_with_weight(0.80), "servicenow") is True

    def test_weight_079_does_not_have_lead_authority(self):
        # One step below the threshold must NOT lead.
        assert _has_lead_authority(_ctx_with_weight(0.79), "servicenow") is False

    def test_weight_081_has_lead_authority(self):
        assert _has_lead_authority(_ctx_with_weight(0.81), "servicenow") is True

    def test_weight_just_below_threshold_does_not_lead(self):
        # Float just under 0.80 must fail — guards against a `>` vs `>=` slip the
        # other way (treating values infinitesimally below 0.80 as eligible).
        assert _has_lead_authority(_ctx_with_weight(0.7999999), "servicenow") is False

    def test_operator_is_inclusive_not_exclusive(self):
        """`>=` (inclusive) is correct; a `>` regression would fail this."""
        info = _weighting_info_for_system(_ctx_with_weight(_CORROBORATION_LEAD_WEIGHT_MIN), "servicenow")
        assert info["source_weight"] == pytest.approx(_CORROBORATION_LEAD_WEIGHT_MIN)
        assert info["lead_authority"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Tie the boundary to the REAL role/priority → weight mapping, so the contract
# split (workflow_system leads, operational_signal_source does not) is anchored
# to the 0.80 threshold rather than to a magic number.
# ─────────────────────────────────────────────────────────────────────────────

class TestLeadAuthorityWithRealMapping:

    def _ctx(self, system_id: str, role: str, priority: str) -> StackBuilderWeightingContext:
        return StackBuilderWeightingContext(
            weightings={system_id: SystemWeighting(system_id=system_id, role=role, priority=priority)},
            selected_system_ids=[system_id],
            is_neutral=False,
        )

    def test_workflow_system_secondary_is_exactly_080_and_leads(self):
        # workflow_system (0.8) × secondary (1.0) = 0.80 → exactly the threshold.
        ctx = self._ctx("servicenow", "workflow_system", "secondary")
        assert ctx.get("servicenow").source_weight == pytest.approx(0.80)
        assert _has_lead_authority(ctx, "servicenow") is True

    def test_operational_signal_source_secondary_is_060_and_cannot_lead(self):
        # operational_signal_source (0.6) × secondary (1.0) = 0.60 → below 0.80.
        ctx = self._ctx("servicenow", "operational_signal_source", "secondary")
        assert ctx.get("servicenow").source_weight == pytest.approx(0.60)
        assert _has_lead_authority(ctx, "servicenow") is False

    def test_workflow_system_tertiary_is_072_and_cannot_lead(self):
        # workflow_system (0.8) × tertiary (0.9) = 0.72 → just below the threshold.
        ctx = self._ctx("jira", "workflow_system", "tertiary")
        assert ctx.get("jira").source_weight == pytest.approx(0.72)
        assert _has_lead_authority(ctx, "jira") is False

    def test_workflow_system_primary_is_088_and_leads(self):
        # workflow_system (0.8) × primary (1.1) = 0.88 → above the threshold.
        ctx = self._ctx("servicenow", "workflow_system", "primary")
        assert ctx.get("servicenow").source_weight == pytest.approx(0.88)
        assert _has_lead_authority(ctx, "servicenow") is True

    def test_system_of_record_leads(self):
        ctx = self._ctx("salesforce", "system_of_record", "secondary")
        assert _has_lead_authority(ctx, "salesforce") is True


# ─────────────────────────────────────────────────────────────────────────────
# Backward-compat: neutral / missing context grants lead authority (the
# pre-weighting behaviour where every source could lead), so older runs are
# unaffected.
# ─────────────────────────────────────────────────────────────────────────────

class TestLeadAuthorityNeutralFallback:

    def test_none_context_leads(self):
        assert _has_lead_authority(None, "servicenow") is True

    def test_neutral_context_leads(self):
        assert _has_lead_authority(StackBuilderWeightingContext.neutral(), "servicenow") is True

    def test_unconfigured_system_leads(self):
        ctx = StackBuilderWeightingContext(
            weightings={"salesforce": SystemWeighting(system_id="salesforce", role="system_of_record", priority="primary")},
            selected_system_ids=["salesforce"],
            is_neutral=False,
        )
        # "servicenow" has no configured weighting → neutral sentinel (weight 1.0) → leads.
        assert _has_lead_authority(ctx, "servicenow") is True
