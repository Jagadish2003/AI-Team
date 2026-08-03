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
    connector_credentials_set:     org_id, connector_id, user_id, timestamp
    connector_credentials_revoked: org_id, connector_id, user_id, timestamp
    scope_declared:        org_id, connector_id, user_id, scope_type, scope_values, timestamp
    user_login:            org_id, user_id, ip_address_hash, timestamp
    setup_state_saved:     org_id, user_id, system_count, pack_id, timestamp
    schema_discovered:     org_id, connector_id, schema_count, table_count, timestamp
    runbook_match_decided: org_id, user_id, recurrence_id, action,
                           previous_state, resulting_state, revision
    evidence_export_generated: org_id, user_id, export_kind, scope, timestamp,
                           run_id, opportunity_id, finding_count, record_count,
                           content_root, signature_prefix, generated_at
    usage_report_exported: org_id, user_id, export_kind, scope, timestamp,
                           period_from, period_to, event_count, run_count,
                           signature_prefix, generated_at

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
# R17-D3 Addendum A (T12): an Owner entered/replaced or revoked a connector's
# STATIC (non-OAuth) credential through the Integration Hub. The payload records
# the ACTION and actor only — never the URL, username, or secret (AC10: static
# credential values are write-only, never readable back through the UI or logs).
CONNECTOR_CREDENTIALS_SET = "connector_credentials_set"
CONNECTOR_CREDENTIALS_REVOKED = "connector_credentials_revoked"
SCOPE_DECLARED = "scope_declared"
USER_LOGIN = "user_login"
SETUP_STATE_SAVED = "setup_state_saved"
SCHEMA_DISCOVERED = "schema_discovered"
# R16-A1 / AT-383 (T7): admin cleared a source's ingestion checkpoint.
# NOTE: this is the AUDIT event name and is intentionally snake_case, matching
# every other audit-event constant in this module. It is a DIFFERENT system from
# the telemetry event for the same action, which uses dot notation
# ("ingestion.checkpoint_reset", registered in app/telemetry.py). Searching the
# codebase for "checkpoint_reset" will surface both — that is expected.
INGESTION_CHECKPOINT_RESET = "ingestion_checkpoint_reset"
# MSP-B5 T4: an analyst accepted, dismissed, or deferred a proposed runbook
# match. The dedicated decision-history table is the domain record; this event
# also places the action in the organisation-wide audit stream.
RUNBOOK_MATCH_DECIDED = "runbook_match_decided"
# 2.0-A2 T1: an opportunity's lifecycle state changed (open -> actioned ->
# monitoring -> measured, plus dismissed/stalled). The dedicated append-only
# opportunity_lifecycle_history table is the domain record; this event places the
# transition in the organisation-wide audit stream with actor, org, target and
# timestamp. Emitted for SYSTEM transitions too, so a state change never appears
# in the portfolio without a corresponding audit row.
OPPORTUNITY_LIFECYCLE_TRANSITIONED = "opportunity_lifecycle_transitioned"

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
    CONNECTOR_CREDENTIALS_SET,
    CONNECTOR_CREDENTIALS_REVOKED,
    SCOPE_DECLARED,
    USER_LOGIN,
    SETUP_STATE_SAVED,
    SCHEMA_DISCOVERED,
    INGESTION_CHECKPOINT_RESET,
    RUNBOOK_MATCH_DECIDED,
    OPPORTUNITY_LIFECYCLE_TRANSITIONED,
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
    # the DB write below raises before the row is built. Attribution goes through
    # the shared resolver (R17-D3 / AT-450 T5-AC2/AC3): the authenticated request
    # org wins, an explicit org_id (background callers) is the fallback, and an
    # unresolved event is marked UNATTRIBUTED — never silently filed under the
    # real "default" tenant as it was before.
    explicit_org_id = kwargs.pop("org_id", None)
    try:
        from app.middleware.tenancy import resolve_event_org_id

        org_id = resolve_event_org_id(explicit_org_id)
    except Exception:
        # Fall back to the shared sentinel (M3) rather than a bare "unknown"
        # literal, so unresolved events are filed under one unambiguous value.
        # Guard the import too: this branch runs precisely when importing from
        # tenancy may be the thing that failed, and log_event must never raise.
        try:
            from app.middleware.tenancy import UNATTRIBUTED_ORG as _unattr
        except Exception:
            _unattr = "_unattributed"
        org_id = explicit_org_id or _unattr

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
