"""Audit logging — AT-82 / T1-S10-B T4.

log_event() is the single write point for audit_log. The table is INSERT-only —
no UPDATE or DELETE ever runs against it.

Registry enforcement (2.0-D4 T1)
--------------------------------
:data:`AUDIT_EVENT_REGISTRY` used to be documentation: ``log_event`` accepted any
string, while ``telemetry.record_event`` genuinely raises for an unregistered
type. That asymmetry was a footgun — an event type with a typo wrote
successfully and was then invisible to any query filtering on the constant, so
the row existed and the audit trail still had a hole.

``log_event`` now **raises** :class:`UnregisteredAuditEvent` for a type not in the
registry, matching telemetry. That is compatible with the never-raise posture
below because the two failures are different in kind: an unregistered type is a
deterministic programming error that cannot be caused by request data, whereas a
write failure is an operational condition. The change is safe rather than hopeful
— a conformance test reconciles every ``log_event`` call site in the backend
against the registry (``tests/contract/test_audit_conformance.py``), so "no
emitter passes an unregistered type" is verified in CI, not assumed. That
reconciliation is what found ``member_invited`` / ``member_removed`` being emitted
while unregistered, and ``connector_queried`` / ``run_completed`` / ``user_login``
registered while never emitted.

Why a write failure does not fail the action (2.0-D4 T1 decision)
----------------------------------------------------------------
A federal reviewer will ask whether any action is important enough that a failed
audit write should fail the action. The considered answer is **no**, for every
action currently audited, because:

  * the actions are already-committed state changes — the credential is revoked,
    the licence is installed — so refusing the response after the fact would
    report a failure that did not happen, which is a worse lie than a missing
    audit row;
  * a failed write is not silent: it emits ``audit.write_failed`` telemetry into
    the immutable store, so the gap is detectable and alertable; and
  * making the audit write a prerequisite would put the audit database on the
    critical path of every mutation, converting an audit outage into a total
    outage.

The honest consequence, stated rather than hidden: "the route called log_event"
and "an audit record exists" are different claims, and D4-AC1 is only satisfied by
the second. The conformance test therefore verifies the stored RECORD for a
representative set of routes, not merely that the call is reachable.

If a future action does need write-or-fail semantics (a signed export, say), add
an explicit wrapper that awaits the row and raises — do not change this
function's posture for everything.

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
# 2.0-D4 T1: a connector's configuration was edited. D4 names "connector
# create/edit/delete" and only create and delete were covered — an edit that
# changes what a connector reads (ServiceNow's cmdb_class_scope is the live
# example) left no trace at all. Distinct from CONNECTOR_CONNECTED, which
# records the access grant rather than a change to how it is used.
CONNECTOR_CONFIGURED = "connector_configured"
# 2.0-D4 T1: an analyst resolved one Security Operations evidence pointer back to
# its underlying record. A DISCLOSURE rather than a state change — audited for the
# same reason the signed export is (T2): reading protected security evidence is
# exactly what a reviewer asks about, and the route's own docstring already
# described itself as "audited" while emitting telemetry only.
EVIDENCE_POINTER_RESOLVED = "evidence_pointer_resolved"
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
# 2.0-B1 T4: a signed evidence-export bundle was generated. The bundle leaves the
# deployment (auditors/regulators/board packs), so WHO exported WHAT and WHEN is
# itself audit-relevant. There is no request-logging middleware in this app — a
# GET is not auto-audited — so routes_evidence_export.py calls log_event()
# explicitly. The payload carries the bundle FINGERPRINT only (scope, ids, record
# count, content root, signature prefix), never bundle content.
# 2.0-B1 T6 (AC6) routes this — and every other export surface — through the
# shared app/export_audit.py write point, which guarantees the payload names the
# acting user, the export scope, and an ISO-8601 UTC timestamp.
EVIDENCE_EXPORT_GENERATED = "evidence_export_generated"
# 2.0-B1 T6 (AC6): the R-1.9.1-L2 signed usage report is an export generation on
# the same trust model (same license report_key, same distributable posture), and
# it previously emitted no audit record at all. Same payload discipline as
# EVIDENCE_EXPORT_GENERATED: period + counts + signature prefix, never report
# content and never the whole MAC.
USAGE_REPORT_EXPORTED = "usage_report_exported"
# 2.0-A2 T1: an opportunity's lifecycle state changed (open -> actioned ->
# monitoring -> measured, plus dismissed/stalled). The dedicated append-only
# opportunity_lifecycle_history table is the domain record; this event places the
# transition in the organisation-wide audit stream with actor, org, target and
# timestamp. Emitted for SYSTEM transitions too, so a state change never appears
# in the portfolio without a corresponding audit row.
OPPORTUNITY_LIFECYCLE_TRANSITIONED = "opportunity_lifecycle_transitioned"

# 2.0-A3 T1 — an analyst decision recorded as a LEARNING signal (accept /
# dismiss / defer-with-reason), keyed on the stable opportunity_identity. Note
# this is distinct from the run-scoped review decision (APPROVED / REJECTED):
# the review answers "is this finding real?", this answers "is this finding
# worth our time?", and only the latter feeds ranking adaptation. Audited
# because A3 requires every input to an adjusted ranking to be inspectable —
# a ranking nobody can trace back to a decision is the invisible drift the
# story exists to prevent.
OPPORTUNITY_FEEDBACK_RECORDED = "opportunity_feedback_recorded"

# 2.0-A3 T4: the org-level ranking-adjustment state changed. Recompute and
# reset both emit this event because both alter the layer that can reorder
# future findings.
RANKING_ADJUSTMENT_CHANGED = "ranking_adjustment_changed"

# 2.0-D4 T1 — workspace membership changes. These two were ALREADY being emitted
# by routes_workspace.py but were absent from the registry below, so they were
# invisible to anything reading the registry as the list of accepted types. That
# is precisely the drift the registry enforcement added to log_event() now
# prevents; registering them is a correction, not a new capability.
MEMBER_INVITED = "member_invited"
MEMBER_REMOVED = "member_removed"

# 2.0-D4 T1 — license install. D4 names "license install" as a state-changing
# action that must audit; before this it emitted only a telemetry event
# ("license.updated"), which is a different store with a different retention and
# access posture, so an auditor reading audit_log saw nothing.
LICENSE_INSTALLED = "license_installed"

# 2.0-D4 T1 — analyst decisions on a run's findings and evidence. D4 names
# "analyst decisions" explicitly. The run-scoped decision lives in the `opps` /
# `evidence` KV blobs, which materialization rewrites and replay resets — so
# without an audit row there is no durable record that a human made the call.
OPPORTUNITY_DECISION_RECORDED = "opportunity_decision_recorded"
EVIDENCE_DECISION_RECORDED = "evidence_decision_recorded"

# 2.0-D4 T2 — a signed audit export was generated. Recursive on purpose: exporting
# the audit log mutates nothing, but it is a DISCLOSURE — someone took a copy of the
# organisation's audit trail out of the system — and a disclosure is exactly the kind
# of thing an auditor expects to find recorded. A later export over an overlapping
# period will therefore contain this row, which is how "who has read the trail
# before?" becomes answerable.
AUDIT_EXPORT_GENERATED = "audit_export_generated"


# ---------------------------------------------------------------------------
# 2.0-D4 T1 — outcome vocabulary (AC1's fifth required field).
# ---------------------------------------------------------------------------
# D4 requires every audited action to record its OUTCOME, not merely that it was
# attempted. A connector_disconnected event that fires identically whether the
# revocation succeeded or failed is worse than useless in a review, because it
# reads as evidence the revocation happened.
#
# This codebase already solves that in two different ways, and BOTH are
# legitimate, so the conformance test accepts either rather than forcing a
# uniform field onto existing emitters:
#
#   1. an explicit ``outcome=`` kwarg on the audit record (preferred for new
#      emissions — one event type, the result carried as data), or
#   2. a dedicated failure event type paired with the success type, which is
#      what CONNECTOR_REVOCATION_FAILED already does.
#
# :data:`OUTCOME_EVENT_PAIRS` declares the pairings for (2) so the pattern is
# machine-checkable instead of a convention someone has to notice.
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_VALUES: frozenset[str] = frozenset({OUTCOME_SUCCESS, OUTCOME_FAILURE})

#: success event type -> the failure event type that records its negative outcome.
OUTCOME_EVENT_PAIRS: dict[str, str] = {
    CONNECTOR_DISCONNECTED: CONNECTOR_REVOCATION_FAILED,
}

# ---------------------------------------------------------------------------
# Registry — every accepted event type listed here.
#
# 2.0-D4 T1: this is now ENFORCED, not documentation. log_event() raises
# ValueError for a type that is not listed here — see the "Registry enforcement"
# section of the module docstring for why that is safe despite log_event's
# never-raise posture, and why it differs from the treatment of a write failure.
# ---------------------------------------------------------------------------

AUDIT_EVENT_REGISTRY: frozenset[str] = frozenset({
    RUN_STARTED,
    RUN_COMPLETED,
    CONNECTOR_QUERIED,
    CONNECTOR_CONNECTED,
    CONNECTOR_CONFIGURED,
    CONNECTOR_DISCONNECTED,
    EVIDENCE_POINTER_RESOLVED,
    CONNECTOR_REVOCATION_FAILED,
    CONNECTOR_CREDENTIALS_SET,
    CONNECTOR_CREDENTIALS_REVOKED,
    SCOPE_DECLARED,
    USER_LOGIN,
    SETUP_STATE_SAVED,
    SCHEMA_DISCOVERED,
    INGESTION_CHECKPOINT_RESET,
    RUNBOOK_MATCH_DECIDED,
    EVIDENCE_EXPORT_GENERATED,
    USAGE_REPORT_EXPORTED,
    OPPORTUNITY_LIFECYCLE_TRANSITIONED,
    OPPORTUNITY_FEEDBACK_RECORDED,
    RANKING_ADJUSTMENT_CHANGED,
    # 2.0-D4 T1 additions.
    MEMBER_INVITED,
    MEMBER_REMOVED,
    LICENSE_INSTALLED,
    OPPORTUNITY_DECISION_RECORDED,
    EVIDENCE_DECISION_RECORDED,
    # 2.0-D4 T2.
    AUDIT_EXPORT_GENERATED,
})


class UnregisteredAuditEvent(ValueError):
    """Raised by :func:`log_event` for an event type not in the registry.

    A programming error, surfaced immediately — see the module docstring's
    "Registry enforcement" section.
    """

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
    """Append one record to audit_log.

    Two failure modes, deliberately treated differently (2.0-D4 T1 — see the
    module docstring's "Registry enforcement" and "Why a write failure does not
    fail the action"):

      * **An unregistered event type raises** :class:`UnregisteredAuditEvent`.
        This is a programming error, not a runtime condition, and letting it
        through was a real footgun: a typo wrote successfully and was then
        invisible to every query filtering on the constant.
      * **A write failure is still swallowed** — an audit failure must not break
        the request that triggered it (AC9). It is not invisible: an
        ``audit.write_failed`` telemetry event is emitted from the failure
        handler so the otherwise-silent failure is observable and alertable
        (AT-292 / FixPack v2 Fix 5).
    """
    # 2.0-D4 T1: registry enforcement. Checked BEFORE the try/except below, so it
    # is genuinely raised rather than being caught by the write-failure handler.
    if event_type not in AUDIT_EVENT_REGISTRY:
        raise UnregisteredAuditEvent(
            f"audit event type {event_type!r} is not in AUDIT_EVENT_REGISTRY — "
            f"add a constant and register it in app/middleware/audit.py. An "
            f"unregistered type writes a row that no registry-filtered query "
            f"will ever return."
        )
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
