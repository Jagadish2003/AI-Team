"""2.0-B4 T2 — the normalised-concept mapping API (AC5's "visible to pack authors").

Four read-only endpoints:

* ``GET /api/concepts/contracts``   — the concept set and its versioned mapping
  contracts. What a pack author builds against (2.0-C3's vocabulary).
* ``GET /api/concepts/conformance`` — which connector supports which concept, and the
  mapper backing each claim.
* ``GET /api/concepts/gaps``        — the declared gaps: concept-level and field-level,
  inverted concept-first so "which sources can carry my detector, and what will be
  missing" is one request rather than thirteen.
* ``GET /api/concepts/connectors/{connector_id}`` — one connector's full position,
  including its outstanding work list.

Why these are unauthenticated-by-org rather than org-scoped
-----------------------------------------------------------
Every other read route in this app is org-scoped because it serves org DATA. These
serve the platform's own mapping contracts — the same answer for every tenant,
containing no customer data whatsoever (a connector id, a concept name, a mapper
module path, and prose written by us). So there is no org to scope to, and
``get_current_org_id()`` is deliberately not called: reading it would imply a tenancy
boundary that does not exist here and invite a future reader to add per-org filtering
to a global registry.

Auth is still required, and the floor is VIEWER rather than analyst: a pack author
reading the vocabulary they must build against is doing the least privileged thing in
the product, and putting the contract behind analyst would make the documentation
harder to reach than the data it describes.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel

from .rbac import require_role
from .security import require_auth

try:
    from discovery.concepts import conformance as conf
    from discovery.concepts.contracts import contract_summary
    from discovery.concepts.gaps import (
        concept_gap_report, connector_gap_report, gap_summary,
    )
    from discovery.concepts.mappers import registry_summary
except ModuleNotFoundError:  # pragma: no cover - project-root execution
    from backend.discovery.concepts import conformance as conf
    from backend.discovery.concepts.contracts import contract_summary
    from backend.discovery.concepts.gaps import (
        concept_gap_report, connector_gap_report, gap_summary,
    )
    from backend.discovery.concepts.mappers import registry_summary


router = APIRouter(
    prefix="/api/concepts",
    tags=["concepts"],
    dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
)


class ConceptContractsResponse(BaseModel):
    """The concept set, its versions, and the per-concept mapping contracts."""

    concept_set_version: int
    concepts: list
    contract_versions: Dict[str, int]
    breaking_change_rules: list
    contracts: Dict[str, Any]


class ConceptConformanceResponse(BaseModel):
    """Per-connector conformance, plus the mapper registry backing every claim."""

    concept_set_version: int
    connectors: Dict[str, Any]
    supported_by_concept: Dict[str, Any]
    gap_count: int
    stale_declarations: list
    mappers: Dict[str, Any]


class ConceptGapsResponse(BaseModel):
    """The declared-gap surface (AC5), concept-first and connector-first."""

    concept_set_version: int
    mapper_count: int
    concepts: Dict[str, Any]
    connectors: Dict[str, Any]
    concept_gap_count: int
    outstanding_count: int
    field_gap_count: int
    registry_behind_code: list


@router.get("/contracts", response_model=ConceptContractsResponse)
def get_concept_contracts() -> ConceptContractsResponse:
    """The versioned concept set and mapping contracts (2.0-B4 T1/AC1)."""
    return ConceptContractsResponse(**contract_summary())


@router.get("/conformance", response_model=ConceptConformanceResponse)
def get_concept_conformance() -> ConceptConformanceResponse:
    """Which connector conforms to which concept, and the mapper proving it."""
    payload = dict(conf.conformance_summary())
    payload["mappers"] = registry_summary()
    return ConceptConformanceResponse(**payload)


@router.get("/gaps", response_model=ConceptGapsResponse)
def get_concept_gaps() -> ConceptGapsResponse:
    """The declared gaps — the AC5 surface.

    Serves both orientations from one call because they answer different questions and
    a pack author usually needs both: ``concepts`` says which sources can carry a given
    concept and what is missing on each, ``connectors`` says what one source still owes.
    """
    return ConceptGapsResponse(**gap_summary())


@router.get("/connectors/{connector_id}")
def get_connector_conformance(connector_id: str) -> Dict[str, Any]:
    """One connector's full position, or 404 naming the declared connectors.

    404 rather than an empty document: an undeclared connector id is a caller mistake
    (usually a shipped-connector id that never got a declaration), and an empty
    response would read as "this connector supports nothing".
    """
    try:
        return connector_gap_report(connector_id)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no conformance declaration for {connector_id!r}; declared connectors "
                f"are {sorted(conf.CONFORMANCE)}"
            ),
        ) from None


@router.get("/by-concept")
def get_gaps_by_concept() -> Dict[str, Any]:
    """The concept-first view alone, for a caller that does not need the rest."""
    return concept_gap_report()


def register_concept_routes(app: FastAPI) -> None:
    """Attach the concept routes to the app exactly once (idempotent)."""
    if getattr(app.state, "concept_routes_registered", False):
        return
    existing_paths = {getattr(route, "path", None) for route in app.routes}
    if "/api/concepts/contracts" in existing_paths:
        app.state.concept_routes_registered = True
        return

    app.include_router(router)
    app.state.concept_routes_registered = True
