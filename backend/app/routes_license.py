"""LIC-1 / T6 (AT-347) — admin license routes (Owner-only).

Two Owner-only endpoints expose the license to the admin UI (LicensePage, T8):

  GET  /api/license             → current status + details (status, customer,
                                  term, expires_at, days_remaining).
  POST /api/license/update-key  → validate a pasted key and store it ONLY if it
                                  is not invalid, then return the refreshed
                                  status. An invalid key is rejected and nothing
                                  is stored (validate-before-store, AC7).

Both endpoints are scoped to the caller's organisation: status/banner read the
current org's license via ``get_current_license_status`` (which resolves the org
from the tenancy context) and update-key writes the pasted key to that org's row
in ``org_licenses``. Licensing is per-tenant — an org with no installed key
evaluates to ``no_license`` until its Owner pastes a valid key here. Validation is
the pure, offline ``licensing.validate_license`` (T3).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import license_limits
from .license_runtime import (
    get_current_license_status,
    persist_validated_status,
    set_org_license_key,
)
from .licensing import (
    REASON_ORG_MISMATCH,
    LicenseStatus,
    validate_license,
)
from .middleware.audit import (
    LICENSE_INSTALLED,
    OUTCOME_SUCCESS,
    log_event as audit_log_event,
)
from .middleware.tenancy import get_current_org_id
from .org_display_name import resolve_org_display_name
from .rbac import require_role
from .security import require_auth
from .telemetry import record_event

logger = logging.getLogger(__name__)

LICENSE_STATUS_PATH = "/api/license"
LICENSE_UPDATE_PATH = "/api/license/update-key"
LICENSE_BANNER_PATH = "/api/license/banner"
LICENSE_LIMITS_PATH = "/api/license/limits"
LICENSE_ORG_NAME_PATH = "/api/license/org-name"

# Plain-language paste-time rejection for a key bound to a different org
# (R-1.9.1-L1 / T2, AC1). The same wording backs the UI reason for the
# org_mismatch banner/status; keep it human, not the machine reason code.
ORG_MISMATCH_MESSAGE = "This license was issued to a different organisation"

router = APIRouter(tags=["license"])


class LicenseStatusResponse(BaseModel):
    """Owner-facing license status. Detail fields are null when there is no
    valid key (no_license / invalid / clock_rollback), where only ``status``
    is meaningful.

    ``deployment_type`` (R-1.9.1-L1 / T1, payload v2) is the deployment topology
    (``saas`` | ``customer_hosted``) parsed from the validated payload and
    exposed here so the License UI can show it (AC5). It is ``null`` for a pre-v2
    key that carries no such field, and for any non-verifiable state.

    ``reason`` (R-1.9.1-L1 / T2, AC1) carries the machine-readable invalid reason
    when ``status`` is ``invalid`` — notably ``org_mismatch`` for a key bound to a
    different organisation — so the License UI can render the specific
    plain-language explanation ("this license was issued to a different
    organisation") rather than a bare "invalid". It is ``null`` for a healthy
    (valid/grace) status."""

    status: str
    customer: Optional[str] = None
    term: Optional[int] = None  # term_months from the validated payload
    deployment_type: Optional[str] = None
    expires_at: Optional[str] = None
    days_remaining: Optional[int] = None
    reason: Optional[str] = None


class LicenseBannerResponse(BaseModel):
    """Minimal license signal for the global expiry banner (T9).

    Readable by ANY authenticated user (not just Owner), because the banner must
    appear on every page for every role — an analyst whose discovery run is
    blocked needs to see why (AC4/AC5). Deliberately carries only what the banner
    renders (``status``, ``expires_at``, ``reason``); the full admin detail stays
    Owner-only on ``GET /api/license``.

    ``reason`` lets the banner copy distinguish a never-licensed install
    (``no_license`` / ``signature_or_format`` → "No valid license installed",
    §5/AC6) from an actually-expired term (no reason → "License expired") and
    from a clock anomaly (``clock_rollback``). It is ``null`` for valid/grace and
    for a genuinely expired (past-grace) key.

    ``grace_days_remaining`` is the number of days left before a grace-state
    license crosses into read-only (discovery runs blocked). It lets the grace
    banner say "runs will be blocked in N days" rather than a bare "expired",
    which would either cause panic or — because runs still work in grace — false
    complacency. Only populated in the ``grace`` state; ``null`` otherwise."""

    status: str
    expires_at: Optional[str] = None
    reason: Optional[str] = None
    grace_days_remaining: Optional[int] = None


class LicenseLimitsResponse(BaseModel):
    """R17-D4 Addendum A / T10 (AT-505) — Integration-Hub license-limit state.

    Systems used vs systems licensed, so the Integration Hub can display current
    usage against the entitlement (AC14). Both counts come from the same
    ``license_limits`` helpers the connect-time gate (T9) enforces with, so the
    number the customer sees is exactly the number that is enforced — the "one
    connected entity = one system" pricing definition (Addendum A §1).

    ``systemsLicensed`` is ``null`` for an unlimited license (``max_systems`` null
    or absent — including pre-addendum keys, and the no-license / invalid states,
    per AC13), in which case ``unlimited`` is ``true`` and ``canConnectMore`` is
    always ``true``. ``canConnectMore`` is the aggregate "is there headroom" signal
    for the hub; a per-connector reconnect of an already-connected system is always
    allowed regardless (forward-only) and is decided by the connect-time gate."""

    systemsUsed: int
    systemsLicensed: Optional[int] = None  # null => unlimited license
    unlimited: bool
    canConnectMore: bool
    # MSP-B13 / T4 (AT-746) — approaching-cap notice + at-cap hard stop the
    # Integration Hub / cloud-connector cards render (AC2/AC5). Additive to the T10
    # shape: pre-T4 clients simply ignore them.
    approachingCap: bool = False
    atCap: bool = False
    notice: Optional[str] = None  # approaching or at-cap wording; null when neither


class LicenseOrgNameResponse(BaseModel):
    """R17-D4 Addendum A / T12 (§2 "Dynamic Organisation Name") — the single
    resolved organisation display name every UI surface consumes.

    ``orgName`` is read from the org's live-validated license payload
    (``org_name``, falling back to ``customer`` for pre-addendum keys) by the one
    resolver in ``org_display_name`` — the "one name, resolved once" of §5, so the
    header, workspace labels, reports, and License page all show the same name
    without per-surface naming logic. Before a key is installed — or for any
    non-verifiable state — it is a neutral default, never a stale or placeholder
    customer name (AC16). A live, side-effect-free read, so pasting a key with a
    different ``org_name`` updates it immediately with no restart (AC15)."""

    orgName: str


class UpdateKeyRequest(BaseModel):
    key: str = Field(..., min_length=1, description="The signed license key string to install.")


def _to_status_response(result: dict) -> LicenseStatusResponse:
    """Map a validate_license/evaluate_license result dict to the response model."""
    payload = result.get("payload") or {}
    return LicenseStatusResponse(
        status=result.get("status"),
        customer=result.get("customer"),
        term=payload.get("term_months"),
        deployment_type=result.get("deployment_type"),
        expires_at=result.get("expires_at"),
        days_remaining=result.get("days_remaining"),
        reason=result.get("reason"),
    )


@router.get(
    LICENSE_STATUS_PATH,
    response_model=LicenseStatusResponse,
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def get_license_status() -> LicenseStatusResponse:
    """Owner-only: current license status derived from the stored key.

    Side-effect-free (does not persist last_seen or emit telemetry); mirrors
    exactly what the T4 validator would compute for the installed key.
    """
    return _to_status_response(get_current_license_status())


@router.get(
    LICENSE_BANNER_PATH,
    response_model=LicenseBannerResponse,
    dependencies=[Depends(require_auth)],
)
def get_license_banner() -> LicenseBannerResponse:
    """Any authenticated user: minimal license signal for the global banner.

    Auth-only (no role gate) so the expiry banner renders on every page for
    every role, including the analysts who can start runs (AC4/AC5). Returns
    only ``status`` + ``expires_at``; the full admin detail stays Owner-only on
    ``GET /api/license``. Side-effect-free, like the status route.
    """
    result = get_current_license_status()
    # In grace, surface days-until-read-only so the banner can say "runs blocked
    # in N days". days_remaining is (expires - today) → negative once expired, so
    # grace_days + days_remaining = days left in the grace window.
    grace_days_remaining = None
    if result.get("status") == LicenseStatus.GRACE:
        days_remaining = result.get("days_remaining")
        if days_remaining is not None:
            payload = result.get("payload") or {}
            grace_days_remaining = int(payload.get("grace_days", 14)) + int(days_remaining)
    return LicenseBannerResponse(
        status=result.get("status"),
        expires_at=result.get("expires_at"),
        reason=result.get("reason"),
        grace_days_remaining=grace_days_remaining,
    )


@router.get(
    LICENSE_ORG_NAME_PATH,
    response_model=LicenseOrgNameResponse,
    dependencies=[Depends(require_auth)],
)
def get_license_org_name() -> LicenseOrgNameResponse:
    """R17-D4 Addendum A / T12: the dynamic organisation display name (§2).

    Auth-only (any role, like the banner) so the resolved name renders on every
    page for every surface (header, workspace labels, reports, License page) — the
    single source every UI surface consumes (§5 "One name, resolved once"), never
    per-surface naming logic. The org is resolved from the tenancy context; the
    name is read from that org's live-validated license by the one resolver in
    ``org_display_name`` — a neutral default before a key is installed (AC16),
    updating immediately when a new key is pasted (AC15). Side-effect-free.
    """
    return LicenseOrgNameResponse(orgName=resolve_org_display_name())


@router.get(
    LICENSE_LIMITS_PATH,
    response_model=LicenseLimitsResponse,
    dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
)
def get_license_limits() -> LicenseLimitsResponse:
    """R17-D4 Addendum A / T10: systems used vs systems licensed for the org.

    Viewer+ (matching ``GET /api/connectors``) so every role that can see the
    Integration Hub can see its usage against the entitlement — not Owner-gated
    like the full status route. Side-effect-free. The org is resolved from the
    tenancy context; the counts are the same ones the connect-time gate enforces,
    so the shown count matches the enforced count (AC14)."""
    state = license_limits.get_limit_state(get_current_org_id())
    return LicenseLimitsResponse(**state)


@router.post(
    LICENSE_UPDATE_PATH,
    response_model=LicenseStatusResponse,
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def update_license_key(body: UpdateKeyRequest) -> LicenseStatusResponse:
    """Owner-only: validate a pasted key, then store it only if not invalid.

    Validate-before-store (AC7): an invalid/tampered key is rejected with 400
    and the org's previously stored key is left untouched, so a bad paste can
    never replace a working license. A valid key is persisted to the caller's
    org row in ``org_licenses`` and the refreshed status is returned immediately —
    no restart required.

    Org binding (R-1.9.1-L1 / T2, AC1): the pasted key is validated BOUND to the
    caller's installation org, so a key issued for a different organisation is
    rejected at paste time with a plain-language reason — Customer A's key pasted
    into Customer B's installation fails closed and says why, and B's working key
    is untouched.
    """
    org_id = get_current_org_id()
    result = validate_license(body.key, installation_org_id=org_id)
    if result.get("status") == LicenseStatus.INVALID:
        if result.get("reason") == REASON_ORG_MISMATCH:
            raise HTTPException(status_code=400, detail=ORG_MISMATCH_MESSAGE)
        raise HTTPException(status_code=400, detail="This key is not valid")

    set_org_license_key(org_id, body.key)
    # Refresh the org's derived status cache (last_status / last_seen_date)
    # IMMEDIATELY after the key write — before the fire-and-forget telemetry,
    # which does its own DB I/O — so the window where the stored key and the
    # cached status disagree is as small as possible. (Live consumers such as the
    # gate/banner re-validate the key directly and never read this cache, but
    # keeping the two writes adjacent avoids any stale-cache surprise.)
    persist_validated_status(result, org_id=org_id)
    # 2.0-D4 T1: license install is a state-changing action D4 names explicitly, so
    # it belongs in the AUDIT stream and not only in telemetry. The two stores are
    # not interchangeable — telemetry is operational and audit is the access-
    # controlled, retained, immutable record an auditor reads — and before this an
    # auditor querying audit_log for a licence change found nothing. The key itself
    # is never recorded: only who installed what entitlement, and the outcome.
    audit_log_event(
        LICENSE_INSTALLED,
        org_id=org_id,
        target=result.get("customer") or org_id,
        outcome=OUTCOME_SUCCESS,
        status=result.get("status"),
        expires_at=result.get("expires_at"),
        term_months=(result.get("payload") or {}).get("term_months"),
        deployment_type=result.get("deployment_type"),
    )
    record_event(
        "license.updated",
        {
            "customer": result.get("customer"),
            "status": result.get("status"),
            "expires_at": result.get("expires_at"),
        },
    )
    return _to_status_response(result)


def register_license_routes(app: FastAPI) -> None:
    """Register the admin license routes once for the provided FastAPI app."""
    if getattr(app.state, "license_routes_registered", False):
        return

    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if LICENSE_STATUS_PATH in existing_paths:
        app.state.license_routes_registered = True
        return

    app.include_router(router)
    app.state.license_routes_registered = True
