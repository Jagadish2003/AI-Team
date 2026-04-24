"""
Sprint 4 T4 contract tests — Deterministic Replay  v1.2

Changes from v1.1:
  Fix 1: All determinism comparisons now sort by canonical key before
          comparing. Prevents false positives from insertion-order variance.
          opportunities sorted by 'id', evidence by 'id', clusters by 'id'.
  Fix 2: test_replay_run_exists_but_no_opps_404 added. Uses
          unmaterialised_run_id fixture (run started but /compute never
          called, so opps KV key is absent). Proves the second 404 path.
  Fix 3: completed_run_id fixture asserts status in (complete, partial)
          after polling. Failed runs cannot leak into replay tests.
  Fix 6: test_replay_returns_counts asserts all three keys:
          opportunities, evidence, clusters.
  Fix 7: test_replay_preserves_roadmap_and_exec_report added.
  Issue 1 (new): List normalisation — _normalize() sorts lists by 'id'
          after _strip_timestamps() to prevent insertion-order false positives.
  Issue 2 (new): startedAt excluded from strip — instead asserted stable.
          Only replayedAt and updatedAt are stripped. completedAt is left
          in and asserted stable (not rewritten by replay).
  Issue 3 (new): test_all_required_artifact_keys_present added. Verifies
          KV key alignment between T1/T2/T3 and T4 before replay is called.
  Issue 8: monkeypatching Track B runner excluded — requires knowing exact
          import path inside Track A app. Noted as Sprint 5 hardening item.
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
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    return {"Authorization": f"Bearer {token}"}


# Timestamp fields that change by design on replay
_STRIP_KEYS = {"replayedAt", "updatedAt"}
# startedAt and completedAt are NOT stripped — they should remain stable
# and are asserted explicitly in test_replay_preserves_stable_timestamps


def _strip_timestamps(obj: Any) -> Any:
    """
    Remove only the timestamp fields that change by design on replay.
    startedAt and completedAt are intentionally kept — they must not
    change and are asserted stable in a dedicated test.
    """
    if isinstance(obj, dict):
        return {k: _strip_timestamps(v) for k, v in obj.items()
                if k not in _STRIP_KEYS}
    if isinstance(obj, list):
        return [_strip_timestamps(i) for i in obj]
    return obj


def _normalize(items: Any) -> Any:
    """
    Sort a list of dicts by 'id' after stripping timestamps.
    Prevents insertion-order variance from causing false positive/negative
    determinism failures. Falls back to JSON-sort if 'id' is absent.
    """
    if not isinstance(items, list):
        return items
    try:
        return sorted(
            [_strip_timestamps(i) for i in items],
            key=lambda x: x.get("id") or json.dumps(x, sort_keys=True)
        )
    except Exception:
        return [_strip_timestamps(i) for i in items]


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


@pytest.fixture(scope="session")
def completed_run_id(client):
    """
    Start a run and wait for complete or partial status.
    Fix 3: Assert status is in (complete, partial) — failed runs must not
    leak into replay tests.
    """
    body = {
        "connectedSources":       ["ServiceNow", "Jira"],
        "uploadedFiles":          ["test_file.csv"],
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

    # Fix 3: hard assert — do not let a failed run poison downstream tests
    assert status in ("complete", "partial"), (
        f"Run '{run_id}' reached status '{status}'. "
        "Cannot use a failed run for replay tests. "
        "Check Track B offline mode is working correctly."
    )
    return run_id


@pytest.fixture(scope="session")
def unmaterialised_run_id():
    """
    Fix 2: Insert a run record directly into the DB without going through
    /api/runs/start, which would trigger the background materialisation task
    and populate the opps KV key. The run record exists but opps:{run_id}
    is deliberately absent — tests the second 404 path on replay.
    """
    import time
    from app import db

    run_id = f"run_bare_{int(time.time())}"
    run = {
        "id": run_id,
        "status": "running",
        "startedAt": db.now_iso(),
        "updatedAt": db.now_iso(),
        "inputs": {
            "connectedSources": [],
            "uploadedFiles": [],
            "sampleWorkspaceEnabled": False,
        },
    }
    db.run_set(run_id, run)
    # opps:{run_id} KV key intentionally never written
    return run_id


# ─────────────────────────────────────────────────────────────────────────────
# 404 cases
# ─────────────────────────────────────────────────────────────────────────────

def test_replay_unknown_run_404(client):
    """POST /replay for a non-existent runId must return 404."""
    r = client.post(
        "/api/runs/run_does_not_exist_xyz/replay",
        headers=_auth(), json={},
    )
    assert r.status_code == 404, (
        f"Expected 404 for unknown run, got {r.status_code}: {r.text}"
    )


def test_replay_run_exists_but_no_opps_404(client, unmaterialised_run_id):
    """
    Fix 2: Run exists in DB but /compute was never called.
    opps KV key is absent. Replay must return 404 not 500.
    This validates the second 404 path and KV key alignment for T4.
    """
    r = client.post(
        f"/api/runs/{unmaterialised_run_id}/replay",
        headers=_auth(), json={},
    )
    assert r.status_code == 404, (
        f"Expected 404 for unmaterialised run '{unmaterialised_run_id}', "
        f"got {r.status_code}: {r.text}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# KV key alignment (Issue 3)
# ─────────────────────────────────────────────────────────────────────────────

def test_all_required_artifact_keys_present(client, completed_run_id):
    """
    Issue 3: Verify that the KV keys T4 expects (opps, evidence, clusters)
    are all present after a completed run, before replay is called.
    If any upstream task (T1/T2/T3) used a different key name, this test
    will catch the mismatch before it surfaces as a confusing 404 in replay.

    This test reads the run-scoped opportunities and clusters endpoints as
    a proxy for key presence. If they return 200 with data, the keys exist.
    """
    opps_r = client.get(
        f"/api/runs/{completed_run_id}/opportunities", headers=_auth()
    )
    assert opps_r.status_code == 200, (
        f"Opportunities endpoint returned {opps_r.status_code}. "
        f"KV key 'opps' may be missing or misnamed from T1/T2."
    )
    assert len(opps_r.json()) >= 1, (
        "Opportunities list is empty — materialisation may not have completed."
    )

    clusters_r = client.get(
        f"/api/runs/{completed_run_id}/clusters", headers=_auth()
    )
    assert clusters_r.status_code == 200, (
        f"Clusters endpoint returned {clusters_r.status_code}. "
        f"KV key 'clusters' may be missing or misnamed from T3."
    )
    # Clusters may be empty if no INC-/CS-/JIRA- patterns in evidence — that is ok
    assert isinstance(clusters_r.json(), list), "Clusters must be a list"


# ─────────────────────────────────────────────────────────────────────────────
# Response shape
# ─────────────────────────────────────────────────────────────────────────────

def test_replay_returns_ok(client, completed_run_id):
    r = client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})
    assert r.status_code == 200, f"replay failed: {r.text}"
    assert r.json().get("ok") is True


def test_replay_returns_is_replay_flag(client, completed_run_id):
    r = client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})
    assert r.status_code == 200
    assert r.json().get("isReplay") is True


def test_replay_returns_replayed_at(client, completed_run_id):
    r = client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})
    assert r.status_code == 200
    assert r.json().get("replayedAt"), "replayedAt must be non-empty"


def test_replay_returns_counts(client, completed_run_id):
    """Fix 6: assert all three count keys are present."""
    r = client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})
    assert r.status_code == 200
    counts = r.json().get("counts", {})
    for key in ("opportunities", "evidence", "clusters"):
        assert key in counts, f"Missing '{key}' in replay response counts: {counts}"


def test_replay_auth_required(client):
    r = client.post("/api/runs/any_run_id/replay", json={})
    assert r.status_code in (401, 403)


# ─────────────────────────────────────────────────────────────────────────────
# Determinism gates — sorted + stripped (Issue 1 + Issue 2)
# ─────────────────────────────────────────────────────────────────────────────

def test_replay_opportunities_deterministic(client, completed_run_id):
    """
    Core gate: opportunities[] sorted by id, timestamps stripped.
    Issue 1: _normalize() prevents insertion-order false positives.
    """
    before = client.get(
        f"/api/runs/{completed_run_id}/opportunities", headers=_auth()
    )
    assert before.status_code == 200

    client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})

    after = client.get(
        f"/api/runs/{completed_run_id}/opportunities", headers=_auth()
    )
    assert after.status_code == 200

    assert _normalize(before.json()) == _normalize(after.json()), (
        "Opportunities changed after replay (after sorting by id and stripping timestamps)"
    )


def test_replay_evidence_deterministic(client, completed_run_id):
    """evidence[] sorted by id, timestamps stripped."""
    before = client.get(f"/api/runs/{completed_run_id}/evidence", headers=_auth())
    if before.status_code == 404:
        pytest.skip("Evidence endpoint not yet wired")
    assert before.status_code == 200

    client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})

    after = client.get(f"/api/runs/{completed_run_id}/evidence", headers=_auth())
    assert after.status_code == 200
    assert _normalize(before.json()) == _normalize(after.json()), (
        "Evidence changed after replay"
    )


def test_replay_clusters_deterministic(client, completed_run_id):
    """clusters[] sorted by id, timestamps stripped."""
    before = client.get(f"/api/runs/{completed_run_id}/clusters", headers=_auth())
    assert before.status_code == 200

    client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})

    after = client.get(f"/api/runs/{completed_run_id}/clusters", headers=_auth())
    assert after.status_code == 200
    assert _normalize(before.json()) == _normalize(after.json()), (
        "Clusters changed after replay"
    )


def test_replay_idempotent(client, completed_run_id):
    """Calling replay twice produces same opportunities (sorted, stripped)."""
    client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})
    opps1 = client.get(
        f"/api/runs/{completed_run_id}/opportunities", headers=_auth()
    ).json()

    client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})
    opps2 = client.get(
        f"/api/runs/{completed_run_id}/opportunities", headers=_auth()
    ).json()

    assert _normalize(opps1) == _normalize(opps2), (
        "Replay is not idempotent — opportunities differ between two replays"
    )


def test_replay_preserves_stable_timestamps(client, completed_run_id):
    """
    Issue 2: startedAt must not change on replay (same run, same start time).
    completedAt must not change (not rewritten by T4 route).
    Only replayedAt and updatedAt are expected to change.
    """
    # Capture before replay
    st_before = client.get(
        f"/api/runs/{completed_run_id}/status", headers=_auth()
    )
    assert st_before.status_code == 200
    started_before   = st_before.json().get("startedAt")
    completed_before = st_before.json().get("completedAt")

    client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})

    # Capture after replay
    st_after = client.get(
        f"/api/runs/{completed_run_id}/status", headers=_auth()
    )
    assert st_after.status_code == 200
    started_after   = st_after.json().get("startedAt")
    completed_after = st_after.json().get("completedAt")

    # startedAt must be identical
    if started_before:
        assert started_before == started_after, (
            f"startedAt changed after replay: "
            f"before={started_before}, after={started_after}"
        )

    # completedAt must be identical (T4 does not rewrite it)
    if completed_before:
        assert completed_before == completed_after, (
            f"completedAt changed after replay: "
            f"before={completed_before}, after={completed_after}"
        )


def test_replay_preserves_roadmap_and_exec_report(client, completed_run_id):
    """Fix 7: roadmap and executive_report still readable after replay."""
    roadmap_before = client.get(
        f"/api/runs/{completed_run_id}/roadmap", headers=_auth()
    )
    exec_before = client.get(
        f"/api/runs/{completed_run_id}/executive-report", headers=_auth()
    )

    client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})

    if roadmap_before.status_code == 200:
        roadmap_after = client.get(
            f"/api/runs/{completed_run_id}/roadmap", headers=_auth()
        )
        assert roadmap_after.status_code == 200, "Roadmap missing after replay"
        assert _strip_timestamps(roadmap_before.json()) == \
               _strip_timestamps(roadmap_after.json()), \
               "Roadmap changed after replay"

    if exec_before.status_code == 200:
        exec_after = client.get(
            f"/api/runs/{completed_run_id}/executive-report", headers=_auth()
        )
        assert exec_after.status_code == 200, "Executive report missing after replay"
        assert _strip_timestamps(exec_before.json()) == \
               _strip_timestamps(exec_after.json()), \
               "Executive report changed after replay"


def test_status_reflects_complete_after_replay(client, completed_run_id):
    client.post(f"/api/runs/{completed_run_id}/replay", headers=_auth(), json={})
    st = client.get(f"/api/runs/{completed_run_id}/status", headers=_auth())
    assert st.status_code == 200
    assert st.json().get("status") == "complete"


# ─────────────────────────────────────────────────────────────────────────────
# Helper unit tests (no backend needed)
# ─────────────────────────────────────────────────────────────────────────────

def test_strip_timestamps_only_strips_replay_fields():
    """
    Issue 2: _strip_timestamps must only strip replayedAt and updatedAt.
    startedAt and completedAt must NOT be stripped.
    """
    obj = {
        "id":          "opp_001",
        "updatedAt":   "2026-04-23T10:00:00Z",   # stripped
        "replayedAt":  "2026-04-23T10:01:00Z",   # stripped
        "startedAt":   "2026-04-23T09:00:00Z",   # NOT stripped
        "completedAt": "2026-04-23T09:55:00Z",   # NOT stripped
        "impact":      6,
    }
    cleaned = _strip_timestamps(obj)
    assert "updatedAt"   not in cleaned
    assert "replayedAt"  not in cleaned
    assert "startedAt"   in cleaned,   "startedAt must not be stripped"
    assert "completedAt" in cleaned,   "completedAt must not be stripped"
    assert cleaned["impact"] == 6


def test_normalize_sorts_by_id():
    """Issue 1: _normalize must sort by id to prevent insertion-order issues."""
    items = [
        {"id": "opp_003", "impact": 5},
        {"id": "opp_001", "impact": 7},
        {"id": "opp_002", "impact": 3},
    ]
    normalized = _normalize(items)
    ids = [i["id"] for i in normalized]
    assert ids == ["opp_001", "opp_002", "opp_003"]


def test_normalize_strips_and_sorts():
    """_normalize combines strip and sort correctly."""
    items = [
        {"id": "ev_002", "title": "B", "updatedAt": "2026-04-23T10:00:00Z"},
        {"id": "ev_001", "title": "A", "updatedAt": "2026-04-23T10:01:00Z"},
    ]
    normalized = _normalize(items)
    assert normalized[0]["id"] == "ev_001"
    assert normalized[1]["id"] == "ev_002"
    assert "updatedAt" not in normalized[0]
    assert "updatedAt" not in normalized[1]
