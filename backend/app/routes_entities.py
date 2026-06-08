"""Entity retrieval routes — T3-S12-A T6.

GET /api/runs/{run_id}/entities
  - Requires Analyst+ (Viewer → 403).
  - Scoped to authenticated org_id.
  - Cross-org or missing run → 404 (never 403, to avoid leaking run existence).
  - Run exists but no entities extracted → [] with HTTP 200.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from . import db
from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .security import require_auth

ENTITIES_ROUTE_PATH = "/api/runs/{run_id}/entities"

router = APIRouter(tags=["entities"])


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

    if getattr(app.state, "entities_routes_registered", False):
        return

    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if ENTITIES_ROUTE_PATH in existing_paths:
        app.state.entities_routes_registered = True
        return

    app.include_router(router)
    app.state.entities_routes_registered = True
