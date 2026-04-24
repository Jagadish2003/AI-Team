"""
Sprint 4 T3 — Clusters route.

Changes from v1.0:
  - Fix 4: response_model=List[LinkedCluster] added so FastAPI validates the
    response shape and exposes the schema in OpenAPI docs.
"""
from typing import Any, Dict, List

from fastapi import Depends, HTTPException

from .security import require_auth
from . import db
from .materialize_t3_hook import get_clusters
from .models_clusters import LinkedCluster


def register_sprint4_t3_routes(app) -> None:
    """Register Sprint 4 T3 endpoints on the existing FastAPI app."""

    async def get_run_clusters(run_id: str) -> List[Dict[str, Any]]:
        run = db.run_get(run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found",
            )
        return get_clusters(run_id)

    app.add_api_route(
        "/api/runs/{run_id}/clusters",
        get_run_clusters,
        methods=["GET"],
        response_model=List[LinkedCluster],   # Fix 4: added
        dependencies=[Depends(require_auth)],
        tags=["runs"],
    )
