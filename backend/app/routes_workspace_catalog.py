"""Workspace catalog API for Sprint 9 ENG-IH-1."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from .db import get_all
from .security import require_auth


WORKSPACE_CATALOG_PATH = "/api/integration-hub/workspace-catalog"

CATEGORY_KEYS = [
    "primary_platforms",
    "operational_systems",
    "comms_knowledge",
    "data_engineering",
]

SYSTEM_CATEGORY: Dict[str, str] = {
    "salesforce": "primary_platforms",
    "sap": "primary_platforms",
    "oracle_ebs": "primary_platforms",
    "workday": "primary_platforms",
    "dynamics365": "primary_platforms",
    "jira": "operational_systems",
    "servicenow": "operational_systems",
    "azure_devops": "operational_systems",
    "linear": "operational_systems",
    "zendesk": "operational_systems",
    "slack": "comms_knowledge",
    "teams": "comms_knowledge",
    "confluence": "comms_knowledge",
    "sharepoint": "comms_knowledge",
    "notion": "comms_knowledge",
    "github": "data_engineering",
    "gitlab": "data_engineering",
    "bitbucket": "data_engineering",
    "azure_repos": "data_engineering",
    "postgresql": "data_engineering",
    "sql_server": "data_engineering",
    "oracle_db": "data_engineering",
    "databricks": "data_engineering",
    "snowflake": "data_engineering",
    "dbt": "data_engineering",
}

PRESENT_STATUSES = {"connected", "needs_auth"}

_catalog_routes_registered = False


class CatalogSystemItem(BaseModel):
    system_id: str
    name: str
    status: str
    products: List[str] = Field(default_factory=list)


class WorkspaceCatalogResponse(BaseModel):
    primary_platforms: List[CatalogSystemItem]
    operational_systems: List[CatalogSystemItem]
    comms_knowledge: List[CatalogSystemItem]
    data_engineering: List[CatalogSystemItem]
    missing_categories: List[str]


def _normalise_status(status: Any) -> str:
    raw = str(status or "not_configured").strip().lower()

    if raw in {"connected", "live", "fixture"}:
        return "connected"
    if raw in {"needs_auth", "error"}:
        return "needs_auth"
    if raw in {"not_configured", "not_connected", "disconnected", "coming_soon"}:
        return "not_configured"

    return "not_configured"


def _products_from_connector(system_id: str, connector: Dict[str, Any]) -> List[str]:
    if system_id != "salesforce":
        return []

    products = connector.get("products", [])
    if not isinstance(products, list):
        return []
    return [product for product in products if isinstance(product, str)]


def build_workspace_catalog(
    connectors: List[Dict[str, Any]],
) -> WorkspaceCatalogResponse:
    buckets: Dict[str, List[CatalogSystemItem]] = {
        category: [] for category in CATEGORY_KEYS
    }

    for connector in connectors:
        system_id = str(connector.get("id") or connector.get("system_id") or "")
        if not system_id:
            continue

        category = SYSTEM_CATEGORY.get(system_id)
        if category is None:
            continue

        status = _normalise_status(connector.get("status"))
        if status == "not_configured":
            continue

        buckets[category].append(
            CatalogSystemItem(
                system_id=system_id,
                name=str(connector.get("name") or system_id),
                status=status,
                products=_products_from_connector(system_id, connector),
            )
        )

    missing_categories = [
        category
        for category in CATEGORY_KEYS
        if not any(item.status in PRESENT_STATUSES for item in buckets[category])
    ]

    return WorkspaceCatalogResponse(
        primary_platforms=buckets["primary_platforms"],
        operational_systems=buckets["operational_systems"],
        comms_knowledge=buckets["comms_knowledge"],
        data_engineering=buckets["data_engineering"],
        missing_categories=missing_categories,
    )


def register_workspace_catalog_routes(app: FastAPI) -> None:
    """Register the workspace catalog endpoint once for a FastAPI app."""

    global _catalog_routes_registered
    route_exists = any(
        getattr(route, "path", None) == WORKSPACE_CATALOG_PATH
        for route in app.routes
    )
    if route_exists:
        _catalog_routes_registered = True
        return

    _catalog_routes_registered = True

    @app.get(
        WORKSPACE_CATALOG_PATH,
        response_model=WorkspaceCatalogResponse,
        dependencies=[Depends(require_auth)],
        summary="Workspace source catalog grouped by capability category",
        tags=["Integration Hub"],
    )
    def get_workspace_catalog() -> WorkspaceCatalogResponse:
        return build_workspace_catalog(get_all("connectors"))
