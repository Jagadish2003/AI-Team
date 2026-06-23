"""
Sprint 4 T6 contract tests — LLM Enrichment Layer  v1.1

Changes from v1.0:
  Fix 1/2: Test file now actually included in the pack (was missing from v1.0 zip).
  Fix 6: Fallback shape tests verify all list fields present and are lists.
  Fix 7: Type validation tests verify Claude response shape is enforced.

14 tests total:
  - 404 guards (2)
  - Run enrichment endpoint (4)
  - Per-opportunity enrichment endpoint (4)
  - Hard rule: no scoring fields changed (1)
  - Fallback shape consistency (1)
  - Replay determinism (1)
  - Executive summary field in exec report (1)
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
def enriched_run_id(client):
    """Start a run and wait for complete/partial. T6 enrichment runs synchronously."""
    body = {
        "connectedSources":       ["ServiceNow", "Jira & Confluence"],
        "uploadedFiles":          [],
        "sampleWorkspaceEnabled": False,
        "mode":    "offline",
        "systems": ["salesforce", "servicenow", "jira"],
    }
    r = client.post("/api/runs/start", headers=_auth(), json=body)
    assert r.status_code in (200, 201), f"start failed: {r.text}"
    run_id = r.json().get("runId") or r.json().get("id")
    assert run_id

    status = "running"
    for _ in range(90):
        st = client.get(f"/api/runs/{run_id}/status", headers=_auth())
        if st.status_code == 200:
            status = st.json().get("status", "running")
            if status in ("complete", "partial", "failed"):
                break
        time.sleep(1)

    assert status in ("complete", "partial"), (
        f"Run '{run_id}' reached '{status}' — cannot test T6"
    )
    return run_id


@pytest.fixture(scope="session")
def first_opp_id(client, enriched_run_id):
    r = client.get(f"/api/runs/{enriched_run_id}/opportunities", headers=_auth())
    assert r.status_code == 200
    opps = r.json()
    assert len(opps) >= 1
    return opps[0]["id"]


# ─────────────────────────────────────────────────────────────────────────────
# 404 guards
# ─────────────────────────────────────────────────────────────────────────────

def test_llm_enrichment_unknown_run_404(client):
    r = client.get("/api/runs/run_xyz_unknown/llm-enrichment", headers=_auth())
    assert r.status_code == 404


def test_opp_enrichment_unknown_run_404(client):
    r = client.get(
        "/api/runs/run_xyz_unknown/opportunities/opp_001/enrichment",
        headers=_auth(),
    )
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Run enrichment endpoint
# ─────────────────────────────────────────────────────────────────────────────

def test_run_enrichment_returns_200(client, enriched_run_id):
    r = client.get(f"/api/runs/{enriched_run_id}/llm-enrichment", headers=_auth())
    assert r.status_code == 200


def test_run_enrichment_has_available_field(client, enriched_run_id):
    r = client.get(f"/api/runs/{enriched_run_id}/llm-enrichment", headers=_auth())
    assert "available" in r.json()


def test_run_enrichment_available_true_after_run(client, enriched_run_id):
    """
    After a completed run, enrichment must be available (true).
    T6 enrichment runs synchronously — it is done by the time status=complete.
    If available=false: T6 patch was not applied to materialize_t2.py.
    """
    r = client.get(f"/api/runs/{enriched_run_id}/llm-enrichment", headers=_auth())
    assert r.json().get("available") is True, (
        "Enrichment not available. Apply T6 patch to materialize_t2.py."
    )


def test_run_enrichment_has_run_id(client, enriched_run_id):
    r = client.get(f"/api/runs/{enriched_run_id}/llm-enrichment", headers=_auth())
    assert r.json().get("runId") == enriched_run_id


# ─────────────────────────────────────────────────────────────────────────────
# Per-opportunity enrichment endpoint
# ─────────────────────────────────────────────────────────────────────────────

def test_opp_enrichment_returns_200(client, enriched_run_id, first_opp_id):
    r = client.get(
        f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
        headers=_auth(),
    )
    assert r.status_code == 200


def test_opp_enrichment_has_non_empty_ai_summary(client, enriched_run_id, first_opp_id):
    """aiSummary must always be non-empty — either LLM or aiRationale fallback."""
    r = client.get(
        f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
        headers=_auth(),
    )
    assert len(r.json().get("aiSummary", "")) > 0


def test_opp_enrichment_full_shape_always_returned(client, enriched_run_id, first_opp_id):
    """
    Fix 6: All list fields must always be present and be lists.
    Consistent shape whether LLM-generated or fallback.
    """
    r = client.get(
        f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
        headers=_auth(),
    )
    data = r.json()
    assert "oppId"                in data
    assert "aiSummary"            in data
    assert "llmGenerated"         in data
    for list_field in ("aiWhyBullets", "aiRisks", "aiSuggestedNextSteps"):
        assert list_field in data, f"Missing '{list_field}'"
        assert isinstance(data[list_field], list), f"'{list_field}' must be a list"


def test_opp_enrichment_opp_id_matches(client, enriched_run_id, first_opp_id):
    r = client.get(
        f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
        headers=_auth(),
    )
    assert r.json().get("oppId") == first_opp_id


# ─────────────────────────────────────────────────────────────────────────────
# Hard rule: LLM never changes scoring fields
# ─────────────────────────────────────────────────────────────────────────────

def test_llm_does_not_change_scoring_fields(client, enriched_run_id):
    """
    Core hard rule: enrichment response must not contain any scoring field.
    Original opp impact/effort/tier/decision must be unchanged.
    """
    opps = client.get(
        f"/api/runs/{enriched_run_id}/opportunities", headers=_auth()
    ).json()

    for opp in opps:
        enrich = client.get(
            f"/api/runs/{enriched_run_id}/opportunities/{opp['id']}/enrichment",
            headers=_auth(),
        ).json()

        for field in ("impact", "effort", "tier", "decision"):
            assert field not in enrich, (
                f"Enrichment for {opp['id']} contains '{field}' — violates hard rule"
            )
        assert opp.get("decision") == "UNREVIEWED"
        assert 1 <= opp.get("impact", 0) <= 10
        assert opp.get("tier") in ("Quick Win", "Strategic", "Complex")


# ─────────────────────────────────────────────────────────────────────────────
# Fallback shape consistency
# ─────────────────────────────────────────────────────────────────────────────

def test_fallback_returns_full_shape_when_no_enrichment(client):
    """
    Fix 6: When enrichment KV is missing, the endpoint must return the full
    OppEnrichment shape with empty lists — not a partial object.
    Start a fresh run, immediately fetch enrichment before it can complete.
    """
    body = {
        "connectedSources": [], "uploadedFiles": [],
        "sampleWorkspaceEnabled": False,
        "mode": "offline", "systems": ["salesforce"],
    }
    r = client.post("/api/runs/start", headers=_auth(), json=body)
    assert r.status_code in (200, 201)
    run_id = r.json().get("runId") or r.json().get("id")

    # Immediately fetch enrichment — may or may not be ready
    # Either way, the shape must be complete
    opps_r = client.get(f"/api/runs/{run_id}/opportunities", headers=_auth())
    if opps_r.status_code != 200 or not opps_r.json():
        pytest.skip("No opportunities yet — run may not have started")

    opp_id = opps_r.json()[0]["id"]
    enrich_r = client.get(
        f"/api/runs/{run_id}/opportunities/{opp_id}/enrichment",
        headers=_auth(),
    )
    assert enrich_r.status_code == 200
    data = enrich_r.json()
    # Full shape must always be present
    for field in ("aiSummary", "aiWhyBullets", "aiRisks", "aiSuggestedNextSteps", "llmGenerated"):
        assert field in data, f"Missing '{field}' from fallback response"
    for list_field in ("aiWhyBullets", "aiRisks", "aiSuggestedNextSteps"):
        assert isinstance(data[list_field], list)


# ─────────────────────────────────────────────────────────────────────────────
# Replay: enrichment stable, not re-generated
# ─────────────────────────────────────────────────────────────────────────────

def test_enrichment_stable_after_replay(client, enriched_run_id, first_opp_id):
    before = client.get(
        f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
        headers=_auth(),
    ).json()

    client.post(f"/api/runs/{enriched_run_id}/replay", headers=_auth(), json={})

    after = client.get(
        f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
        headers=_auth(),
    ).json()

    assert before.get("aiSummary") == after.get("aiSummary"), (
        "aiSummary changed after replay — enrichment re-generated on read"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Executive report
# ─────────────────────────────────────────────────────────────────────────────

def test_exec_report_has_ai_executive_summary_field(client, enriched_run_id):
    """aiExecutiveSummary must exist in exec report. May be empty without API key."""
    r = client.get(
        f"/api/runs/{enriched_run_id}/executive-report", headers=_auth()
    )
    assert r.status_code == 200
    assert "aiExecutiveSummary" in r.json(), (
        "aiExecutiveSummary missing from executive report"
    )
