"""2.0-A3 T2 — the adjustment layer's read surface and its recomputation trigger.

Same spine as the sibling learning routes: org from the tenancy middleware,
never from the request; explicit ``require_auth`` on every route. Org-wide
governance surfaces (state, history, reset) require Owner. Run-scoped
preview/explain/base-order remain Analyst+ operational reads.

**The route that matters most is the base-order one.** ``GET /base-order`` answers
"what would this have ranked without learning?" — and it answers it by serving the
stored order, because the layer never wrote into the finding. That the endpoint
has nothing to undo is the whole point of building this as a layer.

Recomputation is a POST, deliberately: the state is a value computed on request,
not an expression evaluated whenever a page is served. A ranking that shifted
because someone opened a list would be the invisible drift A3 exists to prevent.

Reset and the full audit surface are T4: reset is Owner-only, appends history,
and emits the ranking-adjustment governance audit event.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from .learning_adjustment import adjust_ranking, base_order
from .learning_adjustment_state import (
    ensure_ranking_adjustment_tables,
    get_adjustment_history,
    get_adjustments,
    list_adjustment_state,
    recompute_adjustments,
    reset_adjustments,
)
from .learning_reason import describe_adjustment
from .learning_signal_config import load_config
from .learning_signals import collect_learning_signals
from .middleware.tenancy import get_current_org_id
from .rbac import _get_user_id_from_token, require_role
from .security import require_auth

router = APIRouter(prefix="/api/learning/adjustment", tags=["learning"])


class RecomputeRequest(BaseModel):
    """No body fields. The org comes from the tenancy middleware, never a payload."""


class ResetRequest(BaseModel):
    """Optional governance note for an Owner reset."""

    reason: Optional[str] = None


@router.get("", dependencies=[Depends(require_role("owner"))])
def get_adjustment_state(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    """The current adjustment state for this org, with its caps.

    Includes groups whose learning is INACTIVE (cold start), because a zero that
    means "not enough evidence yet" and a zero that means "learning arrived at
    neutral" are different facts and a reader must be able to tell them apart.

    Owner-only by design: this governs org-wide ranking behaviour, while the
    run-scoped preview/explain surfaces remain analyst-readable operational
    views.
    """
    org_id = get_current_org_id()
    config = load_config()
    policy = config.adjustment
    signal_set = collect_learning_signals(org_id)
    return {
        "orgId": org_id,
        "enabled": policy.enabled,
        "caps": {
            "maxScoreFraction": policy.max_score_fraction,
            "maxRankMove": policy.max_rank_move,
            "pointsPerSignalUnit": policy.points_per_signal_unit,
        },
        "configVersion": config.config_version,
        "learningState": signal_set.activation_state(),
        "groups": list_adjustment_state(org_id),
    }


@router.get("/history", dependencies=[Depends(require_role("owner"))])
def get_history(
    limit: int = Query(200, ge=1, le=1000),
    _token: str = Depends(require_auth),
) -> List[Dict[str, Any]]:
    """Every value this org's adjustments have held, newest first."""
    return get_adjustment_history(get_current_org_id(), limit=limit)


@router.post("/recompute", dependencies=[Depends(require_role("analyst"))])
def post_recompute(
    body: Optional[RecomputeRequest] = None,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Recompute this org's adjustment state from the current signal set.

    Explicit rather than automatic. Each group's prior value is carried into the
    append-only history first, so the sequence of values stays reconstructable.
    """
    try:
        return recompute_adjustments(
            get_current_org_id(), actor_id=_get_user_id_from_token(token)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/reset", dependencies=[Depends(require_role("owner"))])
def post_reset(
    body: Optional[ResetRequest] = None,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Reset the org's adjustment state to neutral.

    Reset is a governance action, so it is Owner-only. It appends reset history
    and emits the audit event after the current state has been neutralised.
    """
    payload = body or ResetRequest()
    try:
        return reset_adjustments(
            get_current_org_id(),
            actor_id=_get_user_id_from_token(token),
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/preview/{run_id}", dependencies=[Depends(require_role("analyst"))])
def preview_adjustment(
    run_id: str,
    _token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """What the layer would do to one run's findings, and why.

    The inspection surface: every finding's base rank, its adjusted rank, the
    delta learning asked for, the delta that survived the cap, and — when they
    differ — which cap bound it. A clipped adjustment means the learned signal
    and the base scorer are in genuine tension, which is worth showing rather
    than quietly resolving.
    """
    from .db import run_kv_get
    from .run_store import read_run

    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

    opps = run_kv_get("opps", run_id, None) or []
    org_id = get_current_org_id()
    signal_set = collect_learning_signals(org_id)
    result = adjust_ranking(
        opps,
        get_adjustments(org_id),
        is_active=signal_set.is_active,
        inactive_reason=signal_set.inactive_reason,
    )
    payload = result.to_dict()
    payload["runId"] = run_id
    payload["learningActive"] = signal_set.is_active
    payload["inactiveReason"] = signal_set.inactive_reason
    payload["learningState"] = signal_set.activation_state()
    return payload


@router.get(
    "/explain/{run_id}/{opportunity_id}",
    dependencies=[Depends(require_role("analyst"))],
)
def explain_adjustment(
    run_id: str,
    opportunity_id: str,
    _token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """AC2 — why ONE finding moved, with links to every contributing signal.

    Returns the STRUCTURED reason (counts, verdicts, direction, magnitude, cap)
    plus a resolvable reference for each contributing decision and outcome. The
    human sentence is rendered from those same fields and travels as ``summary``,
    so a client never composes its own wording.

    A finding that did not move answers 404: there is no ordering change to
    explain, and returning an empty explanation would invite a UI to render
    "this was not adjusted because..." on every unadjusted finding.
    """
    from .db import run_kv_get
    from .run_store import read_run

    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

    opps = run_kv_get("opps", run_id, None) or []
    org_id = get_current_org_id()
    signal_set = collect_learning_signals(org_id)
    result = adjust_ranking(
        opps,
        get_adjustments(org_id),
        is_active=signal_set.is_active,
        inactive_reason=signal_set.inactive_reason,
    )

    record = result.by_opportunity_id().get(opportunity_id)
    if record is None or not record.moved:
        raise HTTPException(404, "no ranking adjustment for this opportunity")

    return {
        "runId": run_id,
        "opportunityId": opportunity_id,
        "opportunityIdentity": record.opportunity_identity,
        "baseRank": record.base_rank,
        "adjustedRank": record.adjusted_rank,
        "baseImpact": round(record.base_impact, 4),
        "caps": {
            "maxScoreFraction": result.policy.max_score_fraction if result.policy else None,
            "maxRankMove": result.policy.max_rank_move if result.policy else None,
        },
        "reason": describe_adjustment(record),
    }


@router.get("/base-order/{run_id}", dependencies=[Depends(require_role("analyst"))])
def get_base_order(
    run_id: str,
    _token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """"What would this have ranked without learning?"

    Answerable because the layer is a layer: it applies at serve time and never
    writes into a finding, so the stored order IS the base order and this
    endpoint has nothing to undo. Returns identifiers and base scores only — the
    question is about ORDER, and returning full findings here would create a
    second serving path for the same data that could drift from the first.
    """
    from .db import run_kv_get
    from .run_store import read_run

    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

    opps = base_order(run_kv_get("opps", run_id, None) or [])
    return {
        "runId": run_id,
        "count": len(opps),
        "order": [
            {
                "baseRank": index,
                "id": opp.get("id"),
                "opportunityIdentity": opp.get("opportunity_identity"),
                "title": opp.get("title"),
                "baseImpact": opp.get("impact"),
                "tier": opp.get("tier"),
            }
            for index, opp in enumerate(opps)
        ],
    }


def register_learning_adjustment_routes(app: FastAPI) -> None:
    if getattr(app.state, "learning_adjustment_routes_registered", False):
        return
    # Startup-only schema safety net for a dev DB that has not run migration
    # 0037 — the same placement as ensure_entities_table(). Never per-request,
    # and it never raises: production is already provisioned and runs under a
    # role without CREATE.
    ensure_ranking_adjustment_tables()
    path = "/api/learning/adjustment/recompute"
    if path not in {getattr(route, "path", None) for route in app.routes}:
        app.include_router(router)
    app.state.learning_adjustment_routes_registered = True


__all__ = ["register_learning_adjustment_routes", "router"]
