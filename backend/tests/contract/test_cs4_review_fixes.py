"""CS-4 code-review fixes — regression tests.

Covers the behaviour added when resolving the CS-4 review findings:

* Issue #4 — db.update_run_step() validates step_id against the canonical
  DISCOVERY_STEPS and ignores (does not persist) an unknown/misspelled id
  instead of silently writing it.
* Issue #1 — db.update_run_step(..., ok=False) records the stage in the run's
  failed_steps, and GET /api/runs/{run_id}/status surfaces failed_steps so the
  UI can render a failed ingest distinctly (not as a completed green check). A
  later ok=True for the same step clears it.
* Issue #2 / AC1 — ncino.ingest() reuses preloaded ProcessInstance records
  (no duplicate Salesforce query) when they are supplied, and falls back to its
  own _fetch_approval_instances when None is passed (Salesforce pass produced
  nothing / failed).
* Issue #3 — DISCOVERY_STEPS now lives in the discovery layer.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import db


def _auth() -> Dict[str, str]:
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _make_run(run_id: str) -> None:
    db.upsert_run(
        run_id,
        {
            "id": run_id,
            "status": "running",
            "startedAt": db.now_iso(),
            "updatedAt": db.now_iso(),
            "inputs": {},
        },
    )


# ---------------------------------------------------------------------------
# Issue #3 — DISCOVERY_STEPS relocated to the discovery layer
# ---------------------------------------------------------------------------

def test_discovery_steps_live_in_discovery_layer():
    from discovery.steps import DISCOVERY_STEPS, DISCOVERY_STEP_IDS

    assert DISCOVERY_STEPS[0] == "sf_crm"
    assert DISCOVERY_STEPS[-1] == "complete"
    # Emission order: each connected source emits its own step at the START of its
    # ingest (CRM -> ServiceNow -> Jira -> Azure events -> AWS events -> Slack ->
    # Teams -> Confluence -> SharePoint -> GitHub -> Java -> .NET), then the
    # pack-specific second SF pass for each declared product (sf_ncino / sf_fsc),
    # then detect/enrich/complete.
    #
    # This list is expected to GROW as sources are added, and the assertion is
    # deliberately exact rather than a subset check: the point of this test is
    # that the vocabulary lives in the discovery layer and is reviewed when it
    # changes. `azure_events` / `aws_events` (the native MSP-B1/B2 cloud event
    # connectors) and `sf_fsc` (the 2.0-D1 Financial Services Cloud pack's second
    # Salesforce pass) were added to discovery/steps.py without this copy of the
    # expectation being updated, so keep the two in step when adding a source.
    assert DISCOVERY_STEPS == [
        "sf_crm", "sn", "jira", "azure_events", "aws_events", "slack", "teams",
        "confluence", "sharepoint", "github", "java_app", "dotnet_app",
        "sf_ncino", "sf_fsc", "detect", "enrich", "complete",
    ]
    assert DISCOVERY_STEP_IDS == frozenset(DISCOVERY_STEPS)
    # It must NOT be re-exported from app.db anymore.
    assert not hasattr(db, "DISCOVERY_STEPS")


# ---------------------------------------------------------------------------
# Issue #4 — unknown step_id is rejected, not silently written
# ---------------------------------------------------------------------------

def test_update_run_step_ignores_unknown_step_id():
    run_id = f"run_cs4val_{int(time.time() * 1000)}"
    _make_run(run_id)

    db.update_run_step(run_id, "sf_crm")
    assert db.get_run(run_id)["current_step"] == "sf_crm"

    # A misspelled id must not clobber the last valid step.
    db.update_run_step(run_id, "sf_crmm")
    assert db.get_run(run_id)["current_step"] == "sf_crm"


# ---------------------------------------------------------------------------
# Issue #1 — failed steps are tracked and surfaced by /status
# ---------------------------------------------------------------------------

def test_failed_step_tracked_and_surfaced_then_cleared(client):
    run_id = f"run_cs4fail_{int(time.time() * 1000)}"
    _make_run(run_id)

    # A failed ingest still advances current_step but is recorded as failed.
    db.update_run_step(run_id, "sn", ok=False)
    run = db.get_run(run_id)
    assert run["current_step"] == "sn"
    assert "sn" in run.get("failed_steps", [])

    r = client.get(f"/api/runs/{run_id}/status", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_step"] == "sn"
    assert "sn" in body.get("failed_steps", [])

    # A subsequent success for the same step clears the failed marker.
    db.update_run_step(run_id, "sn", ok=True)
    assert "sn" not in db.get_run(run_id).get("failed_steps", [])


def test_status_failed_steps_defaults_empty(client):
    run_id = f"run_cs4ok_{int(time.time() * 1000)}"
    _make_run(run_id)
    db.update_run_step(run_id, "sf_crm")  # ok=True default

    r = client.get(f"/api/runs/{run_id}/status", headers=_auth())
    assert r.status_code == 200
    assert r.json().get("failed_steps", []) == []


# ---------------------------------------------------------------------------
# Issue #2 / AC1 — preloaded ProcessInstance reuse vs independent fallback
# ---------------------------------------------------------------------------

def _stub_ncino_live(monkeypatch):
    """Force ncino.ingest down the live path with all network fetches stubbed."""
    from discovery.ingest import ncino

    class _DummyClient:  # noqa: D401 - test stub
        pass

    monkeypatch.setattr(ncino, "is_live", lambda: True)
    monkeypatch.setattr(ncino, "_get_client", lambda: _DummyClient())
    for name in (
        "_fetch_loans",
        "_fetch_stage_history",
        "_fetch_covenant_compliance",
        "_fetch_checklists",
        "_fetch_spreads",
        "_fetch_spread_periods",
    ):
        monkeypatch.setattr(ncino, name, lambda _client: [])
    return ncino


def test_ncino_reuses_preloaded_process_instances_no_duplicate_query(monkeypatch):
    ncino = _stub_ncino_live(monkeypatch)
    calls = {"approval_fetch": 0}

    def _spy(_client):
        calls["approval_fetch"] += 1
        return [{"Id": "pi_self_fetched"}]

    monkeypatch.setattr(ncino, "_fetch_approval_instances", _spy)

    preloaded = [{"Id": "pi_from_sf", "TargetObjectId": "x", "Status": "Pending"}]
    out = ncino.ingest(preloaded_process_instances=preloaded)

    # AC1: no second ProcessInstance query was issued.
    assert calls["approval_fetch"] == 0
    # The Salesforce-provided records are the ones used.
    assert out["process_instances"] == preloaded


def test_ncino_falls_back_to_own_fetch_when_not_preloaded(monkeypatch):
    ncino = _stub_ncino_live(monkeypatch)
    calls = {"approval_fetch": 0}

    def _spy(_client):
        calls["approval_fetch"] += 1
        return [{"Id": "pi_self_fetched"}]

    monkeypatch.setattr(ncino, "_fetch_approval_instances", _spy)

    # None (Salesforce pass produced nothing / failed) → independent fallback.
    out = ncino.ingest(preloaded_process_instances=None)

    assert calls["approval_fetch"] == 1
    assert out["process_instances"] == [{"Id": "pi_self_fetched"}]
