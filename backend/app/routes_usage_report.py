"""R-1.9.1-L2 / T3 (AT-695) — Owner-only signed usage-report route.

  GET /api/usage/report?from=YYYY-MM-DD&to=YYYY-MM-DD
    → the signed usage-report envelope {report, signature, algorithm} for the
      caller's org over the (inclusive) period.

Owner-only: the usage report is commercial/billing data and is signed with the
installation's per-installation report_key, so it is gated exactly like the admin
license routes. The report is built and signed entirely locally — the customer
downloads it and sends it to CloudFulcrum; this endpoint never initiates outbound
contact (the federal no-phone-home posture). Validation/aggregation/signing all
live in ``app.usage_report``; this module is only the HTTP edge.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .security import require_auth
from .usage_report import UsageReportError, generate_signed_report

logger = logging.getLogger(__name__)

USAGE_REPORT_PATH = "/api/usage/report"

router = APIRouter(tags=["usage"])


@router.get(
    USAGE_REPORT_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def get_usage_report(
    from_: str = Query(..., alias="from", description="Period start, inclusive (YYYY-MM-DD)."),
    to: str = Query(..., description="Period end, inclusive (YYYY-MM-DD)."),
) -> dict:
    """Generate the signed usage report for the caller's org over [from, to].

    Returns the envelope ``{report, signature, algorithm}``. A malformed period or
    a license without a ``report_key`` returns 400 with the specific reason — the
    report is never produced unsigned. Side-effect-free (a read of billing
    telemetry + the installed license); no outbound network call.
    """
    org_id = get_current_org_id()
    try:
        return generate_signed_report(org_id, from_, to)
    except UsageReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def register_usage_report_routes(app: FastAPI) -> None:
    """Register the usage-report route once for the provided FastAPI app."""
    if getattr(app.state, "usage_report_routes_registered", False):
        return
    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if USAGE_REPORT_PATH in existing_paths:
        app.state.usage_report_routes_registered = True
        return
    app.include_router(router)
    app.state.usage_report_routes_registered = True
