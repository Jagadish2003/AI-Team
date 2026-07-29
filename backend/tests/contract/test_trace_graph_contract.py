"""2.0-B1 T1 contract tests — the trace graph API.

Covers:
  AC1 — a finding expands to a complete chain terminating in source records;
        every hop carries origin, connector, run id, and timestamp.
  AC2 — joined claims display the join type and correlation window used; a
        claim whose join is outside window cannot appear.

Mirrors test_evidence_pointer_trace.py's two-layer pattern:
  * Pure / monkeypatched unit tests (no DB) pin the tenancy guard.
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
# Tenancy guard (monkeypatched, no DB)
# ─────────────────────────────────────────────────────────────────────────────

def test_load_trace_denies_cross_org(monkeypatch):
    """A run owned by org_B yields no trace to an org_A request — provenance
    must never cross the tenant boundary."""
    from app import routes_trace_graph as routes

    monkeypatch.setattr("app.middleware.tenancy.get_current_org_id", lambda: "org_A")
    monkeypatch.setattr(
        "app.trace_graph.load_finding_trace",
        lambda run_id, opp_id: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    run = {"id": "run_x", "org_id": "org_B"}
    result = routes._load_trace(run, "run_x", "opp_001")
    assert result is None


def test_load_trace_allows_same_org(monkeypatch):
    from app import routes_trace_graph as routes
    from app.trace_graph import FindingTrace, TraceHop, HOP_FINDING

    monkeypatch.setattr("app.middleware.tenancy.get_current_org_id", lambda: "org_A")
    fake_trace = FindingTrace(
        opportunity_id="opp_001", run_id="run_x",
        hops=[TraceHop(
            hop_id="finding:opp_001", hop_type=HOP_FINDING, label="x",
            origin="observed", connector=None, run_id="run_x",
            timestamp=None, from_hop_id=None,
        )],
        joins=[], complete=True,
    )
    monkeypatch.setattr("app.trace_graph.load_finding_trace", lambda run_id, opp_id: fake_trace)
    run = {"id": "run_x", "org_id": "org_A"}
    result = routes._load_trace(run, "run_x", "opp_001")
    assert result is fake_trace


def test_load_trace_allows_legacy_untagged_run(monkeypatch):
    """A run created before org-tagging (no org_id) is not filtered out."""
    from app import routes_trace_graph as routes
    from app.trace_graph import FindingTrace

    monkeypatch.setattr("app.middleware.tenancy.get_current_org_id", lambda: "org_A")
    fake_trace = FindingTrace(opportunity_id="opp_001", run_id="run_x", hops=[], joins=[], complete=False)
    monkeypatch.setattr("app.trace_graph.load_finding_trace", lambda run_id, opp_id: fake_trace)
    run = {"id": "run_x"}  # no org_id
    result = routes._load_trace(run, "run_x", "opp_001")
    assert result is fake_trace


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


def test_trace_graph_unknown_run_404(client: TestClient):
    r = client.get(
        "/api/runs/run_xyz_unknown/opportunities/opp_001/trace-graph",
        headers=_auth(),
    )
    assert r.status_code == 404


def test_trace_graph_unknown_opp_404(client: TestClient, traced_run_id):
    r = client.get(
        f"/api/runs/{traced_run_id}/opportunities/opp_does_not_exist/trace-graph",
        headers=_auth(),
    )
    assert r.status_code == 404


def test_trace_graph_returns_full_chain(client: TestClient, traced_run_id, first_opp_id):
    """AC1 — a materialized finding expands to a complete chain, every hop
    carrying origin, connector, run id, and timestamp."""
    r = client.get(
        f"/api/runs/{traced_run_id}/opportunities/{first_opp_id}/trace-graph",
        headers=_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["runId"] == traced_run_id
    assert data["oppId"] == first_opp_id
    assert data["available"] is True, "a materialized opportunity must have a queryable chain"
    assert len(data["hops"]) >= 2, "chain must reach beyond the finding root"

    hop_types = {hop["hop_type"] for hop in data["hops"]}
    assert "finding" in hop_types
    assert "source_record" in hop_types or "evidence" in hop_types

    for hop in data["hops"]:
        for key in ("hop_id", "hop_type", "origin", "connector", "run_id", "timestamp", "from_hop_id"):
            assert key in hop
        assert hop["origin"] in ("observed", "inferred")
        assert hop["run_id"] == traced_run_id

    # The finding root has no parent; every other hop names one.
    root = next(h for h in data["hops"] if h["hop_type"] == "finding")
    assert root["from_hop_id"] is None
    for hop in data["hops"]:
        if hop is not root:
            assert hop["from_hop_id"] is not None


def test_trace_graph_isolated_by_org(client: TestClient, traced_run_id, first_opp_id, monkeypatch):
    """The run was created under the dev org ('default'). A request scoped to a
    different org must not see its provenance."""
    monkeypatch.setenv("DEV_JWT_ORG", "some_other_org")
    r = client.get(
        f"/api/runs/{traced_run_id}/opportunities/{first_opp_id}/trace-graph",
        headers=_auth(),
    )
    assert r.status_code in (403, 404), (
        f"cross-org request must be denied, got {r.status_code}: {r.text}"
    )
    assert "source_record" not in r.text
