"""Pack lifecycle state API — 2.0-C1 T2 (AT-827) safe disable state machine.

Three endpoints on the pack-state resource:

    GET  /api/packs/state                    — every pack with its state (viewer+)
    PUT  /api/packs/{pack_id}/state          — disable or re-enable a pack (owner)
    GET  /api/packs/{pack_id}/state/history  — append-only audit trail (analyst+)

Role rationale
--------------
Reading state is ``viewer`` — a viewer looking at a finding labelled "produced by a
now-disabled pack" must be able to see that the pack is off. CHANGING state is
``owner``: turning off a discovery pack alters what every future run for the whole
organisation produces, which is an ownership-level decision, not an analyst one
(analysts review findings; owners configure the workspace — the same bar as
connector connect/disconnect).

Every read and write is scoped to the authenticated org via ``get_current_org_id``.
A request body never carries an org id, so one tenant can never change another's
pack state.

Nothing here deletes anything. Re-enabling writes a new state and a new history
row; the disable stays on the trail (2.0-C1 AC4).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .middleware.audit import PACK_STATE_CHANGED, log_event
from .middleware.tenancy import get_current_org_id
from .pack_state import (
    PackNotFound,
    PackStateError,
    PackStateOutcome,
    STATE_ACTIVE,
    STATE_DISABLED,
    pack_state_history,
    pack_state_view,
    set_pack_state,
    set_pinned_pack_version,
)
from .rbac import _get_user_id_from_token, require_role
from .security import require_auth
from discovery.packs.pack_config import (
    PackVersionUnavailable,
    get_pack_version,
    get_rollbackable_versions,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/packs", tags=["packs"])


class PackStateRequest(BaseModel):
    """A pack state transition.

    ``state`` is the TARGET state, not a verb, so the request is idempotent: PUTting
    ``disabled`` on an already-disabled pack succeeds and reports ``changed: false``
    rather than erroring or double-writing history.
    """

    state: Literal["active", "disabled"] = Field(
        description=(
            "Target state. 'disabled' stops the pack executing in future runs; "
            "'active' re-enables it. Historical findings are never affected."
        )
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=1000,
        description=(
            "Optional operator note explaining why, recorded on the state row and "
            "the audit trail (e.g. 'superseded by cloud_ops')."
        ),
    )


def _pack_not_found(pack_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown pack '{pack_id}'")


@router.get(
    "/state",
    dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
    summary="List every discovery pack with its lifecycle state for this org",
)
def list_pack_states() -> Dict[str, Any]:
    org_id = get_current_org_id()
    return {"orgId": org_id, "packs": pack_state_view(org_id)}


@router.put(
    "/{pack_id}/state",
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    summary="Disable or re-enable a discovery pack for this org",
)
def put_pack_state(
    pack_id: str,
    body: PackStateRequest,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Transition a pack between ``active`` and ``disabled``.

    Disabling stops the pack executing in FUTURE runs. It never touches historical
    findings, evidence, or run records — those stay retrievable and are labelled as
    produced by a now-disabled pack (2.0-C1 AC2).
    """
    org_id = get_current_org_id()
    actor_id = _get_user_id_from_token(token)
    try:
        outcome = set_pack_state(
            org_id, pack_id, body.state, actor_id=actor_id, reason=body.reason
        )
    except PackNotFound:
        raise _pack_not_found(pack_id)
    except PackStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # A no-op transition is not an audit event — only a real state change is.
    if outcome.changed:
        log_event(
            PACK_STATE_CHANGED,
            org_id=org_id,
            user_id=actor_id,
            pack_id=outcome.pack_id,
            transition=outcome.transition,
            previous_state=outcome.previous_state,
            resulting_state=outcome.current_state,
            revision=outcome.revision,
        )
        _record_state_changed(outcome)
    return outcome.as_dict()


class PackVersionRequest(BaseModel):
    """A pack version rollback (2.0-C1 T3 / AT-828).

    ``version`` is the TARGET version, so the request is idempotent. ``null`` (or
    omitted) CLEARS the pin, restoring the pack to the version the platform
    currently ships — the "undo the rollback" operation.
    """

    version: Optional[str] = Field(
        default=None,
        max_length=32,
        description=(
            "Prior version to run, from the pack's availableVersions. null clears "
            "the pin and restores the current version. Only affects future runs; "
            "existing findings keep their original version stamp."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=1000,
        description=(
            "Optional operator note explaining why, recorded on the state row and "
            "the audit trail (e.g. 'regression in 1.2.0 queue-ageing thresholds')."
        ),
    )


@router.put(
    "/{pack_id}/version",
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    summary="Roll a discovery pack back to a prior version, or restore the current one",
)
def put_pack_version(
    pack_id: str,
    body: PackVersionRequest,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Pin a pack to a prior version, or clear the pin.

    Affects FUTURE runs only. Existing findings keep the version stamp they were
    produced with and nothing historical is rewritten or backfilled (2.0-C1 AC3).

    **409** when the requested version has no archived artifact — the response names
    the versions that ARE available. The platform will not stamp a run with a version
    whose behaviour it cannot actually serve.
    """
    org_id = get_current_org_id()
    actor_id = _get_user_id_from_token(token)
    try:
        outcome = set_pinned_pack_version(
            org_id, pack_id, body.version, actor_id=actor_id, reason=body.reason
        )
    except PackNotFound:
        raise _pack_not_found(pack_id)
    except PackVersionUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if outcome.changed:
        log_event(
            PACK_STATE_CHANGED,
            org_id=org_id,
            user_id=actor_id,
            pack_id=outcome.pack_id,
            transition=outcome.transition,
            previous_state=outcome.previous_state,
            resulting_state=outcome.current_state,
            revision=outcome.revision,
            previous_version=outcome.previous_version,
            resulting_version=outcome.current_version,
        )
        _record_state_changed(outcome)
    return {
        **outcome.as_dict(),
        "availableVersions": get_rollbackable_versions(pack_id),
        "currentVersion": get_pack_version(pack_id),
        "effectiveVersion": outcome.current_version or get_pack_version(pack_id),
    }


@router.get(
    "/{pack_id}/state/history",
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
    summary="Append-only lifecycle transition history for one pack",
)
def get_pack_state_history(pack_id: str) -> Dict[str, Any]:
    """Newest-first transition history (repo convention for audit lists).

    Re-enabling a pack does not erase the disable — both transitions appear here,
    which is what makes this an audit trail rather than a current-state mirror.
    """
    org_id = get_current_org_id()
    from discovery.packs.pack_config import PACK_REGISTRY

    if pack_id not in PACK_REGISTRY:
        raise _pack_not_found(pack_id)
    return {
        "orgId": org_id,
        "packId": pack_id,
        "transitions": pack_state_history(org_id, pack_id),
    }


def _record_state_changed(outcome: PackStateOutcome) -> None:
    """Mirror the transition into telemetry so run health can correlate it.

    Observability only — a telemetry failure must never fail a state change that
    has already been persisted and audited.
    """
    from .telemetry import record_event

    try:
        record_event(
            "pack.state_changed",
            {
                "org_id": outcome.org_id,
                "pack_id": outcome.pack_id,
                "transition": outcome.transition,
                "previous_state": outcome.previous_state,
                "state": outcome.current_state,
                "revision": outcome.revision,
                "actor_id": outcome.actor_id,
                "changed_at": outcome.changed_at,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pack.state_changed telemetry failed (non-blocking)", exc_info=True
        )


def register_pack_state_routes(app: FastAPI) -> None:
    """Attach the pack-state routes exactly once (idempotent)."""
    existing = {getattr(route, "path", None) for route in app.routes}
    if "/api/packs/state" in existing:
        return
    app.include_router(router)


__all__ = [
    "PackStateRequest",
    "register_pack_state_routes",
    "router",
    "STATE_ACTIVE",
    "STATE_DISABLED",
]
