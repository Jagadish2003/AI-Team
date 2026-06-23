"""
routes_salesforce_products.py — ENG-IH-3 Sprint 9
Salesforce Cloud Product Declaration Endpoint

PATCH /api/connectors/salesforce/products

When a user connects Salesforce in Integration Hub, they declare which
Salesforce cloud products their workspace uses. This declaration is a
workspace-level fact — not a per-discovery choice. It persists in the
connector record and is read by the workspace catalog API (ENG-IH-1)
as salesforce.products.

Stack Builder Screen 2 (ENG-SB-1) reads salesforce.products from the
workspace catalog and pre-populates selectedSalesforceClouds — removing
the cloud expansion panel from Screen 2 entirely.

Supported product IDs (matching frontend SALESFORCE_CLOUDS in
YourSystemsScreen.tsx Sprint 7):
  salesforce_pss    — Public Sector Solutions / Benefits
  salesforce_sc     — Service Cloud
  salesforce_ncino  — nCino
  salesforce_fsc    — Financial Services Cloud
  salesforce_rc     — Revenue Cloud
  salesforce_hc     — Health Cloud

Validation:
  Only the 6 known product IDs are accepted.
  Unknown IDs are silently filtered out (forward-compatible).
  Empty list is valid — user can clear all product declarations.
  Requires Salesforce to be connected (status = connected).
  Returns 404 if Salesforce connector not found.
  Returns 400 if Salesforce is not connected.

Registration:
  register_salesforce_products_routes(app) — called from main.py.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from .db import org_connector_get, org_connector_set
from .middleware.tenancy import get_current_org_id
from .security import require_auth
from .rbac import require_role

# ── Known Salesforce product IDs ──────────────────────────────────────────────

SALESFORCE_PRODUCT_IDS = {
    "salesforce_pss",    # Public Sector Solutions / Benefits
    "salesforce_sc",     # Service Cloud
    "salesforce_ncino",  # nCino
    "salesforce_fsc",    # Financial Services Cloud
    "salesforce_rc",     # Revenue Cloud
    "salesforce_hc",     # Health Cloud
}

SALESFORCE_PRODUCT_LABELS: Dict[str, str] = {
    "salesforce_pss":   "Public Sector Solutions / Benefits",
    "salesforce_sc":    "Service Cloud",
    "salesforce_ncino": "nCino",
    "salesforce_fsc":   "Financial Services Cloud",
    "salesforce_rc":    "Revenue Cloud",
    "salesforce_hc":    "Health Cloud",
}

# ── Models ────────────────────────────────────────────────────────────────────

class SalesforceProductsBody(BaseModel):
    """
    Product declaration payload.
    products: list of Salesforce product IDs to declare for this workspace.
    Unknown IDs are filtered silently.
    Empty list clears all product declarations.
    """
    products: List[str]


class SalesforceProductsResponse(BaseModel):
    ok:       bool = True
    products: List[str]           # validated product IDs that were saved
    labels:   List[str]           # human-readable labels for each product


# ── Registration ──────────────────────────────────────────────────────────────

_salesforce_products_routes_registered = False


def register_salesforce_products_routes(app: FastAPI) -> None:
    """
    Register Salesforce product declaration endpoint.
    Idempotent — safe to call multiple times.
    Called from main.py after register_workspace_catalog_routes(app).
    """
    global _salesforce_products_routes_registered
    if _salesforce_products_routes_registered:
        return
    _salesforce_products_routes_registered = True

    @app.patch(
        "/api/connectors/salesforce/products",
        response_model=SalesforceProductsResponse,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
        summary="Declare Salesforce cloud products for this workspace",
        tags=["Integration Hub"],
    )
    def set_salesforce_products(
        body: SalesforceProductsBody,
    ) -> SalesforceProductsResponse:
        """
        Persist the Salesforce cloud product declaration for this workspace.

        Called from Integration Hub frontend after user connects Salesforce
        and selects which products their workspace uses.

        The workspace catalog API (ENG-IH-1) reads this as:
          salesforce.products: ["salesforce_pss", "salesforce_sc", ...]

        Stack Builder Screen 2 (ENG-SB-1) pre-populates selectedSalesforceClouds
        from the catalog — no cloud picker shown in Screen 2.

        Returns 404 if Salesforce connector not found in database.
        Returns 400 if Salesforce connector is not connected.

        Idempotent — calling twice with the same products overwrites with
        the same values. Calling with empty list clears the declaration.
        """
        org_id = get_current_org_id()
        connector = org_connector_get(org_id, "salesforce")
        if not connector:
            raise HTTPException(
                status_code=404,
                detail="Salesforce connector not found. Ensure the connector is initialised.",
            )

        # Validate connected status
        status = connector.get("status", "")
        if status not in ("connected", "live"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Salesforce connector is not connected (status: '{status}'). "
                    "Connect Salesforce before declaring products."
                ),
            )

        # Validate and filter product IDs
        validated = [p for p in body.products if p in SALESFORCE_PRODUCT_IDS]
        labels    = [SALESFORCE_PRODUCT_LABELS[p] for p in validated]

        # Persist to THIS org's connector record (never the shared catalog).
        connector["products"] = validated
        org_connector_set(org_id, "salesforce", connector)

        return SalesforceProductsResponse(
            products=validated,
            labels=labels,
        )

    @app.get(
        "/api/connectors/salesforce/products",
        response_model=SalesforceProductsResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="Get declared Salesforce cloud products for this workspace",
        tags=["Integration Hub"],
    )
    def get_salesforce_products() -> SalesforceProductsResponse:
        """
        Return the currently declared Salesforce cloud products.
        Used by Integration Hub frontend to show current selection state
        when the user returns to edit their product declaration.

        Returns empty products list if no declaration has been made.
        Returns 404 if Salesforce connector not found.
        """
        connector = org_connector_get(get_current_org_id(), "salesforce")
        if not connector:
            raise HTTPException(
                status_code=404,
                detail="Salesforce connector not found.",
            )

        products = connector.get("products", [])
        labels   = [SALESFORCE_PRODUCT_LABELS.get(p, p) for p in products]

        return SalesforceProductsResponse(
            products=products,
            labels=labels,
        )
