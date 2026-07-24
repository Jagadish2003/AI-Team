"""
routes_teams_channels.py — Microsoft Teams channel selection endpoint.

GET   /api/connectors/teams/channels
PATCH /api/connectors/teams/channels

The Teams analogue of the Slack channel selection (routes_slack_channels.py /
R18-C0 P5). When a customer connects Teams, they choose WHICH granted channels
AgentIQ reads — instead of ingesting every granted standard channel. The Teams
ingestor (R17-A1 reach + R18-A4 depth) then reads ONLY the selected channels for
that org. The selection is a per-org, workspace-level fact stored on the connector
record and is editable later.

A channel is identified by its container id ``"{team_id}/{channel_id}"`` (a
channel id is only unique within its team), which is exactly the value the
ingestor's scope check and checkpoint map key on.

GET returns the channels the customer can choose from (granted standard channels)
plus the current saved selection. PATCH persists the selection.

Behaviour of the saved selection (honoured in discovery.ingest.teams):
  * No selection saved yet (``configured == False``) → the ingestor reads every
    granted channel (backwards-compatible default). The UI pre-selects all so the
    customer can narrow it.
  * A selection saved (``configured == True``) → the ingestor reads ONLY those
    channels (reach signal AND deep content), even where AgentIQ is granted more.
    An empty saved list means read nothing.

Validation:
  Only ids among the currently-selectable channels are accepted; unknown ids are
  filtered out silently (forward-compatible). Requires Teams to be connected for
  the write. Returns 404 if the Teams connector is not found.

Registration:
  register_teams_channels_routes(app) — called from main.py.
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

class TeamsChannel(BaseModel):
    id: str            # container id "{team_id}/{channel_id}"
    name: str          # channel display name
    team: str = ""     # owning team (channels can share a name across teams)


class TeamsChannelsBody(BaseModel):
    """Channel selection payload — the container ids AgentIQ may read for this org.
    Unknown ids are filtered silently. An empty list means read no channels."""
    channels: List[str]


class TeamsChannelsResponse(BaseModel):
    ok:         bool = True
    available:  List[TeamsChannel]   # channels the customer can choose from
    selected:   List[str]            # saved selection (container ids)
    configured: bool                 # whether a selection has been saved


# ── Helpers ───────────────────────────────────────────────────────────────────

def _selectable_channels(org_id: str) -> List[Dict[str, str]]:
    """Granted Teams channels — the options the customer chooses from.

    Sourced from the Teams ingestor so offline (fixture) and live (Microsoft
    Graph) resolve identically to what a discovery run would see. A failure is
    surfaced (503) rather than treated as an empty list — otherwise PATCH would
    validate every submitted id away and silently overwrite a good selection.
    """
    try:
        from discovery.ingest.teams import list_selectable_channels
        return list_selectable_channels(org_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("teams channels: could not list selectable channels: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "Teams channels are temporarily unavailable. "
                "Your saved channel selection was not changed."
            ),
        ) from exc


def _saved_selection(connector: Dict[str, Any]) -> tuple[List[str], bool]:
    """Return (selected ids, configured?). configured is True only once a
    selection has actually been saved (the ``channels`` key is a list)."""
    channels = connector.get("channels")
    if isinstance(channels, list):
        return [str(c) for c in channels], True
    return [], False


# ── Registration ──────────────────────────────────────────────────────────────

_teams_channels_routes_registered = False


def register_teams_channels_routes(app: FastAPI) -> None:
    """Register the Teams channel-selection endpoints. Idempotent."""
    global _teams_channels_routes_registered
    if _teams_channels_routes_registered:
        return
    _teams_channels_routes_registered = True

    @app.get(
        "/api/connectors/teams/channels",
        response_model=TeamsChannelsResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="List selectable Teams channels and the saved selection",
        tags=["Integration Hub"],
    )
    def get_teams_channels() -> TeamsChannelsResponse:
        connector = org_connector_get(get_current_org_id(), "teams")
        if not connector:
            raise HTTPException(status_code=404, detail="Teams connector not found.")

        available = _selectable_channels(get_current_org_id())
        selected, configured = _saved_selection(connector)
        return TeamsChannelsResponse(
            available=[TeamsChannel(**c) for c in available],
            selected=selected,
            configured=configured,
        )

    @app.patch(
        "/api/connectors/teams/channels",
        response_model=TeamsChannelsResponse,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
        summary="Select which Teams channels AgentIQ reads for this workspace",
        tags=["Integration Hub"],
    )
    def set_teams_channels(body: TeamsChannelsBody) -> TeamsChannelsResponse:
        org_id = get_current_org_id()
        connector = org_connector_get(org_id, "teams")
        if not connector:
            raise HTTPException(status_code=404, detail="Teams connector not found.")

        status = connector.get("status", "")
        if status not in ("connected", "live"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Teams connector is not connected (status: '{status}'). "
                    "Connect Teams before selecting channels."
                ),
            )

        available = _selectable_channels(org_id)
        available_ids = {c["id"] for c in available}
        # Validate + de-dup, preserving request order. Unknown ids filtered out.
        validated: List[str] = []
        for cid in body.channels:
            cid = str(cid)
            if cid in available_ids and cid not in validated:
                validated.append(cid)

        connector["channels"] = validated
        org_connector_set(org_id, "teams", connector)

        return TeamsChannelsResponse(
            available=[TeamsChannel(**c) for c in available],
            selected=validated,
            configured=True,
        )
