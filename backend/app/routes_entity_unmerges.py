"""
routes_entity_unmerges.py — Release 2.0-B2 T5: reversing a resolution.

  POST /api/entities/{entity_id}/unmerge        → detach one constituent
  POST /api/entities/{entity_id}/unmerge-all    → split a merged entity completely
  GET  /api/entity-unmerges                     → the org's unmerge log
  POST /api/entity-unmerges/{unmerge_id}/release → allow the pair to merge again
  GET  /api/findings/reevaluation-flags         → findings awaiting re-evaluation

AC4 is "unmerge restores constituents and flags dependent findings for
re-evaluation". The restore and the flagging both happen in
:mod:`app.entity_unmerge`; these routes are the surface an Owner/Analyst reaches
them through, and the read that makes the flags visible rather than merely stored.

Access, and why it is not uniform. Unmerging is gated at ``analyst``, matching the
merge-apply route it reverses — the same people who can join entities can correct
the join. **Releasing a block is gated at ``owner``**, because it is the one action
here that re-permits AUTOMATIC merging of a pair a person deliberately separated:
one person undoing another's correction, after which any later run may join them
again. That asymmetry is intentional, not an oversight.

Org-scoped via ``get_current_org_id()`` throughout; an entity or unmerge id in
another org 404s, indistinguishable from one that does not exist — a "403 that
exists" would confirm the id.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .entity_unmerge import (
    DEFAULT_MAX_RUNS_SCANNED,
    EntityUnmergeError,
    list_unmerges,
    release_merge_block,
    unmerge_all,
    unmerge_entity,
)
from .finding_reevaluation import STATUS_CLEARED, STATUS_PENDING, list_flags
from .middleware.tenancy import get_current_org_id
from .rbac import _get_user_id_from_token, require_role
from .security import require_auth

logger = logging.getLogger(__name__)

UNMERGE_PATH = "/api/entities/{entity_id}/unmerge"
UNMERGE_ALL_PATH = "/api/entities/{entity_id}/unmerge-all"
UNMERGE_LOG_PATH = "/api/entity-unmerges"
RELEASE_PATH = "/api/entity-unmerges/{unmerge_id}/release"
REEVALUATION_FLAGS_PATH = "/api/findings/reevaluation-flags"

router = APIRouter(tags=["entity-unmerges"])


class UnmergeRequest(BaseModel):
    reason: Optional[str] = Field(
        None,
        description="Why the merge is being reversed. Recorded with the unmerge.",
        max_length=1000,
    )
    max_runs: int = Field(
        DEFAULT_MAX_RUNS_SCANNED,
        ge=1,
        le=500,
        description=(
            "How many of the org's most recent runs the dependent-finding sweep "
            "reads. Whatever the bound leaves unread is reported, never dropped "
            "silently."
        ),
    )


class ReleaseRequest(BaseModel):
    reason: Optional[str] = Field(
        None,
        description="Why the pair may merge again. Recorded with the release.",
        max_length=1000,
    )


@router.post(
    UNMERGE_PATH,
    dependencies=[Depends(require_role("analyst"))],
)
def unmerge(
    entity_id: str,
    body: Optional[UnmergeRequest] = None,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """2.0-B2 (T5 / AC4) — detach this entity from the one it was merged into.

    Returns what actually happened rather than a bare 200: which entities were
    handed back (including any sub-merge that travelled with the detached entity),
    how many constituents the survivor has left, how many findings were flagged, and
    how many could not be assessed for dependency. An entity that is not merged
    answers ``not_merged`` — a truthful answer to the request, not an error.
    """
    org_id = get_current_org_id()
    actor = _get_user_id_from_token(token)
    payload = body or UnmergeRequest()
    try:
        outcome = unmerge_entity(
            org_id,
            entity_id,
            actor=actor,
            reason=payload.reason,
            max_runs=payload.max_runs,
        )
    except EntityUnmergeError as exc:
        # An unknown entity is a 404, not a 400: the caller asked about something
        # this org does not have.
        if "does not exist" in str(exc):
            raise HTTPException(status_code=404, detail="entity not found")
        raise HTTPException(status_code=400, detail=str(exc))
    return outcome.to_dict()


@router.post(
    UNMERGE_ALL_PATH,
    dependencies=[Depends(require_role("analyst"))],
)
def unmerge_everything(
    entity_id: str,
    body: Optional[UnmergeRequest] = None,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Split a merged entity completely, one constituent at a time.

    Each detachment is its own reversal with its own block and audit event, so the
    response is the list of them rather than one aggregate outcome.
    """
    org_id = get_current_org_id()
    actor = _get_user_id_from_token(token)
    payload = body or UnmergeRequest()
    try:
        outcomes = unmerge_all(
            org_id,
            entity_id,
            actor=actor,
            reason=payload.reason,
            max_runs=payload.max_runs,
        )
    except EntityUnmergeError as exc:
        if "does not exist" in str(exc):
            raise HTTPException(status_code=404, detail="entity not found")
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "survivorEntityId": entity_id,
        "detached": len([o for o in outcomes if o.applied]),
        "outcomes": [o.to_dict() for o in outcomes],
    }


@router.get(
    UNMERGE_LOG_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def get_unmerge_log(
    status: Optional[str] = Query(
        None, description="Filter by 'blocked' or 'released'. Omit for both."
    ),
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    """The org's unmerges, newest first — one entry per action.

    This is also the answer to "why did this pair stop merging?": a blocked entry
    names the pair, the rule whose merge was undone, and who reversed it.
    """
    org_id = get_current_org_id()
    blocks = list_unmerges(org_id, status=status, limit=limit)
    return {"unmerges": [b.to_dict() for b in blocks], "count": len(blocks)}


@router.post(
    RELEASE_PATH,
    dependencies=[Depends(require_role("owner"))],
)
def release(
    unmerge_id: str,
    body: Optional[ReleaseRequest] = None,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Allow a previously-unmerged pair to be merged again (Owner only).

    Does not itself merge anything — it removes the refusal, so the ordinary
    appliers may join the pair on a later pass. Nothing is deleted: the row keeps
    its unmerge record and gains who released it and why.
    """
    org_id = get_current_org_id()
    actor = _get_user_id_from_token(token)
    payload = body or ReleaseRequest()
    try:
        released = release_merge_block(
            org_id, unmerge_id, actor=actor, reason=payload.reason
        )
    except EntityUnmergeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if released == 0:
        # Unknown id, another org's id, or already released — all 404 from here, so
        # a caller cannot use this route to discover that an id exists elsewhere.
        raise HTTPException(
            status_code=404, detail="no active unmerge block with that id"
        )
    return {"unmergeId": unmerge_id, "releasedKeys": released, "status": "released"}


@router.get(
    REEVALUATION_FLAGS_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def get_reevaluation_flags(
    status: Optional[str] = Query(
        STATUS_PENDING,
        description=(
            f"'{STATUS_PENDING}' (default) or '{STATUS_CLEARED}'. Pass 'all' for both."
        ),
    ),
    limit: int = Query(200, ge=1, le=1000),
) -> Dict[str, Any]:
    """Findings awaiting re-evaluation — the visible half of AC4.

    A cleared flag names the run that re-evaluated the finding, which is what makes
    "re-evaluated on the next run" checkable rather than assumed.
    """
    org_id = get_current_org_id()
    wanted: Optional[str] = None if (status or "").lower() == "all" else status
    flags = list_flags(org_id, status=wanted, limit=limit)
    return {
        "flags": [f.to_dict() for f in flags],
        "count": len(flags),
        "pending": len([f for f in flags if f.is_pending]),
    }


def register_entity_unmerge_routes(app: FastAPI) -> None:
    """Register the unmerge + re-evaluation routes once for the provided app."""
    if getattr(app.state, "entity_unmerge_routes_registered", False):
        return
    existing = {getattr(route, "path", None) for route in app.routes}
    if UNMERGE_PATH in existing:
        app.state.entity_unmerge_routes_registered = True
        return
    app.include_router(router)
    app.state.entity_unmerge_routes_registered = True


__all__ = [
    "UNMERGE_PATH",
    "UNMERGE_ALL_PATH",
    "UNMERGE_LOG_PATH",
    "RELEASE_PATH",
    "REEVALUATION_FLAGS_PATH",
    "router",
    "register_entity_unmerge_routes",
]
