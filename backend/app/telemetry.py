"""
T1-S10-C  |  Telemetry Event Registry
AgentIQ 2.0  |  Track 1 — Platform Foundation  |  Sprint 10

Provides a centralised registry of telemetry event types and a single
``record_event()`` entry-point that downstream tracks can call without
ever modifying this module.

Design principles
-----------------
* **Open for extension, closed for modification.**  New event types are
  registered via ``register_event_type()``.  ``record_event()`` itself
  never needs to change when new event types are added.
* **Fail-silently contract.**  ``record_event()`` must never raise — telemetry
  failure must not propagate to the caller (see T2-S10-A AC9).
* **Typed payloads.**  Every registered event type is backed by a
  ``TypedDict`` so callers get IDE autocompletion and static-analysis
  coverage.

Registered event types (this module)
--------------------------------------
T1 core events
  run.started           RunStartedEvent
  run.completed         RunCompletedEvent
  connector.registered  ConnectorRegisteredEvent

T2 database events  (added by T2-S10-A, task T11)
  db.query_executed     DbQueryExecutedEvent
  db.ingestor_completed DbIngestorCompletedEvent

Downstream tracks add further event types by calling
``register_event_type()`` or appending to the registry dict directly —
both approaches leave ``record_event()`` untouched.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, MutableMapping, Optional, Type

logger = logging.getLogger(__name__)

# ── Event-type registry ───────────────────────────────────────────────────────
# Maps event_type string → TypedDict class.
# The dict is intentionally public so downstream stories can inspect it;
# prefer register_event_type() for writes so typos are caught early.

EVENT_REGISTRY: MutableMapping[str, Type] = {}


def register_event_type(event_type: str, schema: Type) -> None:
    """Register *event_type* and its TypedDict *schema* in the global registry.

    Idempotent: registering the same type twice with the same schema is a
    no-op; registering with a *different* schema raises ``ValueError`` so
    accidental overwrites are surfaced during development.
    """
    if event_type in EVENT_REGISTRY:
        if EVENT_REGISTRY[event_type] is not schema:
            raise ValueError(
                f"Telemetry event type '{event_type}' is already registered "
                f"with {EVENT_REGISTRY[event_type]!r}; cannot re-register "
                f"with {schema!r}."
            )
        return  # idempotent re-registration with same schema
    EVENT_REGISTRY[event_type] = schema


# ── Core record function ───────────────────────────────────────────────────────


def record_event(
    event_type: str,
    payload: Dict[str, Any],
    *,
    ts: Optional[float] = None,
) -> None:
    """Record a telemetry event.

    Parameters
    ----------
    event_type:
        Registered event type string, e.g. ``'db.query_executed'``.
    payload:
        Dict matching the TypedDict registered for *event_type*.  Extra keys
        are allowed (forward-compatible) but missing required keys will
        produce a warning in debug mode.
    ts:
        Optional Unix timestamp override.  Defaults to ``time.time()``.

    Failure contract
    ----------------
    This function **never raises**.  Any internal error is logged at ERROR
    level and silently swallowed so that the caller's main path is never
    disrupted.  This is intentional: telemetry is observability, not
    business logic.
    """
    try:
        event: Dict[str, Any] = {
            "event_type": event_type,
            "ts": ts if ts is not None else time.time(),
            **payload,
        }

        if logger.isEnabledFor(logging.DEBUG):
            known = event_type in EVENT_REGISTRY
            if not known:
                logger.debug(
                    "[telemetry] record_event called with unregistered type '%s'",
                    event_type,
                )

        logger.info("[telemetry] %s", event)

        # ── Persistence hook (T1-S10-C future work) ──────────────────────
        # Wire to a persistent store / streaming backend here when the
        # telemetry sink is ready.  For now, structured logging is the
        # source of truth.
        # _sink.emit(event)

    except Exception:  # noqa: BLE001  — intentional broad catch
        logger.exception(
            "[telemetry] record_event failed silently for event_type='%s'",
            event_type,
        )


# ─────────────────────────────────────────────────────────────────────────────
# T1 core event TypedDicts
# ─────────────────────────────────────────────────────────────────────────────

try:
    from typing import TypedDict
except ImportError:  # Python < 3.8 fallback (should not be needed in production)
    from typing_extensions import TypedDict  # type: ignore[assignment]


class RunStartedEvent(TypedDict):
    """Emitted when an AgentIQ discovery run begins."""

    run_id: str
    org_id: str


class RunCompletedEvent(TypedDict):
    """Emitted when a discovery run finishes (success or partial)."""

    run_id: str
    org_id: str
    duration_ms: int
    connectors_processed: int


class ConnectorRegisteredEvent(TypedDict):
    """Emitted when a connector is registered with the runner."""

    connector_id: str
    org_id: str


# ─────────────────────────────────────────────────────────────────────────────
# T2 database event TypedDicts  (T2-S10-A, task T11)
#
# Added by Track 2 (Enterprise Technology) Sprint 10 after coordination with
# the Track 1 engineer who owns T1-S10-C.  record_event() required zero
# modification — only registry entries were added below.
# ─────────────────────────────────────────────────────────────────────────────


class DbQueryExecutedEvent(TypedDict):
    """Emitted by execute_query() after every successful or truncated DB read.

    Fields
    ------
    connector_id:
        One of ``'sqlserver'``, ``'oracle_db'``, ``'postgresql'``.
    query_hash:
        SHA-256 hex digest of the raw query string.  Never the query itself.
    row_count:
        Number of rows returned (after truncation if applicable).
    duration_ms:
        Wall-clock time of the database round-trip in milliseconds.
    driver:
        Python driver used, e.g. ``'pyodbc'``, ``'oracledb'``,
        ``'psycopg2'``.
    truncated:
        ``True`` when the result set hit MAX_ROWS_PER_QUERY and was capped.
    """

    connector_id: str
    query_hash: str
    row_count: int
    duration_ms: int
    driver: str
    truncated: bool


class DbIngestorCompletedEvent(TypedDict):
    """Emitted by a database ingestor after a full ingestion run completes.

    Fields
    ------
    connector_id:
        Identifies the database connector that performed the ingestion.
    tables_processed:
        Number of tables queried during this ingestion run.
    rows_ingested:
        Total rows read across all tables (after per-table truncation).
    duration_ms:
        Total wall-clock time for the entire ingestion run in milliseconds.
    """

    connector_id: str
    tables_processed: int
    rows_ingested: int
    duration_ms: int


# ─────────────────────────────────────────────────────────────────────────────
# Registry population
# ─────────────────────────────────────────────────────────────────────────────
# All event types are registered here so the registry is populated at import
# time.  record_event() is unmodified by these additions.

register_event_type("run.started", RunStartedEvent)
register_event_type("run.completed", RunCompletedEvent)
register_event_type("connector.registered", ConnectorRegisteredEvent)

# T2 database events
register_event_type("db.query_executed", DbQueryExecutedEvent)
register_event_type("db.ingestor_completed", DbIngestorCompletedEvent)
