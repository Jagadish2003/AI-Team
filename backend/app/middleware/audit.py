"""
T1-S10-B  |  Audit Event Registry
AgentIQ 2.0  |  Track 1 — Platform Foundation  |  Sprint 10

Centralised audit-event registry and ``log_event()`` entry-point used by
every track that needs to write an immutable, org-scoped audit trail.

Design principles
-----------------
* **Open for extension, closed for modification.**
  Event type constants and TypedDicts are added to the registry without
  ever touching ``log_event()``.  New callers — including Track 2 DB
  stories — call ``log_event()`` with their event-type string and keyword
  fields; the function signature is permanently stable.

* **Distinct event types for distinct data-access semantics.**
  ``schema_discovered`` and ``connector_queried`` are *separate* constants
  with *separate* string values.  Schema discovery reads system catalogues
  only (no customer data); query execution reads declared customer tables.
  Audit consumers downstream can therefore apply different retention,
  alerting, or compliance rules to each event class.

* **Fail-silent contract.**
  ``log_event()`` logs at ERROR level on internal failures and swallows the
  exception — an audit write failure must never crash the caller.

Registered event types
----------------------
T1 core events
  connector.registered    ConnectorRegisteredAuditEvent
  run.started             RunStartedAuditEvent
  run.completed           RunCompletedAuditEvent

T1-S10-B  /  T2-S10-A  joint events
  connector_queried       ConnectorQueriedEvent   (execute_query)
  schema_discovered       SchemaDiscoveredEvent   (discover_schema)
  scope_declared          ScopeDeclaredEvent      (save_scope)

Callers (doc reference: T2-S10-A Table 11)
-------------------------------------------
::

    from backend.app.middleware.audit import log_event

    # After execute_query():
    log_event('connector_queried',
        org_id=org_id, run_id=run_id, connector_id=config.connector_id,
        query_hash=result.query_hash,
        row_count=result.row_count, duration_ms=result.duration_ms)

    # After discover_schema() — distinct event, no customer data:
    log_event('schema_discovered',
        org_id=org_id, connector_id=config.connector_id,
        schema_count=len(result.schemas), table_count=len(result.tables))
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, MutableMapping, Optional, Type

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Event-type string constants
#
# Each constant MUST have a unique string value — audit consumers route on
# these strings and must be able to distinguish event classes.
# schema_discovered != connector_queried is enforced by the unit test suite.
# ─────────────────────────────────────────────────────────────────────────────

# T2 database events  (T2-S10-A, tasks T7 / T8 / T12)
CONNECTOR_QUERIED: str = "connector_queried"
SCHEMA_DISCOVERED: str = "schema_discovered"
SCOPE_DECLARED: str = "scope_declared"

# T1 platform lifecycle events
CONNECTOR_REGISTERED: str = "connector.registered"
RUN_STARTED: str = "run.started"
RUN_COMPLETED: str = "run.completed"

# ─────────────────────────────────────────────────────────────────────────────
# Audit-event TypedDicts
# ─────────────────────────────────────────────────────────────────────────────

try:
    from typing import TypedDict
except ImportError:  # Python < 3.8
    from typing_extensions import TypedDict  # type: ignore[assignment]


# ── T2 database events ────────────────────────────────────────────────────────


class ConnectorQueriedEvent(TypedDict):
    """Audit event emitted by execute_query() after every successful DB read.

    Written once per query execution.  Contains the SHA-256 hash of the
    query (never the raw query text) so the audit log is safe to store
    without risk of exposing PII-containing query strings.

    Fields
    ------
    org_id:
        Workspace/tenant identifier.  Used for multi-tenant audit isolation.
    run_id:
        Discovery run that triggered this query.
    connector_id:
        One of ``'sqlserver'``, ``'oracle_db'``, ``'postgresql'``.
    query_hash:
        SHA-256 hex digest of the raw query string (not the query itself).
    row_count:
        Number of rows returned after any truncation.
    duration_ms:
        Wall-clock query round-trip in milliseconds.
    """

    org_id: str
    run_id: str
    connector_id: str
    query_hash: str
    row_count: int
    duration_ms: int


class SchemaDiscoveredEvent(TypedDict):
    """Audit event emitted by discover_schema() after system-catalogue reads.

    This event is DISTINCT from ``connector_queried``:

    * **Schema discovery** reads database system catalogues only.
      No customer data tables are accessed.  There is no ``run_id`` because
      discovery is triggered interactively (scope picker), not by a run.

    * **connector_queried** is written during ingestion runs and references
      declared customer tables.  It carries ``run_id`` and ``query_hash``.

    Audit consumers applying compliance rules (e.g. data-access logging,
    GDPR retention limits) MUST treat these as separate event classes.

    Fields
    ------
    org_id:
        Workspace/tenant identifier.
    connector_id:
        One of ``'sqlserver'``, ``'oracle_db'``, ``'postgresql'``.
    schema_count:
        Number of distinct schemas found in the system catalogue.
    table_count:
        Total number of tables discovered across all schemas.
    """

    org_id: str
    connector_id: str
    schema_count: int
    table_count: int


class ScopeDeclaredEvent(TypedDict):
    """Audit event emitted by save_scope() when an analyst declares scope.

    Fields
    ------
    org_id:
        Workspace/tenant identifier.
    connector_id:
        Database connector whose scope is being declared.
    schemas:
        List of declared schema names.
    tables:
        List of declared table names (empty list = any table in schemas).
    """

    org_id: str
    connector_id: str
    schemas: list
    tables: list


# ── T1 platform lifecycle events ───────────────────────────────────────────────


class ConnectorRegisteredAuditEvent(TypedDict):
    """Emitted when a connector is registered with the platform runner."""

    org_id: str
    connector_id: str


class RunStartedAuditEvent(TypedDict):
    """Emitted when a discovery run begins."""

    org_id: str
    run_id: str


class RunCompletedAuditEvent(TypedDict):
    """Emitted when a discovery run finishes."""

    org_id: str
    run_id: str
    duration_ms: int
    connectors_processed: int


# ─────────────────────────────────────────────────────────────────────────────
# Audit-event registry
#
# Maps event_type string → TypedDict class.
# Public so downstream stories can introspect; use register_audit_event_type()
# for writes to get conflict detection.
# ─────────────────────────────────────────────────────────────────────────────

AUDIT_EVENT_REGISTRY: MutableMapping[str, Type] = {}


def register_audit_event_type(event_type: str, schema: Type) -> None:
    """Register *event_type* and its TypedDict *schema*.

    Idempotent: same type + same schema → no-op.
    Same type + different schema → ``ValueError`` (catch misregistrations early).
    """
    if event_type in AUDIT_EVENT_REGISTRY:
        if AUDIT_EVENT_REGISTRY[event_type] is not schema:
            raise ValueError(
                f"Audit event type '{event_type}' is already registered with "
                f"{AUDIT_EVENT_REGISTRY[event_type]!r}; cannot re-register "
                f"with {schema!r}."
            )
        return
    AUDIT_EVENT_REGISTRY[event_type] = schema


# ─────────────────────────────────────────────────────────────────────────────
# Core log_event() — the T1-S10-B stable interface
#
# This function NEVER needs modification when new event types are added.
# Track 2 stories call log_event('schema_discovered', ...) without any
# change to T1-S10-B code — only registry entries are appended below.
# ─────────────────────────────────────────────────────────────────────────────


def log_event(
    event_type: str,
    *,
    ts: Optional[float] = None,
    **fields: Any,
) -> None:
    """Write an immutable audit event.

    Parameters
    ----------
    event_type:
        Registered audit event type constant, e.g. ``SCHEMA_DISCOVERED``.
        Passing an unregistered type is allowed and emits a DEBUG warning —
        it does **not** raise.
    ts:
        Optional Unix timestamp override.  Defaults to ``time.time()``.
    **fields:
        Keyword arguments matching the TypedDict for *event_type*.
        Extra keys are accepted (forward-compatible).

    Failure contract
    ----------------
    This function **never raises**.  Any internal failure is logged at
    ERROR level and silently dropped — audit failures must not propagate
    to the caller.

    Extension contract
    ------------------
    New event types are supported by adding a string constant, a TypedDict,
    and a ``register_audit_event_type()`` call **below** this function.
    The function body itself is immutable — this is intentional.
    """
    try:
        record: Dict[str, Any] = {
            "event_type": event_type,
            "ts": ts if ts is not None else time.time(),
            **fields,
        }

        if logger.isEnabledFor(logging.DEBUG):
            if event_type not in AUDIT_EVENT_REGISTRY:
                logger.debug(
                    "[audit] log_event called with unregistered type '%s'",
                    event_type,
                )

        logger.info("[audit] %s", record)

        # ── Persistence hook (T1-S10-B future work) ──────────────────────
        # Wire to the audit-events table or append-only audit stream here.
        # _audit_sink.write(record)

    except Exception:  # noqa: BLE001  — intentional broad catch
        logger.exception(
            "[audit] log_event failed silently for event_type='%s'", event_type
        )


# ─────────────────────────────────────────────────────────────────────────────
# Registry population — append only; never modify log_event() above
# ─────────────────────────────────────────────────────────────────────────────

# T1 platform lifecycle
register_audit_event_type(CONNECTOR_REGISTERED, ConnectorRegisteredAuditEvent)
register_audit_event_type(RUN_STARTED, RunStartedAuditEvent)
register_audit_event_type(RUN_COMPLETED, RunCompletedAuditEvent)

# T2 database events  (T2-S10-A, task T12 — schema_discovered added here)
register_audit_event_type(CONNECTOR_QUERIED, ConnectorQueriedEvent)
register_audit_event_type(SCHEMA_DISCOVERED, SchemaDiscoveredEvent)
register_audit_event_type(SCOPE_DECLARED, ScopeDeclaredEvent)
