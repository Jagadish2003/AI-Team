"""
routes_sharepoint_sites.py — SharePoint site selection endpoint (multi-site).

GET   /api/connectors/sharepoint/sites
PATCH /api/connectors/sharepoint/sites

When a customer connects SharePoint in the Integration Hub, they choose WHICH
sites AgentIQ scopes discovery to — the SharePoint analogue of the Slack channel
selection (routes_slack_channels.py). Instead of reading every granted site, the
SharePoint ingestor reads ONLY the sites selected for that org — both the reach
document-library pass (``SharePointIngestor._accessible_libraries``) and the
R18-A5 deep-content pass (``sharepoint_content._accessible_sites``), which share
``SharePointIngestor._selected_site_ids``. The selection is a per-org,
workspace-level fact stored on the connector record and is editable later.

Behaviour of the saved selection (honoured in discovery.ingest.sharepoint):
  * No selection saved yet → the ingestor reads every granted site (default).
  * A selection saved → the ingestor reads ONLY those sites; empty means none.

Validation:
  Only ids among the currently-selectable sites are accepted; unknown ids are
  filtered out silently. Requires SharePoint to be connected for the write.
  404 if the connector is not found, 503 if the site list is temporarily
  unavailable (saved selection preserved).

Registration:
  register_sharepoint_sites_routes(app) — called from main.py.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from .db import org_connector_get, org_connector_set
from .middleware.tenancy import get_current_org_id
from .security import require_auth
from .rbac import require_role

logger = logging.getLogger(__name__)


# ── Models ────────────────────────────────────────────────────────────────────

class SharePointSite(BaseModel):
    id: str
    name: str


class SharePointSitesBody(BaseModel):
    """Site selection payload — the site ids AgentIQ reads for this org. Unknown
    ids are filtered silently. An empty list means read no sites."""
    sites: List[str]


class SharePointSitesResponse(BaseModel):
    ok:         bool = True
    available:  List[SharePointSite]   # sites the customer can choose from
    selected:   List[str]              # saved selection (site ids)
    configured: bool                   # whether a selection has been saved


# ── Helpers ───────────────────────────────────────────────────────────────────

def _selectable_sites(org_id: str) -> List[Dict[str, str]]:
    """Granted sites the customer chooses from. Selection filtering is NOT applied
    here. A failure is surfaced (503) rather than an empty list, so PATCH never
    validates a legitimate id away during an outage."""
    try:
        from discovery.ingest.sharepoint import list_selectable_sites
        return list_selectable_sites(org_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sharepoint sites: could not list selectable sites: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "SharePoint sites are temporarily unavailable. "
                "Your saved site selection was not changed."
            ),
        ) from exc


def _saved_selection(connector: Dict[str, Any]) -> "tuple[List[str], bool]":
    """Return (selected ids, configured?). configured is True once a selection
    (the ``sites`` list) has been saved."""
    sites = connector.get("sites")
    if isinstance(sites, list):
        return [str(s) for s in sites], True
    return [], False


# ── Registration ──────────────────────────────────────────────────────────────

_sharepoint_sites_routes_registered = False


def register_sharepoint_sites_routes(app: FastAPI) -> None:
    """Register the SharePoint site-selection endpoints. Idempotent."""
    global _sharepoint_sites_routes_registered
    if _sharepoint_sites_routes_registered:
        return
    _sharepoint_sites_routes_registered = True

    @app.get(
        "/api/connectors/sharepoint/sites",
        response_model=SharePointSitesResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="List selectable SharePoint sites and the saved selection",
        tags=["Integration Hub"],
    )
    def get_sharepoint_sites() -> SharePointSitesResponse:
        connector = org_connector_get(get_current_org_id(), "sharepoint")
        if not connector:
            raise HTTPException(status_code=404, detail="SharePoint connector not found.")

        # Degrade a listing failure (e.g. Graph permission not granted) to an empty
        # option list so the panel renders its guidance instead of a 503; the saved
        # selection is still returned unchanged.
        try:
            available = _selectable_sites(get_current_org_id())
        except HTTPException:
            available = []
        selected, configured = _saved_selection(connector)
        return SharePointSitesResponse(
            available=[SharePointSite(**s) for s in available],
            selected=selected,
            configured=configured,
        )

    @app.patch(
        "/api/connectors/sharepoint/sites",
        response_model=SharePointSitesResponse,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
        summary="Select which SharePoint sites AgentIQ reads for this workspace",
        tags=["Integration Hub"],
    )
    def set_sharepoint_sites(body: SharePointSitesBody) -> SharePointSitesResponse:
        org_id = get_current_org_id()
        connector = org_connector_get(org_id, "sharepoint")
        if not connector:
            raise HTTPException(status_code=404, detail="SharePoint connector not found.")

        status = connector.get("status", "")
        if status not in ("connected", "live"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"SharePoint connector is not connected (status: '{status}'). "
                    "Connect SharePoint before selecting sites."
                ),
            )

        available = _selectable_sites(org_id)
        available_ids = {s["id"] for s in available}
        validated: List[str] = []
        for sid in body.sites:
            sid = str(sid).strip()
            if sid in available_ids and sid not in validated:
                validated.append(sid)

        connector["sites"] = validated
        org_connector_set(org_id, "sharepoint", connector)

        return SharePointSitesResponse(
            available=[SharePointSite(**s) for s in available],
            selected=validated,
            configured=True,
        )
