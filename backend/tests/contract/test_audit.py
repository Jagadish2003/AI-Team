"""Contract tests — Audit log (AT-82 T10).

AC3: Each event type writes a correct record with all required fields.
AC4: audit_log is INSERT-only — no UPDATE/DELETE in application code.
AC5: DB-level UPDATE/DELETE enforced at app layer (SQLite has no per-table grants).
AC9: Audit failure does not propagate — primary operation continues.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import psycopg2
import pytest

from app import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_org_id() -> str:
    """A unique org_id so each test sees only its own audit rows in the shared
    PostgreSQL audit_log table (AT-288 / Fix 1 — replaces per-test SQLite files)."""
    return f"audit-{uuid.uuid4().hex[:12]}"


def _read_audit_rows(org_id: str) -> list[dict]:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, org_id, event_type, user_id, run_id, connector_id, payload, timestamp "
            "FROM audit_log WHERE org_id = %s ORDER BY timestamp DESC",
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
    from app.middleware.audit import log_event

    org_id = _new_org_id()
    log_event(event_type, org_id=org_id, **kwargs)

    rows = _read_audit_rows(org_id)
    assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
    row = rows[0]
    assert row["event_type"] == event_type
    assert row["org_id"] == org_id
    assert row["timestamp"]

    if expected_payload_keys:
        assert row["payload"] is not None
        for key in expected_payload_keys:
            assert key in row["payload"], f"Missing payload key: {key}"


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


# ---------------------------------------------------------------------------
# AC3 — run_started record includes user_id
# ---------------------------------------------------------------------------


def test_run_started_includes_user_id():
    """run_started log_event must include user_id (AC3)."""
    from app.middleware.audit import log_event

    org_id = _new_org_id()
    log_event(
        "run_started",
        org_id=org_id,
        run_id="run_test_001",
        user_id="dev-token-change-me",
        pack_id="service_cloud",
        system_ids=["salesforce", "servicenow"],
    )
    rows = _read_audit_rows(org_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "run_started"
    assert row["user_id"] == "dev-token-change-me", "user_id must be stored in audit record"
    assert row["run_id"] == "run_test_001"
    assert row["payload"]["pack_id"] == "service_cloud"
    assert row["payload"]["system_ids"] == ["salesforce", "servicenow"]


# ---------------------------------------------------------------------------
# AC4 — connector_connected record includes user_id and scopes_granted
# ---------------------------------------------------------------------------


def test_connector_connected_includes_user_id_and_scopes():
    """connector_connected log_event must include user_id and scopes_granted (AC4)."""
    from app.middleware.audit import log_event

    org_id = _new_org_id()
    log_event(
        "connector_connected",
        org_id=org_id,
        connector_id="salesforce",
        user_id="dev-token-change-me",
        scopes_granted=["api", "refresh_token"],
    )
    rows = _read_audit_rows(org_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "connector_connected"
    assert row["connector_id"] == "salesforce"
    assert row["user_id"] == "dev-token-change-me", "user_id must be stored in audit record"
    assert row["payload"]["scopes_granted"] == ["api", "refresh_token"]


# ---------------------------------------------------------------------------
# AT-292 / FixPack v2 Fix 5 — audit write failures emit telemetry
#
# The audit write path migrated to PostgreSQL (AT-288 / Fix 1), so the failing
# write is simulated with psycopg2.OperationalError — the production equivalent
# of the sqlite3.OperationalError named in the original AC.
# ---------------------------------------------------------------------------


def _failing_audit_connection() -> MagicMock:
    """A db connection mock whose cursor.execute raises on the audit write."""
    mock_con = MagicMock()
    mock_con.cursor.return_value.execute.side_effect = psycopg2.OperationalError(
        "simulated audit write failure"
    )
    return mock_con


def test_audit_write_failure_logs_org_and_event_type(caplog):
    """F5-AC1 — a failed audit write logs at ERROR with org_id and event_type,
    and the exception is never re-raised to the caller."""
    import logging

    import app.middleware.audit as audit_mod

    audit_mod._TABLES_INITIALISED = True  # isolate the failure to the write itself

    with patch("app.middleware.audit.db.connect", return_value=_failing_audit_connection()), \
            patch("app.telemetry.record_event"):
        with caplog.at_level(logging.ERROR, logger="app.middleware.audit"):
            from app.middleware.audit import log_event

            # (a) must not raise to the caller
            log_event("user_login", org_id="org-f5-ac1", user_id="u1")

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "expected an ERROR log on audit write failure"
    combined = " ".join(r.getMessage() for r in error_records)
    assert "org-f5-ac1" in combined, "logger.error must include org_id"
    assert "user_login" in combined, "logger.error must include event_type"


def test_audit_write_failure_emits_telemetry():
    """F5-AC3 — a failed audit write (psycopg2.OperationalError, PostgreSQL
    equivalent of sqlite3.OperationalError) is swallowed and surfaces an
    'audit.write_failed' telemetry event.

    (a) No exception propagates to the caller.
    (b) record_event is called with event_type='audit.write_failed'.
    """
    import app.middleware.audit as audit_mod

    audit_mod._TABLES_INITIALISED = True

    with patch("app.middleware.audit.db.connect", return_value=_failing_audit_connection()), \
            patch("app.telemetry.record_event") as mock_record:
        from app.middleware.audit import log_event

        # (a) must not raise
        log_event("connector_connected", org_id="org-f5-ac3", connector_id="sf")

    # (b) telemetry emitted with the right event type and payload
    mock_record.assert_called_once()
    args, _kwargs = mock_record.call_args
    assert args[0] == "audit.write_failed", "telemetry event_type must be audit.write_failed"
    payload = args[1]
    assert payload["event_type"] == "connector_connected"
    assert payload["org_id"] == "org-f5-ac3"
    assert payload["error"], "payload must carry the stringified error"


def test_audit_write_failed_registered_with_typeddict():
    """F5-AC2 — audit.write_failed is in REGISTERED_EVENT_TYPES and maps to a
    TypedDict payload schema with the documented fields."""
    from typing import get_type_hints

    from app.telemetry import (
        AuditWriteFailedPayload,
        EVENT_PAYLOAD_TYPES,
        REGISTERED_EVENT_TYPES,
    )

    assert "audit.write_failed" in REGISTERED_EVENT_TYPES
    assert EVENT_PAYLOAD_TYPES["audit.write_failed"] is AuditWriteFailedPayload

    hints = get_type_hints(AuditWriteFailedPayload)
    assert {"org_id", "event_type", "error"} <= set(hints)
 