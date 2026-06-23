import os

def _auth_headers():
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    return {"Authorization": f"Bearer {token}"}

def test_audit_requires_tsEpoch_and_is_sorted_newest_first(client):
    # Start a run
    r = client.post("/api/runs/start", headers=_auth_headers(), json={
        "connectedSources": ["ServiceNow", "Jira"],
        "uploadedFiles": ["incident_data.csv"],
        "sampleWorkspaceEnabled": False
    })
    assert r.status_code == 200
    run_id = r.json()["runId"]

    # evidence decision write (if endpoint exists)
    ev = client.get(f"/api/runs/{run_id}/evidence", headers=_auth_headers()).json()
    if isinstance(ev, list) and len(ev) > 0:
        ev_id = ev[0]["id"]
        wd = client.post(
            f"/api/runs/{run_id}/evidence/{ev_id}/decision",
            headers=_auth_headers(),
            json={"decision": "APPROVED"}
        )
        assert wd.status_code in (200, 204)

    # pick an opportunity
    opps = client.get(f"/api/runs/{run_id}/opportunities", headers=_auth_headers()).json()
    assert isinstance(opps, list) and len(opps) > 0
    opp_id = opps[0]["id"]

    # decision write
    d = client.post(
        f"/api/runs/{run_id}/opportunities/{opp_id}/decision",
        headers=_auth_headers(),
        json={"decision": "APPROVED"}
    )
    assert d.status_code == 200

    # override write
    o = client.post(
        f"/api/runs/{run_id}/opportunities/{opp_id}/override",
        headers=_auth_headers(),
        json={"rationaleOverride": "Override rationale", "overrideReason": "Test", "isLocked": False}
    )
    assert o.status_code == 200

    # fetch audit
    audit = client.get(f"/api/runs/{run_id}/audit", headers=_auth_headers()).json()
    assert isinstance(audit, list) and len(audit) >= 2

    epochs = [int(e.get("tsEpoch", 0)) for e in audit]

    # Guard: fail loudly if tsEpoch was never added
    assert any(ep > 0 for ep in epochs),         "No tsEpoch found in audit entries — backend write endpoints/default_audit not updated"

    # Full ordering check
    assert epochs == sorted(epochs, reverse=True),         f"Audit must be newest-first by tsEpoch. Got: {epochs}"
