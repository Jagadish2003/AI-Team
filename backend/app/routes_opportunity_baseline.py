"""2.0-A2 T2 — the read-only baseline API.

Read-only by design: there is **no** POST, PATCH or DELETE here. The baseline is
frozen by the discovery pipeline at finding creation, and AC1 requires it be
immutable thereafter — so exposing any write verb would be handing a client the
one capability the artifact exists to deny.

Standard spine: org from the tenancy middleware only (never a request body), and
``require_role("analyst")`` on every route, since a measurement basis is
operational customer information. A cross-org identity answers 404 identically to
a missing one, so the API never reveals that a baseline exists in another tenant.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from .middleware.tenancy import get_current_org_id
from .opportunity_baseline import (
    ensure_opportunity_baseline_table,
    get_baseline,
    get_baselines_for_run,
    list_baselines,
)
from .opportunity_baseline_artifact import missing_artifact_fields
from .rbac import require_role
from .security import require_auth

router = APIRouter(prefix="/api/opportunity-baseline", tags=["opportunity-baseline"])


@router.get("", dependencies=[Depends(require_role("analyst"))])
def list_opportunity_baselines(
    limit: int = Query(default=200, ge=1, le=1000),
    _token: str = Depends(require_auth),
) -> dict:
    org_id = get_current_org_id()
    items = list_baselines(org_id, limit=limit)
    return {"orgId": org_id, "count": len(items), "items": items}


@router.get("/run/{run_id}", dependencies=[Depends(require_role("analyst"))])
def get_run_opportunity_baselines(
    run_id: str,
    _token: str = Depends(require_auth),
) -> dict:
    """Every baseline CREATED by one run — not every baseline it re-surfaced.

    The distinction matters: a run that re-surfaces an existing finding does not
    create a new basis for it, so it will not appear here.
    """
    org_id = get_current_org_id()
    baselines = get_baselines_for_run(org_id, run_id)
    return {
        "orgId": org_id,
        "runId": run_id,
        "count": len(baselines),
        "baselines": baselines,
    }


@router.get("/{opportunity_identity}", dependencies=[Depends(require_role("analyst"))])
def get_opportunity_baseline(
    opportunity_identity: str,
    _token: str = Depends(require_auth),
) -> dict:
    org_id = get_current_org_id()
    artifact = get_baseline(org_id, opportunity_identity)
    if artifact is None:
        # Findings created before this subtask shipped have no baseline and are
        # therefore never measurable — the honest outcome, stated plainly rather
        # than hidden behind a reconstructed basis.
        raise HTTPException(
            status_code=404,
            detail=(
                f"no frozen baseline for opportunity {opportunity_identity!r}. A "
                "finding created before baseline capture shipped has no "
                "measurement basis and is not measurable."
            ),
        )
    # Surfaced rather than assumed: if a stored artifact is ever incomplete, a
    # reader should see which parts T3/T4 will be missing.
    missing = missing_artifact_fields(artifact)
    return {"artifact": artifact, "complete": not missing, "missingFields": missing}


def register_opportunity_baseline_routes(app: FastAPI) -> None:
    if getattr(app.state, "opportunity_baseline_routes_registered", False):
        return
    # Startup-only schema safety net for a dev DB that has not run migration 0032.
    ensure_opportunity_baseline_table()
    path = "/api/opportunity-baseline/{opportunity_identity}"
    if path not in {getattr(route, "path", None) for route in app.routes}:
        app.include_router(router)
    app.state.opportunity_baseline_routes_registered = True


__all__ = ["register_opportunity_baseline_routes", "router"]
