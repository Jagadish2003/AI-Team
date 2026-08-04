"""
routes_github_repos.py — GitHub repository selection endpoint (multi-repo).

GET   /api/connectors/github/repos
PATCH /api/connectors/github/repos

When a customer connects GitHub in the Integration Hub, they choose WHICH
repositories AgentIQ scopes discovery to — the GitHub analogue of the Slack
channel selection. Instead of auto-discovering every repo the token can access,
the GitHub connector reads ONLY the repos selected for that org. Precedence in
``connectors.saas.github._resolve_repos`` is: saved selection > ``GITHUB_REPOS``
env > auto-discover. The selection is a per-org, workspace-level fact stored on the
connector record (as ``["owner/repo", ...]``) and is editable later.

Behaviour of the saved selection:
  * No selection saved yet → the connector auto-discovers all accessible repos
    (or the ``GITHUB_REPOS`` env scope, if set) — backwards compatible.
  * A selection saved → the connector reads ONLY those repos.

Validation:
  Only ids among the currently-selectable repos are accepted; unknown ids are
  filtered out silently. Requires GitHub to be connected for the write. 404 if the
  connector is not found, 503 if the repo list is temporarily unavailable (saved
  selection preserved).

Registration:
  register_github_repos_routes(app) — called from main.py.
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

class GitHubRepo(BaseModel):
    id: str      # "owner/repo"
    name: str
    owner: str = ""


class GitHubReposBody(BaseModel):
    """Repo selection payload — the ``owner/repo`` ids AgentIQ reads for this org.
    Unknown ids are filtered silently. An empty list clears the selection (back to
    auto-discover / the GITHUB_REPOS env scope)."""
    repos: List[str]


class GitHubReposResponse(BaseModel):
    ok:         bool = True
    available:  List[GitHubRepo]   # repos the customer can choose from
    selected:   List[str]          # saved selection (owner/repo ids)
    configured: bool               # whether a selection has been saved


# ── Helpers ───────────────────────────────────────────────────────────────────

def _selectable_repos(org_id: str) -> List[Dict[str, str]]:
    """Repos the customer chooses from. Selection filtering is NOT applied here. A
    failure is surfaced (503) rather than an empty list, so PATCH never validates a
    legitimate id away during an outage."""
    try:
        from connectors.saas.github import list_selectable_repos
        return list_selectable_repos(org_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("github repos: could not list selectable repos: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "GitHub repositories are temporarily unavailable. "
                "Your saved repository selection was not changed."
            ),
        ) from exc


def _saved_selection(connector: Dict[str, Any]) -> "tuple[List[str], bool]":
    """Return (selected ids, configured?). configured is True once a selection
    (the ``repos`` list) has been saved."""
    repos = connector.get("repos")
    if isinstance(repos, list):
        return [str(r) for r in repos], True
    return [], False


# ── Registration ──────────────────────────────────────────────────────────────

_github_repos_routes_registered = False


def register_github_repos_routes(app: FastAPI) -> None:
    """Register the GitHub repo-selection endpoints. Idempotent."""
    global _github_repos_routes_registered
    if _github_repos_routes_registered:
        return
    _github_repos_routes_registered = True

    @app.get(
        "/api/connectors/github/repos",
        response_model=GitHubReposResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
        summary="List selectable GitHub repositories and the saved selection",
        tags=["Integration Hub"],
    )
    def get_github_repos() -> GitHubReposResponse:
        connector = org_connector_get(get_current_org_id(), "github")
        if not connector:
            raise HTTPException(status_code=404, detail="GitHub connector not found.")

        # Degrade a listing failure (e.g. GitHub token not ready) to an empty
        # option list so the panel renders its guidance instead of a 503; the saved
        # selection is still returned unchanged.
        try:
            available = _selectable_repos(get_current_org_id())
        except HTTPException:
            available = []
        selected, configured = _saved_selection(connector)
        return GitHubReposResponse(
            available=[GitHubRepo(**r) for r in available],
            selected=selected,
            configured=configured,
        )

    @app.patch(
        "/api/connectors/github/repos",
        response_model=GitHubReposResponse,
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
        summary="Select which GitHub repositories AgentIQ reads for this workspace",
        tags=["Integration Hub"],
    )
    def set_github_repos(body: GitHubReposBody, token: str = Depends(require_auth)) -> GitHubReposResponse:
        org_id = get_current_org_id()
        connector = org_connector_get(org_id, "github")
        if not connector:
            raise HTTPException(status_code=404, detail="GitHub connector not found.")

        status = connector.get("status", "")
        if status not in ("connected", "live"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"GitHub connector is not connected (status: '{status}'). "
                    "Connect GitHub before selecting repositories."
                ),
            )

        available = _selectable_repos(org_id)
        available_ids = {r["id"] for r in available}
        validated: List[str] = []
        for rid in body.repos:
            rid = str(rid).strip()
            if rid in available_ids and rid not in validated:
                validated.append(rid)

        previous_scope = connector.get("repos")
        connector["repos"] = validated
        org_connector_set(org_id, "github", connector)
        # 2.0-D4 T1 (AC1): scope pin/unpin is a data-access grant.
        audit_scope_selection(
            connector_id="github",
            scope_key="repos",
            previous=previous_scope,
            selected=validated,
            actor_id=_get_user_id_from_token(token),
            first_selection=not isinstance(previous_scope, list),
        )

        return GitHubReposResponse(
            available=[GitHubRepo(**r) for r in available],
            selected=validated,
            configured=True,
        )
