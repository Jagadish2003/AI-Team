"""R-1.9.1-L2 / T5 (AT-697) — Owner-only usage-summary route (AC6).

  GET /api/usage/summary?from=YYYY-MM-DD&to=YYYY-MM-DD
    → the Owner-facing usage summary for the caller's org over the (inclusive)
      period: run counts per AI mode + the systems-over-time picture.

The pre-invoice VISIBILITY counterpart to the signed usage report
(``routes_usage_report.py``): it shows the customer what the report will say before
they send it. Owner-only, exactly like the report route — usage is commercial data.
Unlike the report it needs NO ``report_key`` (it is an unsigned preview), so an
Owner can view usage even before a report key is provisioned. Built entirely
locally from billing telemetry — no outbound contact (the no-phone-home posture).
Aggregation lives in ``app.usage_summary`` (a projection of ``app.usage_report``);
this module is only the HTTP edge.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .security import require_auth
from .usage_report import UsageReportError
from .usage_summary import build_usage_summary

logger = logging.getLogger(__name__)

USAGE_SUMMARY_PATH = "/api/usage/summary"

router = APIRouter(tags=["usage"])


@router.get(
    USAGE_SUMMARY_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def get_usage_summary(
    from_: str = Query(..., alias="from", description="Period start, inclusive (YYYY-MM-DD)."),
    to: str = Query(..., description="Period end, inclusive (YYYY-MM-DD)."),
) -> dict:
    """Return the Owner-facing usage summary for the caller's org over [from, to].

    The numbers match the signed report's numbers exactly for the same period (AC6)
    — the summary is a projection of the same aggregation. A malformed period
    returns 400 with the specific reason. Side-effect-free (a read of billing
    telemetry); no outbound network call, and no report_key required.
    """
    org_id = get_current_org_id()
    try:
        return build_usage_summary(org_id, from_, to)
    except UsageReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def register_usage_summary_routes(app: FastAPI) -> None:
    """Register the usage-summary route once for the provided FastAPI app."""
    if getattr(app.state, "usage_summary_routes_registered", False):
        return
    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if USAGE_SUMMARY_PATH in existing_paths:
        app.state.usage_summary_routes_registered = True
        return
    app.include_router(router)
    app.state.usage_summary_routes_registered = True
