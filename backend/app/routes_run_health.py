"""R18-C2 T1 — Run-Health Dashboard aggregation endpoints.

Four READ-ONLY, org-scoped API endpoints that assemble the operational state the
Run-Health Dashboard renders — connectors, runs, content/freshness, and packs.
They read existing records/events only (see ``health_aggregation.py``); they
never write and never invent instrumentation.

Access posture (R18-C2 §Boundaries + R17-D3), identical to the retrieval/graph
routes: visible to **Owner and Analyst (read-only), never Viewer** — enforced by
``require_role("analyst")`` — and every query is org-scoped **strictly through
the tenancy context**. No endpoint accepts an org in the body or query, so one
tenant can never read another's run-health (AC5/AC6).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Query

from .health_aggregation import (
    connectors_view,
    content_freshness_view,
    packs_view,
    runs_view,
)
from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .security import require_auth

router = APIRouter(
    prefix="/api/run-health",
    tags=["run-health"],
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)


@router.get("/connectors")
def get_connector_health() -> dict:
    """Per-connector health for every connected system in the calling org (AC1):
    connection state, auth mode, last successful ingestion, checkpoint
    position/age, and last error."""
    org_id = get_current_org_id()
    return {"org_id": org_id, "connectors": connectors_view(org_id)}


@router.get("/runs")
def get_run_health(limit: int = Query(default=10, ge=1, le=100)) -> dict:
    """Recent discovery runs for the calling org (AC2): status, duration, systems,
    detectors evaluated/fired, opportunities, and per-stage outcomes — with
    non-blocking stage failures surfaced as ``degraded``, never hidden."""
    org_id = get_current_org_id()
    return {"org_id": org_id, "runs": runs_view(org_id, limit=limit)}


@router.get("/content")
def get_content_freshness_health() -> dict:
    """Retrieval substrate health for the calling org (AC3): indexed volume per
    source, embedding backlog, stale chunks, refresh/backfill progress, redaction
    count, and skipped-with-reason items — live against the R18-B2/A1/A2 records."""
    org_id = get_current_org_id()
    return content_freshness_view(org_id)


@router.get("/packs")
def get_pack_health() -> dict:
    """Packs executed on the calling org's latest run: pack ids, versions (the
    R16-B1 stamp), and detector counts — read from run/pack data, not recreated."""
    org_id = get_current_org_id()
    return packs_view(org_id)


def register_run_health_routes(app: FastAPI) -> None:
    """Attach the run-health routes to the app exactly once (idempotent)."""
    if getattr(app.state, "run_health_routes_registered", False):
        return
    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if "/api/run-health/connectors" in existing_paths:
        app.state.run_health_routes_registered = True
        return

    app.include_router(router)
    app.state.run_health_routes_registered = True
