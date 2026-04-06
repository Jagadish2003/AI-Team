from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests

from ..types import IngestError
from ..log import warn

FIXTURE = Path(__file__).parent / "fixtures" / "jira_mock.json"

def _mode() -> str:
    return os.getenv("INGEST_MODE", "offline").lower()

def _load_fixture() -> Dict[str, Any]:
    if not FIXTURE.exists():
        raise IngestError(f"Missing Jira fixture: {FIXTURE}. Provide it or remove jira from --systems.")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))

def _get_env(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v if v and v.strip() else None

def get_issue_metrics() -> List[Dict[str, Any]]:
    if _mode() == "offline":
        return _load_fixture()["issue_metrics"]

    base = _get_env("JIRA_URL")
    token = _get_env("JIRA_TOKEN")
    if not base or not token:
        warn("Jira live credentials missing (JIRA_URL/JIRA_TOKEN). Skipping Jira ingestion.")
        return []

    try:
        url = f"{base.rstrip('/')}/rest/api/3/search"
        jql = "created >= -90d"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        r = requests.get(url, params={"jql": jql, "maxResults": 100}, headers=headers, timeout=30)
        r.raise_for_status()
        issues = r.json().get("issues", [])
    except Exception as e:
        raise IngestError(f"Jira get_issue_metrics failed: {e}") from e

    by_proj: Dict[str, Dict[str, Any]] = {}
    for it in issues:
        fields = it.get("fields", {})
        proj = (fields.get("project") or {}).get("key") or "UNKNOWN"
        labels = fields.get("labels") or []
        by_proj.setdefault(proj, {"project": proj, "volume": 0, "salesforce_label_count": 0})
        by_proj[proj]["volume"] += 1
        if any("salesforce" in str(l).lower() for l in labels):
            by_proj[proj]["salesforce_label_count"] += 1
    return list(by_proj.values())

def get_sprint_velocity() -> List[Dict[str, Any]]:
    if _mode() == "offline":
        return _load_fixture()["sprint_velocity"]

    base = _get_env("JIRA_URL")
    token = _get_env("JIRA_TOKEN")
    board_id = _get_env("JIRA_BOARD_ID")
    if not base or not token or not board_id:
        warn("Jira live credentials missing (JIRA_URL/JIRA_TOKEN/JIRA_BOARD_ID). Skipping sprint velocity.")
        return []

    try:
        url = f"{base.rstrip('/')}/rest/agile/1.0/board/{board_id}/sprint"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        r = requests.get(url, params={"state": "closed", "maxResults": 10}, headers=headers, timeout=30)
        r.raise_for_status()
        sprints = r.json().get("values", [])
    except Exception as e:
        raise IngestError(f"Jira get_sprint_velocity failed: {e}") from e

    # TODO: Jira sprint metadata does not include story points or issue counts.
        # completed_points and salesforce_issue_count require a second call per sprint:
        #   GET /rest/agile/1.0/sprint/{sprint_id}/issue
        # These fields remain None until that extension is implemented.
        return [{"sprint_name": s.get("name"), "completed_points": None, "salesforce_issue_count": None} for s in sprints]
