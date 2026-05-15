"""
strs_sn_corroboration.py — Fix Pack Sprint 7
ENG-STRS-CORR-2

ServiceNow corroboration for STRS Benefits Administration detectors.

Follows the exact same pattern as the nCino lending_correlation in servicenow.py:
  - SN_STRS_KEYWORD_MAP: (keywords, detector_id, label) per detector
  - _sn_incident_matches: weighted match
  - get_strs_correlation: returns by_detector dict

ServiceNow is particularly valuable for STRS corroboration because:
  - Member services incidents are logged in ServiceNow
  - System outages affecting benefit processing generate P1/P2 incidents
  - Compliance deadline failures generate change requests and incidents
  - Disability case processing delays generate capacity incidents

Keyword design rationale:
  APPLICATION_STALL:
    Member complaints about application status, system unavailability,
    and processing delays generate ServiceNow incidents.

  BENEFIT_ELECTION_DEADLINE:
    Failed notification delivery, member contact failures, and
    deadline-related escalations generate incidents.

  DISBURSEMENT_OVERDUE:
    Missed payment incidents are P1/P2 in most pension fund ITSM.
    ORC 3307 obligation means these generate immediate escalation.

  DISABILITY_REVIEW_BOTTLENECK:
    Capacity incidents, resource escalations, and case queue incidents
    for disability review team backlog.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# ── STRS ServiceNow keyword map ───────────────────────────────────────────────

SN_STRS_KEYWORD_MAP: List[Tuple[List[str], str, str]] = [
    (
        ["application", "retirement", "member", "submission", "processing",
         "status", "pending", "stalled", "backlog"],
        "APPLICATION_STALL",
        "Retirement application incident",
    ),
    (
        ["election", "deadline", "notification", "payment election",
         "enrollment", "benefit", "default plan", "payout setup"],
        "BENEFIT_ELECTION_DEADLINE",
        "Benefit election deadline incident",
    ),
    (
        ["disbursement", "payment", "overdue", "missed", "delayed",
         "payout", "ORC 3307", "regulatory", "pension payment"],
        "DISBURSEMENT_OVERDUE",
        "Disbursement overdue incident",
    ),
    (
        ["disability", "review", "bottleneck", "capacity", "backlog",
         "medical review", "stopped work", "assessment", "case queue"],
        "DISABILITY_REVIEW_BOTTLENECK",
        "Disability review incident",
    ),
]

SN_ALL_STRS_KEYWORDS = [kw for entry in SN_STRS_KEYWORD_MAP for kw in entry[0]] + [
    "strs",
    "benefits administration",
    "pension",
    "member services",
    "PSS",
]
SN_ALL_STRS_KEYWORDS = list(dict.fromkeys(SN_ALL_STRS_KEYWORDS))


def _sn_incident_matches(incident: Dict[str, Any], keywords: List[str]) -> bool:
    """
    Weighted keyword match. Same scoring as servicenow.py nCino version.

    keyword in short_description (title): score += 1.5
    keyword in description:               score += 0.5
    Threshold: score >= 1.0
    """
    title = (
        incident.get("short_description") or
        incident.get("u_short_description") or ""
    ).lower()
    description = (incident.get("description") or "").lower()

    score = 0.0
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in title:
            score += 1.5
        if kw_lower in description:
            score += 0.5

    return score >= 1.0


def _build_sn_snippet(incident: Dict[str, Any], label: str) -> str:
    """Build a readable evidence snippet from a ServiceNow incident."""
    number  = incident.get("number", "")
    title   = incident.get("short_description", "")[:120]
    state   = incident.get("state", incident.get("incident_state", ""))
    priority = incident.get("priority", "")
    return f"{number}: {title} [State:{state} P:{priority}] — {label}"


def get_strs_correlation(
    incidents: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Build STRS corroboration dict from a list of ServiceNow incidents.

    Returns same shape as nCino get_lending_correlation() in servicenow.py:
      {
        "strs_incidents": [...],
        "by_detector":    { "APPLICATION_STALL": ["INC001: ..."], ... },
        "total_matched":  int,
      }
    """
    by_detector: Dict[str, List[str]] = {}
    matched_incidents = []

    for incident in incidents:
        matched_detectors = []
        for keywords, detector_id, label in SN_STRS_KEYWORD_MAP:
            if _sn_incident_matches(incident, keywords):
                matched_detectors.append((detector_id, label))

        if matched_detectors:
            matched_incidents.append(incident)
            for detector_id, label in matched_detectors:
                snippet = _build_sn_snippet(incident, label)
                by_detector.setdefault(detector_id, []).append(snippet)

    total = len(matched_incidents)
    if total > 0:
        logger.info(
            "ServiceNow STRS correlation: %d incidents matched across %d detectors",
            total, len(by_detector),
        )

    return {
        "strs_incidents": matched_incidents,
        "by_detector":    by_detector,
        "total_matched":  total,
    }


def fetch_strs_sn_incidents(sn_client) -> List[Dict[str, Any]]:
    """
    Fetch ServiceNow incidents relevant to STRS benefit administration.
    Called from strs_benefits.py ingest() in live mode.

    Uses same broad-then-filter pattern as nCino servicenow.py.
    Queries incidents from last 90 days matching STRS keywords.
    """
    try:
        # Build ServiceNow encoded query
        # ^NQ separates OR conditions in ServiceNow
        keyword_sample = SN_ALL_STRS_KEYWORDS[:8]
        or_clauses = "^NQ".join(
            f"short_descriptionLIKE{kw}^ORdescriptionLIKE{kw}"
            for kw in keyword_sample
        )
        encoded_query = (
            f"({or_clauses})"
            f"^sys_created_onONLast 90 days@javascript:gs.beginningOfLast90Days()"
            f"@javascript:gs.endOfToday()"
        )

        incidents = sn_client.get_incidents(
            query=encoded_query,
            fields=["number", "short_description", "description",
                    "state", "priority", "incident_state",
                    "sys_created_on", "category", "subcategory",
                    "assigned_to", "assignment_group"],
            limit=200,
        )

        if isinstance(incidents, dict):
            incidents = incidents.get("result", [])

        logger.info(
            "ServiceNow STRS fetch: %d incidents returned for correlation",
            len(incidents),
        )
        return incidents

    except Exception as e:
        logger.warning("ServiceNow STRS fetch failed (non-blocking): %s", e)
        return []
