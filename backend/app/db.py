import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

DB_PATH = Path(os.getenv("DB_PATH", "database/dev.db"))
RUN_ID_RE = re.compile(r"^RUN_(\d+)$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout: how long a connection should wait for the lock to go away before raising an error.
    # check_same_thread: allow the same connection to be used in multiple threads (FastAPI threads).
    con = sqlite3.connect(str(DB_PATH), timeout=30.0, check_same_thread=False)
    # Avoid changing journal mode on every request; that requires an exclusive
    # lock and can fail under concurrent browser prefetches.
    con.execute("PRAGMA busy_timeout=30000")
    return con


def get_one(table: str, id_: str) -> Optional[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute(f"SELECT payload FROM {table} WHERE id = ?", (id_,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return json.loads(row[0])


def get_all(table: str) -> List[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute(f"SELECT payload FROM {table} ORDER BY id")
    rows = cur.fetchall()
    con.close()
    return [json.loads(r[0]) for r in rows]


def upsert(table: str, id_: str, payload: Dict[str, Any]) -> None:
    con = connect()
    cur = con.cursor()
    cur.execute(
        f"INSERT INTO {table} (id, payload) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
        (id_, json.dumps(payload)),
    )
    con.commit()
    con.close()


def init_tables() -> None:
    con = connect()
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    # Migrate run_events if it exists with the old single-key schema
    cur.execute("PRAGMA table_info(run_events)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if existing_cols and "run_id" not in existing_cols:
        cur.execute("DROP TABLE run_events")
    cur.execute(
        "CREATE TABLE IF NOT EXISTS run_events (run_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id, seq))"
    )
    con.commit()
    con.close()


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute("SELECT payload FROM runs WHERE id = ?", (run_id,))
    row = cur.fetchone()
    con.close()
    return None if not row else json.loads(row[0])


def upsert_run(run_id: str, payload: Dict[str, Any]) -> None:
    con = connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO runs (id, payload) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
        (run_id, json.dumps(payload)),
    )
    con.commit()
    con.close()


def count_runs() -> int:
    """Return the highest legacy RUN_### number."""
    init_tables()
    con = connect()
    cur = con.cursor()
    cur.execute("SELECT id FROM runs")
    rows = cur.fetchall()
    con.close()
    max_n = 0
    for (run_id,) in rows:
        match = RUN_ID_RE.match(str(run_id))
        if match:
            max_n = max(max_n, int(match.group(1)))
    return max_n


def next_run_id() -> str:
    """Generate the runtime run ID used by the discovery runner."""
    init_tables()
    for _ in range(10):
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        if get_run(run_id) is None:
            return run_id
    return f"run_{uuid.uuid4().hex}"


def require_run_exists(run_id: str) -> Dict[str, Any]:
    r = get_run(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="run not found")
    return r


def delete_run_events(run_id: str) -> None:
    con = connect()
    cur = con.cursor()
    cur.execute("DELETE FROM run_events WHERE run_id = ?", (run_id,))
    con.commit()
    con.close()


def insert_run_events(run_id: str, events: List[Dict[str, Any]]) -> None:
    con = connect()
    cur = con.cursor()
    for i, ev in enumerate(events):
        cur.execute(
            "INSERT OR REPLACE INTO run_events (run_id, seq, payload) VALUES (?, ?, ?)",
            (run_id, i, json.dumps(ev)),
        )
    con.commit()
    con.close()


def get_run_events(run_id: str) -> List[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute(
        "SELECT payload FROM run_events WHERE run_id = ? ORDER BY seq ASC", (run_id,)
    )
    rows = cur.fetchall()
    con.close()
    return [json.loads(r[0]) for r in rows]


# --- replay.py db API ---


def run_get(run_id: str) -> Dict[str, Any]:
    """Get run by ID, raises HTTP 404 if not found."""
    return require_run_exists(run_id)


def run_set(run_id: str, run: Dict[str, Any]) -> None:
    """Persist run metadata."""
    upsert_run(run_id, run)


def _init_kv_table() -> None:
    con = connect()
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    con.commit()
    con.close()


def kv_get(key: str) -> Any:
    if key.startswith("events:"):
        run_id = key[len("events:") :]
        return get_run_events(run_id)
    _init_kv_table()
    con = connect()
    cur = con.cursor()
    cur.execute("SELECT payload FROM kv WHERE key = ?", (key,))
    row = cur.fetchone()
    con.close()
    return json.loads(row[0]) if row else None


def kv_set(key: str, value: Any) -> None:
    if key.startswith("events:"):
        run_id = key[len("events:") :]
        delete_run_events(run_id)
        if isinstance(value, list):
            insert_run_events(run_id, value)
        return
    _init_kv_table()
    con = connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO kv (key, payload) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
        (key, json.dumps(value)),
    )
    con.commit()
    con.close()


def run_kv_get(key: str, run_id: str, default: Any = None) -> Any:
    """Helper to get run-scoped key-value data."""
    value = kv_get(f"{key}:{run_id}")
    return value if value is not None else default


def run_kv_set(key: str, run_id: str, value: Any) -> None:
    """Helper to set run-scoped key-value data."""
    kv_set(f"{key}:{run_id}", value)


# ---------------------------------------------------------------------------
# Tenancy-guarded query helpers — AT-82 / T1-S10-B T2
# ---------------------------------------------------------------------------
# These are the single enforcement point for multi-tenant data access.
# Every call to tenancy_get_all / tenancy_get_one first validates that a
# request-scoped org_id has been set in context.  Tables covered:
#   connectors, runs, kv_store, setup_state
#
# IMPORTANT: The existing {id, payload} tables do not have an org_id SQL
# column in the current schema.  Isolation is therefore enforced at the
# Python layer (payload-level filtering) with context presence as the hard
# gate.  Adding a proper org_id SQL column per table is deferred to the
# schema migration that will follow once the schema is stable.
# ---------------------------------------------------------------------------

# Tables that require tenancy enforcement
_TENANCY_PROTECTED: frozenset[str] = frozenset(
    {"connectors", "runs", "kv_store", "setup_state"}
)


def _assert_tenancy(table: str) -> str:
    """Raise TenancyViolationError if no org context is set.

    Returns the current org_id so callers can use it for payload filtering.
    Imported lazily to avoid a circular import at module load time.
    """
    from app.middleware.tenancy import get_current_org_id  # lazy import
    return get_current_org_id()


def tenancy_get_all(table: str) -> List[Dict[str, Any]]:
    """Return all payload rows for *table*, enforcing tenancy context.

    Raises TenancyViolationError if called outside a request with org_id set.
    When the payload contains an 'org_id' field the results are additionally
    filtered to the current org.  Rows without an org_id field pass through
    (legacy data seeded before multi-tenancy was added).
    """
    org_id = _assert_tenancy(table)
    rows = get_all(table)
    # Filter to current org where the payload declares one
    return [
        r for r in rows
        if r.get("org_id") is None or r.get("org_id") == org_id
    ]


def tenancy_get_one(table: str, id_: str) -> Optional[Dict[str, Any]]:
    """Return a single payload row, enforcing tenancy context.

    Returns None if the row does not exist OR belongs to a different org.
    Raises TenancyViolationError if called outside a request with org_id set.
    """
    org_id = _assert_tenancy(table)
    row = get_one(table, id_)
    if row is None:
        return None
    row_org = row.get("org_id")
    if row_org is not None and row_org != org_id:
        return None  # silently deny — same as not found (AC1)
    return row
