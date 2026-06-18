"""Audit logging — AT-82 / T1-S10-B T4.

log_event() is the single write point for audit_log.  It always fails
silently: any exception is logged at ERROR and the caller continues.
The table is INSERT-only — no UPDATE or DELETE ever runs against it.

Event type payload schemas (locked — do not change field names):
    run_started:           org_id, run_id, user_id, pack_id, system_ids, timestamp
    run_completed:         org_id, run_id, duration_ms, opportunities_found, status
    connector_queried:     org_id, run_id, connector_id, query_hash, row_count, duration_ms, timestamp
    connector_connected:   org_id, connector_id, user_id, scopes_granted, timestamp
    connector_disconnected: org_id, connector_id, user_id, timestamp
    scope_declared:        org_id, connector_id, user_id, scope_type, scope_values, timestamp
    user_login:            org_id, user_id, ip_address_hash, timestamp
    setup_state_saved:     org_id, user_id, system_count, pack_id, timestamp
    schema_discovered:     org_id, connector_id, schema_count, table_count, timestamp

Behaviour difference — schema_discovered vs connector_queried:
    schema_discovered  — connector read system catalogues only (no customer data touched).
    connector_queried  — connector queried customer data tables during a run.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from typing_extensions import TypedDict

# ---------------------------------------------------------------------------
# Event type string constants — import these instead of raw strings.
# ---------------------------------------------------------------------------

RUN_STARTED = "run_started"
RUN_COMPLETED = "run_completed"
CONNECTOR_QUERIED = "connector_queried"
CONNECTOR_CONNECTED = "connector_connected"
CONNECTOR_DISCONNECTED = "connector_disconnected"
CONNECTOR_REVOCATION_FAILED = "connector_revocation_failed"
SCOPE_DECLARED = "scope_declared"
USER_LOGIN = "user_login"
SETUP_STATE_SAVED = "setup_state_saved"
SCHEMA_DISCOVERED = "schema_discovered"

# ---------------------------------------------------------------------------
# Registry — every accepted event type listed here.
# log_event() accepts any string; this registry is for documentation and
# unit-test validation only.
# ---------------------------------------------------------------------------

AUDIT_EVENT_REGISTRY: frozenset[str] = frozenset({
    RUN_STARTED,
    RUN_COMPLETED,
    CONNECTOR_QUERIED,
    CONNECTOR_CONNECTED,
    CONNECTOR_DISCONNECTED,
    CONNECTOR_REVOCATION_FAILED,
    SCOPE_DECLARED,
    USER_LOGIN,
    SETUP_STATE_SAVED,
    SCHEMA_DISCOVERED,
})

# ---------------------------------------------------------------------------
# Payload TypedDicts — documentation only; log_event() accepts **kwargs.
# Do not change existing field names — add new event types instead.
# ---------------------------------------------------------------------------


class SchemaDiscoveredEvent(TypedDict):
    """Payload for schema_discovered audit events.

    Written when a connector reads system catalogues to discover schema.
    schema_discovered is distinct from connector_queried:
      - schema_discovered: only system catalogues were accessed (no customer data).
      - connector_queried: customer data tables were queried during execution.

    connector_id:  Which connector performed schema discovery.
    schema_count:  Number of schemas / databases discovered.
    table_count:   Number of tables / collections discovered.
    """
    connector_id: str
    schema_count: int
    table_count: int

from app import db
from database.models.audit_log import (
    CREATE_AUDIT_LOG_IDX_ORG_EVENT,
    CREATE_AUDIT_LOG_IDX_ORG_TS,
    CREATE_AUDIT_LOG_TABLE,
)

logger = logging.getLogger(__name__)

_TABLES_INITIALISED = False


def _ensure_table() -> None:
    """No-op. The audit_log table is provisioned by database/provision/provision.sh."""
    return None


def log_event(event_type: str, **kwargs: Any) -> None:
    """Append one record to audit_log.  Never raises — fails silently (AC9).

    On any write failure the exception is swallowed (an audit failure must not
    break the request that triggered it), but it is no longer invisible: an
    ``audit.write_failed`` telemetry event is emitted from the failure handler
    so the otherwise-silent failure is observable and alertable
    (AT-292 / FixPack v2 Fix 5).
    """
    # Resolve org_id up front so it is available to the failure handler even if
    # the DB write below raises before the row is built.
    org_id = kwargs.pop("org_id", None)
    if not org_id:
        try:
            from app.middleware.tenancy import get_current_org_id_optional

            org_id = get_current_org_id_optional()
        except Exception:
            org_id = None
    org_id = org_id or "default"

    try:
        _ensure_table()
        run_id = kwargs.pop("run_id", None)
        connector_id = kwargs.pop("connector_id", None)
        user_id = kwargs.pop("user_id", None)
        record_id = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat()

        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                """
                INSERT INTO audit_log
                    (id, org_id, event_type, user_id, run_id, connector_id, payload, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record_id,
                    org_id,
                    event_type,
                    user_id,
                    run_id,
                    connector_id,
                    json.dumps(kwargs) if kwargs else None,
                    ts,
                ),
            )
            con.commit()
        finally:
            con.close()
    except Exception as exc:
        # F5-AC1: never re-raise — audit failure must not break the triggering
        # request. Log with org_id + event_type so the failure is traceable.
        logger.error(
            "audit log_event failed — org_id=%s event_type=%s: %s",
            org_id,
            event_type,
            exc,
        )
        # F5-AC2: surface the silent failure as telemetry so it is alertable.
        # Fire-and-forget: telemetry must never raise out of the audit path.
        try:
            from app.telemetry import record_event

            record_event(
                "audit.write_failed",
                {
                    "org_id": org_id,
                    "event_type": event_type,
                    "error": str(exc),
                },
            )
        except Exception:
            pass
