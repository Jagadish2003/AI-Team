import os
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_JWT", "dev-token-change-me")
os.environ.setdefault("DB_PATH", "database/dev.db")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5173")

from app.main import app

client = TestClient(app)

def auth_headers():
    return {"Authorization": f"Bearer {os.environ['DEV_JWT']}"}

def test_auth_required():
    r = client.get("/api/connectors")
    assert r.status_code == 401

def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "ts" in body

def test_connectors_shape():
    r = client.get("/api/connectors", headers=auth_headers())
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 3
    required_keys = {"id","name","tier","status"}
    assert required_keys.issubset(set(data[0].keys()))

def test_start_run_and_run_scoped_reads():
    # Start run
    payload = {"connectedSources":["ServiceNow"],"uploadedFiles":[],"sampleWorkspaceEnabled":True}
    r = client.post("/api/runs/start", headers=auth_headers(), json=payload)
    assert r.status_code == 200
    body = r.json()
    assert "runId" in body and body["runId"].startswith("run_")
    assert body["status"] == "running"
    assert "startedAt" in body

    run_id = body["runId"]

    # run-scoped endpoints must accept runId and return 200
    # events shape
    events = client.get(f"/api/runs/{run_id}/events", headers=auth_headers()).json()
    assert isinstance(events, list) and len(events) >= 1
    assert "stage" in events[0]

    # entities shape (ExtractedEntity)
    ents = client.get(f"/api/runs/{run_id}/entities", headers=auth_headers()).json()
    assert isinstance(ents, list) and len(ents) >= 1
    assert {"id","name","type","confidence"}.issubset(set(ents[0].keys()))

    # mappings shape
    maps = client.get(f"/api/runs/{run_id}/mappings", headers=auth_headers()).json()
    assert isinstance(maps, list) and len(maps) >= 1
    assert {"id","sourceSystem","sourceField","commonField","status","confidence","commonEntity"}.issubset(set(maps[0].keys()))

    # audit shape (ReviewAuditEvent)
    audit = client.get(f"/api/runs/{run_id}/audit", headers=auth_headers()).json()
    assert isinstance(audit, list) and len(audit) >= 1
    assert {"id","tsLabel","action","by"}.issubset(set(audit[0].keys()))

    # executive report shape
    er = client.get(f"/api/runs/{run_id}/executive-report", headers=auth_headers()).json()
    assert {"confidence","sourcesAnalyzed","topQuickWins","snapshotBubbles","roadmapHighlights"}.issubset(set(er.keys()))



def test_invalid_run_id_returns_404():
    """Enforce CONTRACT_RULES: no latest-run fallback."""
    fake_id = "run_does_not_exist_xyz"
    run_scoped = [
        f"/api/runs/{fake_id}",
        f"/api/runs/{fake_id}/events",
        f"/api/runs/{fake_id}/evidence",
        f"/api/runs/{fake_id}/entities",
        f"/api/runs/{fake_id}/mappings",
        f"/api/runs/{fake_id}/opportunities",
        f"/api/runs/{fake_id}/audit",
        f"/api/runs/{fake_id}/roadmap",
        f"/api/runs/{fake_id}/executive-report",
    ]
    for ep in run_scoped:
        r = client.get(ep, headers=auth_headers())
        assert r.status_code == 404, f"{ep} returned {r.status_code} — must return 404 for unknown runId"


def test_permissions_shape():
    r = client.get("/api/permissions", headers=auth_headers())
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert {"id","label","sourceSystem","required","satisfied"}.issubset(set(data[0].keys()))

def test_evidence_shape_and_decision_write_is_run_scoped():
    # create run
    r = client.post("/api/runs/start", headers=auth_headers(), json={"connectedSources":[],"uploadedFiles":[],"sampleWorkspaceEnabled":True})
    run_id = r.json()["runId"]

    ev = client.get(f"/api/runs/{run_id}/evidence", headers=auth_headers()).json()
    assert len(ev) >= 1
    e0 = ev[0]
    assert {"id","tsLabel","source","evidenceType","title","snippet","entities","confidence","decision"}.issubset(set(e0.keys()))

    # write decision (run-scoped)
    w = client.post(f"/api/runs/{run_id}/evidence/{e0['id']}/decision", headers=auth_headers(), json={"decision":"APPROVED"})
    assert w.status_code == 200
    assert w.json()["decision"] == "APPROVED"

def test_opportunity_shape_and_override_write_is_run_scoped():
    r = client.post("/api/runs/start", headers=auth_headers(), json={"connectedSources":[],"uploadedFiles":[],"sampleWorkspaceEnabled":True})
    run_id = r.json()["runId"]

    opps = client.get(f"/api/runs/{run_id}/opportunities", headers=auth_headers()).json()
    assert len(opps) >= 2
    o0 = opps[0]
    assert {"id","title","impact","effort","confidence","tier","decision","aiRationale","override"}.issubset(set(o0.keys()))
    assert o0["confidence"] in ("HIGH","MEDIUM","LOW"), f"confidence enum must be uppercase, got {o0['confidence']!r}"
    assert o0["decision"] in ("UNREVIEWED","APPROVED","REJECTED"), f"decision enum must be uppercase, got {o0['decision']!r}"
    assert {"isLocked","rationaleOverride","overrideReason","updatedAt"}.issubset(set(o0["override"].keys()))

    # override write (run-scoped)
    w = client.post(f"/api/runs/{run_id}/opportunities/{o0['id']}/override", headers=auth_headers(),
                    json={"rationaleOverride":"test override","overrideReason":"test reason","isLocked":False})
    assert w.status_code == 200
    out = w.json()
    assert out["override"]["overrideReason"] == "test reason"

    # decision write (run-scoped)
    d = client.post(
        f"/api/runs/{run_id}/opportunities/{o0['id']}/decision",
        headers=auth_headers(),
        json={"decision": "APPROVED"},
    )
    assert d.status_code == 200
    assert d.json()["decision"] == "APPROVED"

def test_roadmap_stage90_not_empty():
    # Use seed-driven demo data via /roadmap endpoint. Stage 90 should have >=1 Complex item.
    r = client.post("/api/runs/start", headers=auth_headers(), json={"connectedSources":[],"uploadedFiles":[],"sampleWorkspaceEnabled":True})
    run_id = r.json()["runId"]

    roadmap = client.get(f"/api/runs/{run_id}/roadmap", headers=auth_headers()).json()
    assert "stages" in roadmap, "roadmap response missing \"stages\" key"
    stages = roadmap["stages"]
    assert isinstance(stages, list) and len(stages) == 3, f"Expected 3 stages, got {len(stages)}"
    next90 = next((s for s in stages if s.get("id") == "NEXT_90"), None)
    assert next90 is not None, "NEXT_90 stage not found in roadmap"
    assert len(next90.get("opportunities", [])) >= 1, "Stage 90 empty — seed must include at least one Complex UNREVIEWED/APPROVED opportunity"
