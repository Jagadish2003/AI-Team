"""LIC-1 / T6 (AT-347) — admin license routes (Owner-only).

Two Owner-only endpoints expose the license to the admin UI (LicensePage, T8):

  GET  /api/license             → current status + details (status, customer,
                                  term, expires_at, days_remaining).
  POST /api/license/update-key  → validate a pasted key and store it ONLY if it
                                  is not invalid, then return the refreshed
                                  status. An invalid key is rejected and nothing
                                  is stored (validate-before-store, AC7).

Both endpoints reuse the SAME ``license:key`` kv slot as the T4 startup/periodic
validator (``license_runtime.LICENSE_KEY_KV``), so the admin update and the
background validator always read and write one stored license. Validation is
the pure, offline ``licensing.validate_license`` (T3).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import db
from .license_runtime import (
    LICENSE_KEY_KV,
    get_current_license_status,
    persist_validated_status,
)
from .licensing import LicenseStatus, validate_license
from .rbac import require_role
from .security import require_auth
from .telemetry import record_event

logger = logging.getLogger(__name__)

LICENSE_STATUS_PATH = "/api/license"
LICENSE_UPDATE_PATH = "/api/license/update-key"
LICENSE_BANNER_PATH = "/api/license/banner"

router = APIRouter(tags=["license"])


class LicenseStatusResponse(BaseModel):
    """Owner-facing license status. Detail fields are null when there is no
    valid key (no_license / invalid / clock_rollback), where only ``status``
    is meaningful."""

    status: str
    customer: Optional[str] = None
    term: Optional[int] = None  # term_months from the validated payload
    expires_at: Optional[str] = None
    days_remaining: Optional[int] = None


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
    for a genuinely expired (past-grace) key."""

    status: str
    expires_at: Optional[str] = None
    reason: Optional[str] = None


class UpdateKeyRequest(BaseModel):
    key: str = Field(..., min_length=1, description="The signed license key string to install.")


def _to_status_response(result: dict) -> LicenseStatusResponse:
    """Map a validate_license/evaluate_license result dict to the response model."""
    payload = result.get("payload") or {}
    return LicenseStatusResponse(
        status=result.get("status"),
        customer=result.get("customer"),
        term=payload.get("term_months"),
        expires_at=result.get("expires_at"),
        days_remaining=result.get("days_remaining"),
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
    return LicenseBannerResponse(
        status=result.get("status"),
        expires_at=result.get("expires_at"),
        reason=result.get("reason"),
    )


@router.post(
    LICENSE_UPDATE_PATH,
    response_model=LicenseStatusResponse,
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def update_license_key(body: UpdateKeyRequest) -> LicenseStatusResponse:
    """Owner-only: validate a pasted key, then store it only if not invalid.

    Validate-before-store (AC7): an invalid/tampered key is rejected with 400
    and the previously stored key is left untouched, so a bad paste can never
    replace a working license. A valid key is persisted to the shared
    ``license:key`` slot and the refreshed status is returned immediately —
    no restart required.
    """
    result = validate_license(body.key)
    if result.get("status") == LicenseStatus.INVALID:
        raise HTTPException(status_code=400, detail="This key is not valid")

    db.kv_set(LICENSE_KEY_KV, body.key)
    # Refresh the derived status cache (license:last_status / last_seen_date)
    # IMMEDIATELY after the key write — before the fire-and-forget telemetry,
    # which does its own DB I/O — so the window where the stored key and the
    # cached status disagree is as small as possible. (Live consumers such as the
    # gate/banner re-validate the key directly and never read this cache, but
    # keeping the two writes adjacent avoids any stale-cache surprise.)
    persist_validated_status(result)
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
