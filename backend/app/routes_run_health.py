"""R18-C2 T1 — Run-Health Dashboard aggregation endpoints.

Five READ-ONLY, org-scoped API endpoints that assemble the operational state the
Run-Health Dashboard renders — connectors, runs, content/freshness, packs, and
the attention strip.
They read existing records/events only (see ``health_aggregation.py``); they
never write and never invent instrumentation.

Access posture (R18-C2 §Boundaries + R17-D3), identical to the retrieval/graph
routes: visible to **Owner and Analyst (read-only), never Viewer** — enforced by
``require_role("analyst")`` — and every query is org-scoped **strictly through
the tenancy context**. No endpoint accepts an org in the body or query, so one
tenant can never read another's run-health (AC5/AC6).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, Query

from .health_aggregation import (
    ATTENTION_SEVERITY_RANK,
    attention_view,
    connectors_view,
    content_freshness_view,
    packs_view,
    runs_view,
)
from .middleware.tenancy import get_current_org_id
from .run_volume_report import build_run_volume_report
from .scale_envelope import envelope_summary
from .rbac import require_role
from .security import require_auth

logger = logging.getLogger(__name__)

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


@router.get("/volume")
def get_volume_envelope(
    run_id: str = Query(default=None, description="Run to report on; latest when omitted."),
) -> dict:
    """2.0-D4 T4 — the stated scale envelope and how a run fared against it.

    Two things in one place, deliberately. The ENVELOPE says what volumes the
    deployment supports and — just as importantly — how well-founded each of
    those numbers is, so a reader can tell a measured figure from a first guess
    without opening any code. The OBSERVATION says what this run actually did
    against it.

    This is the surface that makes MSP-B7's "loud, never silent" discipline
    visible. Budgets and deferrals have been recorded on the run record since
    B7; until now nothing rendered them, which satisfies the letter of loud and
    none of its intent.
    """
    org_id = get_current_org_id()
    report = None
    resolved = None
    try:
        from .db import tenancy_get_runs
        from .run_store import read_run

        if run_id:
            resolved = read_run(run_id)
        else:
            runs = tenancy_get_runs(org_id, limit=1) or []
            if runs:
                first = runs[0]
                resolved = first if isinstance(first, dict) else None
    except Exception as exc:  # noqa: BLE001 - the envelope is useful without a run
        logger.warning("Could not resolve a run for the volume report: %s", exc)

    if resolved is not None:
        report = build_run_volume_report(resolved).to_dict()

    return {
        "org_id": org_id,
        "envelope": envelope_summary(),
        "run": report,
    }


@router.get("/attention")
def get_attention_items() -> dict:
    """Actionable tenant-health conditions (AC4), derived from existing records.

    Items are ordered by severity (critical to low), then condition timestamp
    (newest first), then stable identifier. Each carries a dashboard panel and
    href suitable for direct frontend navigation.
    """
    org_id = get_current_org_id()
    return {
        "org_id": org_id,
        "severity_order": list(
            sorted(
                ATTENTION_SEVERITY_RANK,
                key=lambda severity: ATTENTION_SEVERITY_RANK[severity],
                reverse=True,
            )
        ),
        "items": attention_view(org_id),
    }


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
