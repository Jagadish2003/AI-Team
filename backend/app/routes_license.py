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
from .license_runtime import LICENSE_KEY_KV, get_current_license_status
from .licensing import LicenseStatus, validate_license
from .rbac import require_role
from .security import require_auth
from .telemetry import record_event

logger = logging.getLogger(__name__)

LICENSE_STATUS_PATH = "/api/license"
LICENSE_UPDATE_PATH = "/api/license/update-key"

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
