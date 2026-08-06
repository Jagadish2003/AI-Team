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

2.0-B1 T6 (AC6) — "every export generation is an audit event naming user, scope,
and time". This is an export generation: a signed artifact leaves the deployment.
It previously emitted no audit record, so it now records through the shared
``app.export_audit`` write point, with the same payload discipline as the evidence
bundles (period + counts + signature prefix; never report content, never the whole
MAC).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from .export_audit import (
    EXPORT_KIND_USAGE_REPORT,
    record_export_generated,
    resolve_export_actor,
)
from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .security import require_auth
from .usage_report import UsageReportError, generate_signed_report

logger = logging.getLogger(__name__)

USAGE_REPORT_PATH = "/api/usage/report"

router = APIRouter(tags=["usage"])


def _usage_report_fingerprint(
    period_from: str, period_to: str, envelope: dict
) -> dict:
    """Non-sensitive identifiers for the audit record — never report content.

    Mirrors ``evidence_export.bundle_fingerprint``: what period was exported, how
    much it covered, and a short signature PREFIX that identifies the artifact
    without reproducing its MAC.
    """
    body = envelope.get("report") if isinstance(envelope, dict) else None
    body = body if isinstance(body, dict) else {}
    runs = body.get("runs") if isinstance(body.get("runs"), dict) else {}
    signature = envelope.get("signature") if isinstance(envelope, dict) else None
    return {
        "period_from": period_from,
        "period_to": period_to,
        "run_count": runs.get("total"),
        "event_count": body.get("event_count"),
        "signature_prefix": (str(signature)[:16] if signature else None),
        "generated_at": body.get("generated_at"),
    }


def _record_usage_export(
    org_id: str, period_from: str, period_to: str, envelope: dict, actor: str
) -> None:
    """T6 / AC6 — audit one issued usage report. Best-effort: the report has
    already been signed and returned, so a recording failure is logged, never
    raised at the caller."""
    try:
        record_export_generated(
            EXPORT_KIND_USAGE_REPORT,
            org_id=org_id,
            actor=actor,
            details=_usage_report_fingerprint(period_from, period_to, envelope),
        )
    except Exception as exc:  # noqa: BLE001 — recording never denies the artifact.
        logger.warning("usage report export audit recording failed: %s", exc)


@router.get(
    USAGE_REPORT_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def get_usage_report(
    from_: str = Query(..., alias="from", description="Period start, inclusive (YYYY-MM-DD)."),
    to: str = Query(..., description="Period end, inclusive (YYYY-MM-DD)."),
    token: str = Depends(require_auth),
) -> dict:
    """Generate the signed usage report for the caller's org over [from, to].

    Returns the envelope ``{report, signature, algorithm}``. A malformed period or
    a license without a ``report_key`` returns 400 with the specific reason — the
    report is never produced unsigned. Reads only (billing telemetry + the
    installed license) and makes no outbound network call; the one side effect is
    the export audit record (T6 / AC6), written after a report is actually
    produced — a refused or unsignable request exports nothing and records nothing.
    """
    org_id = get_current_org_id()
    try:
        envelope = generate_signed_report(org_id, from_, to)
    except UsageReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _record_usage_export(org_id, from_, to, envelope, resolve_export_actor(token))
    return envelope


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
