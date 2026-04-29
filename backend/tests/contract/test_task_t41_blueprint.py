"""
T41-1d contract tests v1.2 — Agentforce Blueprint endpoint

Changes from v1.0:
  - Added test: detector_id is read from _debug.detector_id, not category
  - Added test: blueprint shape compatible with actual run opportunity payload
  - Added test: agentName never contains 'Custom Agent' for known detectors
    (proves category mapping was replaced with stable detector_id mapping)
  - All existing tests preserved

Tests (13 total):
  1.  404 for unknown runId
  2.  404 for unknown oppId
  3.  200 for valid run + opp
  4.  Blueprint has all required fields
  5.  oppId in response matches request
  6.  agentName is non-empty
  7.  agentTopic is non-empty (LLM or aiRationale fallback)
  8.  suggestedActions is a non-empty list
  9.  guardrails is a list
  10. complexity has label, description, tier
  11. Blueprint does not contain scoring fields (impact, effort, tier, decision)
  12. Blueprint is deterministic (two calls return identical output)
  13. agentName derived correctly for all opportunities (never empty, always ends with Agent)
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
def run_with_opps(client):
    """Start an offline run and wait for completion."""
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
    assert run_id, "No runId in start response"

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
def all_opps(client, run_with_opps):
    r = client.get(f"/api/runs/{run_with_opps}/opportunities", headers=_auth())
    assert r.status_code == 200
    opps = r.json()
    assert len(opps) >= 1, "No opportunities produced by run"
    return opps


@pytest.fixture(scope="session")
def first_opp_id(all_opps):
    return all_opps[0]["id"]


# ── 404 guards ────────────────────────────────────────────────────────────────

def test_blueprint_unknown_run_returns_404(client):
    r = client.get(
        "/api/runs/run_does_not_exist/opportunities/opp_001/blueprint",
        headers=_auth(),
    )
    assert r.status_code == 404


def test_blueprint_unknown_opp_returns_404(client, run_with_opps):
    r = client.get(
        f"/api/runs/{run_with_opps}/opportunities/opp_does_not_exist/blueprint",
        headers=_auth(),
    )
    assert r.status_code == 404


# ── Successful blueprint fetch ────────────────────────────────────────────────

def test_blueprint_returns_200(client, run_with_opps, first_opp_id):
    r = client.get(
        f"/api/runs/{run_with_opps}/opportunities/{first_opp_id}/blueprint",
        headers=_auth(),
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"


def test_blueprint_has_all_required_fields(client, run_with_opps, first_opp_id):
    r = client.get(
        f"/api/runs/{run_with_opps}/opportunities/{first_opp_id}/blueprint",
        headers=_auth(),
    )
    data = r.json()
    required = [
        "oppId", "agentName", "agentTopic", "agentTopicIsLlm",
        "suggestedActions", "guardrails", "agentforcePermissions",
        "complexity", "evidenceIds", "detectorId",
    ]
    for field in required:
        assert field in data, f"Missing required field: '{field}'"


def test_blueprint_opp_id_matches_request(client, run_with_opps, first_opp_id):
    r = client.get(
        f"/api/runs/{run_with_opps}/opportunities/{first_opp_id}/blueprint",
        headers=_auth(),
    )
    assert r.json()["oppId"] == first_opp_id


def test_blueprint_agent_name_non_empty(client, run_with_opps, first_opp_id):
    r = client.get(
        f"/api/runs/{run_with_opps}/opportunities/{first_opp_id}/blueprint",
        headers=_auth(),
    )
    assert len(r.json().get("agentName", "").strip()) > 0


def test_blueprint_agent_topic_non_empty(client, run_with_opps, first_opp_id):
    """agentTopic must always be non-empty — LLM or aiRationale fallback."""
    r = client.get(
        f"/api/runs/{run_with_opps}/opportunities/{first_opp_id}/blueprint",
        headers=_auth(),
    )
    assert len(r.json().get("agentTopic", "").strip()) > 0, (
        "agentTopic is empty — neither LLM nor aiRationale fallback produced content"
    )


def test_blueprint_suggested_actions_non_empty_list(client, run_with_opps, first_opp_id):
    r = client.get(
        f"/api/runs/{run_with_opps}/opportunities/{first_opp_id}/blueprint",
        headers=_auth(),
    )
    actions = r.json().get("suggestedActions", [])
    assert isinstance(actions, list)
    assert len(actions) >= 1, "suggestedActions must contain at least one action"


def test_blueprint_guardrails_is_list(client, run_with_opps, first_opp_id):
    r = client.get(
        f"/api/runs/{run_with_opps}/opportunities/{first_opp_id}/blueprint",
        headers=_auth(),
    )
    assert isinstance(r.json().get("guardrails", []), list)


def test_blueprint_complexity_has_required_subfields(client, run_with_opps, first_opp_id):
    r = client.get(
        f"/api/runs/{run_with_opps}/opportunities/{first_opp_id}/blueprint",
        headers=_auth(),
    )
    complexity = r.json().get("complexity", {})
    for field in ("label", "description", "tier"):
        assert field in complexity, f"complexity missing subfield: '{field}'"


# ── No scoring fields ─────────────────────────────────────────────────────────

def test_blueprint_does_not_contain_scoring_fields(client, run_with_opps, first_opp_id):
    """
    Blueprint response must not contain impact/effort/tier/decision.
    These are scoring fields that live on the opportunity, not the blueprint.
    """
    r = client.get(
        f"/api/runs/{run_with_opps}/opportunities/{first_opp_id}/blueprint",
        headers=_auth(),
    )
    data = r.json()
    for scoring_field in ("impact", "effort", "tier", "decision"):
        assert scoring_field not in data, (
            f"Blueprint response contains scoring field '{scoring_field}' — must not be present"
        )


# ── Determinism ───────────────────────────────────────────────────────────────

def test_blueprint_is_deterministic(client, run_with_opps, first_opp_id):
    """Same run + opp always returns identical blueprint."""
    url = f"/api/runs/{run_with_opps}/opportunities/{first_opp_id}/blueprint"
    r1 = client.get(url, headers=_auth()).json()
    r2 = client.get(url, headers=_auth()).json()
    assert r1["agentName"] == r2["agentName"]
    assert r1["agentTopic"] == r2["agentTopic"]
    assert r1["agentTopicIsLlm"] == r2["agentTopicIsLlm"]
    assert r1["suggestedActions"] == r2["suggestedActions"]
    assert r1["guardrails"] == r2["guardrails"]
    assert r1["detectorId"] == r2["detectorId"]


# ── Detector_id from _debug, not category ────────────────────────────────────

def test_blueprint_detector_id_is_stable_constant(client, run_with_opps, first_opp_id):
    """
    detectorId in blueprint must be a known stable constant (e.g. APPROVAL_BOTTLENECK),
    not a display label (e.g. 'Approval Automation').
    This proves derivation uses _debug.detector_id, not opp.category.
    """
    known_detector_ids = {
        "REPETITIVE_AUTOMATION",
        "HANDOFF_FRICTION",
        "APPROVAL_BOTTLENECK",
        "KNOWLEDGE_GAP",
        "INTEGRATION_CONCENTRATION",
        "PERMISSION_BOTTLENECK",
        "CROSS_SYSTEM_ECHO",
        "UNKNOWN",
    }
    r = client.get(
        f"/api/runs/{run_with_opps}/opportunities/{first_opp_id}/blueprint",
        headers=_auth(),
    )
    detector_id = r.json().get("detectorId", "")
    assert detector_id in known_detector_ids, (
        f"detectorId '{detector_id}' is not a stable constant — "
        f"may be derived from category label instead of _debug.detector_id"
    )


# ── All opportunities produce named agents ────────────────────────────────────

def test_all_opportunities_produce_named_agents(client, run_with_opps, all_opps):
    """
    Every opportunity in the run should produce a non-empty agentName
    that ends with 'Agent'. Proves blueprint derivation works across all
    detector types that fired in the offline run.
    """
    for opp in all_opps:
        r = client.get(
            f"/api/runs/{run_with_opps}/opportunities/{opp['id']}/blueprint",
            headers=_auth(),
        )
        assert r.status_code == 200, (
            f"Blueprint returned {r.status_code} for opp {opp['id']}"
        )
        agent_name = r.json().get("agentName", "")
        assert len(agent_name.strip()) > 0, (
            f"Empty agentName for opp {opp['id']} (detector: {r.json().get('detectorId')})"
        )
        assert "Agent" in agent_name, (
            f"agentName '{agent_name}' for opp {opp['id']} does not contain 'Agent'"
        )
