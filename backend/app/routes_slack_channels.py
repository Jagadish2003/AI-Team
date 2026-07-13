"""
routes_slack_channels.py — R18-C0 P5
Slack channel selection endpoint

GET   /api/connectors/slack/channels
PATCH /api/connectors/slack/channels

When a customer connects Slack in the Integration Hub, they choose which public
channels AgentIQ may read. Instead of ingesting every public channel the token
can see, the Slack ingestor (R16-A2) reads ONLY the channels selected for that
org. The selection is a per-org, workspace-level fact stored on the connector
record (like the Salesforce product declaration in routes_salesforce_products.py)
and is editable later.

GET returns the channels the customer can choose from (public channels AgentIQ is
a member of) plus the current saved selection. PATCH persists the selection.

Behaviour of the saved selection (honoured in discovery.ingest.slack):
  * No selection saved yet (``configured == False``) → the ingestor reads every
    accessible public channel (backwards-compatible default). The UI pre-selects
    all so the customer can narrow it.
  * A selection saved (``configured == True``) → the ingestor reads ONLY those
    channels, even if the token has access to more. An empty saved list means
    read nothing.

Validation:
  Only ids among the currently-selectable channels are accepted; unknown ids are
  filtered out silently (forward-compatible). Requires Slack to be connected for
  the write. Returns 404 if the Slack connector is not found.

Registration:
  register_slack_channels_routes(app) — called from main.py.
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

class SlackChannel(BaseModel):
    id: str
    name: str


class SlackChannelsBody(BaseModel):
    """Channel selection payload — the channel ids AgentIQ may read for this org.
    Unknown ids are filtered silently. An empty list means read no channels."""
    channels: List[str]


class SlackChannelsResponse(BaseModel):
    ok:         bool = True
    available:  List[SlackChannel]   # channels the customer can choose from
    selected:   List[str]            # saved selection (channel ids)
    configured: bool                 # whether a selection has been saved


# ── Helpers ───────────────────────────────────────────────────────────────────

def _selectable_channels(org_id: str) -> List[Dict[str, str]]:
    """Public channels AgentIQ can read (member, not archived) — the options the
    customer chooses from. Selection filtering is deliberately NOT applied here.

    Sourced from the Slack ingestor so offline (fixture) and live (Slack Web API)
    resolve identically to what a discovery run would see. A failure must be
    surfaced to the caller: treating an upstream outage as an empty channel list
    would make PATCH validate every submitted id away and silently overwrite a
    previously-good selection with ``[]``.
    """
    try:
        from discovery.ingest.slack import list_selectable_channels
        return list_selectable_channels(org_id)
    except Exception as exc:
        logger.warning("slack channels: could not list selectable channels: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "Slack channels are temporarily unavailable. "
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

_slack_channels_routes_registered = False


def register_slack_channels_routes(app: FastAPI) -> None:
    """Register the Slack channel-selection endpoints. Idempotent."""
    global _slack_channels_routes_registered
    if _slack_channels_routes_registered:
        return
    _slack_channels_routes_registered = True

    @app.get(
        "/api/connectors/slack/channels",
        response_model=SlackChannelsResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="List selectable Slack channels and the saved selection",
        tags=["Integration Hub"],
    )
    def get_slack_channels() -> SlackChannelsResponse:
        connector = org_connector_get(get_current_org_id(), "slack")
        if not connector:
            raise HTTPException(status_code=404, detail="Slack connector not found.")

        available = _selectable_channels(get_current_org_id())
        selected, configured = _saved_selection(connector)
        return SlackChannelsResponse(
            available=[SlackChannel(**c) for c in available],
            selected=selected,
            configured=configured,
        )

    @app.patch(
        "/api/connectors/slack/channels",
        response_model=SlackChannelsResponse,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
        summary="Select which Slack channels AgentIQ reads for this workspace",
        tags=["Integration Hub"],
    )
    def set_slack_channels(body: SlackChannelsBody) -> SlackChannelsResponse:
        org_id = get_current_org_id()
        connector = org_connector_get(org_id, "slack")
        if not connector:
            raise HTTPException(status_code=404, detail="Slack connector not found.")

        status = connector.get("status", "")
        if status not in ("connected", "live"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Slack connector is not connected (status: '{status}'). "
                    "Connect Slack before selecting channels."
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
        org_connector_set(org_id, "slack", connector)

        return SlackChannelsResponse(
            available=[SlackChannel(**c) for c in available],
            selected=validated,
            configured=True,
        )
