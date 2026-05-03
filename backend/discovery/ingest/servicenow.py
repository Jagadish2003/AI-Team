"""
SF-2.3 — ServiceNow Ingestion Module

Offline mode: reads backend/discovery/ingest/fixtures/servicenow_sample.json
Live mode:    calls ServiceNow REST Table API

Environment variables for live mode:
    SERVICENOW_URL    e.g. https://myinstance.service-now.com
    SERVICENOW_TOKEN  Bearer token (or use SERVICENOW_USER + SERVICENOW_PASS for basic auth)
    SERVICENOW_USER   (optional) basic auth username
    SERVICENOW_PASS   (optional) basic auth password

Known fix applied (vs earlier stub):
    - total_count is fetched from the aggregate API, not hardcoded as 0
    - echo_score = match_count / total_count (not hardcoded as 0.0)

D7 signal produced:
    sn_echo_score = incidents referencing SF case IDs / total incidents in window
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import is_live

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "servicenow_sample.json"
SN_API_VERSION = "v1"
WINDOW_DAYS = 90


# ─────────────────────────────────────────────────────────────────────────────
# Custom exception
# ─────────────────────────────────────────────────────────────────────────────

class ServiceNowIngestError(Exception):
    """Raised when live ServiceNow ingestion fails."""


# ─────────────────────────────────────────────────────────────────────────────
# Offline loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_fixture() -> Dict[str, Any]:
    if not FIXTURE_PATH.exists():
        raise ServiceNowIngestError(f"ServiceNow fixture not found: {FIXTURE_PATH}")
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# ServiceNow REST client
# ─────────────────────────────────────────────────────────────────────────────

class ServiceNowClient:
    """
    Minimal ServiceNow Table API client with pagination support.

    Auth priority:
      1. Bearer token (SERVICENOW_TOKEN)
      2. Basic auth (SERVICENOW_USER + SERVICENOW_PASS)
    """

    def __init__(self, instance_url: str, token: str = "", user: str = "", password: str = ""):
        self.instance_url = instance_url.rstrip("/")
        self.token = token
        self.user = user
        self.password = password
        self._session = None

    def _get_session(self):
        try:
            import requests
        except ImportError:
            raise ServiceNowIngestError(
                "requests library required for live mode: pip install requests"
            )
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "Accept": "application/json",
                "Content-Type": "application/json",
            })
            if self.token:
                self._session.headers["Authorization"] = f"Bearer {self.token}"
            elif self.user and self.password:
                self._session.auth = (self.user, self.password)
            else:
                raise ServiceNowIngestError(
                    "Live mode requires SERVICENOW_TOKEN or SERVICENOW_USER + SERVICENOW_PASS"
                )
        return self._session

    def table_query(
        self,
        table: str,
        params: Dict[str, Any],
        max_records: int = 10000,
    ) -> List[Dict]:
        """
        Query a ServiceNow table with sysparm_offset pagination.

        max_records: safety cap — raises if exceeded.
        ServiceNow uses offset-based pagination (not cursor), so we step
        through sysparm_offset in sysparm_limit increments.
        """
        session = self._get_session()
        limit = min(params.get("sysparm_limit", 1000), 1000)  # SN max page = 1000
        offset = 0
        all_records: List[Dict] = []

        base_url = f"{self.instance_url}/api/now/table/{table}"
        query_params = {**params, "sysparm_limit": limit}

        while True:
            query_params["sysparm_offset"] = offset
            try:
                resp = session.get(base_url, params=query_params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                records = data.get("result", [])
                if not records:
                    break
                all_records.extend(records)
                if len(all_records) >= max_records:
                    raise ServiceNowIngestError(
                        f"ServiceNow table/{table} result exceeded {max_records} records. "
                        f"Narrow the query window."
                    )
                if len(records) < limit:
                    break  # last page
                offset += limit
            except ServiceNowIngestError:
                raise
            except Exception as e:
                raise ServiceNowIngestError(
                    f"ServiceNow table/{table} query failed: {e}"
                )

        return all_records

    def aggregate_count(self, table: str, sysparm_query: str = "") -> int:
        """
        Use the ServiceNow Aggregate API to count records without fetching them.
        This is the correct way to get total_count — not a full table scan.

        API: GET /api/now/stats/{table}?sysparm_count=true&sysparm_query=...
        """
        session = self._get_session()
        url = f"{self.instance_url}/api/now/stats/{table}"
        params: Dict[str, Any] = {"sysparm_count": "true"}
        if sysparm_query:
            params["sysparm_query"] = sysparm_query

        try:
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            # Response shape: {"result": {"stats": {"count": "500"}}}
            count_str = (
                data.get("result", {})
                    .get("stats", {})
                    .get("count", "0")
            )
            return int(count_str)
        except ServiceNowIngestError:
            raise
        except Exception as e:
            raise ServiceNowIngestError(
                f"ServiceNow aggregate count on {table} failed: {e}"
            )


def _get_client() -> ServiceNowClient:
    sn_url = os.getenv("SERVICENOW_URL", "").rstrip("/")
    token = os.getenv("SERVICENOW_TOKEN", "")
    user = os.getenv("SERVICENOW_USER", "")
    password = os.getenv("SERVICENOW_PASS", "")

    if not sn_url:
        raise ServiceNowIngestError(
            "Live mode requires SERVICENOW_URL. "
            "Set INGEST_MODE=offline to run without credentials."
        )
    return ServiceNowClient(sn_url, token=token, user=user, password=password)


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion functions
# ─────────────────────────────────────────────────────────────────────────────

def get_incident_metrics(client: Optional[ServiceNowClient] = None) -> Dict[str, Any]:
    """
    Pull ServiceNow incident volume, category breakdown, and avg resolution time.

    Live API calls:
        -- Total incidents in 90-day window (aggregate — no record fetch)
        GET /api/now/stats/incident
            ?sysparm_count=true
            &sysparm_query=sys_created_on>=javascript:gs.daysAgo(90)

        -- Incidents by category with avg resolution
        GET /api/now/table/incident
            ?sysparm_query=sys_created_on>=javascript:gs.daysAgo(90)
            &sysparm_fields=category,state,assignment_group,resolved_at,sys_created_on
            &sysparm_limit=1000

    Returns: incident_metrics dict matching servicenow_sample.json shape
    """
    if not is_live():
        return _load_fixture()["incident_metrics"]

    window_query = f"sys_created_on>=javascript:gs.daysAgo({WINDOW_DAYS})"

    # Total count — aggregate API, not a full record fetch
    total = client.aggregate_count("incident", window_query)

    # Incident details for category breakdown
    records = client.table_query(
        "incident",
        {
            "sysparm_query": window_query,
            "sysparm_fields": "category,state,assignment_group,resolved_at,sys_created_on",
        },
    )

    # Category breakdown
    category_map: Dict[str, Dict] = {}
    total_resolution_hours = 0.0
    resolved_count = 0

    for r in records:
        cat = r.get("category") or "uncategorized"
        if cat not in category_map:
            category_map[cat] = {"category": cat, "volume": 0, "avg_resolution_hours": 0.0}
        category_map[cat]["volume"] += 1

        resolved_at = r.get("resolved_at", "")
        created = r.get("sys_created_on", "")
        if resolved_at and created:
            try:
                from datetime import datetime
                # SN format: "2026-01-15 09:22:31"
                fmt = "%Y-%m-%d %H:%M:%S"
                delta = datetime.strptime(resolved_at, fmt) - datetime.strptime(created, fmt)
                hours = delta.total_seconds() / 3600
                category_map[cat]["avg_resolution_hours"] = round(
                    (category_map[cat].get("_total_hours", 0.0) + hours)
                    / (category_map[cat]["volume"]),
                    1,
                )
                category_map[cat]["_total_hours"] = category_map[cat].get("_total_hours", 0.0) + hours
                total_resolution_hours += hours
                resolved_count += 1
            except Exception:
                pass

    avg_resolution = round(total_resolution_hours / resolved_count, 1) if resolved_count > 0 else 0.0

    # Remove internal tracking key
    for v in category_map.values():
        v.pop("_total_hours", None)

    return {
        "total_incidents_90d": total,
        "avg_resolution_hours": avg_resolution,
        "avg_reassignment_count": 0.0,  # Extended in SF-3.2 using reassignment_count field
        "category_breakdown": list(category_map.values()),
    }


def get_cross_system_references(
    client: Optional[ServiceNowClient] = None,
    patterns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Detect ServiceNow incidents that reference Salesforce case IDs (D7 signal).

    The echo_score = match_count / total_incidents_in_window.

    IMPORTANT — total_count derivation:
        This uses the Aggregate API to get total incident count, NOT a hardcoded
        value and NOT a count of the matched records only. This was the known bug
        in the earlier stub where total_count was hardcoded as 0, making
        echo_score always 0.0.

    Search fields inspected:
        short_description — case reference in summary
        description       — full incident description
        work_notes        — agent work log (most common location for CS- refs)
        comments          — customer-visible comments

    Live API calls:
        -- Total incidents (aggregate)
        GET /api/now/stats/incident?sysparm_count=true&sysparm_query=window

        -- Incidents matching pattern in any of the four fields
        GET /api/now/table/incident
            ?sysparm_query=short_descriptionCONTAINS{pattern}^ORdescriptionCONTAINS{pattern}...
            &sysparm_fields=sys_id,short_description,description,work_notes

    Returns: cross_system_references dict matching servicenow_sample.json shape
    """
    if not is_live():
        return _load_fixture()["cross_system_references"]

    if patterns is None:
        patterns = ["CS-"]  # Salesforce Case ID prefix

    window_query = f"sys_created_on>=javascript:gs.daysAgo({WINDOW_DAYS})"

    # Total incident count — aggregate, not a table scan
    total = client.aggregate_count("incident", window_query)

    if total == 0:
        logger.warning("ServiceNow: no incidents in window — echo_score will be 0.0")
        return {
            "sn_match_count": 0,
            "sn_total_incidents": 0,
            "sn_echo_score": 0.0,
            "matched_pattern": patterns[0] if patterns else "",
            "sample_matches": [],
        }

    match_count = 0
    sample_matches: List[Dict] = []
    matched_pattern = patterns[0] if patterns else ""

    for pattern in patterns:
        # Build OR query across all four description fields
        field_conditions = "^OR".join([
            f"short_descriptionCONTAINS{pattern}",
            f"descriptionCONTAINS{pattern}",
            f"work_notesCONTAINS{pattern}",
            f"commentsCONTAINS{pattern}",
        ])
        full_query = f"{window_query}^({field_conditions})"

        # Count matches — aggregate first, then fetch samples
        pattern_count = client.aggregate_count("incident", full_query)
        match_count += pattern_count

        if pattern_count > 0 and len(sample_matches) < 5:
            sample_recs = client.table_query(
                "incident",
                {
                    "sysparm_query": full_query,
                    "sysparm_fields": "number,short_description,description,work_notes",
                    "sysparm_limit": 5,
                },
            )
            for r in sample_recs[:5]:
                # Determine which field contained the match
                match_field = "short_description"
                for fld in ("short_description", "description", "work_notes", "comments"):
                    if pattern in (r.get(fld) or ""):
                        match_field = fld
                        break
                sample_matches.append({
                    "incident_id": r.get("number", ""),
                    "pattern": pattern,
                    "field": match_field,
                    "short_description": (r.get("short_description") or "")[:120],
                })

    # echo_score: correctly derived from real total_count
    sn_echo_score = round(match_count / total, 4) if total > 0 else 0.0

    return {
        "sn_match_count": match_count,
        "sn_total_incidents": total,
        "sn_echo_score": sn_echo_score,
        "matched_pattern": matched_pattern,
        "sample_matches": sample_matches,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main ingest()
# ─────────────────────────────────────────────────────────────────────────────

def ingest(sn_client: Optional[ServiceNowClient] = None) -> Dict[str, Any]:
    """
    Orchestrate ServiceNow ingestion. Returns combined payload.

    Offline: reads fixture. Live: calls both functions.
    If SERVICENOW_URL is not set in live mode, logs warning and returns {}.
    The runner treats empty SN data as graceful skip — D7 will still fire
    from the Salesforce sf_echo_score side if that threshold is met.
    """
    if not is_live():
        logger.info("ServiceNow ingestion: offline mode (fixture)")
        fixture = _load_fixture()
        raw_incidents = fixture.get("incident_metrics", {}).get("incidents",
                        fixture.get("incident_metrics", {}).get("recent_incidents", []))
        fixture["lending_correlation"] = get_lending_correlation(
            fixture_incidents=raw_incidents
        )
        return fixture

    sn_url = os.getenv("SERVICENOW_URL", "")
    if not sn_url:
        logger.warning(
            "SERVICENOW_URL not set — skipping ServiceNow ingestion. "
            "D7 will rely on Salesforce-side echo score only."
        )
        return {}

    logger.info("ServiceNow ingestion: live mode")
    if sn_client is None:
        sn_client = _get_client()

    try:
        incident_metrics = get_incident_metrics(sn_client)
        cross_system_references = get_cross_system_references(sn_client)

        lending_correlation = get_lending_correlation(client)

        return {
            "incident_metrics":       incident_metrics,
            "cross_system_references": cross_system_references,
            "lending_correlation":    lending_correlation,
        }
    except ServiceNowIngestError:
        raise
    except Exception as e:
        raise ServiceNowIngestError(f"ServiceNow ingestion failed: {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# ENG-AIQ-NC-3 — ServiceNow Lending Correlation
# ─────────────────────────────────────────────────────────────────────────────

SN_LENDING_KEYWORD_MAP = [
    (["covenant", "compliance", "breach", "covenant status"],
     "COVENANT_TRACKING_GAP",
     "Covenant compliance"),
    (["checklist", "document exception", "closing", "pre-close"],
     "CHECKLIST_BOTTLENECK",
     "Document checklist"),
    (["routing", "origination", "reassignment", "underwriting assignment"],
     "LOAN_ORIGINATION_ROUTING_FRICTION",
     "Loan origination routing"),
    (["spreading", "spread", "analyst", "credit analyst"],
     "SPREADING_BOTTLENECK",
     "Financial spreading"),
    (["approval", "credit committee", "loan approval", "approval notification"],
     "APPROVAL_BOTTLENECK",
     "Loan approval"),
]

SN_ALL_LENDING_KEYWORDS = [
    kw for entry in SN_LENDING_KEYWORD_MAP for kw in entry[0]
] + ["loan", "nCino", "ncino", "lending", "borrower"]


def _sn_incident_matches(incident: Dict[str, Any], keywords: List[str]) -> bool:
    """
    Weighted keyword match to reduce false positives.

    Scoring:
      category/subcategory match = 2 points  (explicit classification)
      short_description match    = 1 point   (title-level signal)
      description match          = 0.5 pts   (body text)

    Threshold: score >= 1.5 to fire.
    Single keyword in description only does NOT fire.
    Generic terms like "loan" or "routing" without category or
    short_description match will not reach threshold.
    """
    score = 0.0
    cat_text = " ".join([
        incident.get("category", ""),
        incident.get("subcategory", "") or "",
    ]).lower()
    short_text = incident.get("short_description", "").lower()
    desc_text  = (incident.get("description", "") or "").lower()

    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in cat_text:
            score += 2.0
        elif kw_lower in short_text:
            score += 1.0
        elif kw_lower in desc_text:
            score += 0.5

    return score >= 1.5


def _sn_detector_for_incident(incident: Dict[str, Any]) -> Optional[tuple]:
    """Return (detector_id, banking_label) for best-matching detector, or None."""
    for keywords, detector_id, label in SN_LENDING_KEYWORD_MAP:
        if _sn_incident_matches(incident, keywords):
            return detector_id, label
    return None


def _sn_build_lending_snippet(incident: Dict[str, Any], label: str) -> str:
    """Build a banking-language evidence snippet from a ServiceNow incident."""
    short_desc = incident.get("short_description", "ServiceNow incident")
    priority = incident.get("priority", "")
    state = incident.get("state", "")
    parts = [f"{label}: {short_desc}"]
    if priority:
        parts.append(f"Priority: {priority}")
    if state:
        parts.append(f"State: {state}")
    return ". ".join(parts) + "."


def get_lending_correlation(
    client: Optional["ServiceNowClient"] = None,
    fixture_incidents: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    ENG-AIQ-NC-3: Detect lending-related ServiceNow incidents and map them
    to nCino detector IDs for use as corroborating evidence in S4.

    Returns:
      lending_incidents: list of matched incidents with detector_id and snippet
      by_detector:       dict mapping detector_id → list of snippets
      total_matched:     int
    """
    incidents: List[Dict[str, Any]] = []

    if fixture_incidents is not None:
        incidents = fixture_incidents
    elif not is_live():
        try:
            fixture = _load_fixture()
            raw = fixture.get("incident_metrics", {})
            incidents = raw.get("incidents", raw.get("recent_incidents", []))
        except Exception:
            incidents = []
    else:
        if client is None:
            try:
                client = _get_client()
            except Exception:
                return {"lending_incidents": [], "by_detector": {}, "total_matched": 0}
        try:
            # Fetch recent incidents with lending keywords
            kw_filter = "^".join(
                f"short_descriptionLIKE{kw}^ORdescriptionLIKE{kw}"
                for kw in SN_ALL_LENDING_KEYWORDS[:6]
            )
            query = f"active=true^{kw_filter}^ORDERBYDESCsys_created_on"
            result = client.get(
                "/api/now/table/incident",
                params={
                    "sysparm_query": query,
                    "sysparm_limit": "50",
                    "sysparm_fields": (
                        "sys_id,number,short_description,description,"
                        "category,subcategory,priority,state,sys_created_on"
                    ),
                },
            )
            for inc in result.get("result", []):
                incidents.append({
                    "id":                inc.get("sys_id", ""),
                    "number":            inc.get("number", ""),
                    "short_description": inc.get("short_description", ""),
                    "description":       inc.get("description", "") or "",
                    "category":          inc.get("category", ""),
                    "subcategory":       inc.get("subcategory", "") or "",
                    "priority":          inc.get("priority", ""),
                    "state":             inc.get("state", ""),
                })
        except Exception as e:
            logger.warning("ServiceNow lending correlation fetch failed: %s", e)
            return {"lending_incidents": [], "by_detector": {}, "total_matched": 0}

    # Match incidents to detectors
    lending_incidents: List[Dict[str, Any]] = []
    by_detector: Dict[str, List[str]] = {}

    for incident in incidents:
        match = _sn_detector_for_incident(incident)
        if match is None:
            continue
        detector_id, label = match
        snippet = _sn_build_lending_snippet(incident, label)
        lending_incidents.append({
            "incident_id": incident.get("number") or incident.get("id", ""),
            "detector_id": detector_id,
            "label":       label,
            "snippet":     snippet,
            "source":      "ServiceNow",
            "detectorId":  detector_id,
        })
        by_detector.setdefault(detector_id, []).append(snippet)

    logger.info("SN lending correlation: %d incidents matched", len(lending_incidents))
    return {
        "lending_incidents": lending_incidents,
        "by_detector":       by_detector,
        "total_matched":     len(lending_incidents),
    }
