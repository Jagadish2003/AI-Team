"""
routes_evidence_export.py — Release 2.0-B1 T4: signed evidence export routes.

  GET /api/runs/{run_id}/opportunities/{opp_id}/evidence-export
    → the signed bundle for ONE finding: its trace, the evidence records it
      references, its evidence-pointer spine, and the run + pack versions.

  GET /api/runs/{run_id}/evidence-export
    → the signed bundle for the WHOLE run: every finding as above, plus the
      run's executive report, roadmap, and decision audit.

Both return the envelope ``{bundle, signature, algorithm}`` as a JSON body,
mirroring the only other signed artifact in the product
(``routes_usage_report.py``). Pass ``?download=1`` to receive the identical
envelope as a canonical-bytes attachment — the bytes a third party verifies.

Access control. Gated at ``analyst``: the underlying trace is already
viewer-readable (T1), so the export adds a signature rather than new data and
gating it at ``owner`` would be inconsistent — but it is a distributable,
audited attestation, so it sits above plain ``viewer``. Cross-org requests
hard-404 (the ``routes_secops_evidence`` pattern) rather than returning a signed
empty bundle, because a signed "nothing here" is a misleading attestation.

Every generated bundle is recorded twice — an organisation-wide audit event
(``middleware.audit.log_event``) and a telemetry event — carrying the bundle
FINGERPRINT only, never its content. This app has no request-logging middleware,
so a GET is not auto-audited; the calls here are explicit and deliberate
(supports sibling AC6).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Response

from . import db
from .evidence_export import (
    SCOPE_FINDING,
    SCOPE_REPORT,
    EvidenceExportError,
    bundle_fingerprint,
    envelope_bytes,
    generate_signed_export,
)
from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .security import require_auth

logger = logging.getLogger(__name__)

FINDING_EXPORT_PATH = "/api/runs/{run_id}/opportunities/{opp_id}/evidence-export"
REPORT_EXPORT_PATH = "/api/runs/{run_id}/evidence-export"

router = APIRouter(tags=["evidence-export"])


def _require_run_in_org(run_id: str, org_id: str) -> Dict[str, Any]:
    """Load the run, 404-ing for both unknown and cross-org — a signed export
    must never attest to another tenant's data, and the two cases are
    deliberately indistinguishable to the caller."""
    run = db.run_get(run_id)
    inputs = run.get("inputs") if isinstance(run.get("inputs"), dict) else {}
    run_org = (
        run.get("org_id")
        or run.get("orgId")
        or inputs.get("org_id")
        or inputs.get("orgId")
    )
    if run_org and str(run_org) != org_id:
        raise HTTPException(status_code=404, detail="run not found")
    return run


def _record_export(org_id: str, envelope: Dict[str, Any]) -> None:
    """Audit + telemetry for one issued bundle (fingerprint only, never content).

    Both writes are best-effort: the bundle has already been produced and
    verified, so a failure to record must not deny the caller their artifact —
    but it is logged loudly rather than swallowed silently.
    """
    fingerprint = bundle_fingerprint(envelope)
    try:
        from .middleware.audit import EVIDENCE_EXPORT_GENERATED, log_event

        log_event(EVIDENCE_EXPORT_GENERATED, org_id=org_id, **fingerprint)
    except Exception as exc:  # noqa: BLE001 — log_event is itself non-raising.
        logger.warning("evidence export audit write failed: %s", exc)
    try:
        from .telemetry import record_event

        record_event("export.evidence_generated", {"org_id": org_id, **fingerprint})
    except Exception as exc:  # noqa: BLE001 — telemetry is fire-and-forget.
        logger.debug("evidence export telemetry failed: %s", exc)


def _filename(envelope: Dict[str, Any]) -> str:
    body = envelope.get("bundle") or {}
    scope = str(body.get("scope") or "export")
    run_id = str(body.get("run_id") or "run")
    opp_id = body.get("opportunity_id")
    stem = f"agentiq-evidence-{scope}-{run_id}" + (f"-{opp_id}" if opp_id else "")
    # Conservative: only characters safe in a Content-Disposition filename.
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in stem)
    return f"{safe}.json"


def _serve(envelope: Dict[str, Any], download: bool) -> Any:
    """Return the envelope as a JSON body, or as the canonical-bytes attachment
    a verifier checks when ``download`` is set."""
    if not download:
        return envelope
    return Response(
        content=envelope_bytes(envelope),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{_filename(envelope)}"'
        },
    )


def _generate(
    org_id: str, run_id: str, *, scope: str, opp_id: Optional[str] = None
) -> Dict[str, Any]:
    """Build + sign, mapping the module's single error type onto HTTP.

    A missing run/opportunity is a 404; a license without a ``report_key`` or a
    content-discipline violation is a 400 naming the reason. An unsigned or
    unsafe bundle is never returned.
    """
    try:
        return generate_signed_export(org_id, run_id, scope=scope, opp_id=opp_id)
    except EvidenceExportError as exc:
        message = str(exc)
        if "not found" in message:
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=400, detail=message)


@router.get(
    FINDING_EXPORT_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def get_finding_evidence_export(
    run_id: str,
    opp_id: str,
    download: bool = Query(
        False, description="Return the canonical bundle bytes as a file attachment."
    ),
) -> Any:
    """2.0-B1 (T4 / AC4) — the signed evidence bundle for one finding.

    Returns ``{bundle, signature, algorithm}``. The signature is an HMAC-SHA256
    over the canonical bytes of ``bundle``, keyed by the installation's license
    ``report_key``; altering any byte of the bundle fails verification.
    """
    org_id = get_current_org_id()
    _require_run_in_org(run_id, org_id)
    envelope = _generate(org_id, run_id, scope=SCOPE_FINDING, opp_id=opp_id)
    _record_export(org_id, envelope)
    return _serve(envelope, download)


@router.get(
    REPORT_EXPORT_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def get_report_evidence_export(
    run_id: str,
    download: bool = Query(
        False, description="Return the canonical bundle bytes as a file attachment."
    ),
) -> Any:
    """2.0-B1 (T4 / AC4) — the signed evidence bundle for a whole run's report.

    Covers every finding in the run plus the executive report, roadmap, and
    decision audit. Same signature contract as the per-finding export.
    """
    org_id = get_current_org_id()
    _require_run_in_org(run_id, org_id)
    envelope = _generate(org_id, run_id, scope=SCOPE_REPORT)
    _record_export(org_id, envelope)
    return _serve(envelope, download)


def register_evidence_export_routes(app: FastAPI) -> None:
    """Register the evidence-export routes once for the provided FastAPI app."""
    if getattr(app.state, "evidence_export_routes_registered", False):
        return
    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if FINDING_EXPORT_PATH in existing_paths or REPORT_EXPORT_PATH in existing_paths:
        app.state.evidence_export_routes_registered = True
        return
    app.include_router(router)
    app.state.evidence_export_routes_registered = True
