"""CS-4 T4: current_step field in GET /api/runs/{run_id}/status

Integration tests asserting:
- current_step is None when no step has been recorded for the run.
- current_step reflects the step_id written into the run payload by
  db.update_run_step() (CS-4 T3).
- current_step transitions correctly through sf_crm → sf_ncino → complete
  during a mocked discovery run (AC3).
- current_step returns "complete" when the run finishes.
- The field is absent/None on a fresh run with no step recorded.
"""
from __future__ import annotations

import os
import time
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import db


def _auth() -> Dict[str, str]:
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    return {"Authorization": f"Bearer {token}"}


def _make_run(run_id: str, *, current_step: str | None = None) -> None:
    """Insert a minimal run record into the DB, optionally with a current_step."""
    payload: dict = {
        "id": run_id,
        "status": "running",
        "startedAt": db.now_iso(),
        "updatedAt": db.now_iso(),
        "inputs": {},
    }
    if current_step is not None:
        payload["current_step"] = current_step
    db.upsert_run(run_id, payload)


def _set_step(run_id: str, step_id: str) -> None:
    """Simulate db.update_run_step() by writing current_step into the run payload.

    This helper is intentionally inline — the test must not depend on T3 being
    merged into the same branch. When T3 lands, callers can switch to
    db.update_run_step() and remove this helper.
    """
    run = db.get_run(run_id)
    if run is None:
        return
    run["current_step"] = step_id
    db.upsert_run(run_id, run)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def run_id_no_step() -> str:
    """A run with no current_step recorded yet."""
    rid = f"run_t4_nostep_{int(time.time() * 1000)}"
    _make_run(rid)
    return rid


@pytest.fixture()
def run_id_with_step() -> str:
    """A run pre-loaded with current_step = sf_crm."""
    rid = f"run_t4_step_{int(time.time() * 1000)}"
    _make_run(rid, current_step="sf_crm")
    return rid


# ---------------------------------------------------------------------------
# AC3: current_step absent when no step recorded
# ---------------------------------------------------------------------------

def test_current_step_absent_when_not_set(client, run_id_no_step):
    """current_step must be None / absent when no step has been written."""
    r = client.get(f"/api/runs/{run_id_no_step}/status", headers=_auth())
    assert r.status_code == 200, f"status endpoint returned {r.status_code}: {r.text}"
    data = r.json()
    assert "current_step" in data, "current_step field must always be present in response"
    assert data["current_step"] is None, (
        f"Expected None for unstarted run, got {data['current_step']!r}"
    )


# ---------------------------------------------------------------------------
# AC3: current_step reflects last written step_id
# ---------------------------------------------------------------------------

def test_current_step_reflects_written_step(client, run_id_with_step):
    """current_step returns the step_id written into the run payload."""
    r = client.get(f"/api/runs/{run_id_with_step}/status", headers=_auth())
    assert r.status_code == 200
    assert r.json()["current_step"] == "sf_crm"


# ---------------------------------------------------------------------------
# AC3: field updates in real time as run progresses (transition test)
# ---------------------------------------------------------------------------

def test_current_step_transitions_sf_crm_to_sf_ncino_to_complete(client):
    """AC3: assert step transitions sf_crm → sf_ncino → complete.

    Simulates what the runner does via update_run_step() by writing
    directly to the run payload between status polls.
    """
    rid = f"run_t4_trans_{int(time.time() * 1000)}"
    _make_run(rid)

    # Step 1: sf_crm (after Salesforce ingest)
    _set_step(rid, "sf_crm")
    r = client.get(f"/api/runs/{rid}/status", headers=_auth())
    assert r.status_code == 200
    assert r.json()["current_step"] == "sf_crm", (
        f"Expected sf_crm, got {r.json()['current_step']!r}"
    )

    # Step 2: sf_ncino (after nCino ingest, ncino pack)
    _set_step(rid, "sf_ncino")
    r = client.get(f"/api/runs/{rid}/status", headers=_auth())
    assert r.status_code == 200
    assert r.json()["current_step"] == "sf_ncino", (
        f"Expected sf_ncino, got {r.json()['current_step']!r}"
    )

    # Step 3: complete (final step)
    _set_step(rid, "complete")
    r = client.get(f"/api/runs/{rid}/status", headers=_auth())
    assert r.status_code == 200
    assert r.json()["current_step"] == "complete", (
        f"Expected complete, got {r.json()['current_step']!r}"
    )


# ---------------------------------------------------------------------------
# AC3: full step sequence through all DISCOVERY_STEPS
# ---------------------------------------------------------------------------

def test_current_step_full_sequence(client):
    """Step field updates correctly through the full DISCOVERY_STEPS sequence."""
    rid = f"run_t4_full_{int(time.time() * 1000)}"
    _make_run(rid)

    step_sequence = ["sf_crm", "sn", "jira", "detect", "enrich", "complete"]
    for step in step_sequence:
        _set_step(rid, step)
        r = client.get(f"/api/runs/{rid}/status", headers=_auth())
        assert r.status_code == 200
        assert r.json()["current_step"] == step, (
            f"After writing step={step!r}, status returned {r.json()['current_step']!r}"
        )


# ---------------------------------------------------------------------------
# AC3: complete step at run finish
# ---------------------------------------------------------------------------

def test_current_step_is_complete_at_run_finish(client):
    """current_step returns 'complete' when the run reaches its final stage."""
    rid = f"run_t4_done_{int(time.time() * 1000)}"
    _make_run(rid, current_step="complete")
    r = client.get(f"/api/runs/{rid}/status", headers=_auth())
    assert r.status_code == 200
    assert r.json()["current_step"] == "complete"


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def test_status_requires_auth(client, run_id_no_step):
    """Status endpoint must reject unauthenticated requests."""
    r = client.get(f"/api/runs/{run_id_no_step}/status")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 404 for unknown run
# ---------------------------------------------------------------------------

def test_status_unknown_run_404(client):
    """Status for a non-existent run must return 404."""
    r = client.get("/api/runs/run_does_not_exist_cs4t4/status", headers=_auth())
    assert r.status_code == 404
