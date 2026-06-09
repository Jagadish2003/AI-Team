"""Telemetry write and read API — shared foundation for AgentIQ 2.0.

Public surface
--------------
  record_event(event_type, payload)  — fire-and-forget write for registered events.
  get_telemetry_range(...)           — time-range read scoped to org_id.
  register_event_type(name, schema)  — register a TypedDict schema for an event type.

Signature contract (locked by T3-S10-A):
    record_event(event_type: str, payload: Optional[dict] = None) -> None

All telemetry writes go through record_event(). No story writes directly
to telemetry_events.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, MutableMapping, Optional, Type

from typing_extensions import NotRequired, TypedDict

from database.connection import get_db_connection, get_db_session
from database.models.telemetry import ALL_TELEMETRY_DDL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event type registry — maps event_type string to its payload TypedDict class.
# Mutable so new event types can be registered at module load time.
# ---------------------------------------------------------------------------

EVENT_REGISTRY: MutableMapping[str, Type[Any]] = {}
TELEMETRY_EVENT_REGISTRY = EVENT_REGISTRY   # alias for Track 3 imports
EVENT_TYPE_REGISTRY = EVENT_REGISTRY        # alias for T1-S10-C unit tests

# ---------------------------------------------------------------------------
# Lazy table initialisation
# ---------------------------------------------------------------------------

_table_ready = False


def _ensure_telemetry_table() -> None:
    """Create telemetry_events and its indexes if they do not yet exist."""
    global _table_ready
    if _table_ready:
        return
    with get_db_connection() as conn:
        for ddl in ALL_TELEMETRY_DDL:
            conn.execute(ddl)
        conn.commit()
    _table_ready = True


# ---------------------------------------------------------------------------
# Domain object — one instance per row written/returned
# ---------------------------------------------------------------------------

@dataclass
class TelemetryEvent:
    """In-memory representation of a telemetry_events row."""

    id: str
    org_id: str
    event_type: str
    source: str
    run_id: Optional[str]
    connector_id: Optional[str]
    pack_id: Optional[str]
    duration_ms: Optional[int]
    success: Optional[bool]
    count: Optional[int]
    error_code: Optional[str]
    payload: str        # JSON-serialised dict
    timestamp: datetime


# ---------------------------------------------------------------------------
# Application exception for read failures
# ---------------------------------------------------------------------------

class TelemetryReadError(Exception):
    """Raised by get_telemetry_range() when the database operation fails."""


# ---------------------------------------------------------------------------
# Payload TypedDicts — documentation only; record_event() accepts any dict.
# ---------------------------------------------------------------------------

class RunStartedPayload(TypedDict, total=False):
    pack_id: NotRequired[Optional[str]]
    system_count: NotRequired[Optional[int]]


class RunCompletedPayload(TypedDict, total=False):
    pack_id: NotRequired[Optional[str]]
    system_count: NotRequired[Optional[int]]


class ConnectorHealthPayload(TypedDict):
    status: str                          # 'connected' | 'needs_refresh' | 'needs_auth'
    connector_id: str
    token_expiry_seconds: Optional[int]
    check_duration_ms: int


class RunSignalSnapshotPayload(TypedDict, total=False):
    """T3-S10-A — aggregate signal snapshot write summary per run."""
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    pack_id: NotRequired[str]
    signal_count: int
    detector_count: int
    fired_count: int
    below_threshold: int


RunSignalSnapshotEvent = RunSignalSnapshotPayload   # alias


class PackExecutedPayload(TypedDict, total=False):
    """T1-S14-C — written after each pack execution."""
    pack_id: NotRequired[str]
    detector_count: NotRequired[int]
    duration_ms: NotRequired[int]


class DetectorFiredPayload(TypedDict, total=False):
    """T1-S14-C — one record per detector that fires in a run."""
    detector_id: NotRequired[str]
    pack_id: NotRequired[str]


class LlmEnrichmentAttemptedPayload(TypedDict, total=False):
    """T1-S14-C — written on each LLM enrichment call."""
    model: NotRequired[str]
    prompt_tokens: NotRequired[int]
    completion_tokens: NotRequired[int]


class EntityExtractionCompletedPayload(TypedDict, total=False):
    """T3-S12-A T7 — written after entity extraction completes successfully.

    ambiguous_count is load-bearing for monitoring: a spike in ambiguous
    entities per org_id signals naming-convention changes or data-quality
    degradation in the source system. Not emitted on exception — runner
    warning log covers that failure path.
    """
    entity_count: NotRequired[int]
    ambiguous_count: NotRequired[int]
    failure_count: NotRequired[int]
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    source: NotRequired[str]
    pack_id: NotRequired[str]


class TemporalEnrichmentCompletedPayload(TypedDict, total=False):
    """T3-S11-A — emitted once per run after temporal enrichment completes."""
    run_id: NotRequired[str]
    opp_count: NotRequired[int]


class RelationshipMappingCompletedPayload(TypedDict, total=False):
    """T3-S13-A — emitted once per run after relationship mapping completes.

    Emitted from inside relationship_mapper.map_relationships() on success only.
    The runner's non-blocking wrapper swallows any failure, so the ABSENCE of
    this event alongside a warning log is the diagnostic signal for a failed
    mapping run. observed_edges / inferred_edges count the edges upserted this
    run (inferred edges are always stored regardless of the surfacing flag).
    """
    run_id: NotRequired[str]
    org_id: NotRequired[str]
    observed_edges: NotRequired[int]
    inferred_edges: NotRequired[int]
    total_edges: NotRequired[int]


class RunStartedEvent(TypedDict):
    run_id: str
    org_id: str


class RunCompletedEvent(TypedDict):
    run_id: str
    org_id: str
    duration_ms: int
    connectors_processed: int


class ConnectorRegisteredEvent(TypedDict):
    connector_id: str
    org_id: str


class DbQueryExecutedEvent(TypedDict):
    """T1-S10-C T2 events — written after every connector DB/API query."""
    connector_id: str
    query_hash: str
    row_count: int
    duration_ms: int
    driver: str
    truncated: bool


class DbIngestorCompletedEvent(TypedDict):
    """T1-S10-C T2 events — written after a connector ingestor finishes."""
    connector_id: str
    tables_processed: int
    rows_ingested: int
    duration_ms: int


class DBIngestorCompletedPayload(TypedDict):
    """T2-S11-A Sprint 11 payload for db.ingestor_completed.

    Replaces DbIngestorCompletedEvent in the registry.  Written by every
    Track 2 DB ingestor (SQL Server, Oracle DB, PostgreSQL) at the end of
    each discovery run.  Emitted via record_event() — fire-and-forget.

    Fields
    ------
    connector_id:
        Identifies the source database connector, e.g. ``'sqlserver'``.
    pack_id:
        The detector pack that consumed the ingested signals,
        e.g. ``'sqlserver_opsignal'``.
    query_count:
        Number of execute_query() calls made during this ingestion run
        (one per signal query, e.g. 3 for the SQL Server ingestor).
    signal_count:
        Number of signal metrics successfully extracted across all queries.
    degraded_count:
        Number of metrics with degraded_signal=True (query timeout,
        missing column, or other partial-failure conditions).
    duration_ms:
        Total wall-clock time for the entire ingestor execution in
        milliseconds, from first query to return.
    """
    connector_id: str
    pack_id: str
    query_count: int
    signal_count: int
    degraded_count: int
    duration_ms: int


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def register_event_type(event_type: str, schema: Type[Any]) -> None:
    """Register an event type and its payload TypedDict schema.

    Idempotent when called with the same schema.  Raises ValueError if the
    same event_type is registered with a different schema (catches mistakes).
    """
    if event_type in EVENT_REGISTRY:
        if EVENT_REGISTRY[event_type] is not schema:
            raise ValueError(
                f"Telemetry event type '{event_type}' is already registered "
                f"with {EVENT_REGISTRY[event_type]!r}; cannot re-register "
                f"with {schema!r}."
            )
        return
    EVENT_REGISTRY[event_type] = schema


# ---------------------------------------------------------------------------
# Named aliases — used by AT-211 contract tests and ValueError message copy.
# REGISTERED_EVENT_TYPES: set-like view of every registered event_type name.
# EVENT_PAYLOAD_TYPES:    mapping of event_type → TypedDict schema class.
# Both are live views of EVENT_REGISTRY; no separate sync required.
# ---------------------------------------------------------------------------

REGISTERED_EVENT_TYPES = EVENT_REGISTRY   # alias: keys are the registered names
EVENT_PAYLOAD_TYPES = EVENT_REGISTRY      # alias: values are the TypedDict schemas


# Register Sprint 10 initial set
register_event_type("run.started", RunStartedEvent)
# AT-209 audit: run.completed call sites send pack_id/system_count (see
# discovery/runner.py), which match RunCompletedPayload. The legacy
# RunCompletedEvent required a connectors_processed field that no call site
# emits, so the payload never matched its registered schema. Bind to the
# documented RunCompletedPayload (Task 5A §1b). RunCompletedEvent is retained
# in __all__ for backward-compatible imports.
register_event_type("run.completed", RunCompletedPayload)
register_event_type("connector.registered", ConnectorRegisteredEvent)
register_event_type("connector.health_check", ConnectorHealthPayload)
register_event_type("db.query_executed", DbQueryExecutedEvent)
register_event_type("db.ingestor_completed", DBIngestorCompletedPayload)
register_event_type("run.signal_snapshot", RunSignalSnapshotPayload)
# T3-S11-A Sprint 11
register_event_type("temporal.enrichment_completed", TemporalEnrichmentCompletedPayload)
# T3-S12-A T7 Sprint 12
register_event_type("entity.extraction_completed", EntityExtractionCompletedPayload)
# T3-S13-A Sprint 13 — relationship mapping (emitted by map_relationships())
register_event_type("relationship.mapping_completed", RelationshipMappingCompletedPayload)


# ---------------------------------------------------------------------------
# Public write API — locked signature (T3-S10-A contract)
# ---------------------------------------------------------------------------

def record_event(event_type: str, payload: Optional[dict] = None) -> None:
    """Fire-and-forget telemetry write.

    Signature is locked: record_event(event_type, payload).
    Track 3 (T3-S10-A) calls this with 2 positional args.

    Raises:
        ValueError: if event_type is not in EVENT_REGISTRY.

    The function:
    1. Logs the event via logger.info so tests can observe it via caplog.
    2. Persists to telemetry_events DB table (best-effort; never raises for DB errors).
    """
    if event_type not in EVENT_REGISTRY:
        raise ValueError(
            f"unregistered event type: '{event_type}'. "
            f"Add it to REGISTERED_EVENT_TYPES before calling record_event()."
        )
    try:
        if payload is None:
            payload = {}

        # Log for observability — Track 3 contract tests read from caplog.
        event_log = {
            "event_type": event_type,
            "ts": time.time(),
            **payload,
        }
        logger.info("[telemetry] %s", event_log)

        # Persist to telemetry_events table.
        _ensure_telemetry_table()

        # org_id priority: tenancy context → payload["org_id"] → "unknown"
        try:
            from app.middleware.tenancy import get_current_org_id_optional
            org_id = get_current_org_id_optional() or payload.get("org_id", "unknown")
        except Exception:
            org_id = payload.get("org_id", "unknown")

        payload_str = json.dumps(payload)

        tel_event = TelemetryEvent(
            id=str(uuid.uuid4()),
            org_id=org_id,
            event_type=event_type,
            source=payload.get("source", "telemetry"),
            run_id=payload.get("run_id"),
            connector_id=payload.get("connector_id"),
            pack_id=payload.get("pack_id"),
            duration_ms=payload.get("duration_ms"),
            success=payload.get("success"),
            count=payload.get("count"),
            error_code=payload.get("error_code"),
            payload=payload_str,
            timestamp=datetime.now(timezone.utc),
        )

        with get_db_session() as session:
            session.add(tel_event)
            session.commit()

    except Exception:
        logger.error(
            "telemetry.record_event failed — event_type=%s\n%s",
            event_type,
            traceback.format_exc(),
        )


# ---------------------------------------------------------------------------
# Public read API — only approved read path until T1-S15-C.
# ---------------------------------------------------------------------------

_MAX_LIMIT = 10_000


def get_telemetry_range(
    org_id: str,
    event_type: str,
    from_dt: datetime,
    to_dt: datetime,
    limit: int = 1000,
) -> list[Any]:
    """Return telemetry events for a given org, event type, and UTC time range.

    Always scoped to org_id — events from other orgs are never returned.
    Results are ordered oldest-first (timestamp ASC).

    Raises:
        ValueError:         org_id empty/None, from_dt >= to_dt, or limit < 1.
        TelemetryReadError: DB operation failed.
    """
    if not org_id:
        raise ValueError("org_id must not be empty or None")
    if from_dt >= to_dt:
        raise ValueError("from_dt must be strictly before to_dt")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    if limit > _MAX_LIMIT:
        logger.warning(
            "get_telemetry_range: requested limit %d exceeds maximum %d — clamped",
            limit,
            _MAX_LIMIT,
        )
        limit = _MAX_LIMIT

    _ensure_telemetry_table()

    try:
        with get_db_session() as session:
            return (
                session.query(TelemetryEvent)
                .filter(
                    org_id=org_id,
                    event_type=event_type,
                    from_dt=from_dt.isoformat(),
                    to_dt=to_dt.isoformat(),
                )
                .order_by("timestamp")
                .limit(limit)
                .all()
            )
    except TelemetryReadError:
        raise
    except Exception as exc:
        logger.error(
            "get_telemetry_range failed — org_id=%s event_type=%s: %s",
            org_id,
            event_type,
            exc,
        )
        raise TelemetryReadError(str(exc)) from exc


__all__ = [
    "ConnectorHealthPayload",
    "ConnectorRegisteredEvent",
    "DBIngestorCompletedPayload",           # Sprint 11 — SQL Server ingestor payload
    "DbIngestorCompletedEvent",             # T1-S10-C legacy — kept for backward compat
    "DbQueryExecutedEvent",
    "EntityExtractionCompletedPayload",     # T3-S12-A T7
    "RelationshipMappingCompletedPayload",  # T3-S13-A
    "EVENT_PAYLOAD_TYPES",          # AT-211 alias: event_type → TypedDict schema
    "EVENT_REGISTRY",
    "EVENT_TYPE_REGISTRY",          # alias for T1-S10-C unit tests
    "REGISTERED_EVENT_TYPES",       # AT-211 alias: set-like view of registered names
    "RunCompletedEvent",
    "RunSignalSnapshotEvent",
    "RunSignalSnapshotPayload",
    "RunStartedEvent",
    "TELEMETRY_EVENT_REGISTRY",
    "TemporalEnrichmentCompletedPayload",
    "TelemetryReadError",
    "get_telemetry_range",
    "record_event",
    "register_event_type",
]
