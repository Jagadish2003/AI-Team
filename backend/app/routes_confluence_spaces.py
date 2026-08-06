"""
routes_confluence_spaces.py — Confluence space selection endpoint (multi-space).

GET   /api/connectors/confluence/spaces
PATCH /api/connectors/confluence/spaces

When a customer connects Confluence in the Integration Hub, they choose WHICH
spaces AgentIQ scopes discovery to — the Confluence analogue of the Slack channel
selection (routes_slack_channels.py). Instead of reading every granted space, the
Confluence ingestor reads ONLY the spaces selected for that org (both the reach
signal pass and the R18-A5 deep-content pass, which share
``ConfluenceIngestor._accessible_spaces``). The selection is a per-org,
workspace-level fact stored on the connector record and is editable later.

GET returns the spaces the customer can choose from (granted, non-archived spaces
visible to the connected Confluence credential) plus the current saved selection.
PATCH persists the selection.

Behaviour of the saved selection (honoured in discovery.ingest.confluence):
  * No selection saved yet (``configured == False``) → the ingestor reads every
    granted space (backwards-compatible default). The UI pre-selects all.
  * A selection saved (``configured == True``) → the ingestor reads ONLY those
    spaces. An empty saved list means read nothing.

Validation:
  Only keys among the currently-selectable spaces are accepted; unknown keys are
  filtered out silently. Requires Confluence to be connected for the write.
  Returns 404 if the connector is not found, 503 if the space list is temporarily
  unavailable (saved selection preserved).

Registration:
  register_confluence_spaces_routes(app) — called from main.py.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from .db import org_connector_get, org_connector_set
from .middleware.tenancy import get_current_org_id
from .security import require_auth
from .connector_scope_audit import audit_scope_selection
from .rbac import _get_user_id_from_token, require_role

logger = logging.getLogger(__name__)


# ── Models ────────────────────────────────────────────────────────────────────

class ConfluenceSpace(BaseModel):
    key: str
    name: str


class ConfluenceSpacesBody(BaseModel):
    """Space selection payload — the space keys AgentIQ reads for this org.
    Unknown keys are filtered silently. An empty list means read no spaces."""
    spaces: List[str]


class ConfluenceSpacesResponse(BaseModel):
    ok:         bool = True
    available:  List[ConfluenceSpace]   # spaces the customer can choose from
    selected:   List[str]               # saved selection (space keys)
    configured: bool                    # whether a selection has been saved


# ── Helpers ───────────────────────────────────────────────────────────────────

def _selectable_spaces(org_id: str) -> List[Dict[str, str]]:
    """Granted, non-archived spaces the customer chooses from. Selection filtering
    is deliberately NOT applied here. A failure is surfaced (503) rather than an
    empty list, so PATCH never validates a legitimate key away during an outage."""
    try:
        from discovery.ingest.confluence import list_selectable_spaces
        return list_selectable_spaces(org_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("confluence spaces: could not list selectable spaces: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "Confluence spaces are temporarily unavailable. "
                "Your saved space selection was not changed."
            ),
        ) from exc


def _saved_selection(connector: Dict[str, Any]) -> "tuple[List[str], bool]":
    """Return (selected keys, configured?). configured is True once a selection
    (the ``spaces`` list) has been saved."""
    spaces = connector.get("spaces")
    if isinstance(spaces, list):
        return [str(s) for s in spaces], True
    return [], False


# ── Registration ──────────────────────────────────────────────────────────────

_confluence_spaces_routes_registered = False


def register_confluence_spaces_routes(app: FastAPI) -> None:
    """Register the Confluence space-selection endpoints. Idempotent."""
    global _confluence_spaces_routes_registered
    if _confluence_spaces_routes_registered:
        return
    _confluence_spaces_routes_registered = True

    @app.get(
        "/api/connectors/confluence/spaces",
        response_model=ConfluenceSpacesResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="List selectable Confluence spaces and the saved selection",
        tags=["Integration Hub"],
    )
    def get_confluence_spaces() -> ConfluenceSpacesResponse:
        connector = org_connector_get(get_current_org_id(), "confluence")
        if not connector:
            raise HTTPException(status_code=404, detail="Confluence connector not found.")

        # Degrade a listing failure (e.g. Confluence auth not ready) to an empty
        # option list so the panel renders its "no spaces available" guidance
        # instead of a 503; the saved selection is still returned unchanged.
        try:
            available = _selectable_spaces(get_current_org_id())
        except HTTPException:
            available = []
        selected, configured = _saved_selection(connector)
        return ConfluenceSpacesResponse(
            available=[ConfluenceSpace(**s) for s in available],
            selected=selected,
            configured=configured,
        )

    @app.patch(
        "/api/connectors/confluence/spaces",
        response_model=ConfluenceSpacesResponse,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
        summary="Select which Confluence spaces AgentIQ reads for this workspace",
        tags=["Integration Hub"],
    )
    def set_confluence_spaces(body: ConfluenceSpacesBody, token: str = Depends(require_auth)) -> ConfluenceSpacesResponse:
        org_id = get_current_org_id()
        connector = org_connector_get(org_id, "confluence")
        if not connector:
            raise HTTPException(status_code=404, detail="Confluence connector not found.")

        status = connector.get("status", "")
        if status not in ("connected", "live"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Confluence connector is not connected (status: '{status}'). "
                    "Connect Confluence before selecting spaces."
                ),
            )

        available = _selectable_spaces(org_id)
        available_keys = {s["key"] for s in available}
        validated: List[str] = []
        for key in body.spaces:
            key = str(key).strip()
            if key in available_keys and key not in validated:
                validated.append(key)

        previous_scope = connector.get("spaces")
        connector["spaces"] = validated
        org_connector_set(org_id, "confluence", connector)
        # 2.0-D4 T1 (AC1): scope pin/unpin is a data-access grant.
        audit_scope_selection(
            connector_id="confluence",
            scope_key="spaces",
            previous=previous_scope,
            selected=validated,
            actor_id=_get_user_id_from_token(token),
            first_selection=not isinstance(previous_scope, list),
        )

        return ConfluenceSpacesResponse(
            available=[ConfluenceSpace(**s) for s in available],
            selected=validated,
            configured=True,
        )
