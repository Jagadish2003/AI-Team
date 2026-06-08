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
    """Return all entities for a run, scoped to the authenticated org.

    Returns [] when the run exists but extraction has not run yet.
    Returns 404 when run_id is missing or belongs to a different org.
    """
    org_id = get_current_org_id()

    # Verify the run exists AND belongs to this org in one query to prevent
    # leaking run existence via 403 vs 404 divergence (tenancy isolation rule).
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
        rows = con.execute(
            """
            SELECT id, org_id, entity_type, canonical_name, display_name,
                   source_system, source_record_id, resolution_confidence,
                   resolution_status, first_seen_run_id, last_seen_run_id,
                   run_count, metadata, created_at, updated_at
            FROM entities
            WHERE org_id = ? AND last_seen_run_id = ?
            ORDER BY entity_type, canonical_name
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
                    pass
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
