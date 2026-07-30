"""2.0-A2 T1 — the protected opportunity-lifecycle API.

Every route here is a state-changing action on org-scoped data, so all of them go
through the standard spine:

* **Tenancy** — the org comes from :func:`get_current_org_id` (the tenancy
  middleware). No route reads an org id from the request body or a query param,
  so one org can never read or write another's lifecycle. A cross-org identity
  answers 404, identically to a missing one, so the API never reveals that an
  identity exists in a different tenant.
* **RBAC** — ``require_role("analyst")`` on every route, reads included: a
  lifecycle state is customer-operational information, not public.
* **Audit + telemetry** — emitted inside the store, on the one write path, so no
  route can transition without being recorded.

Routes are keyed on ``opportunity_identity`` — the stable cross-run id — not on a
run, because lifecycle is a property of the problem rather than of one
observation of it.

**The non-inference rule at the API edge.** There is no generic
``PATCH /lifecycle {state: ...}`` endpoint, deliberately. Recording an action is
its own route with ``actionDate`` a REQUIRED body field, so a caller cannot mark
something actioned without supplying the date. There is no route by which a
client can request ``monitoring``, ``measured`` or ``stalled`` — those are the
platform's own moves as runs land (T3).
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .middleware.tenancy import get_current_org_id
from .opportunity_lifecycle import (
    OpportunityLifecycleNotFound,
    dismiss,
    ensure_opportunity_lifecycle_tables,
    ensure_tracked,
    get_lifecycle_history,
    get_lifecycle_or_raise,
    list_lifecycles,
    record_action,
    reopen,
)
from .opportunity_lifecycle_states import (
    ALL_STATES,
    LifecycleTransitionError,
    lifecycle_state_summary,
)
from .rbac import _get_user_id_from_token, require_role
from .security import require_auth

router = APIRouter(prefix="/api/opportunity-lifecycle", tags=["opportunity-lifecycle"])


class RecordActionRequest(BaseModel):
    """Recording that a change was deployed.

    ``actionDate`` has no default — that is the point. A defaulted date would
    fabricate the before/after boundary every later measurement is computed from,
    so the field is required and a missing one is a 422 from the model itself,
    before any handler runs.
    """

    actionDate: str = Field(
        ...,
        description=(
            "ISO date (YYYY-MM-DD) the change was deployed. Required; must not be "
            "in the future."
        ),
    )
    note: Optional[str] = Field(default=None, max_length=2000)


class LifecycleNoteRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)


class TrackRequest(BaseModel):
    """Start tracking an identity at ``open`` (idempotent)."""

    runId: Optional[str] = None


def _not_found(identity: str) -> HTTPException:
    # Same answer for missing and cross-org: never confirm that an identity
    # exists in another tenant.
    return HTTPException(
        status_code=404, detail=f"no lifecycle record for opportunity {identity!r}"
    )


@router.get("/states", dependencies=[Depends(require_role("analyst"))])
def get_lifecycle_states(_token: str = Depends(require_auth)) -> dict:
    """The state machine itself — states, actors, and every legal transition.

    Served so a UI (and a reviewer) can see the legal set without reading Python,
    and so a client never hard-codes its own copy of the transition table.
    """
    return lifecycle_state_summary()


@router.get("", dependencies=[Depends(require_role("analyst"))])
def list_opportunity_lifecycles(
    state: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    _token: str = Depends(require_auth),
) -> dict:
    """Every tracked opportunity in the caller's org, optionally filtered by state.

    The read 2.0-A2 T6's portfolio view builds on.
    """
    if state:
        unknown = sorted({s for s in state if s not in ALL_STATES})
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown lifecycle state(s): {', '.join(unknown)}. "
                    f"Valid states: {', '.join(ALL_STATES)}"
                ),
            )
    org_id = get_current_org_id()
    items = list_lifecycles(org_id, states=state, limit=limit)
    return {"orgId": org_id, "count": len(items), "items": items}


@router.get("/{opportunity_identity}", dependencies=[Depends(require_role("analyst"))])
def get_opportunity_lifecycle(
    opportunity_identity: str,
    _token: str = Depends(require_auth),
) -> dict:
    try:
        return get_lifecycle_or_raise(get_current_org_id(), opportunity_identity)
    except OpportunityLifecycleNotFound:
        raise _not_found(opportunity_identity)


@router.get(
    "/{opportunity_identity}/history",
    dependencies=[Depends(require_role("analyst"))],
)
def get_opportunity_lifecycle_history(
    opportunity_identity: str,
    limit: int = Query(default=200, ge=1, le=1000),
    _token: str = Depends(require_auth),
) -> dict:
    """The append-only transition history, oldest first."""
    org_id = get_current_org_id()
    try:
        # Validate the row under THIS org first, so a cross-org identity gets a
        # 404 rather than an empty history that implies "exists but no moves".
        get_lifecycle_or_raise(org_id, opportunity_identity)
    except OpportunityLifecycleNotFound:
        raise _not_found(opportunity_identity)
    return {
        "orgId": org_id,
        "opportunityIdentity": opportunity_identity,
        "transitions": get_lifecycle_history(org_id, opportunity_identity, limit=limit),
    }


@router.post(
    "/{opportunity_identity}/track",
    dependencies=[Depends(require_role("analyst"))],
)
def track_opportunity(
    opportunity_identity: str,
    body: TrackRequest | None = None,
    _token: str = Depends(require_auth),
) -> dict:
    """Begin tracking at ``open``. Idempotent — never resets an existing state."""
    payload = body or TrackRequest()
    return ensure_tracked(
        get_current_org_id(), opportunity_identity, run_id=payload.runId
    )


@router.post(
    "/{opportunity_identity}/action",
    dependencies=[Depends(require_role("analyst"))],
)
def record_opportunity_action(
    opportunity_identity: str,
    body: RecordActionRequest,
    token: str = Depends(require_auth),
) -> dict:
    """Record that a change was deployed, with its date → ``actioned``.

    The only route that can reach ``actioned``, and it cannot be called without a
    date. A future or malformed date is a 400 with the reason, never a default.
    """
    org_id = get_current_org_id()
    try:
        return record_action(
            org_id,
            opportunity_identity,
            body.actionDate,
            _get_user_id_from_token(token),
            note=body.note,
        )
    except OpportunityLifecycleNotFound:
        raise _not_found(opportunity_identity)
    except LifecycleTransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/{opportunity_identity}/dismiss",
    dependencies=[Depends(require_role("analyst"))],
)
def dismiss_opportunity(
    opportunity_identity: str,
    body: LifecycleNoteRequest | None = None,
    token: str = Depends(require_auth),
) -> dict:
    org_id = get_current_org_id()
    payload = body or LifecycleNoteRequest()
    try:
        return dismiss(
            org_id,
            opportunity_identity,
            _get_user_id_from_token(token),
            note=payload.note,
        )
    except OpportunityLifecycleNotFound:
        raise _not_found(opportunity_identity)
    except LifecycleTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/{opportunity_identity}/reopen",
    dependencies=[Depends(require_role("analyst"))],
)
def reopen_opportunity(
    opportunity_identity: str,
    body: LifecycleNoteRequest | None = None,
    token: str = Depends(require_auth),
) -> dict:
    """Unwind to ``open``, clearing the recorded action date.

    The reversibility path: an analyst who actioned the wrong opportunity undoes
    it here, and the unwind appears in history as its own forward transition
    rather than rewriting the row that recorded the mistake.
    """
    org_id = get_current_org_id()
    payload = body or LifecycleNoteRequest()
    try:
        return reopen(
            org_id,
            opportunity_identity,
            _get_user_id_from_token(token),
            note=payload.note,
        )
    except OpportunityLifecycleNotFound:
        raise _not_found(opportunity_identity)
    except LifecycleTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def register_opportunity_lifecycle_routes(app: FastAPI) -> None:
    if getattr(app.state, "opportunity_lifecycle_routes_registered", False):
        return
    # Startup-only schema safety net for a dev DB that has not run migration
    # 0031 — the same placement as ensure_entities_table(). Never per-request,
    # and it never raises: production is already provisioned and runs under a
    # role without CREATE.
    ensure_opportunity_lifecycle_tables()
    path = "/api/opportunity-lifecycle/{opportunity_identity}/action"
    if path not in {getattr(route, "path", None) for route in app.routes}:
        app.include_router(router)
    app.state.opportunity_lifecycle_routes_registered = True


__all__ = ["register_opportunity_lifecycle_routes", "router"]
