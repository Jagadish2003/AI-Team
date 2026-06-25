"""
test_r16_c2_t2_focus_ranking.py

R16-C2 T2 — Contract test for focus emphasis wired through the persisted run.

Proves the wiring end to end: a Stack Builder configuration launched through
POST /api/stack-builder/launch persists the selected focus_id, which the
discovery layer reads back (focus_affinity.load_focus_for_run) and applies as
emphasis in the shared seed-ranking path (track_a_adapter.export_track_a_seed →
calibration.ranking.rank_opportunities).

Unlike the discovery-layer unit tests (test_r16_c2_t2_focus_emphasis.py), these
do NOT mock run_kv_get — they exercise the real API → database → loader →
ranking path, so the wiring itself is under test.

Acceptance criteria covered (R16-C2 Section 5):
  AC1 / AC7 — identical data under two different non-enterprise focuses produces
              visibly different ranking/emphasis.
  AC2       — a focus emphasises its matching findings above the unbiased view.
  AC4       — enterprise_wide applies no bias (baseline ordering).
  AC5       — same focus + same data => identical ordering, deterministically.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import run_kv_get

from discovery.packs.focus_affinity import load_focus_for_run, build_focus_emphasis
from discovery.track_a_adapter import export_track_a_seed


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


def _launch(client: TestClient, *, focus_id: str) -> str:
    """Launch a run through the real endpoint with the given focus and return its id.

    Everything except focus_id is held identical between calls so any change in
    ordering can only come from the focus.
    """
    body: Dict[str, Any] = {
        # Required by the LaunchRequest schema. Its value is ignored by tenancy
        # (org_id is sourced from the verified JWT, never the request body), but
        # the field must be present or the endpoint returns 422.
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


def _payload_for(run_id: str) -> Dict[str, Any]:
    """A fixed, identical set of opportunities, annotated with the focus the run
    was actually launched with (read back from the DB), then run through the
    real seed/ranking path."""
    focus = load_focus_for_run(run_id)
    detectors = [
        ("APPROVAL_BOTTLENECK", "Complex", 5, 5),    # approvals_compliance affinity
        ("HANDOFF_FRICTION", "Quick Win", 8, 2),     # cross_system_handoffs affinity
        ("KNOWLEDGE_GAP", "Strategic", 6, 3),        # member_customer_service affinity
    ]
    opps = []
    for det, tier, impact, effort in detectors:
        opps.append({
            "detector_id": det, "tier": tier, "impact": impact, "effort": effort,
            "metric_value": 3.0, "threshold": 2.0, "raw_evidence": {}, "evidenceIds": [],
            "focus_emphasis": build_focus_emphasis(focus, det),
        })
    return {"runId": run_id, "focusId": focus, "opportunities": opps}


def _seed_order(run_id: str) -> List[str]:
    seed = export_track_a_seed(_payload_for(run_id))
    return [o["_debug"]["detector_id"] for o in seed["opportunities"]]


# ── AC: focus is persisted and read back ────────────────────────────────────────

def test_focus_id_is_persisted_and_loadable(client):
    run_id = _launch(client, focus_id="approvals_compliance")
    assert run_kv_get("focus_id", run_id) == "approvals_compliance"
    assert load_focus_for_run(run_id) == "approvals_compliance"


# ── AC1 / AC7 — different focuses, visibly different ordering ───────────────────

def test_two_focuses_produce_different_ordering(client):
    run_compliance = _launch(client, focus_id="approvals_compliance")
    run_handoffs = _launch(client, focus_id="cross_system_handoffs")

    order_compliance = _seed_order(run_compliance)
    order_handoffs = _seed_order(run_handoffs)

    assert order_compliance != order_handoffs
    # Each focus floats its own matching finding to the top.
    assert order_compliance[0] == "APPROVAL_BOTTLENECK"
    assert order_handoffs[0] == "HANDOFF_FRICTION"


# ── AC2 — emphasis raises the matching finding above the unbiased view ──────────

def test_focus_emphasises_above_enterprise_wide(client):
    run_enterprise = _launch(client, focus_id="enterprise_wide")
    run_compliance = _launch(client, focus_id="approvals_compliance")

    enterprise_order = _seed_order(run_enterprise)
    compliance_order = _seed_order(run_compliance)

    # Under the unbiased view APPROVAL_BOTTLENECK is last (Complex tier)...
    assert enterprise_order[-1] == "APPROVAL_BOTTLENECK"
    # ...the compliance focus lifts it to the top.
    assert compliance_order[0] == "APPROVAL_BOTTLENECK"


# ── AC4 — enterprise_wide is the unbiased baseline ──────────────────────────────

def test_enterprise_wide_is_baseline(client):
    run_enterprise = _launch(client, focus_id="enterprise_wide")
    order = _seed_order(run_enterprise)
    # Pure tier ordering — no focus bias.
    assert order == ["HANDOFF_FRICTION", "KNOWLEDGE_GAP", "APPROVAL_BOTTLENECK"]


# ── AC5 — deterministic ─────────────────────────────────────────────────────────

def test_same_focus_same_data_is_deterministic(client):
    run_a = _launch(client, focus_id="approvals_compliance")
    run_b = _launch(client, focus_id="approvals_compliance")
    assert _seed_order(run_a) == _seed_order(run_b)
    # And repeated ranking of the same run is stable.
    assert _seed_order(run_a) == _seed_order(run_a)
