"""Database connection helpers for AgentIQ backend.

AT-288 / Fix 1: the backend database is PostgreSQL (psycopg2). Provides two
connection primitives:

  get_db_connection()  — raw psycopg2 connection context manager (DictCursor
                         factory); used for DDL and low-level operations
                         (seed_loader, table init).
  get_db_session()     — thin session adapter context manager; used by the
                         telemetry write and read API so those functions can
                         be unit-tested by mocking this function.

The session adapter exposes add() / commit() / query() methods that mirror
the SQLAlchemy Session interface at the surface level tests exercise, while
the real implementation stays on plain psycopg2.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterator, Optional

import psycopg2
import psycopg2.extras


# ---------------------------------------------------------------------------
# Connection URL — read at call time so tests that set DATABASE_URL via env work
# ---------------------------------------------------------------------------

def _database_url() -> str:
    return os.getenv("DATABASE_URL")


def _connect() -> "psycopg2.extensions.connection":
    conn = psycopg2.connect(
        _database_url(),
        cursor_factory=psycopg2.extras.DictCursor,
        options="-c timezone=UTC",
    )
    conn.autocommit = False
    return conn


# ---------------------------------------------------------------------------
# Raw connection — for DDL, seed loading, and legacy helpers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def get_db_connection() -> Iterator["psycopg2.extensions.connection"]:
    """Context manager yielding a psycopg2 connection (DictCursor factory).

    The caller is responsible for commit() if they perform writes.
    The connection is always closed on exit.
    """
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Telemetry INSERT / SELECT SQL (owned here alongside the session adapter)
# ---------------------------------------------------------------------------
# psycopg2 named paramstyle is %(name)s (not sqlite3's :name).

_INSERT_TELEMETRY = """
INSERT INTO telemetry_events (
    id, org_id, event_type, source,
    run_id, connector_id, pack_id,
    duration_ms, success, count, error_code,
    payload, timestamp
) VALUES (
    %(id)s, %(org_id)s, %(event_type)s, %(source)s,
    %(run_id)s, %(connector_id)s, %(pack_id)s,
    %(duration_ms)s, %(success)s, %(count)s, %(error_code)s,
    %(payload)s, %(timestamp)s
)
"""

_SELECT_TELEMETRY = """
SELECT
    id, org_id, event_type, source,
    run_id, connector_id, pack_id,
    duration_ms, success, count, error_code,
    payload, timestamp
FROM telemetry_events
WHERE org_id      = %(org_id)s
  AND event_type  = %(event_type)s
  AND timestamp  >= %(from_dt)s
  AND timestamp   < %(to_dt)s
ORDER BY timestamp ASC
LIMIT %(limit)s
"""


# ---------------------------------------------------------------------------
# Minimal query builder — returned by _PgSession.query()
# ---------------------------------------------------------------------------

class _TelemetryQuery:
    """Minimal query builder for telemetry_events.

    Supports the call chain:
      session.query(model).filter(**kw).order_by(...).limit(n).all()

    filter() accepts keyword arguments:
      org_id, event_type, from_dt (ISO string), to_dt (ISO string)

    Unrecognised keywords are silently ignored so callers can pass extra
    context without breaking the interface.
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self._filters: dict[str, Any] = {}
        self._limit_val: int = 10_000

    def filter(self, **conditions: Any) -> "_TelemetryQuery":
        self._filters.update(conditions)
        return self

    def order_by(self, *_: Any) -> "_TelemetryQuery":
        # Always orders by timestamp ASC in _SELECT_TELEMETRY; arg is advisory.
        return self

    def limit(self, n: int) -> "_TelemetryQuery":
        self._limit_val = n
        return self

    def all(self) -> list[Any]:
        params = {**self._filters, "limit": self._limit_val}
        cur = self._conn.cursor()
        cur.execute(_SELECT_TELEMETRY, params)
        rows = cur.fetchall()
        result = []
        for row in rows:
            (id_, org_id, event_type, source,
             run_id, connector_id, pack_id,
             duration_ms, success_int, count, error_code,
             payload, timestamp_val) = row
            ts: Optional[datetime]
            if timestamp_val is None:
                ts = None
            elif isinstance(timestamp_val, datetime):
                ts = timestamp_val
            else:
                ts = datetime.fromisoformat(timestamp_val)
            success: Optional[bool] = (
                bool(success_int) if success_int is not None else None
            )
            result.append(SimpleNamespace(
                id=id_, org_id=org_id, event_type=event_type, source=source,
                run_id=run_id, connector_id=connector_id, pack_id=pack_id,
                duration_ms=duration_ms, success=success, count=count,
                error_code=error_code, payload=payload, timestamp=ts,
            ))
        return result


# ---------------------------------------------------------------------------
# Session adapter — yielded by get_db_session()
# ---------------------------------------------------------------------------

class _PgSession:
    """Thin session adapter over psycopg2 for the telemetry_events table.

    Matches the subset of the SQLAlchemy Session interface that the telemetry
    module and its contract tests use:  add(), commit(), query().
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self._pending: list[Any] = []

    def add(self, event: Any) -> None:
        """Stage a TelemetryEvent for insertion on the next commit()."""
        self._pending.append(event)

    def commit(self) -> None:
        """Flush all staged events to the database and clear the pending list."""
        for event in self._pending:
            ts = event.timestamp
            ts_str: str = ts.isoformat() if isinstance(ts, datetime) else str(ts)
            success_val: Optional[int] = None
            if event.success is True:
                success_val = 1
            elif event.success is False:
                success_val = 0
            cur = self._conn.cursor()
            cur.execute(_INSERT_TELEMETRY, {
                "id":           event.id,
                "org_id":       event.org_id,
                "event_type":   event.event_type,
                "source":       event.source,
                "run_id":       event.run_id,
                "connector_id": event.connector_id,
                "pack_id":      event.pack_id,
                "duration_ms":  event.duration_ms,
                "success":      success_val,
                "count":        event.count,
                "error_code":   event.error_code,
                "payload":      event.payload,
                "timestamp":    ts_str,
            })
        self._conn.commit()
        self._pending.clear()

    def query(self, _model_class: Any) -> _TelemetryQuery:
        """Return a query builder for telemetry_events.

        model_class is accepted for API compatibility but ignored; the query
        always targets telemetry_events.
        """
        return _TelemetryQuery(self._conn)


# ---------------------------------------------------------------------------
# Session context manager — the public entry point for telemetry operations
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def get_db_session() -> Iterator[_PgSession]:
    """Context manager yielding a _PgSession for telemetry_events.

    Usage::

        with get_db_session() as session:
            session.add(event)
            session.commit()

    The underlying psycopg2 connection is always closed on exit.
    """
    conn = _connect()
    try:
        yield _PgSession(conn)
    finally:
        conn.close()
