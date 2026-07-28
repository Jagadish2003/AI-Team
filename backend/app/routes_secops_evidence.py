"""Authorized, audited resolution of one Security Operations evidence pointer."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel

from discovery.packs.security_ops_evidence_resolver import (
    REASON_INSUFFICIENT_ROLE,
    REASON_INVALID_POINTER,
    EvidenceAccessDenied,
    RunKVEvidenceRecordStore,
    resolve_evidence_pointer,
)

from . import db
from .middleware.tenancy import get_current_org_id
from .rbac import _get_user_id_from_token, get_user_role, require_role
from .security import require_auth
from .telemetry import record_event

router = APIRouter(prefix="/api/runs", tags=["security-operations-evidence"])


class SecOpsEvidenceResolveRequest(BaseModel):
    pointer: Dict[str, Any]


def _require_run_in_org(run_id: str, org_id: str) -> Dict[str, Any]:
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


@router.get(
    "/{run_id}/secops/volume",
    dependencies=[Depends(require_role("viewer"))],
)
def get_secops_volume(
    run_id: str,
    _token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Expose the shared B7 processed/deferred report for run-health views."""
    org_id = get_current_org_id()
    _require_run_in_org(run_id, org_id)
    return db.run_kv_get("secops_volume", run_id, {}) or {}


@router.post(
    "/{run_id}/secops/evidence/resolve",
    dependencies=[Depends(require_role("analyst"))],
)
def resolve_secops_evidence(
    run_id: str,
    body: SecOpsEvidenceResolveRequest,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Resolve exactly one pointer for an analyst in the run's owning org."""
    org_id = get_current_org_id()
    # Missing and cross-org runs deliberately receive the same 404.
    _require_run_in_org(run_id, org_id)
    user_id = _get_user_id_from_token(token)
    role = get_user_role(org_id, user_id)
    store = RunKVEvidenceRecordStore(run_id, org_id, db_api=db)

    def emit(event_type: str, payload: dict) -> None:
        record_event(event_type, {**payload, "run_id": run_id})

    try:
        return resolve_evidence_pointer(
            body.pointer,
            requesting_org=org_id,
            user_id=user_id,
            role=role,
            store=store,
            emit=emit,
        )
    except EvidenceAccessDenied as exc:
        if exc.reason == REASON_INSUFFICIENT_ROLE:
            raise HTTPException(status_code=403, detail="Insufficient role")
        if exc.reason == REASON_INVALID_POINTER:
            raise HTTPException(status_code=400, detail="invalid evidence pointer")
        # Same response for absent and cross-org records.
        raise HTTPException(status_code=404, detail="evidence pointer not found")


def register_secops_evidence_routes(app: FastAPI) -> None:
    if getattr(app.state, "secops_evidence_routes_registered", False):
        return
    path = "/api/runs/{run_id}/secops/evidence/resolve"
    if path not in {getattr(route, "path", None) for route in app.routes}:
        app.include_router(router)
    app.state.secops_evidence_routes_registered = True
