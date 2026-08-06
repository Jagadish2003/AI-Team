"""
routes_jira_projects.py — Jira project selection endpoint (multi-project).

GET   /api/connectors/jira/projects
PATCH /api/connectors/jira/projects

When a customer connects Jira in the Integration Hub, they choose WHICH projects
AgentIQ scopes discovery to — the Jira analogue of the Slack channel selection
(routes_slack_channels.py / R18-C0 P5). Instead of the hardcoded
``JIRA_PROJECT_KEY`` env default, the Jira ingestor reads the projects selected for
that org (JQL ``project IN (...)``). The selection is a per-org, workspace-level
fact stored on the connector record (the ``projects`` list) and is editable later;
the legacy single ``project`` key is still honoured for backward compatibility.

GET returns the projects the customer can choose from (visible to the connected
Jira credential) plus the current saved selection. PATCH persists the selection.

Behaviour of the saved selection (honoured in discovery.ingest.jira.resolve_jira_project):
  * No selection saved yet (``configured == False``) → the ingestor falls back to
    the ``JIRA_PROJECT_KEY`` env var, else the historical default ``"AIC"``.
  * A selection saved (``configured == True``) → the ingestor scopes to THAT
    project.

Validation:
  Only a key among the currently-selectable projects is accepted; an unknown key
  is rejected (400). ``project: null`` clears the selection (back to the env
  fallback). Requires Jira to be connected for the write. Returns 404 if the Jira
  connector is not found.

Registration:
  register_jira_projects_routes(app) — called from main.py.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from .db import org_connector_get, org_connector_set
from .middleware.tenancy import get_current_org_id
from .security import require_auth
from .connector_scope_audit import audit_scope_selection
from .rbac import _get_user_id_from_token, require_role

logger = logging.getLogger(__name__)


# ── Models ────────────────────────────────────────────────────────────────────

class JiraProject(BaseModel):
    key: str
    name: str


class JiraProjectBody(BaseModel):
    """Project selection payload — the project keys AgentIQ scopes to for this org.
    An empty list clears the selection (fall back to the env default)."""
    projects: List[str] = []


class JiraProjectsResponse(BaseModel):
    ok:         bool = True
    available:  List[JiraProject]     # projects the customer can choose from
    selected:   List[str]             # saved selection (project keys)
    configured: bool                  # whether a selection has been saved


# ── Helpers ───────────────────────────────────────────────────────────────────

def _selectable_projects(org_id: str) -> List[Dict[str, str]]:
    """Projects the customer can choose from — the option list.

    Sourced from the Jira ingestor so offline (fixture) and live (Jira REST)
    resolve identically to what a discovery run would see. A failure is surfaced
    (503) rather than treated as an empty list — otherwise PATCH would validate a
    submitted key away and reject a legitimate selection during a transient outage.
    """
    try:
        from discovery.ingest.jira import list_selectable_projects
        return list_selectable_projects(org_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("jira projects: could not list selectable projects: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "Jira projects are temporarily unavailable. "
                "Your saved project selection was not changed."
            ),
        ) from exc


def _saved_selection(connector: Dict[str, Any]) -> tuple[List[str], bool]:
    """Return (selected keys, configured?). configured is True once a non-empty
    selection has been saved. Reads the multi-project ``projects`` list, falling
    back to the legacy single ``project`` key for backward compatibility."""
    projects = connector.get("projects")
    if isinstance(projects, list):
        keys = [str(p).strip() for p in projects if str(p).strip()]
        if keys:
            return keys, True
    legacy = connector.get("project")
    if isinstance(legacy, str) and legacy.strip():
        return [legacy.strip()], True
    return [], False


# ── Registration ──────────────────────────────────────────────────────────────

_jira_projects_routes_registered = False


def register_jira_projects_routes(app: FastAPI) -> None:
    """Register the Jira project-selection endpoints. Idempotent."""
    global _jira_projects_routes_registered
    if _jira_projects_routes_registered:
        return
    _jira_projects_routes_registered = True

    @app.get(
        "/api/connectors/jira/projects",
        response_model=JiraProjectsResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="List selectable Jira projects and the saved selection",
        tags=["Integration Hub"],
    )
    def get_jira_projects() -> JiraProjectsResponse:
        connector = org_connector_get(get_current_org_id(), "jira")
        if not connector:
            raise HTTPException(status_code=404, detail="Jira connector not found.")

        # Degrade a listing failure (e.g. Jira auth not ready) to an empty option
        # list so the panel renders its guidance instead of a 503; the saved
        # selection is still returned unchanged.
        try:
            available = _selectable_projects(get_current_org_id())
        except HTTPException:
            available = []
        selected, configured = _saved_selection(connector)
        return JiraProjectsResponse(
            available=[JiraProject(**p) for p in available],
            selected=selected,
            configured=configured,
        )

    @app.patch(
        "/api/connectors/jira/projects",
        response_model=JiraProjectsResponse,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
        summary="Select which Jira project AgentIQ scopes discovery to",
        tags=["Integration Hub"],
    )
    def set_jira_projects(body: JiraProjectBody, token: str = Depends(require_auth)) -> JiraProjectsResponse:
        org_id = get_current_org_id()
        connector = org_connector_get(org_id, "jira")
        if not connector:
            raise HTTPException(status_code=404, detail="Jira connector not found.")

        status = connector.get("status", "")
        if status not in ("connected", "live"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Jira connector is not connected (status: '{status}'). "
                    "Connect Jira before selecting projects."
                ),
            )

        available = _selectable_projects(org_id)
        available_keys = {p["key"] for p in available}

        # Keep only known keys (filter unknowns silently), de-duped in submit order.
        chosen: List[str] = []
        for raw in body.projects:
            key = str(raw).strip()
            if key and key in available_keys and key not in chosen:
                chosen.append(key)

        # Persist the multi-project selection; clear the legacy single-project key.
        previous_scope = connector.get("projects")
        connector.pop("project", None)
        if chosen:
            connector["projects"] = chosen
        else:
            connector.pop("projects", None)
        org_connector_set(org_id, "jira", connector)
        # 2.0-D4 T1 (AC1): scope pin/unpin is a data-access grant. Note that
        # clearing the selection REMOVES the key here rather than storing [],
        # so the audit reads the chosen list directly — an empty selection is a
        # real unpin of everything and must still produce a row.
        audit_scope_selection(
            connector_id="jira",
            scope_key="projects",
            previous=previous_scope,
            selected=chosen,
            actor_id=_get_user_id_from_token(token),
            first_selection=not isinstance(previous_scope, list),
        )

        return JiraProjectsResponse(
            available=[JiraProject(**p) for p in available],
            selected=chosen,
            configured=bool(chosen),
        )
