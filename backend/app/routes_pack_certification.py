"""Pack certification review API — 2.0-C2 T2 (AT-832).

Three endpoints:

    GET  /api/packs/certification/criteria           — the review checklist (viewer+)
    POST /api/packs/{pack_id}/certification/reviews  — record a review     (owner)
    GET  /api/packs/{pack_id}/certification/reviews  — the review trail    (analyst+)

Role rationale
--------------
Mirrors the pack-state routes deliberately. Reading the checklist and the trail is
open to viewer/analyst — a reader looking at a Certified badge must be able to see
what "Certified" was checked against, or the badge is just a word. RECORDING a
review is ``owner``: it puts a named person's decision on a permanent, undeletable
trail, which is an ownership-level act.

What this API cannot do
-----------------------
It cannot make a pack Certified. Recording an approval produces the declaration and
the canonical payload to be signed **offline** with the release key; the badge only
changes when that signature ships and AT-831's verification accepts it. Any route
here that could change an effective level would move the trust root from the signing
key into this database, which is the exact hole the signature exists to close — so
the response returns signing material, never a level change.

Org scoping and attribution
---------------------------
Org comes from ``get_current_org_id()`` and the reviewer from the bearer token; a
request body carries neither, so a review can never be attributed to somebody else
or written into another tenant's trail. The reviewed pack version, platform version,
and date are stamped server-side for the same reason.

Nothing here deletes or edits anything. Re-reviewing appends the next revision.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .middleware.audit import PACK_CERTIFICATION_REVIEWED, log_event
from .middleware.tenancy import get_current_org_id
from .pack_certification_review import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    PackCertificationReview,
    ReviewDecisionError,
    ReviewValidationError,
    pack_review_history,
    record_certification_review,
    review_view,
)
from .pack_state import PackNotFound
from .rbac import _get_user_id_from_token, require_role
from .security import require_auth
from discovery.packs.certification_criteria import (
    REQUIRED_CRITERION_IDS,
    criteria_catalog,
)
from discovery.packs.pack_certification import LEVEL_CERTIFIED, LEVEL_PARTNER
from discovery.packs.platform_capabilities import get_platform_version

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/packs", tags=["packs"])


class CriterionVerdict(BaseModel):
    """One checklist verdict.

    ``not_applicable`` requires a note — "this did not apply" is a judgement a later
    auditor has to be able to read, and an unexplained N/A is indistinguishable from
    a skipped item.
    """

    criterionId: str = Field(
        description="Criterion id from GET /api/packs/certification/criteria."
    )
    outcome: Literal["pass", "fail", "not_applicable"] = Field(
        description="The verdict. A required criterion must pass for approval."
    )
    note: Optional[str] = Field(
        default=None,
        max_length=4000,
        description=(
            "Reviewer's note. Required when outcome is 'not_applicable'; "
            "recommended on any 'fail'."
        ),
    )


class CertificationReviewRequest(BaseModel):
    """A checklist-driven certification review.

    The reviewer, the pack version reviewed, the platform version, and the date are
    all recorded server-side and are deliberately absent from this body — a trail
    that lets the caller supply them is not an audit trail.
    """

    proposedLevel: Literal["certified", "partner"] = Field(
        description=(
            "The level this review would grant. 'community' is self-declared and "
            "needs no review, so it is not a valid outcome here."
        )
    )
    decision: Literal["approved", "rejected"] = Field(
        description=(
            "Approval requires a passing verdict on every required criterion; a "
            "rejection requires notes."
        )
    )
    criteria: List[CriterionVerdict] = Field(
        min_length=1,
        description="The checklist verdicts, in the order the reviewer worked.",
    )
    scopeSummary: str = Field(
        max_length=1000,
        description=(
            "What this certification covers, in one sentence. Becomes the signed "
            "scope.summary on an approval."
        ),
    )
    reviewerName: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Display name of the reviewer, for the trail. Optional.",
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=4000,
        description="Free-text review notes. REQUIRED when rejecting.",
    )


def _pack_not_found(pack_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown pack '{pack_id}'")


@router.get(
    "/certification/criteria",
    dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
    summary="The certification review checklist",
)
def get_certification_criteria() -> Dict[str, Any]:
    """The criteria a pack is reviewed against before CloudFulcrum signs it.

    Viewer-readable on purpose: someone looking at a Certified badge must be able to
    see what was actually checked, otherwise the badge is an unfalsifiable claim.
    """
    return {
        "platformVersion": get_platform_version(),
        "requiredCriteria": list(REQUIRED_CRITERION_IDS),
        "criteria": criteria_catalog(),
        "levels": [LEVEL_CERTIFIED, LEVEL_PARTNER],
        "decisions": [DECISION_APPROVED, DECISION_REJECTED],
    }


@router.post(
    "/{pack_id}/certification/reviews",
    status_code=201,
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    summary="Record a certification review of a pack",
)
def post_certification_review(
    pack_id: str,
    body: CertificationReviewRequest,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Append a checklist-driven review to the pack's permanent trail.

    Recording an APPROVAL does not make the pack Certified. The response carries
    ``certificationDeclaration`` and ``canonicalPayload`` — the exact metadata and
    exact bytes for the release key holder to sign offline — so what is signed is
    provably what was approved. The badge changes only once that signature ships.

    **400** when the checklist is malformed (unknown criterion, duplicate verdict,
    unexplained ``not_applicable``). **409** when the decision contradicts the
    checklist (approved with a required criterion missing or failed, or rejected
    with no reason). **404** for an unregistered pack.
    """
    org_id = get_current_org_id()
    actor_id = _get_user_id_from_token(token)
    try:
        review = record_certification_review(
            org_id,
            pack_id,
            reviewer_id=actor_id,
            proposed_level=body.proposedLevel,
            decision=body.decision,
            criteria=[verdict.model_dump() for verdict in body.criteria],
            scope_summary=body.scopeSummary,
            reviewer_name=body.reviewerName,
            notes=body.notes,
        )
    except PackNotFound:
        raise _pack_not_found(pack_id)
    except ReviewDecisionError as exc:
        # The submission is well-formed; the DECISION is unsupported by it. 409
        # rather than 400, matching the version-rollback route's use of conflict for
        # "understood, but not a state this platform will record".
        raise HTTPException(status_code=409, detail=str(exc))
    except ReviewValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    log_event(
        PACK_CERTIFICATION_REVIEWED,
        org_id=org_id,
        user_id=actor_id,
        pack_id=review.pack_id,
        pack_version=review.pack_version,
        revision=review.revision,
        proposed_level=review.proposed_level,
        decision=review.decision,
        criteria=review.criteria_ids,
        reviewed_at=review.reviewed_at,
    )
    _record_review_telemetry(review)
    return review.as_dict()


@router.get(
    "/{pack_id}/certification/reviews",
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
    summary="Append-only certification review trail for one pack",
)
def get_certification_reviews(pack_id: str) -> Dict[str, Any]:
    """Newest-first review trail, alongside the pack's live certification status.

    Both are returned deliberately. The trail is the recorded human decision; the
    ``certification`` block is the signature-verified badge (AT-831). An approved
    but unsigned pack therefore reads as "approved on 2026-07-31 / effective level:
    Community", which is the honest picture — showing only the review would let it
    read as Certified.
    """
    org_id = get_current_org_id()
    from discovery.packs.pack_config import PACK_REGISTRY

    if pack_id not in PACK_REGISTRY:
        # Unlike the lifecycle-history route, a removed pack is not served here: a
        # review trail is only reachable for a pack the registry still declares.
        # Reviews are never deleted, so the rows survive a removal and become
        # reachable again if the pack returns.
        raise _pack_not_found(pack_id)
    return review_view(org_id, pack_id)


def _record_review_telemetry(review: PackCertificationReview) -> None:
    """Mirror the decision into telemetry. Observability only.

    A telemetry failure must never fail a review that has already been persisted and
    audited. Ids and criterion ids only — the reviewer's free-text notes stay in the
    domain record.
    """
    from .telemetry import record_event

    try:
        record_event(
            "pack.certification_reviewed",
            {
                "org_id": review.org_id,
                "pack_id": review.pack_id,
                "pack_version": review.pack_version,
                "revision": review.revision,
                "reviewer_id": review.reviewer_id,
                "proposed_level": review.proposed_level,
                "decision": review.decision,
                "platform_version": review.platform_version,
                "passed_criteria": review.criteria_ids,
                "reviewed_at": review.reviewed_at,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "pack.certification_reviewed telemetry failed (non-blocking)",
            exc_info=True,
        )


def register_pack_certification_routes(app: FastAPI) -> None:
    """Attach the certification review routes exactly once (idempotent)."""
    existing = {getattr(route, "path", None) for route in app.routes}
    if "/api/packs/certification/criteria" in existing:
        return
    app.include_router(router)


__all__ = [
    "CertificationReviewRequest",
    "CriterionVerdict",
    "register_pack_certification_routes",
    "router",
]
