"""Contract tests for R16-A1 / AT-383 (T7) — admin checkpoint-reset action.

Two layers:
  * Repository (`checkpoint_repository.reset_checkpoint`) — hermetic, via an
    in-memory fake of the ingestion_checkpoints table (no provisioned table/DB
    privileges needed). Proves AC7's "clears the checkpoint → next run re-reads
    from the beginning" (a subsequent read returns None).
  * Route (`POST /api/ingestion/checkpoints/reset`) — Owner-gated (Analyst/Viewer
    → 403) and records the reset to BOTH the audit trail and telemetry (AC7).
    The repository + audit/telemetry boundaries are monkeypatched so the route is
    exercised without a real table.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from discovery.ingest.base import Checkpoint
from discovery.ingest import checkpoint_repository as repo

AUTH = {"Authorization": "Bearer dev-token-change-me"}
DEV_USER = "dev-token-change-me"
RESET_PATH = "/api/ingestion/checkpoints/reset"


# ==========================================================================
# Repository layer — hermetic in-memory fake (supports INSERT/SELECT/DELETE)
# ==========================================================================
class _FakeCursor:
    def __init__(self, store):
        self._store = store
        self._result = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("INSERT INTO ingestion_checkpoints"):
            org_id, connector_id, value, captured_at = params
            # upsert; re-saving reactivates a soft-deleted row.
            self._store[(org_id, connector_id)] = {"value": value, "captured_at": captured_at, "is_deleted": False}
            self.rowcount = 1
        elif s.startswith("SELECT value, captured_at FROM ingestion_checkpoints"):
            row = self._store.get((params[0], params[1]))
            self._result = (row["value"], row["captured_at"]) if row and not row["is_deleted"] else None
        elif s.startswith("UPDATE ingestion_checkpoints SET is_deleted"):  # soft-delete reset
            row = self._store.get((params[0], params[1]))
            if row and not row["is_deleted"]:
                row["is_deleted"] = True
                self.rowcount = 1
            else:
                self.rowcount = 0
        else:  # pragma: no cover
            self._result = None

    def fetchone(self):
        return self._result

    def close(self):
        pass


class _FakeConn:
    def __init__(self, store):
        self._store = store
        self.autocommit = False

    def cursor(self):
        return _FakeCursor(self._store)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def store(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(repo, "_connect", lambda: _FakeConn(data))
    return data


def test_reset_clears_checkpoint_so_next_read_is_first_run(store):
    repo.save_checkpoint(Checkpoint.create("slack", "org-1", "cp-42"))
    assert repo.read_checkpoint("org-1", "slack") is not None

    removed = repo.reset_checkpoint("org-1", "slack")

    assert removed is True
    # Next run sees no checkpoint → full re-read from the beginning (AC7).
    assert repo.read_checkpoint("org-1", "slack") is None


def test_reset_returns_false_when_nothing_to_clear(store):
    assert repo.reset_checkpoint("org-1", "never-licensed") is False


def test_reset_only_affects_the_target_key(store):
    repo.save_checkpoint(Checkpoint.create("slack", "org-1", "s1"))
    repo.save_checkpoint(Checkpoint.create("jira", "org-1", "j1"))
    repo.save_checkpoint(Checkpoint.create("slack", "org-2", "s2"))

    repo.reset_checkpoint("org-1", "slack")

    assert repo.read_checkpoint("org-1", "slack") is None        # cleared
    assert repo.read_checkpoint("org-1", "jira").value == "j1"   # other connector intact
    assert repo.read_checkpoint("org-2", "slack").value == "s2"  # other org intact


# ==========================================================================
# Route layer — Owner-gated admin action + audit/telemetry recording
# ==========================================================================
def _set_role(role: str) -> dict:
    """Put the dev user in a fresh org with the given role; return request headers."""
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    org_id = f"ingest_role_{uuid.uuid4().hex[:8]}"
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET role=EXCLUDED.role, created_at=EXCLUDED.created_at",
            (org_id, DEV_USER, role, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    return {**AUTH, "X-Org-Id": org_id}


@pytest.fixture
def captured(monkeypatch):
    """Capture telemetry + audit calls and stub the repository (no real table)."""
    events = {"telemetry": [], "audit": []}
    monkeypatch.setattr(
        "app.routes_ingestion.record_event",
        lambda etype, payload=None: events["telemetry"].append((etype, payload or {})),
    )
    monkeypatch.setattr(
        "app.middleware.audit.log_event",
        lambda etype, **kw: events["audit"].append((etype, kw)),
    )
    return events


def test_owner_reset_clears_and_records(client: TestClient, monkeypatch, captured):
    monkeypatch.setattr("app.routes_ingestion.checkpoints.reset_checkpoint", lambda o, c: True)
    headers = _set_role("owner")

    resp = client.post(RESET_PATH, json={"connector_id": "slack"}, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cleared"] is True
    assert body["connector_id"] == "slack"
    # Recorded to BOTH telemetry and audit (AC7).
    assert ("ingestion.checkpoint_reset", {"org_id": headers["X-Org-Id"], "connector_id": "slack", "had_checkpoint": True}) in captured["telemetry"]
    assert any(e[0] == "ingestion_checkpoint_reset" and e[1].get("connector_id") == "slack" for e in captured["audit"])


def test_owner_reset_reports_nothing_to_clear(client: TestClient, monkeypatch, captured):
    monkeypatch.setattr("app.routes_ingestion.checkpoints.reset_checkpoint", lambda o, c: False)
    headers = _set_role("owner")

    resp = client.post(RESET_PATH, json={"connector_id": "git"}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["cleared"] is False
    # Still recorded (the admin action happened, even if there was no checkpoint).
    assert captured["telemetry"] and captured["telemetry"][-1][1]["had_checkpoint"] is False


@pytest.mark.parametrize("role", ["analyst", "viewer"])
def test_non_owner_forbidden(client: TestClient, monkeypatch, role):
    # Guard against any side effect: reset must never run for a forbidden caller.
    called = {"reset": False}
    monkeypatch.setattr(
        "app.routes_ingestion.checkpoints.reset_checkpoint",
        lambda o, c: called.__setitem__("reset", True) or True,
    )
    headers = _set_role(role)

    resp = client.post(RESET_PATH, json={"connector_id": "slack"}, headers=headers)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Insufficient role"
    assert called["reset"] is False  # blocked before the action ran
