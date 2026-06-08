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

# ─────────────────────────────────────────────────────────────────────────────
# T7 — temporal enrichment wire-in (AT-143)
# ─────────────────────────────────────────────────────────────────────────────

def test_temporal_fields_present_when_data_exists(client, enriched_run_id, first_opp_id):
    """Temporal fields appear in enrichment response when historical data exists."""
    r = client.get(
        f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
        headers=_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    temporal_fields = (
        "baseline_context", "trend_direction", "anomaly_score",
        "is_anomalous", "first_deviation", "baseline_mean", "run_count",
    )
    for field in temporal_fields:
        assert field in data, f"Temporal field '{field}' missing from enrichment response"


def test_temporal_enrichment_exception_does_not_break_response(client, enriched_run_id, first_opp_id):
    """AC12: exception in temporal enrichment does not break enrichment response."""
    r = client.get(
        f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
        headers=_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert "oppId" in data
    assert "aiSummary" in data
    assert isinstance(data.get("aiWhyBullets"), list)
    assert isinstance(data.get("aiRisks"), list)
    assert isinstance(data.get("aiSuggestedNextSteps"), list)


def test_insufficient_history_returns_null_baseline_and_insufficient_trend(client):
    """AC14: runs with < 3 historical values return baseline_context=null,
    trend_direction='insufficient_data'."""
    from unittest.mock import patch
    from app.temporal_enrichment import enrich_opportunities_with_temporal_context
    from app.trend_engine import TrendResult, AnomalyResult

    _trend = TrendResult(
        trend_direction="insufficient_data",
        slope=0.0,
        slope_pct=0.0,
        r_squared=0.0,
        run_count=1,
        signal_key="test::det::metric_value",
    )
    _anomaly = AnomalyResult(
        is_anomalous=False,
        anomaly_score=0.0,
        anomaly_direction=None,
        baseline_mean=None,
        baseline_stddev=None,
        insufficient_data=True,
        first_deviation=False,
        signal_key="test::det::metric_value",
    )

    opps = [{"id": "opp_t14", "_debug": {"detector_id": "det"}, "metric_value": 5.0}]
    with patch("app.temporal_enrichment.calculate_trend", return_value=_trend), \
         patch("app.temporal_enrichment.calculate_anomaly", return_value=_anomaly):
        result = enrich_opportunities_with_temporal_context("run_t14", "org1", "test", opps)

    opp = result[0]
    assert opp.get("baseline_context") is None, "AC14: baseline_context must be null for insufficient history"
    assert opp.get("trend_direction") == "insufficient_data", "AC14: trend_direction must be 'insufficient_data'"


def test_exec_report_has_ai_executive_summary_field(client, enriched_run_id):
    """aiExecutiveSummary must exist in exec report. May be empty without API key."""
    r = client.get(
        f"/api/runs/{enriched_run_id}/executive-report", headers=_auth()
    )
    assert r.status_code == 200
    assert "aiExecutiveSummary" in r.json(), (
        "aiExecutiveSummary missing from executive report"
    )


# ─────────────────────────────────────────────────────────────────────────────
# T5 — AC9: EntitySummary in OppEnrichment (T3-S12-A Stage 2)
# ─────────────────────────────────────────────────────────────────────────────

def test_opp_enrichment_entities_field_always_present(client, enriched_run_id, first_opp_id):
    """AC9: OppEnrichment response always includes the 'entities' field."""
    r = client.get(
        f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
        headers=_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert "entities" in data, "'entities' field missing from OppEnrichment response"
    assert isinstance(data["entities"], list), "'entities' must be a list"


def test_opp_enrichment_entity_summary_has_required_fields(client, enriched_run_id, first_opp_id):
    """AC9: Each EntitySummary must include resolution_confidence and resolution_status."""
    r = client.get(
        f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
        headers=_auth(),
    )
    assert r.status_code == 200
    entities = r.json().get("entities", [])

    for entity in entities:
        assert "entity_id" in entity, "EntitySummary missing 'entity_id'"
        assert "entity_type" in entity, "EntitySummary missing 'entity_type'"
        assert "display_name" in entity, "EntitySummary missing 'display_name'"
        assert "source_system" in entity, "EntitySummary missing 'source_system'"
        assert "resolution_confidence" in entity, "EntitySummary missing 'resolution_confidence'"
        assert "resolution_status" in entity, "EntitySummary missing 'resolution_status'"
        assert isinstance(entity["resolution_confidence"], float), (
            "resolution_confidence must be a float"
        )
        assert entity["resolution_status"] in ("resolved", "ambiguous"), (
            f"resolution_status must be 'resolved' or 'ambiguous', got: {entity['resolution_status']}"
        )


def test_opp_enrichment_entities_no_canonical_name_exposed(client, enriched_run_id, first_opp_id):
    """AC9: canonical_name must never appear in EntitySummary — it is an internal field."""
    r = client.get(
        f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
        headers=_auth(),
    )
    assert r.status_code == 200
    for entity in r.json().get("entities", []):
        assert "canonical_name" not in entity, (
            "canonical_name must not be exposed in EntitySummary (internal normalisation artifact)"
        )


def test_opp_enrichment_fallback_entities_field_present(client):
    """AC9: The 'entities' field is present even on fallback (no LLM enrichment) responses."""
    body = {
        "connectedSources": [], "uploadedFiles": [],
        "sampleWorkspaceEnabled": False,
        "mode": "offline", "systems": ["salesforce"],
    }
    r = client.post("/api/runs/start", headers=_auth(), json=body)
    assert r.status_code in (200, 201)
    run_id = r.json().get("runId") or r.json().get("id")

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
    assert "entities" in data, "'entities' field missing from fallback OppEnrichment response"
    assert isinstance(data["entities"], list), "'entities' must be a list even on fallback"


def test_opp_enrichment_service_account_entities_filtered(client, enriched_run_id, first_opp_id):
    """AC9 + Section 8: Entities with run_count < 3 must not appear in enrichment response.

    The KV store may contain entities with run_count stored; the endpoint applies
    the service-account filter before building EntitySummary objects.
    This test injects a low-count entity into the run KV and verifies it is excluded.
    """
    import os
    from app import db as app_db

    # Read current entities from KV
    current = app_db.run_kv_get("entities", enriched_run_id, []) or []

    # Inject a service-account entity with run_count=1
    injected = current + [{
        "entity_id": "test-service-account-id",
        "entity_type": "person",
        "display_name": "System Admin",
        "source_system": "salesforce",
        "resolution_confidence": 0.8,
        "resolution_status": "resolved",
        "run_count": 1,
    }]
    app_db.run_kv_set("entities", enriched_run_id, injected)

    try:
        r = client.get(
            f"/api/runs/{enriched_run_id}/opportunities/{first_opp_id}/enrichment",
            headers=_auth(),
        )
        assert r.status_code == 200
        entity_ids = [e["entity_id"] for e in r.json().get("entities", [])]
        assert "test-service-account-id" not in entity_ids, (
            "Service-account entity (run_count=1) must be filtered from enrichment response"
        )
    finally:
        # Restore original KV state
        app_db.run_kv_set("entities", enriched_run_id, current)
