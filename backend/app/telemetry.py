"""Telemetry write and read API. Shared foundation for all AgentIQ 2.0
observability surfaces.  See T1-S10-C.

Public surface
--------------
  record_event(...)        — fire-and-forget write.  Never raises.
  get_telemetry_range(...) — time-range read scoped to org_id.
                             Only approved read path until T1-S15-C adds
                             aggregation.

All telemetry writes go through record_event().  No story writes directly
to telemetry_events.

Database
--------
Uses get_db_session() from database.connection — a thin sqlite3 session
adapter whose interface mirrors SQLAlchemy Session for testability.
Table is created lazily on first write/read via _ensure_telemetry_table().
"""

from __future__ import annotations

import json
import logging
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from typing import NotRequired, TypedDict

from database.connection import get_db_connection, get_db_session
from database.models.telemetry import ALL_TELEMETRY_DDL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy table initialisation  (mirrors pattern used by other tables)
# ---------------------------------------------------------------------------

_table_ready = False   # module-level guard; avoids re-running DDL every call


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
    """In-memory representation of a telemetry_events row.

    Attributes mirror the table columns exactly.  payload is kept as a
    JSON string (not a dict) so the dataclass can be passed directly to
    the session adapter without an extra serialisation step.  Callers that
    need the parsed payload should do json.loads(event.payload).

    success is bool | None (not int) — the session adapter converts to
    SQLite INTEGER on write and back on read.
    """

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
    """Raised by get_telemetry_range() when the database operation fails.

    Wraps the underlying exception so callers receive a typed, stable error
    rather than a raw sqlite3 or SQLAlchemy exception.
    """


# ---------------------------------------------------------------------------
# Payload TypedDicts — documentation / type-checking only.
# record_event() accepts any JSON-serialisable dict.
# New event type added in a sprint → add a TypedDict here + registry entry.
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
    """T3-S10-A — written at end of each run for temporal baselining."""
    signal_values: NotRequired[dict[str, Any]]


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
    """T3-S12 — written after entity extraction."""
    entity_count: NotRequired[int]
    failure_count: NotRequired[int]


class DbQueryExecutedEvent(TypedDict):
    """T1-S10-C (T2 events) — written after every connector DB/API query.

    connector_id:  Which connector issued the query.
    query_hash:    SHA-256 hex digest of the query string (never the raw query).
    row_count:     Number of rows returned.
    duration_ms:   Query round-trip time in milliseconds.
    driver:        Connector driver name (e.g. 'salesforce_soql', 'servicenow_rest').
    truncated:     True when the result set was capped by a row limit.
    """
    connector_id: str
    query_hash: str
    row_count: int
    duration_ms: int
    driver: str
    truncated: bool


class DbIngestorCompletedEvent(TypedDict):
    """T1-S10-C (T2 events) — written after a connector ingestor finishes.

    connector_id:     Which connector ran the ingestor.
    tables_processed: Number of entity types / tables processed.
    rows_ingested:    Total rows written to the local store.
    duration_ms:      Total ingestor wall-clock time in milliseconds.
    """
    connector_id: str
    tables_processed: int
    rows_ingested: int
    duration_ms: int


# ---------------------------------------------------------------------------
# Event type registry
# Sprint 10 initial set.  Add new types in the sprint that produces them.
# ---------------------------------------------------------------------------

EVENT_TYPE_REGISTRY: frozenset[str] = frozenset({
    # T1-S10-C (this story)
    "connector.health_check",
    "run.started",
    "run.completed",
    # T1-S10-C T2 events — connector query and ingestor observability
    "db.query_executed",
    "db.ingestor_completed",
    # T3-S10-A
    "run.signal_snapshot",
    # T1-S14-C  (Sprint 14)
    "pack.executed",
    "detector.fired",
    "llm.enrichment_attempted",
    # T3-S12  (Sprint 12, Track 3)
    "entity.extraction_completed",
})


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

def record_event(
    *,
    org_id: str,
    event_type: str,
    source: str,
    run_id: Optional[str] = None,
    connector_id: Optional[str] = None,
    pack_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    success: Optional[bool] = None,
    count: Optional[int] = None,
    error_code: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Fire-and-forget telemetry write.  Never raises.

    Creates a TelemetryEvent and persists it via get_db_session().
    Failures (serialisation error, DB unavailable, etc.) are written to the
    application error log.  The caller's operation is never interrupted.

    Args:
        org_id:       Workspace / tenant identifier.
        event_type:   One of the strings in EVENT_TYPE_REGISTRY.
        source:       Logical component that produced the event.
        run_id:       Discovery run ID, if applicable.
        connector_id: Connector identifier, if applicable.
        pack_id:      Pack identifier, if applicable.
        duration_ms:  Elapsed milliseconds for the measured operation.
        success:      Whether the operation succeeded (True/False/None).
        count:        Cardinality metric (e.g. number of opportunities).
        error_code:   Short error identifier on failure.
        payload:      Arbitrary JSON-serialisable metadata dict.
    """
    try:
        _ensure_telemetry_table()

        # Validate payload is JSON-serialisable before touching the DB.
        payload_str: str = json.dumps(payload if payload is not None else {})

        event = TelemetryEvent(
            id=str(uuid.uuid4()),
            org_id=org_id,
            event_type=event_type,
            source=source,
            run_id=run_id,
            connector_id=connector_id,
            pack_id=pack_id,
            duration_ms=duration_ms,
            success=success,
            count=count,
            error_code=error_code,
            payload=payload_str,
            timestamp=datetime.now(timezone.utc),
        )

        with get_db_session() as session:
            session.add(event)
            session.commit()

    except Exception:
        logger.error(
            "telemetry.record_event failed — event_type=%s org_id=%s\n%s",
            event_type,
            org_id,
            traceback.format_exc(),
        )


# ---------------------------------------------------------------------------
# Public read API
# Only approved read path until T1-S15-C adds aggregation and dashboards.
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

    Args:
        org_id:     Workspace identifier.  Must not be empty or None.
        event_type: Event type string from EVENT_TYPE_REGISTRY.
        from_dt:    Range start (inclusive).  Timezone-aware UTC datetime.
        to_dt:      Range end   (exclusive).  Timezone-aware UTC datetime.
        limit:      Maximum rows returned (default 1 000; capped at 10 000).

    Returns:
        List of TelemetryEvent-compatible objects with columns as attributes.
        payload is the raw JSON string; callers must json.loads() if needed.
        Returns [] on empty result, never None.

    Raises:
        ValueError:          If org_id is empty/None, from_dt >= to_dt, or
                             limit < 1.  Raised before any DB call.
        TelemetryReadError:  If the database operation fails.  Wraps the
                             underlying exception.
    """
    # --- Parameter validation (before any DB call) ---
    if not org_id:
        raise ValueError("org_id must not be empty or None")
    if from_dt >= to_dt:
        raise ValueError("from_dt must be strictly before to_dt")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    # --- Limit clamping ---
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
                session
                .query(TelemetryEvent)
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
            "get_telemetry_range failed — org_id=%s event_type=%s from=%s to=%s: %s",
            org_id,
            event_type,
            from_dt,
            to_dt,
            exc,
        )
        raise TelemetryReadError(str(exc)) from exc
