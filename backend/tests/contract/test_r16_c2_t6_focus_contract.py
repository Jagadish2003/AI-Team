"""
test_r16_c2_t6_focus_contract.py

R16-C2 T6 — Contract tests per Section 5 (the complete suite).

Proves that Discovery Focus is now a REAL behavioural input, not just persisted
setup metadata. A Stack Builder configuration launched through
POST /api/stack-builder/launch persists the chosen focus_id; the discovery
layer reads it back (focus_affinity.load_focus_for_run) and applies it as
emphasis through the shared ranking path (track_a_adapter.export_track_a_seed →
calibration.ranking.rank_opportunities). These tests exercise that real
API → database → loader → ranking path (no run_kv_get mocking).

This is the comprehensive Section-5 deliverable. It complements
test_r16_c2_t2_focus_ranking.py by covering EVERY affinity focus, and adds the
decisive negative guard: the assertions FAIL if focus_id is ignored — because a
focus-ignoring implementation would order identically to the unbiased view.

Acceptance criteria covered (R16-C2 Section 5):
  AC1 - Identical data under two different focuses produces visibly different
        emphasis/ranking.
  AC2 - A focus emphasises its matching findings — they rank higher than they
        would under enterprise_wide.
  AC3 - Emphasis is not exclusion: a HIGH, well-corroborated finding outside the
        focus is still surfaced (here, floated to the top by the guardrail).
  AC4 - enterprise_wide applies no affinity bias — the full, unweighted view.
  AC5 - Deterministic: same focus + same data => identical ordering.
  AC7 - The behaviour change is real, not cosmetic — AC1 demonstrably passes and
        the per-run focus annotation flips with the chosen focus.

(AC6 — the tile boundary copy — is a frontend design-review criterion delivered
in T5; it has no backend contract assertion.)
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import run_kv_get

from discovery.packs.focus_affinity import (
    FOCUS_AFFINITY,
    FOCUS_EMPHASIS_RANK,
    FOCUS_NEUTRAL_RANK,
    build_focus_emphasis,
    detector_matches_focus,
    load_focus_for_run,
)
from discovery.calibration.ranking import rank_opportunities
from discovery.track_a_adapter import export_track_a_seed


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


# ── The seven canonical focuses, and the six that carry an affinity bias ────────

ALL_FOCUSES = sorted(FOCUS_AFFINITY.keys())
AFFINITY_FOCUSES = sorted(f for f, a in FOCUS_AFFINITY.items() if a is not None)

#: One representative detector that is genuinely emphasised by each focus
#: (verified against FOCUS_AFFINITY at import time below). Used as the finding
#: whose ranking must move when that focus is chosen.
AFFINITY_REP: Dict[str, str] = {
    "member_customer_service": "APPLICATION_STALL",
    "core_operations": "DB_QUEUE_DEPTH_ELEVATED",
    "approvals_compliance": "APPROVAL_BOTTLENECK",
    "cross_system_handoffs": "HANDOFF_FRICTION",
    "back_office_productivity": "CHECKLIST_BOTTLENECK",
    "engineering_change": "GITHUB_PR_REVIEW_BOTTLENECK",
}

#: A detector id that no focus emphasises (unknown ids degrade to "no match").
#: It is the neutral filler whose ranking is unaffected by any focus.
NEUTRAL_DETECTOR = "ZZ_UNRELATED_NEUTRAL"


def test_affinity_reps_are_actually_in_their_focus():
    """Guard the fixtures: each representative truly belongs to its focus, and
    the neutral filler belongs to none. If FOCUS_AFFINITY changes, this fails
    loudly instead of letting the behavioural tests pass vacuously."""
    for focus, det in AFFINITY_REP.items():
        assert detector_matches_focus(focus, det), f"{det} not emphasised by {focus}"
    for focus in ALL_FOCUSES:
        assert not detector_matches_focus(focus, NEUTRAL_DETECTOR)


# ── Launch + ranking helpers ────────────────────────────────────────────────────

def _launch(client: TestClient, *, focus_id: str) -> str:
    """Launch a run through the real endpoint with the given focus; return id.

    Everything except focus_id is held identical between calls, so any change in
    ordering can only come from the focus.
    """
    body: Dict[str, Any] = {
        # Required by the schema; ignored by tenancy (org_id comes from the JWT).
        "org_id": "body-org-ignored-by-tenancy",
        "focus_id": focus_id,
        "industry_id": "financial_services",
        "template_id": None,
        "selected_system_ids": ["salesforce", "servicenow"],
        "pack_id": "service_cloud",
        "weightings": {
            "salesforce": {
                "systemId": "salesforce",
                "role": "system_of_record",
                "priority": "primary",
                "workflowFocus": ["approvals"],
                "confirmed": True,
            },
            "servicenow": {
                "systemId": "servicenow",
                "role": "operational_signal_source",
                "priority": "secondary",
                "workflowFocus": ["handoffs_routing"],
                "confirmed": True,
            },
        },
    }
    resp = client.post("/api/stack-builder/launch", headers=_auth(), json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["runId"]


def _make_opps(specs: List[Dict[str, Any]], focus: Optional[str]) -> List[Dict[str, Any]]:
    """Build opportunity dicts annotated with the focus_emphasis the runner would
    attach for ``focus`` — exactly what the persisted run carries into ranking."""
    opps = []
    for s in specs:
        opp = {
            "detector_id": s["det"],
            "tier": s.get("tier", "Complex"),
            "impact": s.get("impact", 5),
            "effort": s.get("effort", 5),
            "confidence": s.get("confidence", "MEDIUM"),
            "metric_value": 3.0,
            "threshold": 2.0,
            "raw_evidence": {},
            "evidenceIds": [],
            "focus_emphasis": build_focus_emphasis(focus, s["det"]),
        }
        opp.update(s.get("extra", {}))
        opps.append(opp)
    return opps


def _seed_order(run_id: str, specs: List[Dict[str, Any]]) -> List[str]:
    """Annotate specs with the run's PERSISTED focus, run them through the real
    seed/ranking path, and return the resulting detector-id order."""
    focus = load_focus_for_run(run_id)
    payload = {"runId": run_id, "focusId": focus, "opportunities": _make_opps(specs, focus)}
    seed = export_track_a_seed(payload)
    return [o["_debug"]["detector_id"] for o in seed["opportunities"]]


# A fixed, identical opportunity set used across the headline tests. Each focus's
# affinity finding is placed at a LOW baseline (Complex tier) so that under the
# unbiased view it sorts last; its focus must lift it.
_BASE_SPECS = [
    {"det": "APPROVAL_BOTTLENECK", "tier": "Complex", "impact": 5, "effort": 5},
    {"det": "HANDOFF_FRICTION", "tier": "Quick Win", "impact": 8, "effort": 2},
    {"det": "DB_QUEUE_DEPTH_ELEVATED", "tier": "Strategic", "impact": 6, "effort": 3},
]


# ── Focus persists and round-trips for every tile ───────────────────────────────

@pytest.mark.parametrize("focus", ALL_FOCUSES)
def test_focus_id_persists_and_loads(client, focus):
    run_id = _launch(client, focus_id=focus)
    assert run_kv_get("focus_id", run_id) == focus
    assert load_focus_for_run(run_id) == focus


# ── AC1 / AC7 — different focus, different emphasis (the decisive case) ──────────

def test_two_focuses_produce_visibly_different_ordering(client):
    run_compliance = _launch(client, focus_id="approvals_compliance")
    run_handoffs = _launch(client, focus_id="cross_system_handoffs")

    order_compliance = _seed_order(run_compliance, _BASE_SPECS)
    order_handoffs = _seed_order(run_handoffs, _BASE_SPECS)

    assert order_compliance != order_handoffs
    assert order_compliance[0] == "APPROVAL_BOTTLENECK"
    assert order_handoffs[0] == "HANDOFF_FRICTION"


def test_a_third_focus_differs_again(client):
    """Three distinct focuses, three distinct top findings — not a two-way fluke."""
    run_core = _launch(client, focus_id="core_operations")
    order_core = _seed_order(run_core, _BASE_SPECS)
    assert order_core[0] == "DB_QUEUE_DEPTH_ELEVATED"


# ── AC2 — every affinity focus lifts its finding above the enterprise view ──────

@pytest.mark.parametrize("focus", AFFINITY_FOCUSES)
def test_focus_emphasises_its_affinity_above_enterprise(client, focus):
    rep = AFFINITY_REP[focus]
    # rep sits at a low baseline (Complex); a neutral Quick Win outranks it
    # under the unbiased view, and the focus must lift rep above it.
    specs = [
        {"det": rep, "tier": "Complex", "impact": 5, "effort": 5},
        {"det": NEUTRAL_DETECTOR, "tier": "Quick Win", "impact": 8, "effort": 2},
    ]

    run_enterprise = _launch(client, focus_id="enterprise_wide")
    run_focus = _launch(client, focus_id=focus)

    enterprise_order = _seed_order(run_enterprise, specs)
    focus_order = _seed_order(run_focus, specs)

    # Unbiased: rep is last. Focused: rep is first. The focus raised its rank.
    assert enterprise_order[-1] == rep, f"{focus}: expected rep last under enterprise"
    assert focus_order[0] == rep, f"{focus}: expected focus to lift rep to top"
    assert focus_order.index(rep) < enterprise_order.index(rep)


# ── The decisive negative guard: assertions FAIL if focus_id is ignored ─────────

@pytest.mark.parametrize("focus", AFFINITY_FOCUSES)
def test_ignoring_focus_would_break_these_tests(focus):
    """If the ranking ignored focus_id, the focus-aware and focus-unaware orders
    would be identical for every focus. Prove they differ for affinity focuses
    (and are identical for the unbiased path), so a focus-dropping regression
    is caught."""
    rep = AFFINITY_REP[focus]
    specs = [
        {"det": rep, "tier": "Complex", "impact": 5, "effort": 5},
        {"det": NEUTRAL_DETECTOR, "tier": "Quick Win", "impact": 8, "effort": 2},
    ]
    # Same opportunity objects, ranked focus-aware vs focus-unaware (None).
    opps = _make_opps(specs, focus)
    focus_aware = [o["detector_id"] for o in rank_opportunities(opps, focus_id=focus)]
    focus_blind = [o["detector_id"] for o in rank_opportunities(
        _make_opps(specs, None), focus_id=None
    )]

    assert focus_aware != focus_blind, (
        f"{focus}: focus made no difference — focus_id is being ignored"
    )
    assert focus_aware[0] == rep
    assert focus_blind[0] == NEUTRAL_DETECTOR


# ── AC3 — emphasis is NOT exclusion ─────────────────────────────────────────────

def test_high_corroborated_out_of_focus_finding_is_still_surfaced(client):
    """A HIGH, well-corroborated handoff finding must remain in the results —
    and float to the top — even under approvals_compliance focus."""
    run_compliance = _launch(client, focus_id="approvals_compliance")
    specs = [
        {"det": "APPROVAL_BOTTLENECK", "tier": "Quick Win", "impact": 7, "effort": 2},
        {"det": "PERMISSION_BOTTLENECK", "tier": "Quick Win", "impact": 6, "effort": 2},
        {"det": "COVENANT_TRACKING_GAP", "tier": "Strategic", "impact": 8, "effort": 4},
        {
            "det": "HANDOFF_FRICTION",  # OUTSIDE approvals_compliance focus
            "tier": "Complex",
            "impact": 9,
            "effort": 5,
            "extra": {
                "confidence": "HIGH",
                "corroboration_sources": ["ServiceNow", "Jira"],
                "corroboration_label": "Triple corroboration: Salesforce + ServiceNow + Jira",
                "triple_corroboration": True,
                "corroboration_rule_ids": ["COR-01", "COR-02", "COR-03"],
            },
        },
    ]

    order = _seed_order(run_compliance, specs)

    # Present (not hidden), and the guardrail floats it above focus emphasis.
    assert "HANDOFF_FRICTION" in order
    assert order[0] == "HANDOFF_FRICTION"
    assert len(order) == len(specs)


# ── AC4 — enterprise_wide is the unbiased baseline (no affinity bias) ───────────

def test_enterprise_wide_is_pure_tier_order(client):
    run_enterprise = _launch(client, focus_id="enterprise_wide")
    order = _seed_order(run_enterprise, _BASE_SPECS)
    # No focus bias → pure tier ordering: Quick Win, Strategic, Complex.
    assert order == ["HANDOFF_FRICTION", "DB_QUEUE_DEPTH_ELEVATED", "APPROVAL_BOTTLENECK"]


def test_enterprise_wide_annotation_is_neutral_for_every_detector():
    for det in (*AFFINITY_REP.values(), NEUTRAL_DETECTOR):
        fe = build_focus_emphasis("enterprise_wide", det)
        assert fe["matched"] is False
        assert fe["rank"] == FOCUS_NEUTRAL_RANK
        assert fe["affinity"] == []


def test_affinity_focus_annotation_emphasises_its_match():
    for focus, det in AFFINITY_REP.items():
        fe = build_focus_emphasis(focus, det)
        assert fe["matched"] is True
        assert fe["rank"] == FOCUS_EMPHASIS_RANK


# ── AC5 — determinism ────────────────────────────────────────────────────────────

def test_same_focus_same_data_is_deterministic(client):
    run_a = _launch(client, focus_id="approvals_compliance")
    run_b = _launch(client, focus_id="approvals_compliance")

    assert _seed_order(run_a, _BASE_SPECS) == _seed_order(run_b, _BASE_SPECS)
    # Repeated ranking of the same run is stable too.
    assert _seed_order(run_a, _BASE_SPECS) == _seed_order(run_a, _BASE_SPECS)


@pytest.mark.parametrize("focus", AFFINITY_FOCUSES)
def test_ranking_primitive_is_deterministic(focus):
    rep = AFFINITY_REP[focus]
    specs = [
        {"det": rep, "tier": "Complex", "impact": 5, "effort": 5},
        {"det": NEUTRAL_DETECTOR, "tier": "Quick Win", "impact": 8, "effort": 2},
    ]
    orders = {
        tuple(o["detector_id"] for o in rank_opportunities(_make_opps(specs, focus), focus_id=focus))
        for _ in range(5)
    }
    assert len(orders) == 1


# ── AC7 — the per-run focus annotation flips with the chosen focus ──────────────

def test_persisted_focus_annotation_flips_with_focus(client):
    """The same finding is emphasised under its focus and neutral under
    enterprise_wide — read back through the persisted run, proving the wiring is
    behavioural, not cosmetic."""
    det = "APPROVAL_BOTTLENECK"

    run_compliance = _launch(client, focus_id="approvals_compliance")
    run_enterprise = _launch(client, focus_id="enterprise_wide")

    fe_focus = build_focus_emphasis(load_focus_for_run(run_compliance), det)
    fe_enterprise = build_focus_emphasis(load_focus_for_run(run_enterprise), det)

    assert fe_focus["matched"] is True
    assert fe_enterprise["matched"] is False
