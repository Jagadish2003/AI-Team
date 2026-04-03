from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests

from ..types import IngestError
from ..log import warn

FIXTURE = Path(__file__).parent / "fixtures" / "servicenow_mock.json"

def _mode() -> str:
    return os.getenv("INGEST_MODE", "offline").lower()

def _load_fixture() -> Dict[str, Any]:
    if not FIXTURE.exists():
        raise IngestError(f"Missing ServiceNow fixture: {FIXTURE}. Provide it or remove servicenow from --systems.")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))

def _get_env(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v if v and v.strip() else None

def get_incident_metrics() -> List[Dict[str, Any]]:
    if _mode() == "offline":
        return _load_fixture()["incident_metrics"]

    base = _get_env("SERVICENOW_URL")
    token = _get_env("SERVICENOW_TOKEN")
    if not base or not token:
        warn("ServiceNow live credentials missing (SERVICENOW_URL/SERVICENOW_TOKEN). Skipping ServiceNow ingestion.")
        return []

    try:
        url = f"{base.rstrip('/')}/api/now/table/incident"
        params = {"sysparm_fields": "category,reassignment_count", "sysparm_limit": "1000"}
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        r = requests.get(url, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        rows = r.json().get("result", [])
    except Exception as e:
        raise IngestError(f"ServiceNow get_incident_metrics failed: {e}") from e

    by_cat: Dict[str, Dict[str, Any]] = {}
    for it in rows:
        cat = it.get("category") or "Uncategorized"
        by_cat.setdefault(cat, {"category": cat, "volume": 0, "reassignments": []})
        by_cat[cat]["volume"] += 1
        try:
            by_cat[cat]["reassignments"].append(float(it.get("reassignment_count") or 0))
        except Exception:
            pass

    out = []
    for cat, agg in by_cat.items():
        ra = agg["reassignments"]
        out.append({"category": cat, "volume": agg["volume"], "avg_reassignments": (sum(ra)/len(ra)) if ra else 0.0})
    return out

def get_cross_system_references(external_pattern: str = "SF-") -> Dict[str, Any]:
    if _mode() == "offline":
        return _load_fixture()["cross_system_references"]

    base = _get_env("SERVICENOW_URL")
    token = _get_env("SERVICENOW_TOKEN")
    if not base or not token:
        warn("ServiceNow live credentials missing. Skipping cross-system reference query.")
        return {"match_count": 0, "total_count": 0, "echo_score": 0.0}

    try:
        url = f"{base.rstrip('/')}/api/now/table/incident"
        query = f"short_descriptionLIKE{external_pattern}^ORdescriptionLIKE{external_pattern}"
        params = {"sysparm_query": query, "sysparm_fields": "sys_id", "sysparm_limit": "1000"}
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        r = requests.get(url, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        matches = r.json().get("result", [])
        match_count = len(matches)
        # Attempt to fetch total count. Many ServiceNow instances expose total via the X-Total-Count header.
        total_count = 0
        try:
            total_r = requests.get(
                url,
                params={"sysparm_fields": "sys_id", "sysparm_limit": "1", "sysparm_count": "true"},
                headers=headers,
                timeout=30,
            )
            total_r.raise_for_status()
            total_count = int(total_r.headers.get("X-Total-Count", 0))
        except Exception:
            # Fallback: keep total_count=0 if the instance does not expose totals without special config.
            total_count = 0

        echo_score = (match_count / total_count) if total_count > 0 else 0.0
        return {"match_count": match_count, "total_count": total_count, "echo_score": echo_score}
    except Exception as e:
        raise IngestError(f"ServiceNow get_cross_system_references failed: {e}") from e
