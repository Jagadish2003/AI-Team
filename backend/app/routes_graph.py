"""ENT-4 / T3-S14-A graph API routes.

All routes are Analyst+ and org-scoped through tenancy context. The request
body and query string are never trusted for org_id; callers may ask for graph
objects by id, but ownership is checked only against get_current_org_id().
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import db
from .graph_query import (
    GraphPathNode,
    GraphTraversalNode,
    entity_exists,
    entity_neighbourhood,
    entity_path,
    opportunity_neighbourhood,
    org_graph_summary,
)
from .middleware.tenancy import get_current_org_id
from .rbac import require_role
from .security import require_auth


router = APIRouter(
    prefix="/api/graph",
    tags=["graph"],
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)


class GraphNeighbourhoodResponse(BaseModel):
    nodes: List[GraphTraversalNode] = Field(default_factory=list)
    node_count: int
    max_depth: int
    include_inferred: bool


class OpportunityNeighbourhoodResponse(GraphNeighbourhoodResponse):
    opportunity_id: str
    seed_entity_ids: List[str] = Field(default_factory=list)


class EntityNeighbourhoodResponse(GraphNeighbourhoodResponse):
    entity_id: str


class GraphPathResponse(BaseModel):
    from_entity_id: str
    to_entity_id: str
    max_depth: int
    path: List[GraphPathNode] = Field(default_factory=list)
    path_found: bool


class OrgGraphSummaryResponse(BaseModel):
    org_id: str
    entity_counts_by_type: Dict[str, int]
    relationship_counts_by_type: Dict[str, int]
    top_entities_by_edge_count: List[Dict[str, Any]]


def _run_sort_key(run: Dict[str, Any]) -> tuple[str, str]:
    return (
        str(run.get("updatedAt") or run.get("startedAt") or ""),
        str(run.get("id") or run.get("runId") or ""),
    )


def _opp_exists(run_id: str, opp_id: str) -> bool:
    opps = db.run_kv_get("opps", run_id, []) or []
    if any(isinstance(opp, dict) and opp.get("id") == opp_id for opp in opps):
        return True

    enrichment = db.run_kv_get("llm_enrichment", run_id, None)
    per_opp = enrichment.get("perOpportunity", {}) if isinstance(enrichment, dict) else {}
    return opp_id in per_opp


def _entity_ids_from_items(items: Any) -> List[str]:
    ids: List[str] = []
    if not isinstance(items, list):
        return ids
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("resolution_status") not in (None, "resolved"):
            continue
        entity_id = item.get("entity_id") or item.get("id")
        if entity_id:
            ids.append(str(entity_id))
    return ids


def _seed_entity_ids_for_opportunity(org_id: str, opp_id: str) -> List[str] | None:
    """Return seed entity ids for an org-owned opportunity, or None if absent.

    ENT4's document says opportunity graph traversal seeds from
    OppEnrichment.entities. Current storage keeps opportunity rows in run KV,
    and entity summaries are run-scoped, so this helper finds the newest
    org-visible run containing the opportunity and uses its resolved entity
    summaries as the seed set. It never scans runs outside the current org.
    """
    runs = sorted(db.tenancy_get_runs(org_id), key=_run_sort_key, reverse=True)
    for run in runs:
        run_id = str(run.get("id") or "")
        if not run_id or not _opp_exists(run_id, opp_id):
            continue

        enrichment = db.run_kv_get("llm_enrichment", run_id, None)
        per_opp = enrichment.get("perOpportunity", {}) if isinstance(enrichment, dict) else {}
        opp_enrichment = per_opp.get(opp_id) if isinstance(per_opp, dict) else None
        if isinstance(opp_enrichment, dict):
            seed_ids = _entity_ids_from_items(opp_enrichment.get("entities"))
            if seed_ids:
                return seed_ids

        return _entity_ids_from_items(db.run_kv_get("entities", run_id, []) or [])
    return None


@router.get(
    "/opportunity/{opp_id}/neighbourhood",
    response_model=OpportunityNeighbourhoodResponse,
)
def get_opportunity_neighbourhood(
    opp_id: str,
    max_depth: int = Query(2, ge=0, le=5),
    include_inferred: bool = Query(False),
) -> OpportunityNeighbourhoodResponse:
    org_id = get_current_org_id()
    seed_ids = _seed_entity_ids_for_opportunity(org_id, opp_id)
    if seed_ids is None:
        raise HTTPException(status_code=404, detail="opportunity not found")

    nodes = opportunity_neighbourhood(
        org_id=org_id,
        seed_entity_ids=seed_ids,
        max_depth=max_depth,
        include_inferred=include_inferred,
    )
    return OpportunityNeighbourhoodResponse(
        opportunity_id=opp_id,
        seed_entity_ids=seed_ids,
        nodes=nodes,
        node_count=len(nodes),
        max_depth=max_depth,
        include_inferred=include_inferred,
    )


@router.get(
    "/entity/{entity_id}/neighbourhood",
    response_model=EntityNeighbourhoodResponse,
)
def get_entity_neighbourhood(
    entity_id: str,
    max_depth: int = Query(2, ge=0, le=5),
    include_inferred: bool = Query(False),
) -> EntityNeighbourhoodResponse:
    org_id = get_current_org_id()
    if not entity_exists(org_id, entity_id):
        raise HTTPException(status_code=404, detail="entity not found")

    nodes = entity_neighbourhood(
        org_id=org_id,
        entity_id=entity_id,
        max_depth=max_depth,
        include_inferred=include_inferred,
    )
    return EntityNeighbourhoodResponse(
        entity_id=entity_id,
        nodes=nodes,
        node_count=len(nodes),
        max_depth=max_depth,
        include_inferred=include_inferred,
    )


@router.get("/path", response_model=GraphPathResponse)
def get_graph_path(
    from_entity_id: str = Query(...),
    to_entity_id: str = Query(...),
    max_depth: int = Query(5, ge=0, le=5),
) -> GraphPathResponse:
    org_id = get_current_org_id()
    if not entity_exists(org_id, from_entity_id) or not entity_exists(org_id, to_entity_id):
        raise HTTPException(status_code=404, detail="entity not found")

    path = entity_path(
        org_id=org_id,
        from_entity_id=from_entity_id,
        to_entity_id=to_entity_id,
        max_depth=max_depth,
    )
    return GraphPathResponse(
        from_entity_id=from_entity_id,
        to_entity_id=to_entity_id,
        max_depth=max_depth,
        path=path,
        path_found=bool(path),
    )


@router.get("/org/summary", response_model=OrgGraphSummaryResponse)
def get_org_summary() -> OrgGraphSummaryResponse:
    org_id = get_current_org_id()
    return OrgGraphSummaryResponse(**org_graph_summary(org_id))


def register_graph_routes(app: FastAPI) -> None:
    """Register graph routes once for the provided FastAPI app."""
    if getattr(app.state, "graph_routes_registered", False):
        return

    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if "/api/graph/org/summary" in existing_paths:
        app.state.graph_routes_registered = True
        return

    app.include_router(router)
    app.state.graph_routes_registered = True
