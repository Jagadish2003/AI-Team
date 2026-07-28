"""Protected MSP-B5 analyst workflow for proposed runbook matches."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel

from .middleware.audit import RUNBOOK_MATCH_DECIDED, log_event
from .middleware.tenancy import get_current_org_id
from .rbac import _get_user_id_from_token, require_role
from .runbook_match_decisions import (
    RunbookMatchDecisionError,
    RunbookMatchNotFound,
    get_runbook_match_decision_store,
)
from .security import require_auth

router = APIRouter(prefix="/api/runbook-matches", tags=["runbook-matches"])


class RunbookMatchDecisionRequest(BaseModel):
    action: Literal["accept", "dismiss", "defer"]


def _not_found() -> HTTPException:
    # Same answer for missing and cross-org records: never reveal another org's
    # recurrence by checking without the authenticated org key.
    return HTTPException(status_code=404, detail="runbook match not found")


@router.get(
    "/{recurrence_id}",
    dependencies=[Depends(require_role("analyst"))],
)
def get_runbook_match_state(
    recurrence_id: str,
    _token: str = Depends(require_auth),
) -> dict:
    try:
        return get_runbook_match_decision_store().current(
            get_current_org_id(), recurrence_id
        )
    except RunbookMatchNotFound:
        raise _not_found()


@router.post(
    "/{recurrence_id}/decision",
    dependencies=[Depends(require_role("analyst"))],
)
def decide_runbook_match(
    recurrence_id: str,
    body: RunbookMatchDecisionRequest,
    token: str = Depends(require_auth),
) -> dict:
    org_id = get_current_org_id()
    actor_id = _get_user_id_from_token(token)
    try:
        outcome = get_runbook_match_decision_store().decide(
            org_id, recurrence_id, body.action, actor_id
        )
    except RunbookMatchNotFound:
        raise _not_found()
    except RunbookMatchDecisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if outcome.changed:
        log_event(
            RUNBOOK_MATCH_DECIDED,
            org_id=org_id,
            user_id=actor_id,
            recurrence_id=recurrence_id,
            action=outcome.action,
            previous_state=outcome.previous_state,
            resulting_state=outcome.current_state,
            revision=outcome.revision,
        )
    return outcome.as_dict()


@router.get(
    "/{recurrence_id}/decision-history",
    dependencies=[Depends(require_role("analyst"))],
)
def get_runbook_match_history(
    recurrence_id: str,
    _token: str = Depends(require_auth),
) -> dict:
    org_id = get_current_org_id()
    store = get_runbook_match_decision_store()
    try:
        # Validate the current row under this org before returning an empty
        # history, so cross-org and missing identifiers receive the same 404.
        store.current(org_id, recurrence_id)
    except RunbookMatchNotFound:
        raise _not_found()
    return {
        "org_id": org_id,
        "recurrence_id": recurrence_id,
        "decisions": store.history(org_id, recurrence_id),
    }


def register_runbook_match_routes(app: FastAPI) -> None:
    if getattr(app.state, "runbook_match_routes_registered", False):
        return
    path = "/api/runbook-matches/{recurrence_id}/decision"
    if path not in {getattr(route, "path", None) for route in app.routes}:
        app.include_router(router)
    app.state.runbook_match_routes_registered = True
