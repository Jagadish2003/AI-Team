"""2.0-A3 T1 — the learning feedback API and the signal-set read surface.

Same spine as the sibling 2.0-A2 routes:

* **Tenancy** — the org comes from :func:`get_current_org_id` (the tenancy
  middleware). No route reads an org id from the request body or a query
  param, which is how AC6's isolation holds at the edge as well as in the SQL.
* **RBAC** — ``require_role("analyst")`` on every route, reads included: what a
  team has accepted and dismissed is customer-operational information.
* **Audit** — emitted inside the store on its one write path, so no route can
  record a decision without it being recorded in the audit stream.

**What this API deliberately is not.** There is no route that applies an
adjustment, and none that returns an adjusted ranking. T1 produces the signal
set; T2 owns the bounded adjustment. ``GET /signals`` exists so the set is
inspectable on its own — before anything consumes it, and afterwards when a
customer asks what the layer actually learned from.

**Defer is here because it has nowhere else to be.** The review decision enum
(``APPROVED``/``REJECTED``/``UNREVIEWED``) is validated in two places in
``main.py``, one of which is the EVIDENCE decision, where deferring is
meaningless. Rather than widen a shared contract with a state that is invalid at
one of its call sites, ``defer`` gets an explicit route and a closed reason
vocabulary — which it needs anyway, since a defer with no stated reason carries
nothing to learn from.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .learning_feedback import (
    DEFER_REASONS,
    FEEDBACK_ACTIONS,
    MAX_REASON_DETAIL_CHARS,
    FeedbackError,
    ensure_opportunity_feedback_table,
    get_feedback,
    get_feedback_history,
    list_feedback,
    record_feedback,
)
from .learning_signal_config import load_config
from .learning_signals import (
    collect_learning_signals,
    describe_signal_set,
    similarity_key,
)
from .middleware.tenancy import get_current_org_id
from .rbac import _get_user_id_from_token, require_role
from .security import require_auth

router = APIRouter(prefix="/api/learning", tags=["learning"])


class RecordFeedbackRequest(BaseModel):
    """One analyst decision about one opportunity.

    ``reasonCode`` is REQUIRED for a defer and must come from the closed
    vocabulary. Free text is refused: a reason the layer cannot group on teaches
    it nothing, and free text entering a learning input is an unbounded PII
    surface. ``reasonDetail`` carries elaboration for the review surface and is
    never parsed or learned from.
    """

    action: str = Field(..., description=f"One of: {', '.join(FEEDBACK_ACTIONS)}")
    reasonCode: Optional[str] = Field(
        None, description=f"One of: {', '.join(DEFER_REASONS)}. Required for defer."
    )
    reasonDetail: Optional[str] = Field(
        None, max_length=MAX_REASON_DETAIL_CHARS, description="Free-text elaboration."
    )
    detectorId: Optional[str] = None
    packId: Optional[str] = None
    signalConcept: Optional[str] = None
    runId: Optional[str] = Field(
        None, description="Provenance only — the decision belongs to the opportunity."
    )


@router.get("/vocabulary", dependencies=[Depends(require_role("analyst"))])
def get_vocabulary(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    """The actions and reason codes a client may send.

    Advertised rather than hardcoded in the frontend, so the closed vocabulary
    has exactly one definition and a client cannot drift into sending a reason
    the learning layer will silently weight at zero.
    """
    config = load_config()
    return {
        "actions": list(FEEDBACK_ACTIONS),
        "deferReasons": [
            {
                "code": reason,
                "learningMultiplier": config.defer_multiplier(reason),
                "informsRanking": config.defer_multiplier(reason) > 0,
            }
            for reason in DEFER_REASONS
        ],
        "deferRequiresReason": True,
        "maxReasonDetailChars": MAX_REASON_DETAIL_CHARS,
    }


@router.post(
    "/feedback/{opportunity_identity}",
    dependencies=[Depends(require_role("analyst"))],
)
def post_feedback(
    opportunity_identity: str,
    body: RecordFeedbackRequest,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Record one decision. Appends; never updates.

    Changing your mind appends a new row. The earlier judgement is preserved
    because what the team thought at the time is itself part of the record —
    and because a store that edits its own history cannot answer "why was this
    ranked higher last month?".
    """
    try:
        org_id = get_current_org_id()
        actor_id = _get_user_id_from_token(token)
        record = record_feedback(
            org_id,
            opportunity_identity,
            body.action,
            actor_id=actor_id,
            reason_code=body.reasonCode,
            reason_detail=body.reasonDetail,
            detector_id=body.detectorId,
            pack_id=body.packId,
            signal_concept=body.signalConcept,
            run_id=body.runId,
        )
        from .learning_adjustment_state import recompute_after_signal_change

        recompute_after_signal_change(
            org_id,
            actor_id=actor_id,
            trigger="learning_feedback",
        )
        return record
    except FeedbackError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get(
    "/feedback/{opportunity_identity}",
    dependencies=[Depends(require_role("analyst"))],
)
def get_identity_feedback(
    opportunity_identity: str,
    _token: str = Depends(require_auth),
) -> List[Dict[str, Any]]:
    """Every decision ever recorded about one opportunity, oldest first."""
    return get_feedback_history(get_current_org_id(), opportunity_identity)


@router.get("/feedback", dependencies=[Depends(require_role("analyst"))])
def get_org_feedback(
    action: Optional[List[str]] = Query(None),
    detectorId: Optional[List[str]] = Query(None),
    packId: Optional[List[str]] = Query(None),
    limit: int = Query(500, ge=1, le=5000),
    _token: str = Depends(require_auth),
) -> List[Dict[str, Any]]:
    """Decisions in this org, newest first."""
    return list_feedback(
        get_current_org_id(),
        actions=action,
        detector_ids=detectorId,
        pack_ids=packId,
        limit=limit,
    )


@router.get(
    "/feedback/entry/{feedback_id}",
    dependencies=[Depends(require_role("analyst"))],
)
def get_feedback_entry(
    feedback_id: str,
    _token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """One decision by id — what an explainability link resolves to.

    A cross-org id answers 404 identically to a missing one, so the API never
    reveals that a decision exists in a different tenant.
    """
    record = get_feedback(get_current_org_id(), feedback_id)
    if not record:
        raise HTTPException(status_code=404, detail="feedback not found")
    return record


@router.get("/signals", dependencies=[Depends(require_role("analyst"))])
def get_signal_set(
    detectorId: Optional[str] = Query(None),
    packId: Optional[str] = Query(None),
    limit: int = Query(2000, ge=1, le=5000),
    _token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """The current learning signal set for this org.

    Reports its own cold-start state (``isActive`` / ``inactiveReason``) so the
    UI can say "learning is not yet active" from the same source of truth the
    adjustment layer gates on — rather than from a second count that could
    disagree with it.

    ``detectorId``/``packId`` narrow to the signals SIMILAR to one finding type,
    which is what an explainability surface asks for.
    """
    signal_set = collect_learning_signals(get_current_org_id(), limit=limit)
    payload = describe_signal_set(signal_set)

    if detectorId or packId:
        key = similarity_key(detectorId, packId)
        matches = signal_set.similar_to(key)
        payload["similarTo"] = {
            "key": key.to_dict(),
            "signals": [
                {**signal.to_dict(), "similarityScore": round(score, 4)}
                for signal, score in matches
            ],
        }
    return payload


@router.get("/config", dependencies=[Depends(require_role("analyst"))])
def get_learning_config(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    """The weighting in force, and how well-founded each part of it is.

    Exposed because A3's whole discipline is that learning must never become
    invisible drift: a customer asking "why is this ranked here?" is entitled to
    see the weights, and — via each section's ``basis`` — to see that most of
    them are still provisional first guesses rather than measured values.
    """
    config = load_config()
    return {
        "configVersion": config.config_version,
        "configurationScope": config.configuration_scope,
        "outcomeSignals": {
            name: {"weight": s.weight, "direction": s.direction}
            for name, s in sorted(config.outcome_signals.items())
        },
        "decisionSignals": {
            name: {"weight": s.weight, "direction": s.direction}
            for name, s in sorted(config.decision_signals.items())
        },
        "deferReasons": dict(sorted(config.defer_reasons.items())),
        "comparability": dict(sorted(config.comparability.items())),
        "confounders": {
            "materialCaveatMultiplier": config.material_caveat_multiplier,
            "advisoryCaveatMultiplier": config.advisory_caveat_multiplier,
        },
        "recency": {
            "halfLifeDays": config.recency.half_life_days,
            "floor": config.recency.floor,
        },
        "coldStart": {
            "activationPolicy": config.cold_start.activation_policy,
            "basis": config.basis_for("cold_start"),
            "minimumDecisions": config.cold_start.minimum_decisions,
            "minimumSignals": config.cold_start.minimum_signals,
            "minimumDistinctIdentities": config.cold_start.minimum_distinct_identities,
        },
        "similarity": {
            "sameDetectorSamePack": config.similarity.same_detector_same_pack,
            "sameDetectorOtherPack": config.similarity.same_detector_other_pack,
            "sameSignalConcept": config.similarity.same_signal_concept,
            "minimumScore": config.similarity.minimum_score,
        },
        "bases": dict(sorted(config.bases.items())),
    }


def register_learning_routes(app: FastAPI) -> None:
    if getattr(app.state, "learning_routes_registered", False):
        return
    # Startup-only schema safety net for a dev DB that has not run migration
    # 0036 — the same placement as ensure_entities_table(). Never per-request,
    # and it never raises: production is already provisioned and runs under a
    # role without CREATE.
    ensure_opportunity_feedback_table()
    path = "/api/learning/feedback/{opportunity_identity}"
    if path not in {getattr(route, "path", None) for route in app.routes}:
        app.include_router(router)
    app.state.learning_routes_registered = True


__all__ = ["register_learning_routes", "router"]
