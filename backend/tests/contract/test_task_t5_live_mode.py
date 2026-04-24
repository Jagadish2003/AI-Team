"""
Sprint 4 T5 contract tests  v1.3

Changes from v1.2:
  Issue A: Evidence determinism normalisation now explicitly sorts by stable
           key (id, then (source, title) as fallback) and strips volatile
           fields (replayedAt, updatedAt). Comment explains the reasoning.
  Issue 2: Roadmap determinism test added. build_roadmap() is deterministic
           so roadmap bytes should be identical before and after replay.
           If T2 starts storing roadmap in KV, this test still passes.
  Issue 3: exec report snapshotBubbles asserted as [] (no seed data).
           roadmapHighlights asserted as derived from opps (no seed record).
  Issue B: /status ownership — test only checks T2-owned status endpoint.
           No duplicate /status added to main.py by T5.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from app.main import app


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


# Only strip fields that change by design on replay.
# startedAt and completedAt must NOT be stripped — they should be stable.
_STRIP_KEYS = {"replayedAt", "updatedAt"}


def _strip_timestamps(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_timestamps(v) for k, v in obj.items()
                if k not in _STRIP_KEYS}
    if isinstance(obj, list):
        return [_strip_timestamps(i) for i in obj]
    return obj


def _normalize(items: Any) -> Any:
    """
    Sort list of dicts by stable key after stripping timestamps.
    Primary sort key: 'id'. Fallback: (source, title) for evidence objects
    that may not have an id field. Final fallback: JSON serialisation.
    This prevents insertion-order variance from causing false failures.
    """
    if not isinstance(items, list):
        return items
    def sort_key(x):
        if "id" in x:
            return x["id"]
        return json.dumps(
            {k: v for k, v in sorted(x.items()) if k not in _STRIP_KEYS},
            sort_keys=True
        )
    return sorted([_strip_timestamps(i) for i in items], key=sort_key)


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


@pytest.fixture(scope="session")
def live_run_id(client):
    """
    Start a run and wait for complete/partial.
    Hard assert — failed runs must not leak into T5 tests.
    """
    body = {
        "connectedSources":       ["ServiceNow", "Jira & Confluence"],
        "uploadedFiles":          ["upload_001"],
        "sampleWorkspaceEnabled": False,
        "mode":    "offline",
        "systems": ["salesforce", "servicenow", "jira"],
    }
    r = client.post("/api/runs/start", headers=_auth(), json=body)
    assert r.status_code in (200, 201), f"start failed: {r.text}"
    run_id = r.json().get("runId") or r.json().get("id")
    assert run_id

    status = "running"
    for _ in range(30):
        st = client.get(f"/api/runs/{run_id}/status", headers=_auth())
        if st.status_code == 200:
            status = st.json().get("status", "running")
            if status in ("complete", "partial", "failed"):
                break
        time.sleep(1)

    assert status in ("complete", "partial"), (
        f"Run '{run_id}' reached '{status}' — cannot use for T5 tests. "
        "Check Track B offline mode is working."
    )
    return run_id


# ─────────────────────────────────────────────────────────────────────────────
# 404 guards
# ─────────────────────────────────────────────────────────────────────────────

def test_evidence_unknown_run_404(client):
    r = client.get("/api/runs/run_xyz_unknown/evidence", headers=_auth())
    assert r.status_code == 404

def test_roadmap_unknown_run_404(client):
    r = client.get("/api/runs/run_xyz_unknown/roadmap", headers=_auth())
    assert r.status_code == 404

def test_exec_report_unknown_run_404(client):
    r = client.get("/api/runs/run_xyz_unknown/executive-report", headers=_auth())
    assert r.status_code == 404

def test_status_unknown_run_404(client):
    r = client.get("/api/runs/run_xyz_unknown/status", headers=_auth())
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# S3 status — owned by T2 routes, T5 does not touch it
# ─────────────────────────────────────────────────────────────────────────────

def test_status_returns_200(client, live_run_id):
    """T2-owned endpoint. T5 does not add or modify /status."""
    r = client.get(f"/api/runs/{live_run_id}/status", headers=_auth())
    assert r.status_code == 200

def test_status_has_required_fields(client, live_run_id):
    r = client.get(f"/api/runs/{live_run_id}/status", headers=_auth())
    assert r.status_code == 200
    data = r.json()
    for field in ("runId", "status"):
        assert field in data, f"Missing '{field}': {data}"

def test_status_reflects_complete(client, live_run_id):
    r = client.get(f"/api/runs/{live_run_id}/status", headers=_auth())
    assert r.json().get("status") in ("complete", "partial")


# ─────────────────────────────────────────────────────────────────────────────
# S4 evidence — run-scoped, hard assertion
# ─────────────────────────────────────────────────────────────────────────────

def test_evidence_returns_list(client, live_run_id):
    r = client.get(f"/api/runs/{live_run_id}/evidence", headers=_auth())
    assert r.status_code == 200
    assert isinstance(r.json(), list)

def test_evidence_is_run_scoped(client, live_run_id):
    """
    Hard assertion — no skip. /evidence confirmed present in A-Task-12.
    If this fails, T2 PATCH_materialize_t2.py has not been applied.
    """
    r = client.get(f"/api/runs/{live_run_id}/evidence", headers=_auth())
    assert r.status_code == 200
    assert len(r.json()) >= 1, (
        "Evidence empty after completed run. "
        "Apply PATCH_materialize_t2.py to materialize_t2.py."
    )

def test_evidence_deterministic_after_replay(client, live_run_id):
    """
    Evidence determinism gate.
    Normalised: sorted by id (stable key), timestamps stripped.
    Hard assertion — no skip.
    """
    before = client.get(f"/api/runs/{live_run_id}/evidence", headers=_auth())
    assert before.status_code == 200

    client.post(f"/api/runs/{live_run_id}/replay", headers=_auth(), json={})

    after = client.get(f"/api/runs/{live_run_id}/evidence", headers=_auth())
    assert after.status_code == 200

    assert _normalize(before.json()) == _normalize(after.json()), (
        "Evidence changed after replay. T4 read-only contract may be violated."
    )


# ─────────────────────────────────────────────────────────────────────────────
# S9 roadmap — run-scoped, determinism pinned
# ─────────────────────────────────────────────────────────────────────────────

def test_roadmap_returns_200(client, live_run_id):
    r = client.get(f"/api/runs/{live_run_id}/roadmap", headers=_auth())
    assert r.status_code == 200

def test_roadmap_has_stages(client, live_run_id):
    r = client.get(f"/api/runs/{live_run_id}/roadmap", headers=_auth())
    assert isinstance(r.json(), dict) and len(r.json()) >= 1

def test_roadmap_deterministic_after_replay(client, live_run_id):
    """
    Issue 2: build_roadmap() is deterministic — same opps → same roadmap.
    Pin roadmap bytes before and after replay.
    When T2 starts storing roadmap in KV, this test still passes.
    """
    before = client.get(f"/api/runs/{live_run_id}/roadmap", headers=_auth())
    assert before.status_code == 200

    client.post(f"/api/runs/{live_run_id}/replay", headers=_auth(), json={})

    after = client.get(f"/api/runs/{live_run_id}/roadmap", headers=_auth())
    assert after.status_code == 200

    assert _strip_timestamps(before.json()) == _strip_timestamps(after.json()), (
        "Roadmap changed after replay. build_roadmap() must be deterministic."
    )


# ─────────────────────────────────────────────────────────────────────────────
# S10 executive report — fully live, no seed decorations
# ─────────────────────────────────────────────────────────────────────────────

def test_exec_report_returns_200(client, live_run_id):
    r = client.get(f"/api/runs/{live_run_id}/executive-report", headers=_auth())
    assert r.status_code == 200

def test_exec_report_has_sources_analyzed(client, live_run_id):
    r = client.get(f"/api/runs/{live_run_id}/executive-report", headers=_auth())
    sa = r.json().get("sourcesAnalyzed", {})
    assert "totalConnected" in sa

def test_exec_report_top_quick_wins_from_run(client, live_run_id):
    """topQuickWins must all have tier == Quick Win — live data not seed."""
    r = client.get(f"/api/runs/{live_run_id}/executive-report", headers=_auth())
    for opp in r.json().get("topQuickWins", []):
        assert opp.get("tier") == "Quick Win", (
            f"Non-Quick-Win in topQuickWins: {opp.get('id')} tier={opp.get('tier')}"
        )

def test_exec_report_snapshot_bubbles_empty(client, live_run_id):
    """
    Issue 3 / Option A: snapshotBubbles must be [] until T6 LLM enrichment.
    No seed record data on S10.
    """
    r = client.get(f"/api/runs/{live_run_id}/executive-report", headers=_auth())
    bubbles = r.json().get("snapshotBubbles")
    assert bubbles == [], (
        f"snapshotBubbles should be [] (no seed data). Got: {bubbles}"
    )

def test_exec_report_roadmap_highlights_derived(client, live_run_id):
    """
    Issue 3 / Option A: roadmapHighlights derived from opps tier counts.
    next30Count must equal the number of Quick Win opportunities.
    No seed record used.
    """
    opps_r = client.get(f"/api/runs/{live_run_id}/opportunities", headers=_auth())
    opps   = opps_r.json()
    qw_count = sum(1 for o in opps if o.get("tier") == "Quick Win")

    exec_r = client.get(f"/api/runs/{live_run_id}/executive-report", headers=_auth())
    highlights = exec_r.json().get("roadmapHighlights", {})
    assert highlights.get("next30Count") == qw_count, (
        f"roadmapHighlights.next30Count={highlights.get('next30Count')} "
        f"but Quick Win count from opps={qw_count}. "
        "Highlights must be derived from run opps, not seed record."
    )


# ─────────────────────────────────────────────────────────────────────────────
# isReplay surfaced after replay
# ─────────────────────────────────────────────────────────────────────────────

def test_status_shows_is_replay_after_replay(client, live_run_id):
    client.post(f"/api/runs/{live_run_id}/replay", headers=_auth(), json={})
    r = client.get(f"/api/runs/{live_run_id}/status", headers=_auth())
    assert r.json().get("isReplay") is True
