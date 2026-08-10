"""
routes_entity_merges.py — Release 2.0-B2 T2: merged-entity provenance API.

  GET  /api/entities/{entity_id}/provenance  → every constituent source identity
                                                and the rule that merged each
  POST /api/entities/provenance              → the same, for many entities at once
  POST /api/entity-merges/apply              → apply what T1 and T3 authorised

AC2 is "a resolved entity exposes all constituent source identities and the rule
that resolved it". The provenance is stored on the survivor's ``metadata``, so it
already travels with every surface that returns an entity (notably
``GET /api/runs/{run_id}/entities``); these routes are the DIRECT read for a
caller that wants provenance without pulling whole entity rows — and the bulk
form exists because a finding view resolving provenance for every entity it
traverses must not issue one request per node.

Access. Gated at ``analyst``, matching the entity read surface
(``routes_entities``) — provenance names source systems and record ids, which is
the same sensitivity as the entities it describes. Org-scoped via
``get_current_org_id()``; an entity in another org 404s, indistinguishable from
one that does not exist.

The apply route is the deliberate seam between "decided" and "done": T1 decides
and writes nothing, T3 records a human answer and writes nothing to the graph, and
a merge only happens when someone (or a scheduled caller) invokes this. Wiring it
into the discovery run is an operational decision, not something this task should
make silently — and unmerge (T5) is what makes a merge reversible.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .entity_merge import (
    EntityMergeError,
    apply_org_merges,
    get_entity_provenance,
    provenance_for_entities,
)
from .middleware.tenancy import get_current_org_id
from .rbac import _get_user_id_from_token, require_role
from .security import require_auth

logger = logging.getLogger(__name__)

PROVENANCE_PATH = "/api/entities/{entity_id}/provenance"
BULK_PROVENANCE_PATH = "/api/entities/provenance"
APPLY_MERGES_PATH = "/api/entity-merges/apply"

#: A bulk provenance request is bounded: a finding traverses tens of entities, not
#: thousands, and an unbounded id list is an easy way to turn one request into a
#: full-table read.
MAX_BULK_ENTITY_IDS = 200

router = APIRouter(tags=["entity-merges"])


class BulkProvenanceRequest(BaseModel):
    entity_ids: List[str] = Field(default_factory=list, description="Entity ids to resolve.")


class ApplyMergesRequest(BaseModel):
    entity_types: Optional[List[str]] = Field(
        None,
        description="Entity types to resolve and merge. Defaults to every scannable type.",
    )
    include_confirmed: bool = Field(
        True,
        description="Also apply pairs a human confirmed in the Entity Matches review surface.",
    )


@router.get(
    PROVENANCE_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def get_provenance(entity_id: str) -> Dict[str, Any]:
    """2.0-B2 (T2 / AC2) — one entity's constituent identities and merge rules.

    An entity that was never merged still answers: it reports its own single
    identity with ``is_merged: false``. That is an honest "made of one thing"
    rather than an empty body a caller has to interpret as either "not merged" or
    "not found" — those two are different, and only the second is a 404.
    """
    org_id = get_current_org_id()
    provenance = get_entity_provenance(org_id, entity_id)
    if provenance is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return provenance.to_dict()


@router.post(
    BULK_PROVENANCE_PATH,
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def get_bulk_provenance(body: BulkProvenanceRequest) -> Dict[str, Any]:
    """Provenance for many entities in one round trip (the finding-view seam).

    Unknown ids are simply absent from the map rather than erroring the whole
    request: a finding may reference an entity that has since been removed, and
    that should degrade one node, not the whole view.
    """
    org_id = get_current_org_id()
    ids = [i for i in (body.entity_ids or []) if str(i or "").strip()]
    if len(ids) > MAX_BULK_ENTITY_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"at most {MAX_BULK_ENTITY_IDS} entity ids per request",
        )
    resolved = provenance_for_entities(org_id, ids)
    return {
        "provenance": {eid: p.to_dict() for eid, p in resolved.items()},
        "requested": len(ids),
        "resolved": len(resolved),
    }


@router.post(
    APPLY_MERGES_PATH,
    # require_auth is listed explicitly (not left implicit via require_role's own
    # Depends(require_auth)) so the full signature/expiry/blocklist check is a
    # declared dependency of this write route, matching the read routes and
    # surviving any future refactor of require_role.
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def apply_merges(
    body: ApplyMergesRequest,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Apply every merge T1 and T3 have authorised for this org.

    Auto-merge tiers (explicit cross-reference, org alias table) plus — unless
    ``include_confirmed`` is false — the pairs a human confirmed in the review
    surface. Idempotent: a pair already merged is reported as ``already_merged``
    and is NOT written again. Each applied merge emits its own audit event.
    """
    org_id = get_current_org_id()
    actor = _get_user_id_from_token(token)
    try:
        report = apply_org_merges(
            org_id,
            entity_types=body.entity_types,
            actor=actor,
            include_confirmed=body.include_confirmed,
        )
    except EntityMergeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return report.to_dict()


def register_entity_merge_routes(app: FastAPI) -> None:
    """Register the merge-provenance routes once for the provided FastAPI app."""
    if getattr(app.state, "entity_merge_routes_registered", False):
        return
    existing = {getattr(route, "path", None) for route in app.routes}
    if PROVENANCE_PATH in existing:
        app.state.entity_merge_routes_registered = True
        return
    app.include_router(router)
    app.state.entity_merge_routes_registered = True
