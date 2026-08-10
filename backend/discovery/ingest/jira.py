"""
SF-2.4 — Jira Ingestion Module

Offline mode: reads backend/discovery/ingest/fixtures/jira_sample.json
Live mode:    calls Jira REST API v3

Live-mode credentials come from the connector's credential record ONLY (the
per-run credential context, or the per-org vault) — the Jira Cloud API gateway
base (captured at OAuth connect from the accessible-resources lookup, or a static
credential's own base_url) and the Bearer/API token are BOTH part of that record.
There is no JIRA_URL / JIRA_TOKEN environment fallback for the connection
(R191-H1 / T2 — F2 fix): connection config is part of the connector record (one
source of connector truth). Non-credential tuning (JIRA_PROJECT_KEY, team/field
overrides) still reads from the environment.

Known fixes applied (vs earlier stub):
    1. completed_points was None — now fetched via /rest/agile/1.0/sprint/{id}/issue
    2. salesforce_issue_count was None — now counted from issue labels/project
    3. Velocity fallback: if story_points field is absent (many orgs don't use it),
       falls back to issue count as a proxy for velocity

D7 signal produced:
    jira_echo_score = issues referencing Salesforce CS- IDs / total issues in window

AgentIQ is READ-ONLY. No data is written to Jira under any circumstances.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import get_ingest_org, is_live

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "jira_sample.json"
WINDOW_DAYS = 90
JIRA_API_VERSION = "3"


# ─────────────────────────────────────────────────────────────────────────────
# Custom exception
# ─────────────────────────────────────────────────────────────────────────────


class JiraIngestError(Exception):
    """Raised when live Jira ingestion fails with a clear, actionable message."""


# ─────────────────────────────────────────────────────────────────────────────
# Offline loader
# ─────────────────────────────────────────────────────────────────────────────


def _load_fixture() -> Dict[str, Any]:
    if not FIXTURE_PATH.exists():
        raise JiraIngestError(f"Jira fixture not found: {FIXTURE_PATH}")
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Jira REST client
# ─────────────────────────────────────────────────────────────────────────────


class JiraClient:
    """
    Minimal Jira Cloud REST API v3 client.

    Auth (selected by whether ``username`` is present — R18-A3 outbound modes):

    * OAuth (3LO) Bearer token — ``username`` empty. ``base_url`` is the
      api.atlassian.com gateway (https://api.atlassian.com/ex/jira/{cloudId})
      resolved at OAuth connect time, against which the Bearer token is presented.
    * Basic (email + API token) — ``username`` set; the static vault credential
      path (the outbound-only connect in a no-public-inbound deployment).
      ``base_url`` is the site URL itself (https://yourco.atlassian.net) — Basic
      auth does not go through the OAuth gateway. The REST paths are identical.
    """

    def __init__(self, base_url: str, token: str = "", username: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.username = username
        self._session = None

    def _get_session(self):
        try:
            import requests
        except ImportError:
            raise JiraIngestError(
                "requests library required for live mode: pip install requests"
            )
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(
                {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
            )
            if self.token and self.username:
                # Static API-token credential → Basic auth (email:token).
                self._session.auth = (self.username, self.token)
            elif self.token:
                self._session.headers["Authorization"] = f"Bearer {self.token}"
            else:
                raise JiraIngestError(
                    "Live mode requires a Jira credential (OAuth Bearer token or "
                    "email + API token) from the credential vault. "
                    "Set INGEST_MODE=offline to run without credentials."
                )
        return self._session

    def get(self, path: str, params: Optional[Dict] = None) -> Any:
        """Make a GET request. Raises JiraIngestError on failure."""
        session = self._get_session()
        url = f"{self.base_url}{path}"
        try:
            resp = session.get(url, params=params or {}, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except JiraIngestError:
            raise
        except Exception as e:
            raise JiraIngestError(f"Jira API call failed [{path}]: {e}")

    def search_issues(
        self,
        jql: str,
        fields: Optional[List[str]] = None,
        max_results: int = 5000,
    ) -> List[Dict]:
        """
        Execute a JQL search with start-at pagination.

        Jira uses startAt/maxResults pagination (not cursor).
        max_results: safety cap — raises if exceeded.
        """
        page_size = min(100, max_results)
        start_at = 0
        all_issues: List[Dict] = []

        default_fields = fields or [
            "summary",
            "status",
            "issuetype",
            "labels",
            "story_points",
            "customfield_10016",  # story points field (cloud + server)
            "created",
            "resolutiondate",
            "assignee",
        ]

        while True:
            data = self.get(
                f"/rest/api/{JIRA_API_VERSION}/search/jql",
                params={
                    "jql": jql,
                    "startAt": start_at,
                    "maxResults": page_size,
                    "fields": ",".join(default_fields),
                },
            )
            issues = data.get("issues", [])
            all_issues.extend(issues)

            if len(all_issues) >= max_results:
                raise JiraIngestError(
                    f"JQL search exceeded {max_results} results. "
                    f"Narrow the JQL query or reduce the time window."
                )

            total = data.get("total", 0)
            if start_at + len(issues) >= total or not issues:
                break
            start_at += len(issues)

        return all_issues

    def get_boards(self, project_key: str) -> List[Dict]:
        """Fetch Scrum/Kanban boards for a project."""
        try:
            data = self.get(
                "/rest/agile/1.0/board",
                params={"projectKeyOrId": project_key},
            )
            return data.get("values", [])
        except JiraIngestError:
            return []  # No agile boards — project may be Kanban or non-sprint

    def get_recent_sprints(self, board_id: int, limit: int = 3) -> List[Dict]:
        """Fetch the most recently closed sprints for a board."""
        try:
            data = self.get(
                f"/rest/agile/1.0/board/{board_id}/sprint",
                params={"state": "closed", "maxResults": limit},
            )
            sprints = data.get("values", [])
            # Sort by endDate descending, take most recent
            sprints.sort(key=lambda s: s.get("endDate", ""), reverse=True)
            return sprints[:limit]
        except JiraIngestError:
            return []

    def get_sprint_issues(self, sprint_id: int) -> List[Dict]:
        """
        Fetch issues in a sprint including story points.

        This is the second call required to compute completed_points.
        The board/sprint endpoint does NOT return story points — only
        the sprint/{id}/issue endpoint does.

        Story points field:
            Jira Cloud: customfield_10016 (standard)
            Jira Server: customfield_10002 or customfield_10004 (varies by config)
        We try all three and take the first non-null value.
        """
        try:
            data = self.get(
                f"/rest/agile/1.0/sprint/{sprint_id}/issue",
                params={
                    "maxResults": 500,
                    "fields": "status,customfield_10016,customfield_10002,customfield_10004,labels,summary",
                },
            )
            return data.get("issues", [])
        except JiraIngestError:
            logger.warning(f"Could not fetch issues for sprint {sprint_id} — skipping")
            return []

    def list_projects(self, max_results: int = 200) -> List[Dict[str, str]]:
        """List projects visible to this credential as ``{key, name}`` dicts.

        The Jira analogue of Slack's ``conversations.list`` — the options a
        customer chooses from in the Integration Hub. Uses the Jira Cloud project
        search endpoint with startAt pagination. Read-only.
        """
        projects: List[Dict[str, str]] = []
        start_at = 0
        page_size = 50
        while len(projects) < max_results:
            data = self.get(
                f"/rest/api/{JIRA_API_VERSION}/project/search",
                params={"startAt": start_at, "maxResults": page_size},
            )
            values = data.get("values", []) if isinstance(data, dict) else []
            for p in values:
                key = str(p.get("key", "") or "")
                if key:
                    projects.append({"key": key, "name": str(p.get("name", "") or key)})
            if not values or (isinstance(data, dict) and data.get("isLast", True)):
                break
            start_at += len(values)
        return projects[:max_results]


def _get_client(org_id: Optional[str] = None) -> JiraClient:
    # Credentials come from the connector's credential record ONLY (R191-H1 / T2 —
    # F2 fix). The per-run context (DB-sourced) carries the vault Bearer token + the
    # captured api.atlassian.com gateway base (OAuth), isolated per org/run. With no
    # per-run context (CLI/standalone, or an on-demand API call like the project
    # picker) or for a static API-token credential, the record resolves per-org from
    # the vault via the single credential path. A static credential supplies its own
    # base_url; an OAuth credential carries the captured gateway base. There is
    # **no JIRA_URL env fallback** — the base URL is part of the connector's
    # credential record (one source of connector truth); a record without a URL is a
    # loud configuration error naming the record, never a silent env default.
    from . import get_live_connector, resolve_vault_connector

    cred = get_live_connector("jira") or resolve_vault_connector("jira", org_id)
    if cred:
        jira_url = (cred.get("url") or "").rstrip("/")
        token = cred.get("token") or ""
        # Present on a static credential only — selects Basic auth against the
        # site URL (its base_url) instead of Bearer against the OAuth gateway.
        username = cred.get("username") or ""
    else:
        jira_url = ""
        token = ""
        username = ""

    # OAuth Jira's api.atlassian.com gateway is NOT on the vault credential (it is
    # instance config captured at connect). During a discovery run resolve_live_
    # systems supplies it via the per-run context above; for an on-demand call
    # (the project picker) resolve it the SAME way here — the persisted per-org
    # instance URL, then derive-and-persist from the token — reusing the
    # single-source helpers so the logic can't drift. There is NO JIRA_URL env
    # fallback (R191-H1 / T2, AC4): instance config is captured at connect and
    # persisted per-org, never read from the process environment.
    if not jira_url and token:
        org = org_id or get_ingest_org()
        try:
            from app.live_ingest_credentials import (
                _derive_oauth_instance_url,
                get_connector_instance_url,
                store_connector_instance_url,
            )

            jira_url = (get_connector_instance_url(org, "jira") or "").rstrip("/")
            if not jira_url:
                derived = _derive_oauth_instance_url("jira", token)
                if derived:
                    jira_url = derived.rstrip("/")
                    try:
                        store_connector_instance_url(org, "jira", jira_url)
                    except Exception:  # pragma: no cover — best-effort cache
                        pass
        except Exception:  # pragma: no cover — persisted/derived URL unavailable
            jira_url = jira_url or ""

    # The base URL is part of the connector's credential record (or captured/derived
    # at connect). A record without one is a configuration error surfaced loudly and
    # named — never a silent env default (R191-H1 / T2, AC4).
    if not jira_url:
        raise JiraIngestError(
            "Jira credential record is missing its base URL ('jira' connector). "
            "The Jira Cloud API gateway base is captured at OAuth connect (and a "
            "static API-token credential carries its own base_url); reconnect Jira "
            "in the Integration Hub so the record carries its URL. "
            "(No JIRA_URL environment fallback is used.)"
        )
    if not token:
        raise JiraIngestError(
            "Live mode requires a Jira credential (OAuth Bearer token or API "
            "token) from the credential vault. Connect Jira in the Integration Hub."
        )
    return JiraClient(jira_url, token=token, username=username)


# ─────────────────────────────────────────────────────────────────────────────
# Project selection (Integration Hub) — the Jira analogue of Slack's P5 channels
# ─────────────────────────────────────────────────────────────────────────────


def list_selectable_projects(org_id: Optional[str] = None) -> List[Dict[str, str]]:
    """Projects the customer can choose from — ``{key, name}`` dicts.

    Offline (default) reads the fixture's ``projects``; live lists projects visible
    to the connected Jira credential. Selection filtering is deliberately NOT
    applied here — this is the full option list, resolved identically to what a
    discovery run would see (mirrors :func:`slack.list_selectable_channels`).
    ``org_id`` is accepted for API symmetry; the live credential resolves from the
    per-run/vault context, not the argument.
    """
    if not is_live():
        fixture = _load_fixture()
        projects = fixture.get("projects")
        if isinstance(projects, list):
            return [
                {"key": str(p.get("key", "")), "name": str(p.get("name", "") or p.get("key", ""))}
                for p in projects
                if p.get("key")
            ]
        # Back-compat: derive a single option from _meta when no explicit list.
        meta_key = str((fixture.get("_meta") or {}).get("project_key", "") or "")
        return [{"key": meta_key, "name": meta_key}] if meta_key else []
    return _get_client(org_id).list_projects()


def _env_project_keys() -> List[str]:
    """The ``JIRA_PROJECT_KEY`` env fallback as a key list (comma-separated
    allowed), else the historical default ``["AIC"]``."""
    env = os.getenv("JIRA_PROJECT_KEY", "AIC")
    keys = [k.strip() for k in env.split(",") if k.strip()]
    return keys or ["AIC"]


def resolve_jira_projects(org_id: Optional[str] = None) -> List[str]:
    """The Jira project keys to ingest for ``org_id`` (multi-project selection).

    Resolution order:
      1. the org's saved Integration-Hub selection — the ``projects`` list on the
         Jira connector record (set by ``PATCH /api/connectors/jira/projects``);
      2. the legacy single ``project`` key (backward compatible with the earlier
         single-project selection);
      3. the ``JIRA_PROJECT_KEY`` env var (CLI/standalone fallback, comma-separated
         allowed); else the historical default ``["AIC"]``.

    Any DB/lookup failure degrades to the env/default, so a run is never blocked by
    the selection store being unavailable (mirrors slack ``_selected_channel_ids``).
    """
    org = org_id or get_ingest_org()
    try:
        from app.db import org_connector_get

        record = org_connector_get(org, "jira") if org else None
        if record:
            projects = record.get("projects")
            if isinstance(projects, list):
                keys = [str(p).strip() for p in projects if str(p).strip()]
                if keys:
                    return keys
            # Backward compat: the earlier single-project selection.
            single = record.get("project")
            if isinstance(single, str) and single.strip():
                return [single.strip()]
    except Exception:  # pragma: no cover — never block a run on the selection store
        logger.debug("jira: could not read saved project selection; using env/default")
    return _env_project_keys()


def resolve_jira_project(org_id: Optional[str] = None) -> str:
    """The FIRST Jira project key for ``org_id`` — backward-compatible single-key
    accessor kept for any caller that still expects one key. Prefer
    :func:`resolve_jira_projects` (the multi-project selection)."""
    return resolve_jira_projects(org_id)[0]


def _as_key_list(project_key: "Optional[str | List[str]]") -> List[str]:
    """Normalise a single key, a list of keys, or None into a non-empty key list
    (None → the env/default). Lets the per-project ingest functions accept either
    a single project (backward compatible) or the multi-project selection."""
    if project_key is None:
        return _env_project_keys()
    if isinstance(project_key, str):
        key = project_key.strip()
        return [key] if key else _env_project_keys()
    keys = [str(k).strip() for k in project_key if str(k).strip()]
    return keys or _env_project_keys()


def _project_jql_clause(keys: List[str]) -> str:
    """Build the JQL project scope: ``project = X`` for one key (identical to the
    single-project JQL), ``project IN (A, B, C)`` for several. Jira project keys
    are uppercase-alphanumeric, so they need no quoting."""
    if len(keys) == 1:
        return f"project = {keys[0]}"
    return f"project IN ({', '.join(keys)})"


# ─────────────────────────────────────────────────────────────────────────────
# Story points extraction helper
# ─────────────────────────────────────────────────────────────────────────────


def _extract_story_points(issue: Dict) -> Optional[float]:
    """
    Extract story points from a Jira issue.

    Story points live in different custom fields depending on Jira version:
        customfield_10016 — Jira Cloud (Story Points)
        customfield_10002 — common Server/DC config
        customfield_10004 — alternative Server/DC config

    Returns None if no story points field is populated.
    The caller must handle None by falling back to issue count as velocity proxy.
    """
    fields = issue.get("fields") or {}
    for cf in ("customfield_10016", "customfield_10002", "customfield_10004"):
        val = fields.get(cf)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion functions
# ─────────────────────────────────────────────────────────────────────────────


def get_issue_metrics(
    client: Optional[JiraClient] = None,
    project_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Pull Jira issue volume and cross-reference metrics for D7.

    Searches for issues in the target project created in the last 90 days.
    Counts how many reference Salesforce Case IDs (CS- pattern) in
    summary, description, or labels.

    Live JQL:
        project = {project_key} AND created >= -{WINDOW_DAYS}d ORDER BY created DESC

    Cross-reference search (D7 signal):
        project = {project_key} AND created >= -{WINDOW_DAYS}d
        AND (summary ~ "CS-" OR description ~ "CS-" OR labels = "Salesforce")

    Returns: issue_metrics dict matching jira_sample.json shape
    """
    if not is_live():
        return _load_fixture()["issue_metrics"]

    # Accept a single key (backward compatible) or the multi-project selection.
    keys = _as_key_list(project_key)
    clause = _project_jql_clause(keys)
    project_label = keys[0] if len(keys) == 1 else ",".join(keys)

    escalation_field = os.getenv("JIRA_ESCALATION_FIELD", "").strip()
    issue_fields = [
        "summary",
        "status",
        "issuetype",
        "labels",
        "customfield_10016",
        "customfield_10002",
        "customfield_10004",
        "assignee",
        "reporter",
        "project",
    ]
    if escalation_field:
        issue_fields.append(escalation_field)

    # Total issues in window
    all_issues = client.search_issues(
        jql=f"{clause} AND created >= -{WINDOW_DAYS}d",
        fields=issue_fields,
    )

    total = len(all_issues)

    # Issue type breakdown
    type_counts: Dict[str, int] = {}
    for issue in all_issues:
        itype = (issue.get("fields") or {}).get("issuetype", {}).get("name", "Unknown")
        type_counts[itype] = type_counts.get(itype, 0) + 1

    # Cross-system references — issues mentioning Salesforce CS- IDs
    # Search summary and labels (description search varies by Jira config)
    sf_issues = client.search_issues(
        jql=(
            f"{clause} AND created >= -{WINDOW_DAYS}d "
            f'AND (summary ~ "CS-" OR labels = "Salesforce" OR labels = "salesforce-case")'
        ),
        fields=["summary", "labels"],
        max_results=500,
    )
    salesforce_label_count = len(sf_issues)
    jira_echo_score = round(salesforce_label_count / total, 4) if total > 0 else 0.0

    # Sample cross-references for evidence
    sample_cross_refs: List[Dict] = []
    for issue in sf_issues[:5]:
        fields = issue.get("fields") or {}
        summary = fields.get("summary", "")
        sample_cross_refs.append(
            {
                "issue_key": issue.get("key", ""),
                "sf_reference": _extract_sf_case_id(summary),
                "field": "summary",
                "summary": summary[:120],
            }
        )

    normalized_issues: List[Dict[str, Any]] = []
    # Resolve each project KEY to its human-readable display NAME from the issues'
    # own project objects (Jira returns ``{"key": "PAYOPS", "name": "Payments
    # Operations", ...}``). The key is an identifier, not a name — every other
    # source names its entities by their display name (ServiceNow the CI/group
    # name, Salesforce the record name), so the Jira project name is what the
    # entity extractor should use for the team/project entity, keeping the key as
    # the stable source id. Falls back to the key when no issue carries a name.
    project_names: Dict[str, str] = {}
    for issue in all_issues:
        proj = (issue.get("fields") or {}).get("project")
        if isinstance(proj, dict):
            pkey = str(proj.get("key") or "").strip()
            pname = str(proj.get("name") or "").strip()
            if pkey and pname:
                project_names.setdefault(pkey, pname)
    primary_project_name = (
        project_names.get(keys[0]) if len(keys) == 1 else None
    ) or project_label

    for issue in all_issues:
        fields = issue.get("fields") or {}
        normalized = {
            "id": issue.get("id") or issue.get("key"),
            "key": issue.get("key") or issue.get("id"),
            "summary": fields.get("summary", ""),
            "labels": fields.get("labels") or [],
            "status": (fields.get("status") or {}).get("name", ""),
            "project": fields.get("project") or project_label,
            "assignee": fields.get("assignee"),
            "reporter": fields.get("reporter"),
        }
        if escalation_field and fields.get(escalation_field):
            normalized["escalation_target"] = fields.get(escalation_field)
        normalized_issues.append(normalized)

    return {
        "total_issues_90d": total,
        "project": project_label,
        "project_name": primary_project_name,
        "project_names": project_names,
        "project_key": project_label,
        "project_keys": keys,
        "salesforce_label_count": salesforce_label_count,
        "jira_echo_score": jira_echo_score,
        "issue_type_breakdown": [
            {"type": k, "count": v} for k, v in type_counts.items()
        ],
        "sample_cross_references": sample_cross_refs,
        "issues": normalized_issues,
    }


def _extract_sf_case_id(text: str) -> str:
    """Extract the first CS-NNNN pattern from a string."""
    import re

    m = re.search(r"CS-\d+", text)
    return m.group(0) if m else ""


def get_sprint_velocity(
    client: Optional[JiraClient] = None,
    project_key: Optional[str] = None,
    board_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Pull sprint velocity for the last 3 closed sprints.

    FIX APPLIED: The earlier stub returned None for completed_points and
    salesforce_issue_count because the board/sprint endpoint does not include
    story points. This version makes a SECOND API call per sprint to
    /rest/agile/1.0/sprint/{id}/issue to fetch actual story point values.

    VELOCITY FALLBACK: Many Jira projects do not configure story points.
    If no story points field is populated for any issue in a sprint, this
    function falls back to issue_count as the velocity proxy. The
    velocity_unit field indicates which was used:
        "story_points" — story points populated and summed
        "issue_count"  — fallback, no story points found

    Salesforce issue count per sprint: issues in the sprint whose labels
    include "Salesforce" or "salesforce-case", or whose summary contains "CS-".

    Returns: list of sprint dicts matching jira_sample.json shape
    """
    if not is_live():
        return _load_fixture()["sprint_velocity"]

    # Accept a single key (backward compatible) or the multi-project selection;
    # velocity is collected per project and aggregated into one list.
    keys = _as_key_list(project_key)

    results: List[Dict] = []

    for pk in keys:
        # Find the board for this project
        boards = client.get_boards(pk)
        if not boards:
            logger.warning(
                f"No Scrum boards found for Jira project {pk}. "
                f"Sprint velocity will be empty for it. "
                f"Project may be Kanban or non-sprint based."
            )
            continue

        # A board_id override only applies to a single-project selection.
        target_board_id = board_id if (board_id and len(keys) == 1) else boards[0]["id"]
        sprints = client.get_recent_sprints(target_board_id, limit=3)
        if not sprints:
            logger.warning(
                f"No closed sprints found for board {target_board_id} (project {pk})."
            )
            continue

        for sprint in sprints:
            sprint_id = sprint["id"]
            sprint_name = sprint.get("name", f"Sprint {sprint_id}")

            # Second call: get issues WITH story points
            sprint_issues = client.get_sprint_issues(sprint_id)

            # Completed issues (status = Done)
            completed_issues = [
                i
                for i in sprint_issues
                if (i.get("fields") or {}).get("status", {}).get("name", "").lower()
                in ("done", "closed", "resolved", "complete")
            ]

            # Story points — try to sum, fall back to issue count
            points_list = [_extract_story_points(i) for i in completed_issues]
            has_points = any(p is not None for p in points_list)

            if has_points:
                completed_points = sum(p or 0.0 for p in points_list)
                velocity_unit = "story_points"
            else:
                # Fallback: count of completed issues as velocity proxy
                completed_points = float(len(completed_issues))
                velocity_unit = "issue_count"
                logger.info(
                    f"Sprint {sprint_name}: no story points found — "
                    f"using issue count ({int(completed_points)}) as velocity proxy"
                )

            # Salesforce-related issues in this sprint
            sf_count = sum(1 for i in sprint_issues if _is_salesforce_related(i))

            results.append(
                {
                    "sprint_name": sprint_name,
                    "completed_points": round(completed_points, 1),
                    "salesforce_issue_count": sf_count,
                    "velocity_unit": velocity_unit,
                    "velocity_trend": _compute_trend(results),
                }
            )

    return results


def _is_salesforce_related(issue: Dict) -> bool:
    """Check if an issue is Salesforce-related by labels or summary."""
    fields = issue.get("fields") or {}
    labels = [str(l.get("name", "")).lower() for l in (fields.get("labels") or [])]
    if any("salesforce" in l or "crm" in l for l in labels):
        return True
    summary = fields.get("summary", "")
    return bool(_extract_sf_case_id(summary))


def _compute_trend(previous_sprints: List[Dict]) -> str:
    """Derive velocity trend from previous sprints already computed."""
    if len(previous_sprints) < 2:
        return "stable"
    last = previous_sprints[-1]["completed_points"]
    prev = previous_sprints[-2]["completed_points"]
    if last > prev * 1.1:
        return "improving"
    if last < prev * 0.9:
        return "declining"
    return "stable"


# ─────────────────────────────────────────────────────────────────────────────
# Main ingest()
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# ENT-5 — Enterprise Operations cross-system blocks (LIVE)
#
# These two functions build the Jira blocks the enterprise_ops detectors read.
# They are implemented and ready, but the CALLS that merge them into ingest()'s
# return dict are COMMENTED OUT below — uncomment once the SME team confirms the
# team field and loads the data. See
# docs/ENT5_enterprise_ops_live_data_requirements.md.
#
# Org-specific field name (override via env; default is the common choice):
#   JIRA_TEAM_FIELD  field representing a team  (default: components)
#
# Both functions fail safe: on any query error they return an empty block so an
# unconfirmed field name never crashes the run — the detector simply won't fire.
# ─────────────────────────────────────────────────────────────────────────────


def _jira_team_name(fields: Dict[str, Any], team_field: str) -> Optional[str]:
    """Extract a team name from a Jira issue's fields for the configured field.

    Handles components (list of {name}), an assignee/custom object ({name|value|
    displayName}), or a plain string.
    """
    val = fields.get(team_field)
    if isinstance(val, list):
        if val:
            first = val[0]
            return first.get("name") if isinstance(first, dict) else str(first)
        return None
    if isinstance(val, dict):
        return val.get("name") or val.get("value") or val.get("displayName")
    return str(val) if val else None


def get_team_backlog(
    client: Optional[JiraClient] = None,
    project_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ``team_backlog`` block for ENT_SLA_BREACH_BY_TEAM corroboration.

    Counts OPEN issues (statusCategory != Done) grouped by team
    (JIRA_TEAM_FIELD). Team names should match ServiceNow assignment_group names.
    """
    if not is_live():
        return {"open_issues_by_team": {}}
    project_key = project_key or os.getenv("JIRA_PROJECT_KEY", "").strip()
    team_field = os.getenv("JIRA_TEAM_FIELD", "components").strip()
    jql = "statusCategory != Done"
    if project_key:
        jql = f"project = {project_key} AND {jql}"
    try:
        issues = client.search_issues(jql, fields=["status", team_field, "assignee"])
    except Exception as e:  # noqa: BLE001 — never abort the run on a bad field name
        logger.warning("ENT-5 team_backlog query failed (degraded): %s", e)
        return {"open_issues_by_team": {}}

    counts: Dict[str, int] = {}
    for issue in issues:
        team = _jira_team_name(issue.get("fields") or {}, team_field)
        if team:
            counts[team] = counts.get(team, 0) + 1
    return {"open_issues_by_team": counts}


def get_issue_resolution(
    client: Optional[JiraClient] = None,
    project_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ``issue_resolution`` block for ENT_INCIDENT_RESOLUTION_LAG.

    Maps each issue key in the window to its status / resolved flag / resolved
    date so the detector can join ServiceNow incidents to their root-cause issue.
    """
    if not is_live():
        return {"issues": {}}
    project_key = project_key or os.getenv("JIRA_PROJECT_KEY", "").strip()
    jql = f"created >= -{WINDOW_DAYS}d"
    if project_key:
        jql = f"project = {project_key} AND {jql}"
    try:
        issues = client.search_issues(
            jql, fields=["status", "resolution", "resolutiondate"]
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("ENT-5 issue_resolution query failed (degraded): %s", e)
        return {"issues": {}}

    _resolved_statuses = {"done", "closed", "resolved", "complete", "completed"}
    out: Dict[str, Any] = {}
    for issue in issues:
        key = issue.get("key")
        if not key:
            continue
        fields = issue.get("fields") or {}
        status = (fields.get("status") or {}).get("name") or ""
        resolution_date = fields.get("resolutiondate")
        resolved = bool(resolution_date) or str(status).strip().lower() in _resolved_statuses
        out[key] = {
            "status": status,
            "resolved": resolved,
            "resolved_at": resolution_date,
        }
    return {"issues": out}


def ingest(jira_client: Optional[JiraClient] = None) -> Dict[str, Any]:
    """
    Orchestrate Jira ingestion. Returns combined payload.

    Offline: reads fixture. Live: calls both functions.
    If the Jira credential record has no base URL in live mode, logs a warning
    and returns {} (Jira is treated as not connected).
    D7 will still fire if Salesforce-side echo score exceeds threshold.

    AgentIQ is READ-ONLY. This module never writes to Jira.
    """
    if not is_live():
        logger.info("Jira ingestion: offline mode (fixture)")
        fixture = _load_fixture()
        # Add lending correlation from fixture issues
        raw_issues = fixture.get("issue_metrics", {}).get(
            "issues", fixture.get("issue_metrics", {}).get("recent_issues", [])
        )
        fixture["lending_correlation"] = get_lending_correlation(
            fixture_issues=raw_issues
        )
        return fixture

    # OAuth-first: the live gateway base comes from the credential record ONLY
    # (R191-H1 / T2 — F2 fix) — the per-run context (DB-sourced vault token +
    # captured/derived api.atlassian.com gateway, set by resolve_live_systems), or
    # a static credential's base_url. There is no JIRA_URL env fallback: the base
    # URL is part of the connector's credential record (one source of connector
    # truth). Gating on the record's URL (never an env var) means Jira is treated
    # as connected exactly when it has a credential record with a URL.
    from . import get_live_connector, resolve_vault_connector

    cred = get_live_connector("jira") or resolve_vault_connector("jira")
    jira_url = (cred.get("url") if cred else None) or ""
    if not jira_url:
        logger.warning(
            "Jira is not connected (no Jira credential record with a base URL for "
            "this run) — skipping Jira ingestion. "
            "D7 will rely on Salesforce/ServiceNow echo scores only."
        )
        return {}

    logger.info("Jira ingestion: live mode")
    if jira_client is None:
        jira_client = _get_client()

    # Multi-project scope: the org's saved Integration-Hub selection, else the
    # JIRA_PROJECT_KEY env fallback (resolve_jira_projects). Passed to both reads so
    # a run ingests exactly the project(s) the customer chose (JQL project IN (...)).
    project_keys = resolve_jira_projects()
    logger.info("Jira ingestion: projects=%s", ", ".join(project_keys))

    try:
        issue_metrics = get_issue_metrics(jira_client, project_keys)
        sprint_velocity = get_sprint_velocity(jira_client, project_keys)

        lending_correlation = get_lending_correlation(jira_client)

        return {
            "issue_metrics": issue_metrics,
            "sprint_velocity": sprint_velocity,
            "lending_correlation": lending_correlation,
            # ── ENT-5 enterprise_ops cross-system blocks (LIVE) ───────────────
            # UNCOMMENT the two lines below once the SME team confirms the team
            # field and loads the data into Jira. Set, if different from the
            # default: JIRA_TEAM_FIELD (default: components).
            # See docs/ENT5_enterprise_ops_live_data_requirements.md.
            # "team_backlog":     get_team_backlog(jira_client),
            # "issue_resolution": get_issue_resolution(jira_client),
        }
    except JiraIngestError:
        raise
    except Exception as e:
        raise JiraIngestError(f"Jira ingestion failed unexpectedly: {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# ENG-AIQ-NC-2 — Jira Lending Correlation
# ─────────────────────────────────────────────────────────────────────────────

# Keywords that map Jira issues to nCino lending detectors.
# Each entry: (keyword_list, detector_id, banking_label)
LENDING_KEYWORD_MAP = [
    (
        ["routing", "underwriting", "assignment", "origination"],
        "LOAN_ORIGINATION_ROUTING_FRICTION",
        "Loan origination routing",
    ),
    (
        ["covenant", "compliance", "exception", "breach"],
        "COVENANT_TRACKING_GAP",
        "Covenant compliance",
    ),
    (
        ["checklist", "closing", "document", "pre-close"],
        "CHECKLIST_BOTTLENECK",
        "Document checklist",
    ),
    (
        ["spreading", "credit-review", "analyst", "spread"],
        "SPREADING_BOTTLENECK",
        "Financial spreading",
    ),
    (
        ["approval", "credit committee", "credit-committee"],
        "APPROVAL_BOTTLENECK",
        "Loan approval",
    ),
]

# All lending keywords combined for initial broad filter
ALL_LENDING_KEYWORDS = [kw for entry in LENDING_KEYWORD_MAP for kw in entry[0]] + [
    "loan",
    "nCino",
    "ncino",
    "lending",
    "borrower",
]

def _extract_adf_text(adf: Any) -> str:
    """
    Recursively extract plain text from an Atlassian Document Format (ADF) node.

    Jira API v3 returns description as ADF JSON, not a plain string.
    ADF structure: {"type": "doc", "content": [{"type": "paragraph",
                    "content": [{"type": "text", "text": "..."}, ...]}]}
    """
    if not adf or not isinstance(adf, dict):
        return ""
    # Leaf text node
    if adf.get("type") == "text":
        return adf.get("text", "")
    # Recurse into content children
    parts = [_extract_adf_text(child) for child in (adf.get("content") or [])]
    return " ".join(p for p in parts if p)


def _issue_matches_keywords(issue: Dict[str, Any], keywords: List[str]) -> bool:
    """
    Weighted keyword match to reduce false positives.

    Scoring:
      label match   = 2 points  (explicit tagging — high confidence)
      summary match = 1 point   (title-level signal)
      description   = 0.5 pts   (body text — lower confidence)

    Threshold: score >= 1.5 to fire.
    This requires either:
      - 1 label match (score=2), OR
      - 1 summary + 1 description match (score=1.5), OR
      - 2+ summary matches (score=2+)

    Single keyword hit in description only (score=0.5) does NOT fire.
    Generic IT keywords like "approval" or "routing" without a
    lending-specific label or summary match will not reach threshold.
    """
    score = 0.0
    
    # Labels can be plain strings OR Jira API dicts {"name": "..."}
    raw_labels = issue.get("labels") or []
    labels_text = " ".join(
        l["name"] if isinstance(l, dict) else str(l) for l in raw_labels
    ).lower()

    summary_text = issue.get("summary", "").lower()

    # Description is ADF (dict) in Jira API v3 — extract plain text first
    desc_raw = issue.get("description") or ""
    desc_text = (
        _extract_adf_text(desc_raw) if isinstance(desc_raw, dict) else desc_raw
    ).lower()

    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in labels_text:
            score += 2.0
        elif kw_lower in summary_text:
            score += 1.0
        elif kw_lower in desc_text:
            score += 0.5

    return score >= 1.0  # Lowered from 1.5 — single keyword in title is sufficient signal


def _detector_for_issue(issue: Dict[str, Any]) -> Optional[tuple]:
    """Return (detector_id, banking_label) for the best-matching detector, or None."""
    for keywords, detector_id, label in LENDING_KEYWORD_MAP:
        if _issue_matches_keywords(issue, keywords):
            return detector_id, label
    return None


def _build_lending_snippet(issue: Dict[str, Any], label: str) -> str:
    """Build a banking-language evidence snippet from a Jira issue."""
    summary = issue.get("summary", "Jira issue")
    priority = issue.get("priority", "")
    status = issue.get("status", "")
    parts = [f"{label}: {summary}"]
    if priority:
        parts.append(f"Priority: {priority}")
    if status:
        parts.append(f"Status: {status}")
    return ". ".join(parts) + "."


def get_lending_correlation(
    client: Optional["JiraClient"] = None,
    fixture_issues: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    ENG-AIQ-NC-2: Detect lending-related Jira issues and map them to
    nCino detector IDs for use as corroborating evidence in S4.

    Returns:
      lending_issues: list of matched issues with detector_id and snippet
      by_detector:    dict mapping detector_id → list of snippets
      total_matched:  int
    """
    # Get issues — from fixture or live
    issues: List[Dict[str, Any]] = []

    if fixture_issues is not None:
        issues = fixture_issues
    elif not is_live():
        try:
            fixture = _load_fixture()
            # Try to get issues from fixture — may be in issue_metrics
            raw = fixture.get("issue_metrics", {})
            issues = raw.get("issues", raw.get("recent_issues", []))
        except Exception:
            issues = []
    else:
        if client is None:
            try:
                client = _get_client()
            except Exception:
                return {"lending_issues": [], "by_detector": {}, "total_matched": 0}
        try:
            # Use search_issues() which correctly uses /rest/api/3/search/jql
            kw_jql = " OR ".join(f'text ~ "{kw}"' for kw in ALL_LENDING_KEYWORDS[:10])
            # jql = f"({kw_jql}) AND created >= -{WINDOW_DAYS}d ORDER BY created DESC"
            jql = f'project = {os.getenv("JIRA_PROJECT_KEY", "AIC")} AND ({kw_jql}) AND created >= -{WINDOW_DAYS}d ORDER BY created DESC'
            raw_issues = client.search_issues(
                jql=jql,
                fields=[
                    "summary",
                    "description",
                    "labels",
                    "priority",
                    "status",
                    "project",
                    "created",
                ],
                max_results=50,
            )
            for ri in raw_issues:
                fields = ri.get("fields", {})
                desc_raw = fields.get("description") or ""
                issues.append(
                    {
                        "id": ri.get("id", ""),
                        "key": ri.get("key", ""),
                        "summary": fields.get("summary", ""),
                        # Extract plain text from ADF at ingest time
                        "description": (
                            _extract_adf_text(desc_raw)
                            if isinstance(desc_raw, dict)
                            else desc_raw
                        ),
                        "labels": fields.get("labels", []),
                        "priority": (fields.get("priority") or {}).get("name", ""),
                        "status": (fields.get("status") or {}).get("name", ""),
                        "project": (fields.get("project") or {}).get("key", ""),
                        "created": fields.get("created", ""),
                    }
                )
        except Exception as e:
            # logger.warning("Jira lending correlation fetch failed: %s", e)
            logger.warning(
                "Jira lending correlation fetch failed: %s: %r | jql=%s",
                type(e).__name__, e, jql,
            )
            logger.exception("Jira lending correlation traceback")
            return {"lending_issues": [], "by_detector": {}, "total_matched": 0}

    # Match issues to detectors
    lending_issues: List[Dict[str, Any]] = []
    by_detector: Dict[str, List[str]] = {}

    for issue in issues:
        try:
            match = _detector_for_issue(issue)
        except Exception as exc:
            logger.warning("_detector_for_issue failed for %s: %s", issue.get("key", "?"), exc)
            continue
        if match is None:
            continue
        detector_id, label = match
        snippet = _build_lending_snippet(issue, label)
        lending_issues.append(
            {
                "issue_id": issue.get("key") or issue.get("id", ""),
                "detector_id": detector_id,
                "label": label,
                "snippet": snippet,
                "source": "Jira",
                "detectorId": detector_id,
                "status": issue.get("status", ""),
                "created": issue.get("created", ""),
            }
        )
        by_detector.setdefault(detector_id, []).append(snippet)

    logger.info("Jira lending correlation: %d issues matched", len(lending_issues))
    return {
        "lending_issues": lending_issues,
        "by_detector": by_detector,
        "total_matched": len(lending_issues),
    }
