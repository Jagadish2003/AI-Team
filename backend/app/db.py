import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import HTTPException

# Load backend/.env so DATABASE_URL is honoured even when db.py is imported by a
# standalone script (seed loader, a one-off shell) that did not call
# load_dotenv() itself. override=False: a real exported env var still wins, and
# the value the test conftest sets in os.environ is preserved.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# AT-288 / Fix 1: the application database is PostgreSQL. The connection string
# is read from DATABASE_URL (the local default matches deployment/.env.template).
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agentiq:agentiq@localhost:5432/agentiq"
)
RUN_ID_RE = re.compile(r"^RUN_(\d+)$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect():
    # AT-288 F1-AC2: connect() uses psycopg2.connect(DATABASE_URL).
    # cursor_factory=DictCursor makes con.cursor() yield rows that support both
    # positional (row[0]) and column-name (row["col"]) access — the stock
    # psycopg2 equivalent of sqlite3.Row, no custom translation layer.
    # options=-c timezone=UTC pins the session to UTC so timestamps round-trip
    # consistently regardless of the server's local timezone. The app stores and
    # compares ISO-8601 UTC values everywhere.
    con = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.DictCursor,
        options="-c timezone=UTC",
    )
    con.autocommit = False
    return con


def get_one(table: str, id_: str) -> Optional[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute(f"SELECT payload FROM {table} WHERE id = %s", (id_,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return _loads(row[0])


def get_all(table: str) -> List[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute(f"SELECT payload FROM {table} ORDER BY id")
    rows = cur.fetchall()
    con.close()
    return [_loads(r[0]) for r in rows]


def upsert(table: str, id_: str, payload: Dict[str, Any]) -> None:
    con = connect()
    cur = con.cursor()
    cur.execute(
        f"INSERT INTO {table} (id, payload) VALUES (%s, %s) "
        "ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload",
        (id_, json.dumps(payload)),
    )
    con.commit()
    con.close()


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
    con = connect()
    cur = con.cursor()
    # `seq` is a monotonically increasing insertion-order column. It replaces the
    # SQLite implicit `rowid` that entity visibility ordering relied on (see
    # database/models/entities.py ENTITIES_VISIBLE_AS_OF_RUN_FROM_WHERE).
    cur.execute(
        "CREATE TABLE IF NOT EXISTS runs ("
        "id TEXT PRIMARY KEY, payload TEXT NOT NULL, seq BIGSERIAL)"
    )
    cur.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS seq BIGSERIAL")
    # Migrate run_events if it exists with the old single-key schema.
    if _table_exists(cur, "run_events") and not _column_exists(cur, "run_events", "run_id"):
        cur.execute("DROP TABLE run_events")
    cur.execute(
        "CREATE TABLE IF NOT EXISTS run_events ("
        "run_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, "
        "PRIMARY KEY (run_id, seq))"
    )
    con.commit()
    con.close()


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute("SELECT payload FROM runs WHERE id = %s", (run_id,))
    row = cur.fetchone()
    con.close()
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
    con = connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO runs (id, payload) VALUES (%s, %s) "
        "ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload",
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
    con = connect()
    cur = con.cursor()
    cur.execute("DELETE FROM run_events WHERE run_id = %s", (run_id,))
    con.commit()
    con.close()


def insert_run_events(run_id: str, events: List[Dict[str, Any]]) -> None:
    con = connect()
    cur = con.cursor()
    for i, ev in enumerate(events):
        cur.execute(
            "INSERT INTO run_events (run_id, seq, payload) VALUES (%s, %s, %s) "
            "ON CONFLICT (run_id, seq) DO UPDATE SET payload=EXCLUDED.payload",
            (run_id, i, json.dumps(ev)),
        )
    con.commit()
    con.close()


def get_run_events(run_id: str) -> List[Dict[str, Any]]:
    con = connect()
    cur = con.cursor()
    cur.execute(
        "SELECT payload FROM run_events WHERE run_id = %s ORDER BY seq ASC", (run_id,)
    )
    rows = cur.fetchall()
    con.close()
    return [_loads(r[0]) for r in rows]


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
    cur.execute("SELECT payload FROM kv WHERE key = %s", (key,))
    row = cur.fetchone()
    con.close()
    return _loads(row[0]) if row else None


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
        "INSERT INTO kv (key, payload) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload",
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
    """Returns runs for org_id only. Never cross-org."""
    return _tenancy_filter("runs", org_id)


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
    org's own connection state. Catalog ordering is preserved."""
    merged = _connector_catalog()
    merged.update(_org_connector_overrides(org_id))
    return list(merged.values())


def org_connector_get(org_id: str, connector_id: str) -> Optional[Dict[str, Any]]:
    """This org's connector record if it has one, else the catalog template.
    Returns None only when the connector id is unknown entirely."""
    row = get_one("connectors", f"{org_id}{_ORG_CONNECTOR_SEP}{connector_id}")
    if row is not None:
        return row
    return get_one("connectors", connector_id)


def org_connector_set(org_id: str, connector_id: str, payload: Dict[str, Any]) -> None:
    """Persist this org's connector state to its namespaced row, tagging org_id.
    Never touches the shared catalog row or another org's row."""
    record = {**payload, "id": connector_id, "org_id": org_id}
    upsert("connectors", f"{org_id}{_ORG_CONNECTOR_SEP}{connector_id}", record)
