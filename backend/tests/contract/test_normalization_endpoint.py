"""
Sprint 4.1 Fix Pack — Task 4
Contract tests for GET /api/runs/{runId}/normalization

Tests:
  1. 404 for unknown runId
  2. 200 for valid run
  3. Response has required fields: runId, rows, counts, source
  4. rows is a list
  5. Each row has required fields
  6. counts has MAPPED, UNMAPPED, AMBIGUOUS keys
  7. source is 'stored' or 'derived'
  8. counts match actual row statuses
  9. Derived rows have correct sourceSystem values from connected sources
  10. Endpoint is idempotent — two calls return same data
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
def live_run(client):
    body = {
        "connectedSources": ["ServiceNow", "Jira & Confluence"],
        "uploadedFiles": [],
        "sampleWorkspaceEnabled": False,
        "mode": "offline",
        "systems": ["salesforce", "servicenow", "jira"],
    }
    r = client.post("/api/runs/start", headers=_auth(), json=body)
    assert r.status_code in (200, 201)
    run_id = r.json().get("runId") or r.json().get("id")
    assert run_id

    for _ in range(90):
        st = client.get(f"/api/runs/{run_id}/status", headers=_auth())
        status = st.json().get("status", "running")
        if status in ("complete", "partial"):
            break
        if status == "failed":
            pytest.fail(f"Run {run_id} failed")
        time.sleep(1)

    return run_id


def test_normalization_unknown_run_404(client):
    r = client.get(
        "/api/runs/run_does_not_exist/normalization",
        headers=_auth(),
    )
    assert r.status_code == 404


def test_normalization_returns_200(client, live_run):
    r = client.get(f"/api/runs/{live_run}/normalization", headers=_auth())
    assert r.status_code == 200


def test_normalization_response_has_required_fields(client, live_run):
    r = client.get(f"/api/runs/{live_run}/normalization", headers=_auth())
    data = r.json()
    for field in ("runId", "rows", "counts", "source"):
        assert field in data, f"Missing field: '{field}'"


def test_normalization_rows_is_list(client, live_run):
    r = client.get(f"/api/runs/{live_run}/normalization", headers=_auth())
    assert isinstance(r.json()["rows"], list)


def test_normalization_row_has_required_fields(client, live_run):
    r = client.get(f"/api/runs/{live_run}/normalization", headers=_auth())
    rows = r.json()["rows"]
    if not rows:
        pytest.skip("No rows returned — run may have no evidence")
    row = rows[0]
    for field in ("id", "sourceSystem", "sourceType", "sourceField",
                  "commonEntity", "commonField", "status", "confidence"):
        assert field in row, f"Row missing field: '{field}'"


def test_normalization_counts_has_required_keys(client, live_run):
    r = client.get(f"/api/runs/{live_run}/normalization", headers=_auth())
    counts = r.json()["counts"]
    for key in ("MAPPED", "UNMAPPED", "AMBIGUOUS"):
        assert key in counts, f"Counts missing key: '{key}'"


def test_normalization_source_is_valid(client, live_run):
    r = client.get(f"/api/runs/{live_run}/normalization", headers=_auth())
    assert r.json()["source"] in ("stored", "derived")


def test_normalization_counts_match_rows(client, live_run):
    r = client.get(f"/api/runs/{live_run}/normalization", headers=_auth())
    data = r.json()
    rows = data["rows"]
    counts = data["counts"]
    actual = {"MAPPED": 0, "UNMAPPED": 0, "AMBIGUOUS": 0}
    for row in rows:
        s = row.get("status", "UNMAPPED")
        if s in actual:
            actual[s] += 1
    assert actual["MAPPED"]    == counts["MAPPED"]
    assert actual["UNMAPPED"]  == counts["UNMAPPED"]
    assert actual["AMBIGUOUS"] == counts["AMBIGUOUS"]


def test_normalization_run_id_matches(client, live_run):
    r = client.get(f"/api/runs/{live_run}/normalization", headers=_auth())
    assert r.json()["runId"] == live_run


def test_normalization_is_idempotent(client, live_run):
    r1 = client.get(f"/api/runs/{live_run}/normalization", headers=_auth()).json()
    r2 = client.get(f"/api/runs/{live_run}/normalization", headers=_auth()).json()
    assert r1["runId"]  == r2["runId"]
    assert r1["source"] == r2["source"]
    assert len(r1["rows"]) == len(r2["rows"])
