"""Contract tests — Audit log (AT-82 T10).

AC3: Each event type writes a correct record with all required fields.
AC4: audit_log is INSERT-only — no UPDATE/DELETE in application code.
AC5: DB-level UPDATE/DELETE enforced at app layer (SQLite has no per-table grants).
AC9: Audit failure does not propagate — primary operation continues.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_audit_rows(db_path: str, org_id: str = "default") -> list[dict]:
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "SELECT id, org_id, event_type, user_id, run_id, connector_id, payload, timestamp "
            "FROM audit_log WHERE org_id = ? ORDER BY timestamp DESC",
            (org_id,),
        )
        rows = cur.fetchall()
    finally:
        con.close()
    return [
        {
            "id": r[0], "org_id": r[1], "event_type": r[2], "user_id": r[3],
            "run_id": r[4], "connector_id": r[5],
            "payload": json.loads(r[6]) if r[6] else None,
            "timestamp": r[7],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# AC3 — each event type writes correct fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event_type,kwargs,expected_payload_keys", [
    (
        "run_started",
        {"run_id": "run_abc", "pack_id": "service_cloud", "system_ids": ["salesforce"]},
        ["pack_id", "system_ids"],
    ),
    (
        "run_completed",
        {"run_id": "run_abc", "duration_ms": 1200, "opportunities_found": 3, "status": "complete"},
        ["duration_ms", "opportunities_found", "status"],
    ),
    (
        "connector_connected",
        {"connector_id": "salesforce", "scopes_granted": ["api"]},
        ["scopes_granted"],
    ),
    (
        "connector_disconnected",
        {"connector_id": "servicenow"},
        [],
    ),
    (
        "setup_state_saved",
        {"system_count": 3, "pack_id": "ncino"},
        ["system_count", "pack_id"],
    ),
    (
        "user_login",
        {"user_id": "u1", "ip_address_hash": "sha256:abc"},
        ["ip_address_hash"],
    ),
    (
        "scope_declared",
        {"connector_id": "jira", "user_id": "u1", "scope_type": "project", "scope_values": ["PROJ"]},
        ["scope_type", "scope_values"],
    ),
    (
        "connector_queried",
        {"run_id": "run_x", "connector_id": "sf", "query_hash": "sha256:xyz", "row_count": 50, "duration_ms": 200},
        ["query_hash", "row_count", "duration_ms"],
    ),
])
def test_log_event_writes_correct_record(event_type, kwargs, expected_payload_keys):
    """log_event inserts a row with all required top-level and payload fields (AC3)."""
    import app.db as db_mod
    import app.middleware.audit as audit_mod

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    original_db_path = db_mod.DB_PATH
    db_mod.DB_PATH = Path(db_path)
    audit_mod._TABLES_INITIALISED = False

    try:
        from app.middleware.audit import log_event
        log_event(event_type, **kwargs)

        rows = _read_audit_rows(db_path)
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        row = rows[0]
        assert row["event_type"] == event_type
        assert row["org_id"] == "default"
        assert row["timestamp"]

        if expected_payload_keys:
            assert row["payload"] is not None
            for key in expected_payload_keys:
                assert key in row["payload"], f"Missing payload key: {key}"
    finally:
        db_mod.DB_PATH = original_db_path
        audit_mod._TABLES_INITIALISED = False
        try:
            os.unlink(db_path)
        except OSError:
            pass  # Windows may still hold a handle; temp dir cleanup handles it


# ---------------------------------------------------------------------------
# AC4 — no UPDATE or DELETE on audit_log in application code
# ---------------------------------------------------------------------------


def test_no_update_on_audit_log():
    """audit.py must never execute UPDATE on audit_log."""
    import inspect
    import app.middleware.audit as audit_mod

    source = inspect.getsource(audit_mod)
    # Allow UPDATE in comments but not in actual SQL strings
    import re
    # Find UPDATE statements in non-comment lines
    lines = [l for l in source.splitlines() if not l.strip().startswith("#")]
    non_comment = "\n".join(lines)
    assert "UPDATE audit_log" not in non_comment, "audit.py must never UPDATE audit_log"


def test_no_delete_on_audit_log():
    """audit.py must never execute DELETE on audit_log."""
    import inspect
    import app.middleware.audit as audit_mod

    source = inspect.getsource(audit_mod)
    lines = [l for l in source.splitlines() if not l.strip().startswith("#")]
    non_comment = "\n".join(lines)
    assert "DELETE FROM audit_log" not in non_comment, "audit.py must never DELETE from audit_log"


# ---------------------------------------------------------------------------
# AC9 — audit failure does not propagate
# ---------------------------------------------------------------------------


def test_audit_failure_does_not_raise():
    """log_event with a broken DB connection must not raise — caller continues (AC9)."""
    import app.middleware.audit as audit_mod
    audit_mod._TABLES_INITIALISED = False

    with patch("app.middleware.audit.db.connect", side_effect=Exception("DB unavailable")):
        # Must not raise
        from app.middleware.audit import log_event
        log_event("run_started", run_id="r1", pack_id="svc", system_ids=[])


def test_audit_failure_logs_error(caplog):
    """log_event logs at ERROR level when DB is unavailable (AC9)."""
    import logging
    import app.middleware.audit as audit_mod
    audit_mod._TABLES_INITIALISED = False

    with patch("app.middleware.audit.db.connect", side_effect=Exception("kaboom")):
        with caplog.at_level(logging.ERROR, logger="app.middleware.audit"):
            from app.middleware.audit import log_event
            log_event("connector_connected", connector_id="sf")

    assert any("log_event" in r.message for r in caplog.records)


def test_primary_operation_continues_after_audit_failure(client):
    """A route continues to return 200 even if audit logging fails (AC9)."""
    import app.middleware.audit as audit_mod
    audit_mod._TABLES_INITIALISED = False

    with patch("app.middleware.audit.db.connect", side_effect=Exception("broken")):
        resp = client.get(
            "/api/health",
        )
    assert resp.status_code == 200
