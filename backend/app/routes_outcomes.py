"""2.0-A2 T6 - read-only outcome surfaces."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query

from .middleware.tenancy import get_current_org_id
from .opportunity_lifecycle import ensure_opportunity_lifecycle_tables
from .opportunity_movement import ensure_opportunity_movement_table
from .opportunity_movement_record import (
    VERDICT_COMPARABLE,
    VERDICT_NOT_COMPARABLE,
    VERDICT_WEAK,
)
from .outcome_surfaces import (
    build_opportunity_outcome_view,
    build_outcome_portfolio_view,
)
from .projection_validation import PROJECTION_VALIDATION_VERDICTS
from .rbac import require_role
from .security import require_auth

router = APIRouter(prefix="/api/outcomes", tags=["outcomes"])

_COMPARABILITY_VERDICTS = (VERDICT_COMPARABLE, VERDICT_WEAK, VERDICT_NOT_COMPARABLE)
_CONFIDENCES = ("LOW", "MEDIUM", "HIGH")


def _validate_subset(name: str, values: Optional[List[str]], allowed: tuple[str, ...]) -> None:
    if not values:
        return
    unknown = sorted({value for value in values if value not in allowed})
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown {name} filter(s): {', '.join(unknown)}. "
                f"Valid: {', '.join(allowed)}"
            ),
        )


@router.get("", dependencies=[Depends(require_role("analyst"))])
def get_outcome_portfolio(
    comparabilityVerdict: Optional[List[str]] = Query(default=None),
    projectionVerdict: Optional[List[str]] = Query(default=None),
    pack: Optional[List[str]] = Query(default=None),
    detector: Optional[List[str]] = Query(default=None),
    confidence: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    _token: str = Depends(require_auth),
) -> dict:
    _validate_subset(
        "comparability verdict",
        comparabilityVerdict,
        _COMPARABILITY_VERDICTS,
    )
    _validate_subset(
        "projection verdict",
        projectionVerdict,
        PROJECTION_VALIDATION_VERDICTS,
    )
    if confidence:
        upper = [value.upper() for value in confidence]
        unknown = sorted({value for value in upper if value not in _CONFIDENCES})
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown confidence filter(s): {', '.join(unknown)}. "
                    f"Valid: {', '.join(_CONFIDENCES)}"
                ),
            )
        confidence = upper

    return build_outcome_portfolio_view(
        get_current_org_id(),
        comparability_verdicts=comparabilityVerdict,
        projection_verdicts=projectionVerdict,
        pack_ids=pack,
        detector_ids=detector,
        confidences=confidence,
        limit=limit,
    )


@router.get("/{opportunity_identity}", dependencies=[Depends(require_role("analyst"))])
def get_opportunity_outcome(
    opportunity_identity: str,
    limit: int = Query(default=200, ge=1, le=1000),
    _token: str = Depends(require_auth),
) -> dict:
    view = build_opportunity_outcome_view(
        get_current_org_id(),
        opportunity_identity,
        limit=limit,
    )
    if view is None:
        raise HTTPException(status_code=404, detail="outcome view not found")
    return view


def register_outcome_routes(app: FastAPI) -> None:
    if getattr(app.state, "outcome_routes_registered", False):
        return
    ensure_opportunity_lifecycle_tables()
    ensure_opportunity_movement_table()
    if "/api/outcomes/{opportunity_identity}" not in {
        getattr(route, "path", None) for route in app.routes
    }:
        app.include_router(router)
    app.state.outcome_routes_registered = True


__all__ = ["register_outcome_routes", "router"]
