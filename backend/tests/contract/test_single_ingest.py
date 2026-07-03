"""Regression guard: a discovery run ingests each system-of-record exactly once.

Before the single-ingest fix, materialization ran a ``_probe_systems`` pre-pass
that ingested Salesforce/ServiceNow/Jira (throwing the data away) and THEN the
discovery runner ingested the same systems again — doubling the most expensive
live ingest per run. The runner is now the sole ingest and returns the
per-system summary in its payload. This test counts ingest calls across one full
offline run and asserts each system of record fires exactly once.
"""
import os
import time

from discovery.ingest import jira as jira_mod
from discovery.ingest import salesforce as sf
from discovery.ingest import servicenow as sn


def _auth():
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


def test_systems_of_record_ingested_once(client, monkeypatch):
    counts = {"salesforce": 0, "servicenow": 0, "jira": 0}

    def _wrap(name, original):
        def _counting(*args, **kwargs):
            counts[name] += 1
            return original(*args, **kwargs)

        return _counting

    # The runner resolves `module.ingest` at call time, so patching the module
    # attribute is picked up. Each wrapper calls through to the real offline
    # ingest so the run still produces fixture data and reaches complete/partial.
    monkeypatch.setattr(sf, "ingest", _wrap("salesforce", sf.ingest))
    monkeypatch.setattr(sn, "ingest", _wrap("servicenow", sn.ingest))
    monkeypatch.setattr(jira_mod, "ingest", _wrap("jira", jira_mod.ingest))

    body = {
        "connectedSources": [],
        "uploadedFiles": [],
        "sampleWorkspaceEnabled": False,
        "mode": "offline",
        "systems": ["salesforce", "servicenow", "jira"],
    }
    r = client.post("/api/runs/start", headers=_auth(), json=body)
    assert r.status_code in (200, 201), r.text
    run_id = r.json()["runId"]

    # BackgroundTasks run within the TestClient request, but poll defensively so
    # the ingest counters are populated before we assert.
    status = "running"
    for _ in range(30):
        st = client.get(f"/api/runs/{run_id}/status", headers=_auth())
        if st.status_code == 200:
            status = st.json().get("status", "running")
            if status in ("complete", "partial", "failed"):
                break
        time.sleep(1)

    assert status in ("complete", "partial"), f"run reached {status!r}"
    assert counts == {"salesforce": 1, "servicenow": 1, "jira": 1}, (
        f"each system of record must be ingested exactly once, got {counts}"
    )
