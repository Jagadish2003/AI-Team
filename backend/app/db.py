import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool as _pg_pool
from dotenv import load_dotenv
from fastapi import HTTPException

from . import connector_roadmap
# 2.0-C1 T4 (AT-829): run history is never deleted. history_retention owns the
# protected-table set and is dependency-free, so importing it here is safe.
from .history_retention import assert_no_history_deletion

logger = logging.getLogger(__name__)

# The canonical discovery step-id list lives in the discovery layer
# (discovery/steps.py, DISCOVERY_STEPS / DISCOVERY_STEP_IDS). update_run_step()
# validates against it via _discovery_step_ids() — no local copy is kept here.
DATABASE_URL = os.getenv("DATABASE_URL")

RUN_ID_RE = re.compile(r"^RUN_(\d+)$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Connection pooling
# ---------------------------------------------------------------------------
# Every helper below borrows a connection, runs one or two statements, and
# releases it via `closing(connect())` / `conn.close()`. Opening a fresh
# psycopg2 connection per call paid a full TCP + auth + "SET timezone" handshake
# to the (remote) database on EVERY DB touch — a single discovery run makes
# hundreds of them (entity/edge upserts, run-event writes, KV reads), so the
# handshake, not the queries, dominated run time.
#
# A process-wide pool pays that handshake a handful of times and hands back warm
# connections. connect() returns a thin proxy whose .close() returns the
# connection to the pool (reset to a clean transaction state) instead of
# physically closing the socket, so every existing call site recycles
# transparently — no call-site changes required. This mirrors the pooling the
# contract-test harness already relies on (tests/contract/conftest.py).
#
# Sizing is env-tunable: DB_POOL_MIN (default 1), DB_POOL_MAX (default 16).
_POOL = None
_POOL_DSN = None
_POOL_LOCK = threading.Lock()


def _pool_bounds() -> tuple:
    def _int(name: str, default: int) -> int:
        try:
            return max(1, int(os.getenv(name, str(default))))
        except (TypeError, ValueError):
            return default

    minc = _int("DB_POOL_MIN", 1)
    maxc = _int("DB_POOL_MAX", 16)
    return minc, max(minc, maxc)


def _get_pool():
    """Return the process-wide connection pool, creating it lazily.

    Rebuilt if DATABASE_URL changes (tests point the app at a disposable DB) or
    if the pool was closed. A lock guards creation so concurrent first-callers
    build exactly one pool. The DSN is read fresh from the environment (not the
    module-level constant) so a late DATABASE_URL override is honoured.
    """
    global _POOL, _POOL_DSN
    dsn = os.getenv("DATABASE_URL")
    pool = _POOL
    if pool is not None and _POOL_DSN == dsn and not getattr(pool, "closed", False):
        return pool
    with _POOL_LOCK:
        if (
            _POOL is not None
            and _POOL_DSN == dsn
            and not getattr(_POOL, "closed", False)
        ):
            return _POOL
        if _POOL is not None:
            try:
                _POOL.closeall()
            except Exception:
                pass
        minc, maxc = _pool_bounds()
        # AT-288 F1-AC2 preserved: DictCursor rows support both row[0] and
        # row["col"]; options=-c timezone=UTC pins the session to UTC so ISO-8601
        # timestamps round-trip consistently. Both apply to every pooled conn.
        _POOL = _pg_pool.ThreadedConnectionPool(
            minc,
            maxc,
            dsn,
            cursor_factory=psycopg2.extras.DictCursor,
            options="-c timezone=UTC",
        )
        _POOL_DSN = dsn
        return _POOL


class _PooledConnection:
    """Proxy over a pooled psycopg2 connection.

    Delegates all attribute access (cursor/commit/rollback/…) to the real
    connection, but overrides close() to return the connection to the pool —
    rolled back to a clean state first — instead of physically closing it. This
    preserves the existing open / commit / close semantics at every call site
    while recycling the underlying socket. __del__ is a safety net so a call
    site that raises before close() cannot leak a connection out of the pool.
    """

    def __init__(self, conn, pool):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_released", False)

    def close(self):
        if object.__getattribute__(self, "_released"):
            return
        object.__setattr__(self, "_released", True)
        conn = object.__getattribute__(self, "_conn")
        pool = object.__getattribute__(self, "_pool")
        try:
            if not conn.closed:
                # Clear any uncommitted/aborted transaction so the next borrower
                # starts clean (read-only helpers never commit).
                conn.rollback()
        except Exception:
            # A broken connection can't be reused — drop it from the pool.
            try:
                pool.putconn(conn, close=True)
            except Exception:
                pass
            return
        try:
            pool.putconn(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def __enter__(self):
        object.__getattribute__(self, "_conn").__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return object.__getattribute__(self, "_conn").__exit__(exc_type, exc, tb)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_conn"), name, value)


def connect():
    """Borrow a pooled psycopg2 connection.

    The returned object behaves like a psycopg2 connection (DictCursor, UTC
    session); .close() returns it to the pool rather than closing the socket, so
    `closing(connect())` and `conn.close()` recycle instead of re-handshaking.
    On pool exhaustion it waits briefly for a slot before surfacing the error.
    """
    pool = _get_pool()
    delay = 0.02
    conn = None
    for _ in range(200):  # bounded wait (~a few seconds) for a free slot
        try:
            conn = pool.getconn()
            break
        except _pg_pool.PoolError:
            time.sleep(delay)
            delay = min(delay * 1.5, 0.25)
    if conn is None:
        conn = pool.getconn()  # last try — let a genuine PoolError surface
    try:
        conn.rollback()  # discard any residual transaction from a prior borrower
    except Exception:
        pass
    try:
        if conn.autocommit:
            conn.autocommit = False
    except Exception:
        pass
    return _PooledConnection(conn, pool)


def get_one(table: str, id_: str) -> Optional[Dict[str, Any]]:
    # closing() guarantees the connection is returned even if the query raises —
    # without it a failed query leaks the connection, and on the shared remote DB
    # those accumulate until max_connections is hit and every new connect() hangs.
    with closing(connect()) as con:
        cur = con.cursor()
        cur.execute(f"SELECT payload FROM {table} WHERE id = %s", (id_,))
        row = cur.fetchone()
    if not row:
        return None
    return _loads(row[0])


def get_all(table: str) -> List[Dict[str, Any]]:
    with closing(connect()) as con:
        cur = con.cursor()
        cur.execute(f"SELECT payload FROM {table} ORDER BY id")
        rows = cur.fetchall()
    return [_loads(r[0]) for r in rows]


def upsert(table: str, id_: str, payload: Dict[str, Any]) -> None:
    with closing(connect()) as con:
        cur = con.cursor()
        cur.execute(
            f"INSERT INTO {table} (id, payload) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload",
            (id_, json.dumps(payload)),
        )
        con.commit()


def _loads(value: Any) -> Any:
    """JSON payload columns are stored as TEXT; decode defensively.

    psycopg2 returns TEXT columns as str. Guard against a value that is already
    a dict/list (e.g. a JSONB column) so callers get a consistent Python object.
    """
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return cur.fetchone() is not None


def init_tables() -> None:
    """No-op. Schema is provisioned externally.

    The runs/run_events tables (and all others) are created by
    database/provision/provision.sh; the application no longer creates or
    migrates tables at runtime.
    """
    return None


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with closing(connect()) as con:
        cur = con.cursor()
        cur.execute("SELECT payload FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    return None if not row else _loads(row[0])


def _current_request_org() -> Optional[str]:
    """Return the current request's org_id, or None outside a request context.

    Lazy import keeps db.py free of a hard module-level dependency on the
    middleware layer (tenancy.py imports db lazily too, so a top-level import
    here would risk a cycle). Background jobs and tests that write runs outside
    a request simply get None.
    """
    try:
        from app.middleware.tenancy import get_current_org_id_optional

        return get_current_org_id_optional()
    except Exception:
        return None


def upsert_run(run_id: str, payload: Dict[str, Any]) -> None:
    # Multi-tenancy: stamp the owning org so run-scoped reads can enforce
    # isolation (see require_run_exists). Preserve an org_id already present on
    # the payload — status updates and materialization read-modify-write the
    # full record, so the original owner is kept. Only stamp from the current
    # request context on first write (creation). Writes with no existing org_id
    # AND no request context (background jobs) leave the run untagged, which
    # reads treat as legacy/global.
    if not payload.get("org_id"):
        org_id = _current_request_org()
        if org_id is not None:
            payload = {**payload, "org_id": org_id}
    with closing(connect()) as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO runs (id, payload) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload",
            (run_id, json.dumps(payload)),
        )
        con.commit()


def _discovery_step_ids() -> frozenset:
    """Return the canonical discovery step-id set from the discovery layer.

    Imported lazily (and tolerant of either package root) so db.py keeps no
    module-load dependency on the discovery package. Returns an empty set if
    the module cannot be located, which disables validation rather than
    breaking step recording.
    """
    try:
        from discovery.steps import DISCOVERY_STEP_IDS
    except ModuleNotFoundError:
        try:
            from backend.discovery.steps import DISCOVERY_STEP_IDS
        except ModuleNotFoundError:
            return frozenset()
    return DISCOVERY_STEP_IDS


def update_run_step(run_id: str, step_id: str, ok: bool = True) -> None:
    """Record the current discovery step for run_id.

    The run JSON payload is the source of truth: ``current_step`` is written
    there and is what ``GET /api/runs/{run_id}/status`` reads back (via
    ``get_run()``) — no schema-aware query is involved. The ``current_step``
    SQL column (migration 0011) is kept as a denormalized mirror for SQL-level
    querying/observability only; it is written here but is not the API read
    path. When the column is absent the payload write still succeeds.

    ``ok=False`` marks the stage as failed: the step is recorded in the
    payload's ``failed_steps`` list (so the UI can show it as failed rather
    than as a green-check completion) while ``current_step`` still advances so
    progress keeps moving. A subsequent ``ok=True`` for the same step clears it
    from ``failed_steps``.

    ``step_id`` is validated against the canonical discovery step set; an
    unknown id (e.g. a typo) is logged as a WARNING and skipped rather than
    silently written, so a misspelled step never clobbers a valid one. Never
    raises — a step-tracking failure must not abort a discovery run.
    """
    valid_ids = _discovery_step_ids()
    if valid_ids and step_id not in valid_ids:
        logger.warning(
            "update_run_step: unknown step_id=%r (not in DISCOVERY_STEPS=%s) — "
            "skipping write for run_id=%s",
            step_id,
            sorted(valid_ids),
            run_id,
        )
        return

    con = None
    try:
        con = connect()
        cur = con.cursor()
        cur.execute("SELECT payload FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        if not row:
            return
        payload = json.loads(row[0])
        payload["current_step"] = step_id

        # Track per-step failure so the progress UI does not render a failed
        # ingest as a completed (green-check) stage. current_step still advances
        # so the run keeps progressing; failed_steps records which stages failed.
        failed_steps = payload.get("failed_steps")
        if not isinstance(failed_steps, list):
            failed_steps = []
        if ok:
            failed_steps = [s for s in failed_steps if s != step_id]
        elif step_id not in failed_steps:
            failed_steps.append(step_id)
        payload["failed_steps"] = failed_steps

        payload_json = json.dumps(payload)
        try:
            cur.execute(
                "UPDATE runs SET payload = %s, current_step = %s WHERE id = %s",
                (payload_json, step_id, run_id),
            )
        except psycopg2.Error:
            # current_step column not present — clear the aborted transaction
            # (PostgreSQL aborts it on error) and update the payload only.
            con.rollback()
            cur.execute(
                "UPDATE runs SET payload = %s WHERE id = %s",
                (payload_json, run_id),
            )
        con.commit()
    except Exception as exc:
        logger.warning(
            "update_run_step skipped: run_id=%s step_id=%s error=%s",
            run_id,
            step_id,
            exc,
        )
    finally:
        if con is not None:
            con.close()


def count_runs() -> int:
    """Return the highest legacy RUN_### number."""
    init_tables()
    with closing(connect()) as con:
        cur = con.cursor()
        cur.execute("SELECT id FROM runs")
        rows = cur.fetchall()
    max_n = 0
    for row in rows:
        match = RUN_ID_RE.match(str(row[0]))
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
    # Multi-tenancy: a run tagged with an org_id is visible only to that org.
    # Cross-org access is denied as 404 — indistinguishable from not-found, the
    # same silent-deny used by tenancy_get_one. Untagged legacy runs (no org_id)
    # and reads outside a request context (background jobs) stay visible.
    org_id = _current_request_org()
    run_org = r.get("org_id")
    if org_id is not None and run_org is not None and run_org != org_id:
        raise HTTPException(status_code=404, detail="run not found")
    return r


def delete_run_events(run_id: str) -> None:
    """SOFT-delete a run's events. Despite the name, no row is ever removed.

    Marks the run's events deleted; insert_run_events re-activates any
    (run_id, seq) it rewrites, and get_run_events filters is_deleted, so rewriting a
    shrunk event list correctly drops the now-stale higher-seq rows from READS while
    the underlying rows remain.

    2.0-C1 T4 (AT-829 / AC4): ``run_events`` is a protected history table — the app
    login role has DELETE/TRUNCATE REVOKED on it (provision.sql / alembic 0033), so
    this MUST stay an UPDATE. A previous version of this comment claimed the app role
    had "UPDATE but not DELETE" generally; that was not true of this provisioning
    path, which grants ALL PRIVILEGES and then revokes only on the protected tables.
    The soft-delete shape is now the enforced contract rather than an assumption.
    """
    statement = "UPDATE run_events SET is_deleted = TRUE WHERE run_id = %s"
    # Self-check: if this ever became a hard DELETE, fail here with the named
    # history-retention reason rather than as an opaque privilege error in production.
    assert_no_history_deletion(statement, operation="delete_run_events")
    with closing(connect()) as con:
        cur = con.cursor()
        cur.execute(statement, (run_id,))
        con.commit()


def insert_run_events(run_id: str, events: List[Dict[str, Any]]) -> None:
    with closing(connect()) as con:
        cur = con.cursor()
        for i, ev in enumerate(events):
            cur.execute(
                "INSERT INTO run_events (run_id, seq, payload) VALUES (%s, %s, %s) "
                "ON CONFLICT (run_id, seq) DO UPDATE SET "
                "payload=EXCLUDED.payload, is_deleted=FALSE",
                (run_id, i, json.dumps(ev)),
            )
        con.commit()


def get_run_events(run_id: str) -> List[Dict[str, Any]]:
    with closing(connect()) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT payload FROM run_events WHERE run_id = %s AND is_deleted = FALSE "
            "ORDER BY seq ASC",
            (run_id,),
        )
        rows = cur.fetchall()
    return [_loads(r[0]) for r in rows]


# --- replay.py db API ---


def run_get(run_id: str) -> Dict[str, Any]:
    """Get run by ID, raises HTTP 404 if not found."""
    return require_run_exists(run_id)


def run_set(run_id: str, run: Dict[str, Any]) -> None:
    """Persist run metadata."""
    upsert_run(run_id, run)


def _init_kv_table() -> None:
    """No-op. The kv table is provisioned by database/provision/provision.sh."""
    return None


def kv_get(key: str) -> Any:
    if key.startswith("events:"):
        run_id = key[len("events:") :]
        return get_run_events(run_id)
    _init_kv_table()
    with closing(connect()) as con:
        cur = con.cursor()
        cur.execute("SELECT payload FROM kv WHERE key = %s", (key,))
        row = cur.fetchone()
    return _loads(row[0]) if row else None


def kv_set(key: str, value: Any) -> None:
    if key.startswith("events:"):
        run_id = key[len("events:") :]
        delete_run_events(run_id)
        if isinstance(value, list):
            insert_run_events(run_id, value)
        return
    _init_kv_table()
    with closing(connect()) as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO kv (key, payload) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload",
            (key, json.dumps(value)),
        )
        con.commit()


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


# ===========================================================================
# Legacy table audit — AT-156 / T1-S11 Task 2 Section 4b (Item 5)
# ===========================================================================
# Every table reachable through this application is enumerated below with its
# tenancy status: (a) org-scoped customer data isolated by org_id (with a
# tenancy_get_* wrapper where served through db.py's generic accessors), (b)
# run-scoped customer data isolated via run ownership, or (c) intentionally
# cross-org reference/global data. No table is left undocumented (AC18).
#
# --- Org-scoped customer-data tables served via db.py ----------------------
#   connectors  — {id, payload}; org_id carried in payload.
#                 Wrapper: tenancy_get_connectors(org_id).
#   runs        — {id, payload}; org_id carried in payload.
#                 Wrapper: tenancy_get_runs(org_id).
#                 (The request-scoped generic guard tenancy_get_all/_one also
#                  filters these by the current org context.)
#
# --- Run-scoped customer data (isolation via run ownership) ----------------
#   kv          — {key, payload}. Stores run artifacts keyed "{name}:{run_id}"
#                 (opps, evidence, roadmap, executive_report, …; see
#                 run_kv_get/run_kv_set) AND org-keyed entries such as the
#                 Stack Builder state written as "setup_state:{org_id}". No
#                 org_id column — isolation is transitive through the run_id
#                 (org-scoped via runs) or the explicit org_id key segment.
#   run_events  — (run_id, seq, payload). Per-run event stream, keyed by run_id.
#   opportunities / evidence / executive_reports — legacy {id, payload} tables.
#                 Authoritative runtime data is the run-scoped kv store above;
#                 these tables back decision/override reads (get_one/upsert by
#                 id) and inherit org isolation through run ownership. Not
#                 cross-org reference data.
#
# --- Intentionally cross-org reference / demo seed data --------------------
# CROSS-ORG TABLE: entities, mappings, permissions, uploads, audit_events
# Global reference / demo seed catalogues (normalization entities, process
# mappings, permission catalogue, upload templates, legacy demo audit rows).
# No per-org customer data. Tenancy guard does not apply. Read-only reference.
#
# CROSS-ORG REGISTRY: industries
# NOTE: "industries" is NOT a database table — it is an in-code registry
# (discovery/packs/industry_registry.py, INDUSTRY_REGISTRY). Global reference
# data only; read-only from all orgs. Documented here because Section 4b lists
# it; "setup_state" likewise is not its own table (see kv above).
#
# --- Org-native tables owned by other modules (enforced there) -------------
#   workspace_members — native org_id; enforced in app/rbac.py ("WHERE org_id").
#   audit_log         — native org_id; enforced in app/middleware/audit.py and
#                       the /api/audit-log reader ("WHERE org_id = %s").
#   signal_snapshots  — native org_id; enforced in app/temporal.py — every
#                       query (history, baseline, run-signals) filters
#                       "WHERE org_id = %s" (verified AT-156).
#   telemetry_events  — native org_id column (database/models/telemetry.py).
#   credentials       — native org_id; UNIQUE(org_id, connector_id); enforced
#                       in app/auth/vault.py.
#   nonces / oauth_nonces — short-lived OAuth/CSRF nonces. No customer data.
# ===========================================================================


def _tenancy_filter(table: str, org_id: str) -> List[Dict[str, Any]]:
    """Return rows of *table* belonging to *org_id* only. Never cross-org.

    org_id is passed explicitly (not read from request context) so the helper
    is safe to call from background jobs as well as request handlers
    (Section 4d). Strict equality — rows tagged with a different org_id are
    excluded.
    """
    return [r for r in get_all(table) if r.get("org_id") == org_id]


def tenancy_get_connectors(org_id: str) -> List[Dict[str, Any]]:
    """Returns connectors for org_id only. Never cross-org."""
    return _tenancy_filter("connectors", org_id)


def tenancy_get_runs(org_id: str) -> List[Dict[str, Any]]:
    """Returns runs for org_id only. Never cross-org.

    Runs carry their owning org under either 'orgId' (camelCase — the key
    POST /api/runs/start writes) or 'org_id' (snake_case, used by some other
    paths), so unlike the generic _tenancy_filter this matches on whichever key
    is present. Strict equality: a run tagged with a different org is excluded,
    and an untagged legacy run (neither key) is visible to no org — there is no
    None-is-visible-to-everyone path that could leak another org's run.
    """
    return [
        r
        for r in get_all("runs")
        if (r.get("org_id") or r.get("orgId")) == org_id
    ]


# ---------------------------------------------------------------------------
# Org-scoped connector state (workspace isolation)
# ---------------------------------------------------------------------------
# The `connectors` table holds a SHARED catalog (seed rows with no org_id,
# status "disconnected") PLUS per-org connection state. Per-org state is stored
# under a namespaced primary key f"{org_id}::{connector_id}" with org_id tagged
# in the payload, so connecting / configuring / declaring products in one org
# never mutates the shared catalog or another org's state.
#
# Reads merge the catalog with the current org's overrides (override wins). This
# replaces the old model where connect mutated the single global connector row,
# which leaked connection state across every org (a fresh org saw another org's
# connectors as already connected).
# ---------------------------------------------------------------------------

_ORG_CONNECTOR_SEP = "::"


def _connector_catalog() -> Dict[str, Dict[str, Any]]:
    """Shared catalog rows (no org_id), keyed by connector id."""
    return {
        r["id"]: r
        for r in get_all("connectors")
        if "id" in r and not r.get("org_id")
    }


def _org_connector_overrides(org_id: str) -> Dict[str, Dict[str, Any]]:
    """Per-org connector rows for org_id, keyed by connector id."""
    return {
        r["id"]: r
        for r in get_all("connectors")
        if "id" in r and r.get("org_id") == org_id
    }


def org_connectors_list(org_id: str) -> List[Dict[str, Any]]:
    """All connectors visible to org_id: catalog defaults overlaid with this
    org's own connection state. Catalog ordering is preserved.

    Each row is stamped with the R191-R1 T5 roadmap flags (`roadmap` /
    `roadmapTarget`) via `connector_roadmap.annotate_connector`, so a tile whose
    ingestion does not ship yet (SAP/D365 and other unshipped connectors) is
    surfaced as a non-connectable "Coming — <target>" tile. This is additive —
    it never mutates status/tier — so per-org connection state is preserved."""
    merged = _connector_catalog()
    for connector_id, override in _org_connector_overrides(org_id).items():
        merged[connector_id] = {**merged.get(connector_id, {}), **override}
    return [connector_roadmap.annotate_connector(row) for row in merged.values()]


def org_connector_get(org_id: str, connector_id: str) -> Optional[Dict[str, Any]]:
    """This org's connector record if it has one, else the catalog template.
    Returns None only when the connector id is unknown entirely. The returned
    row carries the roadmap flags (see `org_connectors_list`)."""
    catalog = get_one("connectors", connector_id)
    row = get_one("connectors", f"{org_id}{_ORG_CONNECTOR_SEP}{connector_id}")
    if row is not None:
        return connector_roadmap.annotate_connector({**(catalog or {}), **row})
    if catalog is None:
        return None
    return connector_roadmap.annotate_connector(catalog)


def org_connector_set(org_id: str, connector_id: str, payload: Dict[str, Any]) -> None:
    """Persist this org's connector state to its namespaced row, tagging org_id.
    Never touches the shared catalog row or another org's row."""
    record = {**payload, "id": connector_id, "org_id": org_id}
    upsert("connectors", f"{org_id}{_ORG_CONNECTOR_SEP}{connector_id}", record)
