import os

def _auth_headers():
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    return {"Authorization": f"Bearer {token}"}

def test_roadmap_and_sources_from_run_inputs(client):
    body = {
        "connectedSources": ["ServiceNow", "Jira"],
        "uploadedFiles": ["incident_data.csv", "cmdb_records.xlsx"],
        "sampleWorkspaceEnabled": False
    }
    r = client.post("/api/runs/start", headers=_auth_headers(), json=body)
    assert r.status_code == 200
    run_id = r.json()["runId"]

    rm = client.get(f"/api/runs/{run_id}/roadmap", headers=_auth_headers()).json()
    assert "stages" in rm and isinstance(rm["stages"], list) and len(rm["stages"]) == 3

    er = client.get(f"/api/runs/{run_id}/executive-report", headers=_auth_headers()).json()
    sa = er.get("sourcesAnalyzed") or {}
    assert isinstance(sa, dict)

    # Must be derived from the *run inputs* (not live connector state)
    assert sa.get("totalConnected") == len(body["connectedSources"])
    assert sa.get("uploadedFiles") == len(body["uploadedFiles"])

    # Presence + type checks (recommended list may evolve)
    assert "recommendedConnected" in sa
    assert isinstance(sa.get("recommendedConnected"), int)
    assert sa.get("recommendedConnected") >= 0
    assert sa.get("sampleWorkspaceEnabled") is False
