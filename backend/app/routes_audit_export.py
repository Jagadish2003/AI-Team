"""
routes_audit_export.py — 2.0-D4 T2: the Owner-only signed audit export API.

    POST /api/audit/export        generate a signed export for a period
    POST /api/audit/export/verify verify a signed export document

Access control is **Owner-only**, consistent with the existing run-scoped
``GET /api/runs/{run_id}/audit``. The organisation comes from the tenancy
middleware and is pushed into the SQL predicate — never taken from the request
body, because a caller-supplied org on an audit export is a cross-tenant
disclosure waiting to happen.

Why POST rather than GET
------------------------
Two reasons, and the second is the one that matters.

Generating an export is a DISCLOSURE, so it emits an audit event. POST puts it in
the class of routes 2.0-D4 T1's conformance sweep enumerates (POST/PUT/PATCH/
DELETE), which means the recursive "the export must itself be audited" requirement
is not merely implemented here — it is *enforced* by that sweep, and a future change
that dropped the audit emission would fail CI rather than quietly removing the
record of who read the trail.

Secondarily, a POST keeps the period out of URLs, browser history and proxy access
logs, which is a small privacy improvement on a compliance endpoint.

The verify endpoint takes no key from the caller
-----------------------------------------------
``/verify`` is a convenience for the customer, not the auditor's trust anchor. It
verifies against the DEPLOYMENT's configured public key. An auditor who wants an
independent check does not use this endpoint at all — they verify the file offline
with the published public key, which is the entire reason the scheme is asymmetric
(see :mod:`app.export_signing`).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .audit_export import AuditExportError, build_signed_export, normalise_period
from .export_signing import ExportSigningError, verify_export
from .middleware.audit import (
    AUDIT_EXPORT_GENERATED,
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    log_event as audit_log_event,
)
from .middleware.tenancy import get_current_org_id
from .rbac import _get_user_id_from_token, require_role
from .security import require_auth

logger = logging.getLogger(__name__)

AUDIT_EXPORT_PATH = "/api/audit/export"
AUDIT_EXPORT_VERIFY_PATH = "/api/audit/export/verify"

router = APIRouter(tags=["audit-export"])


class AuditExportRequest(BaseModel):
    """A period. Deliberately no ``org_id`` field — see the module docstring."""

    period_from: str = Field(
        ..., alias="from",
        description="Inclusive period start (ISO-8601 date or timestamp, UTC).",
    )
    period_to: str = Field(
        ..., alias="to",
        description=(
            "Inclusive period end. A plain date covers the whole day, so "
            "to=2026-07-20 includes every event on the 20th."
        ),
    )

    model_config = {"populate_by_name": True}


class AuditExportVerifyRequest(BaseModel):
    document: Dict[str, Any] = Field(
        ..., description="A previously generated signed export document."
    )


class AuditExportVerifyResponse(BaseModel):
    verified: bool
    reason: str


def _actor(token: str) -> Optional[str]:
    """The authenticated user, for the export's ``generated_by`` and audit actor.

    Uses ``rbac._get_user_id_from_token``, the same resolution every other audited
    route uses (see routes_runbook_matches), so the actor recorded on an export
    matches the actor recorded everywhere else. Best-effort: attribution failing
    must not block a compliance export, and an unattributed actor is visible as
    null rather than guessed.
    """
    try:
        return _get_user_id_from_token(token)
    except Exception:  # noqa: BLE001 — attribution is best-effort, never fatal
        return None


@router.post(
    AUDIT_EXPORT_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def generate_audit_export(
    body: AuditExportRequest,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Owner-only: a signed audit export for this org and period.

    **Exactly one audit row is written per request, carrying the real outcome.**

    The first version wrote a ``success`` row up front, reasoning that a
    disclosure must never go unrecorded if assembly failed after the read. That
    was the wrong trade. When ``build_signed_export`` then raised — an inverted
    period, an unconfigured signing key — a second row was appended with
    ``failure``, and the trail permanently held a success disclosure for a period
    that was never exported. An auditor reading it could not tell which row
    reflected reality, which is precisely the ambiguity this feature exists to
    remove. A missing row is a gap you can see; a false success is a lie you
    cannot detect.

    So the row is written once, at the point the outcome is known: after the
    payload is built and before it is returned. The residual risk — the response
    failing to reach the caller after the row is written — is both far smaller and
    far less damaging, because it errs toward recording a disclosure that did not
    complete rather than inventing one that never happened.

    The period recorded is the NORMALISED one, identical to the boundaries the
    signed file carries, so the trail and the artifact it describes can be
    compared without apparent contradiction.

    The consequence stated in :mod:`app.audit_export` still holds: an export never
    contains its own generation record, only those of previous exports.
    """
    org_id = get_current_org_id()
    actor = _actor(token)

    def _audit(outcome: str, *, target: str, **detail: Any) -> None:
        audit_log_event(
            AUDIT_EXPORT_GENERATED,
            org_id=org_id,
            user_id=actor,
            target=target,
            outcome=outcome,
            **detail,
        )

    # Normalise up front so a refused period is reported with the same boundaries
    # a successful one would have used, and so the failure row below is not
    # forced to fall back to the raw request strings.
    try:
        period_from, period_to = normalise_period(body.period_from, body.period_to)
    except AuditExportError as exc:
        _audit(
            OUTCOME_FAILURE,
            target="audit_log",
            reason=str(exc),
            requested_from=str(body.period_from),
            requested_to=str(body.period_to),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from None

    try:
        export = build_signed_export(
            org_id,
            period_from,
            period_to,
            generated_by=actor,
        )
    except AuditExportError as exc:
        # A bad period is the caller's error; record the refused attempt so an
        # auditor sees that an export was tried, not just that one succeeded.
        _audit(
            OUTCOME_FAILURE,
            target=f"audit_log:{period_from}..{period_to}",
            reason=str(exc),
            period_from=period_from,
            period_to=period_to,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ExportSigningError as exc:
        # No signing key: fail loudly rather than return an unsigned artifact that
        # looks like evidence.
        _audit(
            OUTCOME_FAILURE,
            target=f"audit_log:{period_from}..{period_to}",
            reason="signing_key_unavailable",
            period_from=period_from,
            period_to=period_to,
        )
        logger.error("audit export refused: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from None

    # The disclosure has happened: the rows are read, signed, and about to leave.
    # sign_export returns the payload FLAT with a `signature` block added, so the
    # counts are top-level rather than nested under a "payload" key.
    signed = export if isinstance(export, dict) else {}
    _audit(
        OUTCOME_SUCCESS,
        target=f"audit_log:{period_from}..{period_to}",
        period_from=period_from,
        period_to=period_to,
        record_count=signed.get("record_count"),
        # Recorded because a capped export discloses less than the period the row
        # names; without it the trail overstates what was handed over.
        complete=signed.get("complete"),
    )
    return export


@router.post(
    AUDIT_EXPORT_VERIFY_PATH,
    response_model=AuditExportVerifyResponse,
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def verify_audit_export(body: AuditExportVerifyRequest) -> AuditExportVerifyResponse:
    """Owner-only: verify a signed export against this deployment's public key.

    Never raises for a bad document — a malformed or altered file is a legitimate
    answer of ``verified: false`` with a reason, not a 500.
    """
    ok, reason = verify_export(body.document)
    return AuditExportVerifyResponse(verified=ok, reason=reason)


def register_audit_export_routes(app: FastAPI) -> None:
    """Register the audit-export routes once for the provided FastAPI app."""
    if getattr(app.state, "audit_export_routes_registered", False):
        return
    existing = {getattr(route, "path", None) for route in app.routes}
    if AUDIT_EXPORT_PATH in existing:
        app.state.audit_export_routes_registered = True
        return
    app.include_router(router)
    app.state.audit_export_routes_registered = True
