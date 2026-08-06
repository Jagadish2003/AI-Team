"""
routes_entity_match_proposals.py — Release 2.0-B2 T3: the review surface's API.

  GET  /api/entity-match-proposals              → the org's review queue + counts
  GET  /api/entity-match-proposals/{id}         → one proposal + its decision history
  POST /api/entity-match-proposals/{id}/decision→ confirm / reject (audited)
  POST /api/entity-match-proposals/scan         → run the ranked engine and record
                                                   whatever it PROPOSES

Access. Gated at ``analyst`` — the story's surface is Owner/Analyst, and a viewer
has nothing actionable here (the same reasoning as the other analyst+ write
workflows). Every route is org-scoped through ``get_current_org_id()``; a request
body never carries an org. A proposal id belonging to another tenant hard-404s,
indistinguishable from a typo — a "403 that exists" would confirm the id.

What a decision does, and does not do. Confirming records a durable, attributable
statement that two entities are the same thing, and stops the pair being proposed
again. It does NOT merge the graph: applying a confirmed identity with its
provenance is a separate task, and doing it behind this button would put an
irreversible change one click away from a review screen. ``confirmed_pairs`` in
:mod:`app.entity_match_proposals` is the read that applier consumes.

Auditing. A decision is a state-changing human action, so it emits an audit event
naming the actor, the proposal, and the transition. The scan deliberately does
not: it is a deterministic recomputation of derived state (it can only add or
refresh pending questions, never change an answer), so auditing it would add
volume without adding accountability.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .entity_match_proposals import (
    DECISION_ACTIONS,
    PROPOSAL_STATUSES,
    SCANNABLE_ENTITY_TYPES,
    ProposalDecisionError,
    ProposalNotFound,
    decide,
    get_proposal,
    history,
    list_proposals,
    scan_for_proposals,
    status_counts,
)
from .middleware.tenancy import get_current_org_id
from .rbac import _get_user_id_from_token, require_role
from .security import require_auth

logger = logging.getLogger(__name__)

PROPOSALS_PATH = "/api/entity-match-proposals"
PROPOSAL_PATH = "/api/entity-match-proposals/{proposal_id}"
DECISION_PATH = "/api/entity-match-proposals/{proposal_id}/decision"
SCAN_PATH = "/api/entity-match-proposals/scan"

router = APIRouter(tags=["entity-match-proposals"])


# ── request / response models ───────────────────────────────────────────────


class ProposalDecisionRequest(BaseModel):
    """``confirm`` = these are the same thing; ``reject`` = they are not.

    There is deliberately no third "defer" action: a proposal that is neither
    confirmed nor rejected is already pending, so deferring is what happens when
    the reviewer does nothing.
    """

    action: str = Field(..., description="confirm | reject")
    note: Optional[str] = Field(
        None, description="Optional reviewer note recorded with the decision."
    )


class ProposalListResponse(BaseModel):
    proposals: List[Dict[str, Any]] = Field(default_factory=list)
    counts: Dict[str, int] = Field(default_factory=dict)
    status: Optional[str] = None


class ProposalDetailResponse(BaseModel):
    proposal: Dict[str, Any]
    history: List[Dict[str, Any]] = Field(default_factory=list)


class ScanResponse(BaseModel):
    created: int = 0
    refreshed: int = 0
    skipped_already_decided: int = 0
    entity_types: List[str] = Field(default_factory=list)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="entity match proposal not found")


# ── routes ──────────────────────────────────────────────────────────────────


@router.get(
    PROPOSALS_PATH,
    response_model=ProposalListResponse,
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def list_entity_match_proposals(
    status: Optional[str] = Query(
        None, description="Filter by status: pending | confirmed | rejected."
    ),
    limit: int = Query(200, ge=1, le=1000),
) -> ProposalListResponse:
    """2.0-B2 (T3) — the review queue for this org.

    Defaults to EVERY status rather than pending-only so the surface can show
    what has already been decided; the UI filters. ``counts`` always carries all
    three statuses (zero-filled), so the tabs never have to tell "none" apart from
    "not reported". An unrecognised ``status`` yields an empty list rather than
    quietly falling back to everything.
    """
    org_id = get_current_org_id()
    if status is not None and status not in PROPOSAL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {list(PROPOSAL_STATUSES)}",
        )
    proposals = list_proposals(org_id, status=status, limit=limit)
    return ProposalListResponse(
        proposals=[p.to_dict() for p in proposals],
        counts=status_counts(org_id),
        status=status,
    )


@router.get(
    PROPOSAL_PATH,
    response_model=ProposalDetailResponse,
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def get_entity_match_proposal(proposal_id: str) -> ProposalDetailResponse:
    """One proposal with the evidence behind it and its full decision history."""
    org_id = get_current_org_id()
    try:
        proposal = get_proposal(org_id, proposal_id)
    except ProposalNotFound:
        raise _not_found()
    return ProposalDetailResponse(
        proposal=proposal.to_dict(),
        history=history(org_id, proposal_id),
    )


@router.post(
    DECISION_PATH,
    response_model=Dict[str, Any],
    dependencies=[Depends(require_role("analyst"))],
)
def decide_entity_match_proposal(
    proposal_id: str,
    body: ProposalDecisionRequest,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Confirm or reject one proposed match.

    Idempotent — repeating the decision already in force returns ``changed:
    false`` and writes no duplicate history row. Reversing a decision is allowed
    and appends a new forward row, so the original answer is never edited away.
    """
    org_id = get_current_org_id()
    actor_id = _get_user_id_from_token(token)
    try:
        outcome = decide(org_id, proposal_id, body.action, actor_id, note=body.note)
    except ProposalNotFound:
        raise _not_found()
    except ProposalDecisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if outcome.changed:
        _audit_decision(org_id, actor_id, outcome)
    return outcome.to_dict()


@router.post(
    SCAN_PATH,
    response_model=ScanResponse,
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def scan_entity_match_proposals(
    entity_types: Optional[List[str]] = Query(
        None,
        description=(
            "Entity types to scan. Defaults to every scannable type; a type "
            "outside that set is ignored."
        ),
    ),
) -> ScanResponse:
    """Recompute this org's proposals from the ranked engine.

    Writes nothing to the graph — the engine only decides (T1), and this persists
    the propose-only outcomes. An already-answered pair is never re-opened; the
    count of those is reported rather than hidden.
    """
    org_id = get_current_org_id()
    requested = [t for t in (entity_types or []) if t in SCANNABLE_ENTITY_TYPES]
    outcome = scan_for_proposals(org_id, entity_types=requested or None)
    return ScanResponse(
        created=outcome.created,
        refreshed=outcome.refreshed,
        skipped_already_decided=outcome.skipped_already_decided,
        entity_types=requested or list(SCANNABLE_ENTITY_TYPES),
    )


def _audit_decision(org_id: str, actor_id: str, outcome: Any) -> None:
    """Record the decision in the organisation-wide audit trail.

    Best-effort: the decision is already persisted in its own append-only history,
    so a failure to also write the org-wide audit row must not fail the request —
    but it is logged rather than swallowed.
    """
    try:
        from .middleware.audit import ENTITY_MATCH_PROPOSAL_DECIDED, log_event

        proposal = outcome.proposal
        log_event(
            ENTITY_MATCH_PROPOSAL_DECIDED,
            org_id=org_id,
            user_id=actor_id,
            proposal_id=proposal.proposal_id,
            entity_type=proposal.entity_type,
            left_entity_id=proposal.left_entity_id,
            right_entity_id=proposal.right_entity_id,
            action=outcome.action,
            previous_status=outcome.previous_status,
            resulting_status=outcome.resulting_status,
            revision=outcome.revision,
            tier=proposal.tier,
            timestamp=outcome.decided_at,
        )
    except Exception as exc:  # noqa: BLE001 — log_event is itself non-raising.
        logger.warning("entity match proposal audit write failed: %s", exc)


def register_entity_match_proposal_routes(app: FastAPI) -> None:
    """Register the review-surface routes once for the provided FastAPI app."""
    if getattr(app.state, "entity_match_proposal_routes_registered", False):
        return
    existing = {getattr(route, "path", None) for route in app.routes}
    if PROPOSALS_PATH in existing:
        app.state.entity_match_proposal_routes_registered = True
        return
    app.include_router(router)
    app.state.entity_match_proposal_routes_registered = True
