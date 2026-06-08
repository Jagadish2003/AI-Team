"""Entity retrieval routes — T3-S12-A T6.

GET /api/runs/{run_id}/entities
  - Requires Analyst+ (Viewer → 403).
  - Scoped to authenticated org_id.
  - Cross-org or missing run → 404 (never 403, to avoid leaking run existence).
  - Run exists but no entities extracted → [] with HTTP 200.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from . import db
from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .security import require_auth
from database.models.entities import ALL_ENTITIES_DDL

ENTITIES_ROUTE_PATH = "/api/runs/{run_id}/entities"
REQUIRED_ENTITY_COLUMNS = frozenset({
    "id",
    "org_id",
    "entity_type",
    "canonical_name",
    "display_name",
    "source_system",
    "source_record_id",
    "resolution_confidence",
    "resolution_status",
    "first_seen_run_id",
    "last_seen_run_id",
    "run_count",
    "metadata",
    "created_at",
    "updated_at",
})

router = APIRouter(tags=["entities"])
logger = logging.getLogger(__name__)


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def ensure_entities_table() -> None:
    """Ensure dev databases have the Sprint 12 entity schema.

    Older seed databases had a generic payload-backed ``entities`` table used
    by the retired Evidence Collection mock. The Sprint 12 graph code needs
    real columns. Preserve the old rows by renaming that legacy table, then
    create the locked graph schema.
    """
    con = db.connect()
    try:
        columns = {
            row[1]
            for row in con.execute("PRAGMA table_info(entities)").fetchall()
        }

        if columns and not REQUIRED_ENTITY_COLUMNS.issubset(columns):
            legacy_name = "entities_legacy_payload"
            if _table_exists(con, legacy_name):
                legacy_name = f"entities_legacy_payload_{int(time.time())}"
            con.execute(f"ALTER TABLE entities RENAME TO {legacy_name}")
            logger.warning(
                "Renamed legacy entities table to %s before creating graph schema",
                legacy_name,
            )

        for ddl in ALL_ENTITIES_DDL:
            con.execute(ddl)
        con.commit()
    except Exception:
        con.rollback()
        logger.exception("ensure_entities_table failed")
        raise
    finally:
        con.close()


@router.get(
    ENTITIES_ROUTE_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def list_entities(run_id: str) -> List[Dict[str, Any]]:
    """Return all entities visible as of a run, scoped to the authenticated org.

    "Visible as of run N" means every entity first observed in run N or any
    earlier run — entities accumulate in the org and persist once seen. An
    entity first seen in run 1 and not re-extracted in run 2 is still a real
    org entity and MUST remain visible when querying run 2. Filtering on
    ``last_seen_run_id = run_id`` (the prior behaviour) silently dropped those
    older entities on every re-run; this query instead bounds visibility by the
    run in which each entity was *first* seen.

    Run IDs are random (``run_<hex>``) and carry no orderable information, so we
    derive chronological order from the ``runs`` table's implicit rowid
    (insertion order = creation order). An entity is included when the run it
    was first seen in was created no later than the queried run.

    Returns [] when the run exists but no entities are visible yet.
    Returns 404 when run_id is missing or belongs to a different org.

    NOTE: schema DDL is created once at startup via register_entities_routes();
    this hot path performs no DDL (no per-request CREATE TABLE / lock risk).
    """
    org_id = get_current_org_id()

    # Verify the run exists AND belongs to this org before serving any data.
    # This existence check is itself org-scoped: a run owned by another org is
    # reported as 404 (never 403), so an analyst in org-B cannot distinguish
    # "run does not exist" from "run exists in org-A" — closing the run
    # enumeration channel (tenancy isolation rule).
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    run_org = run.get("org_id") or run.get("orgId")
    if run_org and run_org != org_id:
        # Cross-org: return 404, not 403 — do not reveal the run exists.
        raise HTTPException(status_code=404, detail="run not found")

    con = db.connect()
    try:
        con.row_factory = sqlite3.Row
        # All values are bound parameters (?) — never string-interpolated — so
        # source_record_id and other external-system values cannot inject SQL.
        # LEFT JOIN keeps entities whose first_seen run row is absent (defensive:
        # never drop a real entity just because its origin run record is gone).
        rows = con.execute(
            """
            SELECT e.id, e.org_id, e.entity_type, e.canonical_name, e.display_name,
                   e.source_system, e.source_record_id, e.resolution_confidence,
                   e.resolution_status, e.first_seen_run_id, e.last_seen_run_id,
                   e.run_count, e.metadata, e.created_at, e.updated_at
            FROM entities e
            LEFT JOIN runs r_first ON r_first.id = e.first_seen_run_id
            WHERE e.org_id = ?
              AND (
                    r_first.rowid IS NULL
                 OR r_first.rowid <= (SELECT rowid FROM runs WHERE id = ?)
              )
            ORDER BY e.entity_type, e.canonical_name
            """,
            (org_id, run_id),
        ).fetchall()
        result = []
        for r in rows:
            row_dict = dict(r)
            if row_dict.get("metadata") and isinstance(row_dict["metadata"], str):
                try:
                    row_dict["metadata"] = json.loads(row_dict["metadata"])
                except (json.JSONDecodeError, ValueError):
                    # Corrupt metadata must not leak a raw string to a consumer
                    # that expects a dict. Return {} and flag the offending row.
                    logger.warning(
                        "entities: dropping unparseable metadata for entity %s (run %s)",
                        row_dict.get("id"),
                        run_id,
                    )
                    row_dict["metadata"] = {}
            result.append(row_dict)
        return result
    finally:
        con.close()


def register_entities_routes(app: FastAPI) -> None:
    """Register entity routes once for the provided FastAPI app."""
    ensure_entities_table()

    if getattr(app.state, "entities_routes_registered", False):
        return

    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if ENTITIES_ROUTE_PATH in existing_paths:
        app.state.entities_routes_registered = True
        return

    app.include_router(router)
    app.state.entities_routes_registered = True
