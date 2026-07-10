"""R18-B2 T6 — retrieval freshness metrics API.

``GET /api/retrieval/freshness`` serves the org's freshness-lag picture (AC7):
pending change events, failed refreshes, stale chunk count, embedding backlog,
and model-backfill progress — the queryable surface the Sprint-2 run-health
dashboard renders. Staleness is allowed to exist; it is never allowed to be
invisible (Section 1: "Lag is visible").

Same access posture as the graph routes: Analyst+ and org-scoped strictly
through the tenancy context — the request can never name an org (no body/query
org parameter exists), so one tenant can never read another's lag numbers.

The route deliberately does NOT catch storage errors: metrics that degrade to
zeros would report perfect freshness at the exact moment the system cannot be
trusted. A failing read surfaces as an HTTP error the operator can see.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, Field

from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .retrieval.metrics import freshness_metrics
from .security import require_auth

router = APIRouter(
    prefix="/api/retrieval",
    tags=["retrieval"],
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)


class BackfillProgressResponse(BaseModel):
    """Model-repin convergence (R18-B2 T5, measured for the dashboard)."""

    active_model: str
    active_model_version: str
    embedded_total: int
    on_active_model: int
    awaiting_backfill: int
    progress: float = Field(ge=0.0, le=1.0)
    complete: bool


class FreshnessMetricsResponse(BaseModel):
    """The org's freshness-lag picture (AC7) — see ``retrieval/metrics.py``."""

    org_id: str
    generated_at: str
    pending_change_events: int
    failed_refreshes: int
    stale_chunks: int
    chunks_total: int
    chunks_embedded: int
    pending_embeddings: int
    backfill: BackfillProgressResponse


@router.get("/freshness", response_model=FreshnessMetricsResponse)
def get_freshness_metrics() -> FreshnessMetricsResponse:
    """Return the calling org's retrieval freshness metrics (AC7).

    The org comes from the tenancy context only. Numbers are computed live from
    the same org-partitioned primitives the freshness workers run on, so what
    the dashboard shows is what the workers actually see.
    """
    org_id = get_current_org_id()
    return FreshnessMetricsResponse(**freshness_metrics(org_id))


def register_retrieval_routes(app: FastAPI) -> None:
    """Attach the retrieval routes to the app exactly once (idempotent)."""
    if getattr(app.state, "retrieval_routes_registered", False):
        return
    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if "/api/retrieval/freshness" in existing_paths:
        app.state.retrieval_routes_registered = True
        return

    app.include_router(router)
    app.state.retrieval_routes_registered = True
