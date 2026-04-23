"""
SF-2.4 — Jira Ingestion Module

Offline mode: reads backend/discovery/ingest/fixtures/jira_sample.json
Live mode:    calls Jira REST API v3

Environment variables for live mode:
    JIRA_URL     e.g. https://mycompany.atlassian.net
    JIRA_TOKEN   API token (personal access token or OAuth)
    JIRA_USER    Email address associated with the token (required for cloud)

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

from . import is_live

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
    Minimal Jira REST API v3 client.

    Auth: API token with basic auth (email:token base64).
    Jira Cloud requires email + API token (not password).
    Jira Server/DC supports PAT (personal access token) as Bearer.
    """

    def __init__(self, base_url: str, user: str = "", token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.token = token
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
            self._session.headers.update({
                "Accept": "application/json",
                "Content-Type": "application/json",
            })
            if self.user and self.token:
                # Jira Cloud: basic auth with email + API token
                self._session.auth = (self.user, self.token)
            elif self.token:
                # Jira Server/DC: Bearer PAT
                self._session.headers["Authorization"] = f"Bearer {self.token}"
            else:
                raise JiraIngestError(
                    "Live mode requires JIRA_TOKEN. "
                    "For Jira Cloud also set JIRA_USER (email). "
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
            "summary", "status", "issuetype", "labels",
            "story_points", "customfield_10016",  # story points field (cloud + server)
            "created", "resolutiondate", "assignee",
        ]

        while True:
            data = self.get(
                f"/rest/api/{JIRA_API_VERSION}/search",
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
                params={"projectKeyOrId": project_key, "type": "scrum"},
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


def _get_client() -> JiraClient:
    jira_url = os.getenv("JIRA_URL", "").rstrip("/")
    token = os.getenv("JIRA_TOKEN", "")
    user = os.getenv("JIRA_USER", "")

    if not jira_url:
        raise JiraIngestError(
            "Live mode requires JIRA_URL. "
            "Set INGEST_MODE=offline to run without credentials."
        )
    if not token:
        raise JiraIngestError(
            "Live mode requires JIRA_TOKEN. "
            "For Jira Cloud also set JIRA_USER (email address)."
        )
    return JiraClient(jira_url, user=user, token=token)


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

    if not project_key:
        project_key = os.getenv("JIRA_PROJECT_KEY", "CRM")

    # Total issues in window
    all_issues = client.search_issues(
        jql=f"project = {project_key} AND created >= -{WINDOW_DAYS}d",
        fields=["summary", "status", "issuetype", "labels",
                "customfield_10016", "customfield_10002", "customfield_10004"],
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
            f"project = {project_key} AND created >= -{WINDOW_DAYS}d "
            f"AND (summary ~ \"CS-\" OR labels = \"Salesforce\" OR labels = \"salesforce-case\")"
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
        sample_cross_refs.append({
            "issue_key": issue.get("key", ""),
            "sf_reference": _extract_sf_case_id(summary),
            "field": "summary",
            "summary": summary[:120],
        })

    return {
        "total_issues_90d": total,
        "project": project_key,
        "salesforce_label_count": salesforce_label_count,
        "jira_echo_score": jira_echo_score,
        "issue_type_breakdown": [
            {"type": k, "count": v} for k, v in type_counts.items()
        ],
        "sample_cross_references": sample_cross_refs,
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

    if not project_key:
        project_key = os.getenv("JIRA_PROJECT_KEY", "CRM")

    # Find the board for this project
    boards = client.get_boards(project_key)
    if not boards:
        logger.warning(
            f"No Scrum boards found for Jira project {project_key}. "
            f"Sprint velocity will be empty. "
            f"Project may be Kanban or non-sprint based."
        )
        return []

    target_board_id = board_id or boards[0]["id"]
    sprints = client.get_recent_sprints(target_board_id, limit=3)

    if not sprints:
        logger.warning(f"No closed sprints found for board {target_board_id}.")
        return []

    results: List[Dict] = []

    for sprint in sprints:
        sprint_id = sprint["id"]
        sprint_name = sprint.get("name", f"Sprint {sprint_id}")

        # Second call: get issues WITH story points
        sprint_issues = client.get_sprint_issues(sprint_id)

        # Completed issues (status = Done)
        completed_issues = [
            i for i in sprint_issues
            if (i.get("fields") or {}).get("status", {}).get("name", "").lower() in
               ("done", "closed", "resolved", "complete")
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
        sf_count = sum(
            1 for i in sprint_issues
            if _is_salesforce_related(i)
        )

        results.append({
            "sprint_name": sprint_name,
            "completed_points": round(completed_points, 1),
            "salesforce_issue_count": sf_count,
            "velocity_unit": velocity_unit,
            "velocity_trend": _compute_trend(results),
        })

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

def ingest(jira_client: Optional[JiraClient] = None) -> Dict[str, Any]:
    """
    Orchestrate Jira ingestion. Returns combined payload.

    Offline: reads fixture. Live: calls both functions.
    If JIRA_URL is not set in live mode, logs warning and returns {}.
    D7 will still fire if Salesforce-side echo score exceeds threshold.

    AgentIQ is READ-ONLY. This module never writes to Jira.
    """
    if not is_live():
        logger.info("Jira ingestion: offline mode (fixture)")
        return _load_fixture()

    jira_url = os.getenv("JIRA_URL", "")
    if not jira_url:
        logger.warning(
            "JIRA_URL not set — skipping Jira ingestion. "
            "D7 will rely on Salesforce/ServiceNow echo scores only."
        )
        return {}

    logger.info("Jira ingestion: live mode")
    if jira_client is None:
        jira_client = _get_client()

    try:
        issue_metrics = get_issue_metrics(jira_client)
        sprint_velocity = get_sprint_velocity(jira_client)

        return {
            "issue_metrics": issue_metrics,
            "sprint_velocity": sprint_velocity,
        }
    except JiraIngestError:
        raise
    except Exception as e:
        raise JiraIngestError(f"Jira ingestion failed unexpectedly: {e}") from e
