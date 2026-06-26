"""
R16-B1 (T6) contract tests — Evidence Pointer storage + retrieval.

Covers the acceptance criterion that belongs to this task:

  AC7 — Given an opportunity, the evidence pointers can be queried back to the
        source artifacts that produced it (source_system + source_artifact +
        source_timestamp).

Plus the cross-cutting guarantees the task prompt calls out:
  - Tenant boundary: evidence from one org is never returned for another org.
  - AC8 (exercised here): the extensible pointer fields chunk_id /
    retrieval_result_id are present and null in 1.6.

Two layers:
  * Pure / monkeypatched unit tests run without a database — they pin the
    derivation, the structure, and the org-isolation logic of the retrieval
    helper directly (mirroring the _load_* unit tests in
    test_task_t6_llm_enrichment.py and the require_run_exists tests in
    test_run_tenancy.py).
  * End-to-end tests drive the real route + tenancy middleware over a live
    offline run.
"""
from __future__ import annotations

import os
from typing import Dict

import pytest
from fastapi.testclient import TestClient


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


# ─────────────────────────────────────────────────────────────────────────────
# Derivation + structure (pure — no DB)
# ─────────────────────────────────────────────────────────────────────────────

def test_pointer_has_mandatory_spine_and_null_extensible_fields():
    """AC7 spine + AC8 extensible-null, on a derived pointer."""
    from app.evidence_pointers import build_pointers_for_opportunity

    opp = {
        "id": "opp_001",
        "evidence": [
            {"id": "ev_sf_aaa", "source": "Salesforce", "tsLabel": "24 Jun 2026, 10:00"},
        ],
        "detector_id": "HANDOFF_FRICTION",
        "signal_source": "salesforce",
    }
    pointers = build_pointers_for_opportunity(opp, run_completed_at="2026-06-24T10:05:00Z")
    assert len(pointers) == 1
    p = pointers[0]

    # AC7 — mandatory spine, queryable back to the source artifact.
    assert p["source_system"] == "salesforce"
    assert p["source_artifact"] == "ev_sf_aaa"
    assert p["source_timestamp"] == "24 Jun 2026, 10:00"
    assert p["origin"] == "observed"

    # AC8 — extensible retrieval fields present and null in 1.6.
    assert "chunk_id" in p and p["chunk_id"] is None
    assert "retrieval_result_id" in p and p["retrieval_result_id"] is None


def test_pointers_cover_each_evidence_source_system():
    """Multiple source systems on one opportunity each yield a pointer (AC7)."""
    from app.evidence_pointers import build_pointer_index

    opps = [{
        "id": "opp_001",
        "evidenceIds": ["ev_sf_aaa", "ev_jira_bbb"],
        "_debug": {"detector_id": "HANDOFF_FRICTION", "signal_source": "salesforce"},
    }]
    evidence = [
        {"id": "ev_sf_aaa", "source": "Salesforce", "tsLabel": "24 Jun 2026, 10:00"},
        {"id": "ev_jira_bbb", "source": "Jira", "tsLabel": "24 Jun 2026, 10:01"},
    ]
    index = build_pointer_index(opps, evidence=evidence, run_completed_at="2026-06-24T10:05:00Z")
    systems = sorted(p["source_system"] for p in index["opp_001"])
    assert systems == ["jira", "salesforce"]
    # Every pointer can be walked back to a source artifact + timestamp.
    for p in index["opp_001"]:
        assert p["source_artifact"] and p["source_timestamp"]


def test_opportunity_without_evidence_still_has_detector_provenance():
    """A finding with no evidence rows still resolves to its detector firing as
    an observed source artifact — never zero provenance (AC7)."""
    from app.evidence_pointers import build_pointers_for_opportunity

    opp = {"id": "opp_002", "evidenceIds": [], "_debug": {
        "detector_id": "KNOWLEDGE_GAP", "signal_source": "salesforce"}}
    pointers = build_pointers_for_opportunity(opp, run_completed_at="2026-06-24T10:05:00Z")
    assert len(pointers) == 1
    assert pointers[0]["source_artifact"] == "KNOWLEDGE_GAP"
    assert pointers[0]["source_system"] == "salesforce"


def test_inferred_pointer_without_job_id_is_invalid():
    """The spine validator rejects inferred content with no extraction_job_id —
    inferred knowledge must always name the job that produced it (origin is the
    safety property). Observed pointers validate without a job id."""
    from app.evidence_pointers import _is_valid_pointer, _pointer

    observed = _pointer(source_system="jira", source_artifact="X", source_timestamp="t")
    assert _is_valid_pointer(observed) is True

    inferred_bad = {**observed, "origin": "inferred", "extraction_job_id": None}
    assert _is_valid_pointer(inferred_bad) is False

    inferred_ok = {**observed, "origin": "inferred", "extraction_job_id": "job_123"}
    assert _is_valid_pointer(inferred_ok) is True


def test_store_and_get_roundtrip(monkeypatch):
    """store_evidence_pointers writes a run-scoped index that
    get_evidence_pointers_for_opportunity reads back by opportunity id."""
    from app import evidence_pointers as ep

    store: Dict[str, object] = {}
    monkeypatch.setattr(ep.db, "run_kv_set", lambda key, run_id, value: store.__setitem__(f"{key}:{run_id}", value))
    monkeypatch.setattr(ep.db, "run_kv_get", lambda key, run_id, default=None: store.get(f"{key}:{run_id}", default))

    opps = [{"id": "opp_001", "evidenceIds": ["ev_sf_aaa"],
             "_debug": {"detector_id": "HANDOFF_FRICTION", "signal_source": "salesforce"}}]
    evidence = [{"id": "ev_sf_aaa", "source": "Salesforce", "tsLabel": "24 Jun 2026, 10:00"}]

    count = ep.store_evidence_pointers("run_rt", opps, evidence=evidence, run_completed_at="t")
    assert count == 1

    got = ep.get_evidence_pointers_for_opportunity("run_rt", "opp_001")
    assert len(got) == 1 and got[0]["source_artifact"] == "ev_sf_aaa"

    # Unknown opportunity → empty, never raises.
    assert ep.get_evidence_pointers_for_opportunity("run_rt", "opp_999") == []


def test_pointers_isolated_by_run(monkeypatch):
    """Cross-run isolation: pointers are keyed by run, so the SAME opp id stored
    for run A must NOT surface when queried against run B (same org). A valid opp
    from a different run returns an empty trail, never run A's pointers — the
    run_id is part of the lookup key, mirroring opportunity_instances'
    (opportunity_identity, run_id) keying."""
    from app import evidence_pointers as ep

    store: Dict[str, object] = {}
    monkeypatch.setattr(ep.db, "run_kv_set", lambda key, run_id, value: store.__setitem__(f"{key}:{run_id}", value))
    monkeypatch.setattr(ep.db, "run_kv_get", lambda key, run_id, default=None: store.get(f"{key}:{run_id}", default))

    opps = [{"id": "opp_001", "evidenceIds": ["ev_sf_aaa"],
             "_debug": {"detector_id": "HANDOFF_FRICTION", "signal_source": "salesforce"}}]
    evidence = [{"id": "ev_sf_aaa", "source": "Salesforce", "tsLabel": "24 Jun 2026, 10:00"}]
    ep.store_evidence_pointers("run_a", opps, evidence=evidence, run_completed_at="t")

    # run_a has the trail for opp_001 ...
    assert len(ep.get_evidence_pointers_for_opportunity("run_a", "opp_001")) == 1
    # ... but the same opp_id queried against run_b (no pointers stored for it)
    # returns empty — run A's provenance never leaks across the run boundary.
    assert ep.get_evidence_pointers_for_opportunity("run_b", "opp_001") == []


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval helper — org isolation (monkeypatched, no DB)
# ─────────────────────────────────────────────────────────────────────────────

def test_load_evidence_pointers_denies_cross_org(monkeypatch):
    """Tenant boundary: a run owned by org_B yields NO pointers to an org_A
    request, even if pointers are stored — provenance points back to real
    business systems and must never cross orgs."""
    from app import routes_sprint4_t6 as routes

    monkeypatch.setattr("app.middleware.tenancy.get_current_org_id", lambda: "org_A")
    monkeypatch.setattr(routes.db, "get_run", lambda rid: {"id": rid, "org_id": "org_B"})
    # If the org guard failed, this would return a pointer — make that detectable.
    monkeypatch.setattr(
        "app.evidence_pointers.get_evidence_pointers_for_opportunity",
        lambda rid, oid: [{"source_system": "salesforce", "source_artifact": "leak",
                           "source_timestamp": "t", "origin": "observed"}],
    )

    result = routes._load_evidence_pointers("run_x", "opp_001")
    assert result == [], "cross-org request must receive no provenance pointers"


def test_load_evidence_pointers_allows_same_org(monkeypatch):
    from app import routes_sprint4_t6 as routes

    monkeypatch.setattr("app.middleware.tenancy.get_current_org_id", lambda: "org_A")
    monkeypatch.setattr(routes.db, "get_run", lambda rid: {"id": rid, "org_id": "org_A"})
    monkeypatch.setattr(
        "app.evidence_pointers.get_evidence_pointers_for_opportunity",
        lambda rid, oid: [{"source_system": "salesforce", "source_artifact": "ev_sf_aaa",
                           "source_timestamp": "24 Jun 2026, 10:00", "origin": "observed"}],
    )

    result = routes._load_evidence_pointers("run_x", "opp_001")
    assert len(result) == 1
    assert result[0].source_system == "salesforce"
    assert result[0].source_artifact == "ev_sf_aaa"
    # Pydantic model carries the AC8 extensible fields, defaulted to null.
    assert result[0].chunk_id is None
    assert result[0].retrieval_result_id is None


def test_load_evidence_pointers_allows_legacy_untagged_run(monkeypatch):
    """A run created before org-tagging (no org_id) is not filtered — no break."""
    from app import routes_sprint4_t6 as routes

    monkeypatch.setattr("app.middleware.tenancy.get_current_org_id", lambda: "org_A")
    monkeypatch.setattr(routes.db, "get_run", lambda rid: {"id": rid})  # no org_id
    monkeypatch.setattr(
        "app.evidence_pointers.get_evidence_pointers_for_opportunity",
        lambda rid, oid: [{"source_system": "jira", "source_artifact": "ev_jira_bbb",
                           "source_timestamp": "t", "origin": "observed"}],
    )
    result = routes._load_evidence_pointers("run_legacy", "opp_001")
    assert len(result) == 1


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end via the real route + tenancy middleware
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def traced_run_id(client: TestClient):
    """Start an offline run and wait until it materializes opportunities."""
    import time

    body = {
        "connectedSources": ["ServiceNow", "Jira & Confluence"],
        "uploadedFiles": [], "sampleWorkspaceEnabled": False,
        "mode": "offline", "systems": ["salesforce", "servicenow", "jira"],
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
    assert status in ("complete", "partial"), f"run reached '{status}'"
    return run_id


@pytest.fixture(scope="module")
def first_opp_id(client: TestClient, traced_run_id):
    r = client.get(f"/api/runs/{traced_run_id}/opportunities", headers=_auth())
    assert r.status_code == 200 and r.json()
    return r.json()[0]["id"]


def test_evidence_trace_unknown_run_404(client: TestClient):
    r = client.get(
        "/api/runs/run_xyz_unknown/opportunities/opp_001/evidence-trace",
        headers=_auth(),
    )
    assert r.status_code == 404


def test_evidence_trace_unknown_opp_404(client: TestClient, traced_run_id):
    r = client.get(
        f"/api/runs/{traced_run_id}/opportunities/opp_does_not_exist/evidence-trace",
        headers=_auth(),
    )
    assert r.status_code == 404


def test_evidence_trace_returns_source_trail(client: TestClient, traced_run_id, first_opp_id):
    """AC7 — given an opportunity, the trace returns pointers back to the source
    artifacts, each exposing source_system + source_artifact + source_timestamp."""
    r = client.get(
        f"/api/runs/{traced_run_id}/opportunities/{first_opp_id}/evidence-trace",
        headers=_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["runId"] == traced_run_id
    assert data["oppId"] == first_opp_id
    assert data["available"] is True, "a materialized opportunity must have a queryable trail"
    assert len(data["pointers"]) >= 1

    for p in data["pointers"]:
        # AC7 minimum fields
        assert p["source_system"], "pointer missing source_system"
        assert p["source_artifact"], "pointer missing source_artifact"
        assert p["source_timestamp"], "pointer missing source_timestamp"
        assert p["origin"] in ("observed", "inferred")
        # AC8 — extensible fields present and null in 1.6
        assert p["chunk_id"] is None
        assert p["retrieval_result_id"] is None


def test_evidence_trace_isolated_by_org(client: TestClient, traced_run_id, first_opp_id, monkeypatch):
    """The run was created under the dev org ('default'). A request scoped to a
    different org must not see its provenance.

    The request is denied before any pointer is read: the RBAC gate rejects a
    user with no role in the foreign org (403), and even with a role the
    cross-org run read denies as not-found (404). Either way no provenance
    crosses the tenant boundary — assert denial, not a specific code. The
    pointer-level guard is covered directly in
    test_load_evidence_pointers_denies_cross_org."""
    monkeypatch.setenv("DEV_JWT_ORG", "some_other_org")
    r = client.get(
        f"/api/runs/{traced_run_id}/opportunities/{first_opp_id}/evidence-trace",
        headers=_auth(),
    )
    assert r.status_code in (403, 404), (
        f"cross-org request must be denied, got {r.status_code}: {r.text}"
    )
    # The denial must NOT leak provenance in the body.
    assert "source_artifact" not in r.text
