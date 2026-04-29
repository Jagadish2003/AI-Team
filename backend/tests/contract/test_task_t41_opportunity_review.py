"""
T41-2 v1.1 contract tests — Opportunity Review backend API shape

These tests verify the backend API endpoints required by the merged screen.
They are additive — run alongside the existing analyst-review contract suite.

Frontend-specific acceptance criteria (redirects, bubble selection, real-time
update, Blueprint button gating) are tested in:
  frontend/src/__tests__/OpportunityReviewPage.test.tsx

Tests:
  1.  Opportunities returns 200
  2.  Opportunities response is a list
  3.  Opportunity has all required fields
  4.  Decision field is a valid value
  5.  Tier field is a valid value
  6.  Impact in 1-10 range
  7.  Effort in 1-10 range
  8.  Audit returns 200
  9.  Audit is a list
  10. Set decision APPROVED returns 200 with updated decision
  11. Decision persists on subsequent GET
  12. Set decision back to UNREVIEWED succeeds
  13. Save override with valid payload succeeds
  14. Save override with rationale but empty reason rejected (400/422)
  15. Save override with empty rationale and empty reason succeeds
  16. Quadrant data integrity — at least 2 distinct impact levels after T41-6
"""
from __future__ import annotations

import os
import time
from typing import Dict

import pytest
from fastapi.testclient import TestClient
from app.main import app


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


@pytest.fixture(scope="session")
def live_run(client):
    body = {
        "connectedSources": ["ServiceNow", "Jira & Confluence"],
        "uploadedFiles": [],
        "sampleWorkspaceEnabled": False,
        "mode": "offline",
        "systems": ["salesforce", "servicenow", "jira"],
    }
    r = client.post("/api/runs/start", headers=_auth(), json=body)
    assert r.status_code in (200, 201), f"Failed to start run: {r.text}"
    run_id = r.json().get("runId") or r.json().get("id")
    assert run_id

    for _ in range(90):
        st = client.get(f"/api/runs/{run_id}/status", headers=_auth())
        status = st.json().get("status", "running")
        if status in ("complete", "partial"):
            break
        if status == "failed":
            pytest.fail(f"Run {run_id} failed during setup")
        time.sleep(1)

    return run_id


@pytest.fixture(scope="session")
def first_opp(client, live_run):
    r = client.get(f"/api/runs/{live_run}/opportunities", headers=_auth())
    assert r.status_code == 200
    opps = r.json()
    assert len(opps) >= 1, "No opportunities produced"
    return opps[0]


# ── Opportunity shape ─────────────────────────────────────────────────────────

def test_opportunities_returns_200(client, live_run):
    r = client.get(f"/api/runs/{live_run}/opportunities", headers=_auth())
    assert r.status_code == 200


def test_opportunities_is_list(client, live_run):
    assert isinstance(
        client.get(f"/api/runs/{live_run}/opportunities", headers=_auth()).json(),
        list,
    )


def test_opportunity_has_required_fields(first_opp):
    for field in ["id","title","category","tier","impact","effort",
                  "confidence","decision","evidenceIds"]:
        assert field in first_opp, f"Missing: '{field}'"


def test_opportunity_decision_is_valid(first_opp):
    assert first_opp["decision"] in {"UNREVIEWED","APPROVED","REJECTED"}


def test_opportunity_tier_is_valid(first_opp):
    assert first_opp["tier"] in {"Quick Win","Strategic","Complex"}


def test_opportunity_impact_in_range(first_opp):
    assert 1 <= first_opp["impact"] <= 10


def test_opportunity_effort_in_range(first_opp):
    assert 1 <= first_opp["effort"] <= 10


# ── Audit ─────────────────────────────────────────────────────────────────────

def test_audit_returns_200(client, live_run):
    assert client.get(f"/api/runs/{live_run}/audit", headers=_auth()).status_code == 200


def test_audit_is_list(client, live_run):
    assert isinstance(
        client.get(f"/api/runs/{live_run}/audit", headers=_auth()).json(),
        list,
    )


# ── Decision ──────────────────────────────────────────────────────────────────

def test_set_decision_approved(client, live_run, first_opp):
    r = client.post(
        f"/api/runs/{live_run}/opportunities/{first_opp['id']}/decision",
        headers=_auth(), json={"decision": "APPROVED"},
    )
    assert r.status_code == 200
    assert r.json().get("decision") == "APPROVED"


def test_decision_persists(client, live_run, first_opp):
    client.post(
        f"/api/runs/{live_run}/opportunities/{first_opp['id']}/decision",
        headers=_auth(), json={"decision": "REJECTED"},
    )
    opps = client.get(f"/api/runs/{live_run}/opportunities", headers=_auth()).json()
    updated = next((o for o in opps if o["id"] == first_opp["id"]), None)
    assert updated is not None
    assert updated["decision"] == "REJECTED"


def test_decision_back_to_unreviewed(client, live_run, first_opp):
    r = client.post(
        f"/api/runs/{live_run}/opportunities/{first_opp['id']}/decision",
        headers=_auth(), json={"decision": "UNREVIEWED"},
    )
    assert r.status_code == 200


# ── Override ──────────────────────────────────────────────────────────────────

def test_save_override_valid(client, live_run, first_opp):
    r = client.post(
        f"/api/runs/{live_run}/opportunities/{first_opp['id']}/override",
        headers=_auth(),
        json={"rationaleOverride": "Adjusted.", "overrideReason": "SME review.", "isLocked": False},
    )
    assert r.status_code == 200


def test_save_override_missing_reason_rejected(client, live_run, first_opp):
    r = client.post(
        f"/api/runs/{live_run}/opportunities/{first_opp['id']}/override",
        headers=_auth(),
        json={"rationaleOverride": "Some text.", "overrideReason": "", "isLocked": False},
    )
    assert r.status_code in (400, 422)


def test_save_override_empty_both_ok(client, live_run, first_opp):
    r = client.post(
        f"/api/runs/{live_run}/opportunities/{first_opp['id']}/override",
        headers=_auth(),
        json={"rationaleOverride": "", "overrideReason": "", "isLocked": False},
    )
    assert r.status_code == 200


# ── Quadrant data integrity ───────────────────────────────────────────────────

def test_opportunities_span_multiple_impact_levels(client, live_run):
    """
    After T41-6 scorer recalibration, opportunities should have
    at least 2 distinct impact levels for meaningful quadrant spread.
    """
    opps = client.get(f"/api/runs/{live_run}/opportunities", headers=_auth()).json()
    impacts = {o["impact"] for o in opps}
    assert len(impacts) >= 2, (
        f"All opportunities share the same impact score {impacts}. "
        "T41-6 scorer recalibration may not have been applied."
    )
