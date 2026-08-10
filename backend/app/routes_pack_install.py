"""Authored-pack installation API — 2.0-C3 T4 (AT-839).

Four endpoints on the installed-pack resource:

    POST /api/packs/install                        — install a signed bundle (owner)
    GET  /api/packs/installed                      — the org's authored packs (viewer+)
    GET  /api/packs/installed/{pack_id}/validation — the sandbox verdict (analyst+)
    PUT  /api/packs/installed/{pack_id}/activation — activate / withdraw (owner)

Role rationale
--------------
Installing and activating are ``owner``: an authored pack changes what every
future run for the whole organisation produces, and it introduces third-party
content into the deployment — the same bar as connecting a connector, and higher
in consequence. Reading the list is ``viewer``, because someone looking at a
finding attributed to a partner pack must be able to see which pack that is and
who published it.

Every read and write is scoped to the authenticated org via
``get_current_org_id``; the request body never carries an org id.

Why a base64 body rather than a multipart upload
--------------------------------------------------
A bundle is small (capped at 16 MiB) and this keeps the API dependency-free of
``python-multipart``, which this deployment does not otherwise need. The trade is
~33% wire overhead on an infrequent, human-initiated operation — worth it to avoid
adding a parsing dependency to a route that accepts untrusted third-party bytes.

Refusals are specific
---------------------
Every gate returns **409 with the gate named** (signature, validation, sandbox
limits, compatibility, certification policy) and the specific failures listed,
except an unreadable certification policy, which is **503** — the platform could
not determine compliance, which is a different thing from having determined
non-compliance, and an operator needs to tell them apart.

The validation verdict is READABLE (2.0-C3 T6 / AT-841) rather than only
returnable at the moment of refusal: activation re-runs the author's fixtures
against today's platform, and an operator whose pack stopped activating needs the
reasons without re-uploading the bundle. It is analyst+ rather than viewer,
because the reasons quote partner-supplied fixture and manifest text.
"""
from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .middleware.audit import PACK_ACTIVATION_CHANGED, PACK_INSTALLED, log_event
from .middleware.tenancy import get_current_org_id
from .pack_installation import (
    REASON_CERTIFICATION_POLICY_UNAVAILABLE,
    REASON_NOT_INSTALLED,
    PackInstallRefused,
    get_installed_pack,
    install_pack_bundle,
    list_installed_packs,
    set_installed_pack_activation,
)
from .rbac import _get_user_id_from_token, require_role
from .security import require_auth
from .telemetry import record_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/packs", tags=["packs"])

#: Refusing an oversized body before base64-decoding it: the decoded bundle is
#: capped again inside the verifier, but a request that could never be a valid
#: bundle should not be decoded into memory first.
MAX_ENCODED_BUNDLE_CHARS = 24 * 1024 * 1024


class PackInstallRequest(BaseModel):
    """A signed pack bundle to install."""

    bundle_base64: str = Field(
        alias="bundleBase64",
        description="The signed .aiqpack bundle, base64-encoded.",
    )
    activate: bool = Field(
        default=False,
        description=(
            "Activate immediately on a successful install. Default false: "
            "installing records a pack, activating puts it into every future run."
        ),
    )

    model_config = {"populate_by_name": True}


class PackActivationRequest(BaseModel):
    """The TARGET activation state, so the call is idempotent."""

    active: bool = Field(
        description=(
            "true activates the installed pack, re-running the sandbox validation "
            "(manifest + the author's fixtures), the compatibility gate, and the "
            "certification-policy gate against today's platform; false withdraws "
            "it from service. Withdrawal runs no gates and never deletes the pack "
            "or its history."
        )
    )


def _refusal_status(reason: str) -> int:
    if reason == REASON_CERTIFICATION_POLICY_UNAVAILABLE:
        return 503
    if reason == REASON_NOT_INSTALLED:
        return 404
    return 409


def _record_sandbox_verdict(
    org_id: str, pack_id: str, validation: Any, *, trigger: str, actor: str
) -> None:
    """Cost and outcome of a sandbox run. Never the failure text."""
    if not isinstance(validation, dict) or not validation:
        return
    try:
        record_event(
            "pack.sandbox_validated",
            {
                "org_id": org_id,
                "pack_id": pack_id,
                "trigger": trigger,
                "ok": bool(validation.get("ok")),
                "stage": str(validation.get("stage") or ""),
                "failure_count": len(validation.get("reasons") or []),
                "case_count": int(validation.get("caseCount") or 0),
                "record_count": int(validation.get("recordCount") or 0),
                "duration_ms": int(validation.get("durationMs") or 0),
                "actor_id": actor,
            },
        )
    except Exception:  # noqa: BLE001 - telemetry never blocks the operation
        logger.warning("Could not record pack.sandbox_validated", exc_info=True)


def _record_refusal(org_id: str, refusal: PackInstallRefused, actor: str) -> None:
    try:
        record_event(
            "pack.install_refused",
            {
                "org_id": org_id,
                "pack_id": refusal.pack_id,
                "reason": refusal.reason,
                "failure_count": len(refusal.failures),
                "actor_id": actor,
            },
        )
    except Exception:  # noqa: BLE001 - telemetry must never mask the refusal
        logger.warning("Could not record pack.install_refused", exc_info=True)


@router.post(
    "/install",
    status_code=201,
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def install_pack(
    request: PackInstallRequest,
    token: str = Depends(require_auth),
    org_id: str = Depends(get_current_org_id),
) -> Dict[str, Any]:
    """Install a signed authored-pack bundle for this org."""
    actor = _get_user_id_from_token(token)
    if len(request.bundle_base64) > MAX_ENCODED_BUNDLE_CHARS:
        raise HTTPException(status_code=413, detail="bundle exceeds the maximum size")
    try:
        payload = base64.b64decode(request.bundle_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"bundleBase64 is not valid base64: {exc}"
        ) from exc

    try:
        outcome = install_pack_bundle(
            org_id, payload, actor_id=actor, activate=request.activate
        )
    except PackInstallRefused as refusal:
        _record_refusal(org_id, refusal, actor)
        raise HTTPException(
            status_code=_refusal_status(refusal.reason), detail=refusal.to_dict()
        ) from refusal

    log_event(
        PACK_INSTALLED,
        org_id=org_id,
        user_id=actor,
        pack_id=outcome.record.pack_id,
        pack_version=outcome.record.pack_version,
        bundle_digest=outcome.record.bundle_digest,
        signing_key_id=outcome.record.signing_key_id,
        activated=outcome.activated,
    )
    try:
        record_event(
            "pack.installed",
            {
                "org_id": org_id,
                "pack_id": outcome.record.pack_id,
                "pack_version": outcome.record.pack_version,
                "bundle_digest": outcome.record.bundle_digest,
                "signing_key_id": outcome.record.signing_key_id,
                "activated": outcome.activated,
                "actor_id": actor,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("Could not record pack.installed", exc_info=True)
    _record_sandbox_verdict(
        org_id,
        outcome.record.pack_id,
        outcome.record.validation,
        trigger="install",
        actor=actor,
    )
    return outcome.to_dict()


@router.get(
    "/installed",
    dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
)
def get_installed_packs(org_id: str = Depends(get_current_org_id)) -> Dict[str, Any]:
    """Every authored pack installed for this org, with its provenance."""
    records: List[Dict[str, Any]] = [
        record.to_dict() for record in list_installed_packs(org_id)
    ]
    return {"packs": records, "count": len(records)}


@router.get(
    "/installed/{pack_id}/validation",
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def get_pack_validation(
    pack_id: str, org_id: str = Depends(get_current_org_id)
) -> Dict[str, Any]:
    """The most recent sandbox verdict for an installed pack.

    A pack that has never been re-validated since install carries its install-time
    verdict. ``validation`` is null only for a row written before AT-841 — an
    absent verdict is reported as absent rather than as a pass.
    """
    record = get_installed_pack(org_id, pack_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"pack '{pack_id}' is not installed for this org"
        )
    validation = dict(record.validation or {})
    return {
        "packId": record.pack_id,
        "packVersion": record.pack_version,
        "status": record.status,
        "fixtureCount": len(record.fixtures),
        "validation": validation or None,
    }


@router.put(
    "/installed/{pack_id}/activation",
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def set_pack_activation(
    pack_id: str,
    request: PackActivationRequest,
    token: str = Depends(require_auth),
    org_id: str = Depends(get_current_org_id),
) -> Dict[str, Any]:
    """Activate or withdraw an installed pack, re-running the gates on activation."""
    actor = _get_user_id_from_token(token)
    try:
        record = set_installed_pack_activation(
            org_id, pack_id, active=request.active, actor_id=actor
        )
    except PackInstallRefused as refusal:
        _record_refusal(org_id, refusal, actor)
        raise HTTPException(
            status_code=_refusal_status(refusal.reason), detail=refusal.to_dict()
        ) from refusal

    log_event(
        PACK_ACTIVATION_CHANGED,
        org_id=org_id,
        user_id=actor,
        pack_id=record.pack_id,
        status=record.status,
        pack_version=record.pack_version,
    )
    try:
        record_event(
            "pack.activation_changed",
            {
                "org_id": org_id,
                "pack_id": record.pack_id,
                "pack_version": record.pack_version,
                "status": record.status,
                "actor_id": actor,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("Could not record pack.activation_changed", exc_info=True)
    if request.active:
        _record_sandbox_verdict(
            org_id, record.pack_id, record.validation, trigger="activation", actor=actor
        )
    return record.to_dict()


def register_pack_install_routes(app: FastAPI) -> None:
    """Register the installed-pack routes (called from ``app/main.py``)."""
    app.include_router(router)


__all__ = ["register_pack_install_routes", "router"]
