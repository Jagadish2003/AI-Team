"""
R16-C2 T3: emphasis, not exclusion.

Focus is a lens, not a blindfold. These tests prove that a selected focus can
emphasise matching findings without burying a HIGH, well-corroborated finding
that sits outside the chosen lens.
"""
from __future__ import annotations

from discovery.calibration.ranking import (
    is_high_well_corroborated,
    rank_opportunities,
)
from discovery.packs.focus_affinity import build_focus_emphasis


def _opp(detector_id: str, tier: str, impact: int, effort: int, **extra):
    opp = {
        "detector_id": detector_id,
        "tier": tier,
        "impact": impact,
        "effort": effort,
        "confidence": "MEDIUM",
    }
    opp.update(extra)
    return opp


def _ids(opps):
    return [o["detector_id"] for o in opps]


def _approval_focus_dataset():
    focus_id = "approvals_compliance"
    ordinary_in_focus = [
        _opp("APPROVAL_BOTTLENECK", "Quick Win", 7, 2),
        _opp("PERMISSION_BOTTLENECK", "Quick Win", 6, 2),
        _opp("COVENANT_TRACKING_GAP", "Strategic", 8, 4),
        _opp("DB_SLA_BREACH_RATE", "Strategic", 7, 3),
        _opp("ENT_SLA_BREACH_BY_TEAM", "Strategic", 6, 3),
        _opp("BENEFIT_ELECTION_DEADLINE", "Complex", 8, 5),
    ]
    protected_out_of_focus = _opp(
        "HANDOFF_FRICTION",
        "Complex",
        9,
        5,
        confidence="HIGH",
        corroboration_sources=["ServiceNow", "Jira"],
        corroboration_label="Triple corroboration: Salesforce + ServiceNow + Jira",
        triple_corroboration=True,
        corroboration_rule_ids=["COR-01", "COR-02", "COR-03"],
    )
    opps = ordinary_in_focus + [protected_out_of_focus]
    for opp in opps:
        opp["focus_emphasis"] = build_focus_emphasis(focus_id, opp["detector_id"])
    return opps


def test_out_of_focus_high_corroborated_finding_is_still_surfaced_in_top_five():
    opps = _approval_focus_dataset()

    ranked = rank_opportunities(opps, focus_id="approvals_compliance")
    order = _ids(ranked)

    assert "HANDOFF_FRICTION" in order
    assert order.index("HANDOFF_FRICTION") < 5
    assert order[0] == "HANDOFF_FRICTION"


def test_focus_guardrail_preserves_the_full_eligible_result_set():
    opps = _approval_focus_dataset()

    ranked = rank_opportunities(opps, focus_id="approvals_compliance")

    assert sorted(_ids(ranked)) == sorted(_ids(opps))
    assert len(ranked) == len(opps)


def test_only_high_with_real_corroboration_receives_surface_guardrail():
    assert is_high_well_corroborated(
        {
            "confidence": "HIGH",
            "corroboration_rule_ids": ["COR-01"],
            "corroboration_sources": ["ServiceNow"],
        }
    )
    assert not is_high_well_corroborated(
        {
            "confidence": "HIGH",
            "corroboration_rule_ids": ["COR-05"],
            "corroboration_sources": ["Slack (supporting only)"],
        }
    )
    assert not is_high_well_corroborated(
        {
            "confidence": "MEDIUM",
            "corroboration_rule_ids": ["COR-01"],
            "corroboration_sources": ["ServiceNow"],
        }
    )
