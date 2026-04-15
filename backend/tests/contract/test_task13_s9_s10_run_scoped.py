import os
from fastapi.testclient import TestClient
from backend.app.main import app

def _auth_headers():
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    return {"Authorization": f"Bearer {token}"}

def test_s9_roadmap_and_s10_exec_report_are_run_scoped():
    client = TestClient(app)

    body = {
        "connectedSources": ["ServiceNow", "Jira & Confluence"],
        "uploadedFiles": ["incident_data.csv", "cmdb_records.xlsx"],
        "sampleWorkspaceEnabled": False
    }
    r = client.post("/api/runs/start", headers=_auth_headers(), json=body)
    assert r.status_code == 200
    run_id = r.json().get("runId") or r.json().get("id")
    assert run_id

    rr = client.get(f"/api/runs/{run_id}/roadmap", headers=_auth_headers())
    assert rr.status_code == 200
    roadmap = rr.json()
    assert "stages" in roadmap and isinstance(roadmap["stages"], list)

    er = client.get(f"/api/runs/{run_id}/executive-report", headers=_auth_headers())
    assert er.status_code == 200
    data = er.json()
    sa = data.get("sourcesAnalyzed") or {}
    assert isinstance(sa.get("totalConnected"), int)
    assert isinstance(sa.get("uploadedFiles"), int)
    assert isinstance(sa.get("recommendedConnected"), int)
    assert sa["totalConnected"] == len(body.get("connectedSources", []))
    assert sa["uploadedFiles"] == len(body.get("uploadedFiles", []))
