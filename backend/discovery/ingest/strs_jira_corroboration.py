"""
strs_jira_corroboration.py — Fix Pack Sprint 7
ENG-STRS-CORR-1

Jira corroboration for STRS Benefits Administration detectors.

Follows the exact same pattern as the nCino lending_correlation in jira.py:
  - STRS_KEYWORD_MAP: (keywords, detector_id, label) per detector
  - _issue_matches_keywords: weighted match to reduce false positives
  - get_strs_correlation: main function returning by_detector dict

The runner.py calls this from the strs_benefits ingest block and
passes the result into strs_benefits.py metrics as 'jira_strs_correlation'.
The four STRS detectors then consume jira_data via their detect() parameters.

Keyword design rationale:
  APPLICATION_STALL:
    Members whose retirement applications are stalled contact member services,
    create IT tickets for system issues, and generate Jira stories for process
    fixes. Keywords: application, retirement, stall, delay, member, backlog

  BENEFIT_ELECTION_DEADLINE:
    Payment election deadline misses generate Jira stories around notification
    failures, member communication issues, and deadline tracking.
    Keywords: election, deadline, payment, notification, benefit, payout

  DISBURSEMENT_OVERDUE:
    Overdue disbursements generate immediate escalation tickets.
    Keywords: disbursement, payment, overdue, payout, missed, delayed

  DISABILITY_REVIEW_BOTTLENECK:
    Disability reviews generate case management Jira stories,
    compliance deadline tickets, and capacity bottleneck stories.
    Keywords: disability, review, bottleneck, capacity, backlog, case
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# ── STRS keyword map ──────────────────────────────────────────────────────────
# Each entry: (keyword_list, detector_id, label)
# Mirrors LENDING_KEYWORD_MAP pattern from jira.py exactly.

STRS_KEYWORD_MAP: List[Tuple[List[str], str, str]] = [
    (
        ["application", "retirement", "stall", "delay", "member", "backlog",
         "processing", "submitted", "in review", "pending"],
        "APPLICATION_STALL",
        "Retirement application processing",
    ),
    (
        ["election", "deadline", "payment election", "payout", "notification",
         "benefit election", "default plan", "enrollment"],
        "BENEFIT_ELECTION_DEADLINE",
        "Benefit election deadline",
    ),
    (
        ["disbursement", "overdue", "missed payment", "payment failure",
         "payout", "delayed payment", "ORC 3307", "regulatory"],
        "DISBURSEMENT_OVERDUE",
        "Benefit disbursement overdue",
    ),
    (
        ["disability", "review", "bottleneck", "capacity", "case",
         "stopped work", "medical", "disability review", "assessment"],
        "DISABILITY_REVIEW_BOTTLENECK",
        "Disability review bottleneck",
    ),
]

# All STRS keywords combined for initial broad filter (JQL text search)
ALL_STRS_KEYWORDS = [kw for entry in STRS_KEYWORD_MAP for kw in entry[0]] + [
    "strs",
    "benefits",
    "pension",
    "member services",
    "PSS",
    "public sector",
]

# Deduplicate
ALL_STRS_KEYWORDS = list(dict.fromkeys(ALL_STRS_KEYWORDS))


def _issue_matches_keywords(issue: Dict[str, Any], keywords: List[str]) -> bool:
    """
    Weighted keyword match. Same scoring logic as jira.py nCino version.

    Scoring:
      keyword in title (summary):     score += 1.5
      keyword in description text:    score += 0.5

    Single keyword hit in description only (score=0.5) does NOT fire.
    Single keyword hit in title (score=1.5) DOES fire.
    Multiple description hits accumulate.
    """
    summary = (issue.get("summary") or issue.get("fields", {}).get("summary") or "").lower()
    description = (issue.get("description_text") or "").lower()

    score = 0.0
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in summary:
            score += 1.5
        if kw_lower in description:
            score += 0.5

    return score >= 1.0


def _build_snippet(issue: Dict[str, Any], label: str) -> str:
    """Build a readable evidence snippet from a Jira issue."""
    key     = issue.get("key", "")
    summary = issue.get("summary") or issue.get("fields", {}).get("summary") or ""
    status  = issue.get("status") or issue.get("fields", {}).get("status", {}).get("name") or ""
    return f"{key}: {summary[:120]} [{status}] — {label}"


def get_strs_correlation(
    issues: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Build STRS corroboration dict from a list of Jira issues.

    Called from strs_benefits.py ingest() with the full issue list.
    Returns the same shape as nCino get_lending_correlation():
      {
        "strs_issues":  [...],       # matched issue list
        "by_detector":  {            # detector_id → list of snippet strings
            "APPLICATION_STALL": ["STRS-42: Member application stuck..."],
            ...
        },
        "total_matched": int,
      }
    """
    by_detector: Dict[str, List[str]] = {}
    matched_issues = []

    for issue in issues:
        matched_detectors = []
        for keywords, detector_id, label in STRS_KEYWORD_MAP:
            if _issue_matches_keywords(issue, keywords):
                matched_detectors.append((detector_id, label))

        if matched_detectors:
            matched_issues.append(issue)
            for detector_id, label in matched_detectors:
                snippet = _build_snippet(issue, label)
                by_detector.setdefault(detector_id, []).append(snippet)

    total = len(matched_issues)
    if total > 0:
        logger.info(
            "Jira STRS correlation: %d issues matched across %d detectors",
            total, len(by_detector),
        )

    return {
        "strs_issues":   matched_issues,
        "by_detector":   by_detector,
        "total_matched": total,
    }


def fetch_strs_jira_issues(jira_client) -> List[Dict[str, Any]]:
    """
    Fetch Jira issues relevant to STRS benefit administration.
    Called from strs_benefits.py ingest() in live mode.

    JQL: text contains any STRS keyword, created in last 90 days.
    Uses same broad-then-filter pattern as nCino jira.py.
    """
    try:
        # Build JQL text search — limit to first 8 keywords to avoid JQL length limit
        keyword_sample = ALL_STRS_KEYWORDS[:8]
        kw_jql = " OR ".join(f'text ~ "{kw}"' for kw in keyword_sample)
        jql = (
            f"({kw_jql}) "
            f"AND created >= -90d "
            f"ORDER BY created DESC"
        )

        issues = jira_client.search_issues(
            jql,
            fields=["summary", "description", "status", "created",
                    "priority", "labels", "issuetype", "assignee"],
            max_results=200,
        )

        # Normalise to flat dict — handle both JiraIssue objects and raw dicts
        normalised = []
        for issue in issues:
            if hasattr(issue, "fields"):
                # jira-python library object
                desc_raw = getattr(issue.fields, "description", "") or ""
                normalised.append({
                    "key":              issue.key,
                    "summary":          getattr(issue.fields, "summary", "") or "",
                    "description_text": desc_raw if isinstance(desc_raw, str) else str(desc_raw),
                    "status":           getattr(getattr(issue.fields, "status", None), "name", "") or "",
                    "created":          getattr(issue.fields, "created", "") or "",
                })
            elif isinstance(issue, dict):
                fields = issue.get("fields", {})
                normalised.append({
                    "key":              issue.get("key", ""),
                    "summary":          fields.get("summary", "") or "",
                    "description_text": str(fields.get("description") or ""),
                    "status":           (fields.get("status") or {}).get("name", "") or "",
                    "created":          fields.get("created", "") or "",
                })

        logger.info(
            "Jira STRS fetch: %d issues returned for correlation", len(normalised)
        )
        return normalised

    except Exception as e:
        logger.warning("Jira STRS fetch failed (non-blocking): %s", e)
        return []
