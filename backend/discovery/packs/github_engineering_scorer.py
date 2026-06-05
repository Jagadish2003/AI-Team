"""Canonical scorer for the GitHub Engineering Signal Pack.

T7 / AT-191 adds Jira corroboration: GITHUB_PR_REVIEW_BOTTLENECK elevates from
MEDIUM to HIGH when Jira is also connected and shows unresolved issues that have
been open without PR linkage for more than 7 days in the same time window. This
is the first Track 1 cross-system confidence elevation outside the
ServiceNow/Jira pair. The other two GitHub detectors are never affected.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    from backend.discovery.models import DetectorResult
except ModuleNotFoundError:
    from discovery.models import DetectorResult

logger = logging.getLogger(__name__)

# Only this detector participates in Jira corroboration elevation (AC5).
CORROBORATION_DETECTOR = "GITHUB_PR_REVIEW_BOTTLENECK"
# Jira issues open longer than this many days count as corroborating evidence.
JIRA_OPEN_DAYS_THRESHOLD = 7
# Statuses that mean an issue is resolved (no longer unresolved/open).
_RESOLVED_STATUSES = {
    "done", "closed", "resolved", "complete", "completed", "cancelled", "canceled",
}

_GITHUB_ENGINEERING_SCORES: Dict[str, Dict[str, Any]] = {
    "GITHUB_PR_REVIEW_BOTTLENECK": {
        "tier": "Quick Win",
        "impact": 7,
        "effort": 2,
        "confidence": "MEDIUM",  # elevates to HIGH with Jira corroboration (T7)
        "roadmap_stage": "quick_win",
    },
    "GITHUB_COMMIT_CONCENTRATION": {
        "tier": "Strategic",
        "impact": 8,
        "effort": 3,
        "confidence": "MEDIUM",
        "roadmap_stage": "strategic",
    },
    "GITHUB_STALE_BRANCHES": {
        "tier": "Quick Win",
        "impact": 5,
        "effort": 1,
        "confidence": "MEDIUM",
        "roadmap_stage": "quick_win",
    },
}

_DEFAULT_SCORE: Dict[str, Any] = {
    "tier": "Quick Win",
    "impact": 5,
    "effort": 2,
    "confidence": "MEDIUM",
    "roadmap_stage": "quick_win",
}

_EFFORT_LABEL: Dict[int, str] = {1: "Low", 2: "Low", 3: "Low-Med", 4: "Medium", 7: "High"}


def is_github_engineering_detector(detector_id: str) -> bool:
    """Return True when detector_id belongs to the GitHub Engineering pack."""
    return detector_id in _GITHUB_ENGINEERING_SCORES


def get_score(detector_id: str) -> Dict[str, Any]:
    """Return a copy of the score table entry for detector_id."""
    return dict(_GITHUB_ENGINEERING_SCORES.get(detector_id, _DEFAULT_SCORE))


def score_opportunity(detector_id: str, metric_value: float) -> Dict[str, Any]:
    """Return a score dict plus the detector metric value."""
    score = get_score(detector_id)
    score["metric_value"] = metric_value
    return score


# ── Jira corroboration (T7 / AT-191) ────────────────────────────────────────────

def _extract_issues(jira_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull the raw issue list out of a Jira ingest payload.

    Handles the shapes produced by discovery/ingest/jira.py: issues live under
    issue_metrics.issues / issue_metrics.recent_issues, or a top-level issues
    list. Returns [] when none are present.
    """
    if not isinstance(jira_data, dict):
        return []
    im = jira_data.get("issue_metrics") or {}
    for container, key in ((im, "issues"), (im, "recent_issues"), (jira_data, "issues")):
        if isinstance(container, dict):
            issues = container.get(key)
            if isinstance(issues, list):
                return issues
    return []


def _issue_in_org(issue: Dict[str, Any], org_id: Optional[str]) -> bool:
    """Org-scope guard (AC4).

    jira_data is already org-scoped upstream (the runner ingests Jira per org),
    so an issue with no org marker is treated as in-scope. When an issue *does*
    carry an org_id/orgId it must match the active org — preventing any
    cross-org issue from elevating confidence.
    """
    if org_id is None:
        return True
    issue_org = issue.get("org_id", issue.get("orgId"))
    if issue_org is None:
        return True
    return str(issue_org) == str(org_id)


def _is_unresolved(issue: Dict[str, Any]) -> bool:
    """True when the issue is still open (not resolved/closed)."""
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    if issue.get("resolution") or fields.get("resolution"):
        return False
    if issue.get("resolutiondate") or fields.get("resolutiondate"):
        return False
    status = issue.get("status") or (fields.get("status") or {}).get("name") or ""
    if isinstance(status, dict):
        status = status.get("name", "")
    return str(status).strip().lower() not in _RESOLVED_STATUSES


def _opened_before(issue: Dict[str, Any], cutoff: datetime) -> bool:
    """True when the issue was created before `cutoff` (i.e. open > threshold).

    Reads an explicit numeric `days_open` if present, otherwise parses a
    created/created_at/opened timestamp. Missing/unparseable dates are treated
    as not-corroborating (conservative — avoids false elevation).
    """
    days_open = issue.get("days_open")
    if isinstance(days_open, (int, float)) and not isinstance(days_open, bool):
        return days_open > JIRA_OPEN_DAYS_THRESHOLD

    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    created = (
        issue.get("created")
        or issue.get("created_at")
        or issue.get("opened")
        or fields.get("created")
    )
    if not isinstance(created, str) or not created:
        return False
    try:
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if created_dt.tzinfo is None:
        created_dt = created_dt.replace(tzinfo=timezone.utc)
    return created_dt < cutoff


def _has_pr_linkage(issue: Dict[str, Any]) -> bool:
    """True when the issue already has a linked pull request.

    Checks common shapes: an explicit boolean flag, a populated PR list, or a
    development/issuelinks structure referencing a pull request.
    """
    if issue.get("pr_linked") is True or issue.get("has_pr_link") is True:
        return True
    for key in ("pull_requests", "linked_prs", "prs"):
        val = issue.get(key)
        if isinstance(val, list) and len(val) > 0:
            return True
    dev = issue.get("development") or {}
    if isinstance(dev, dict):
        prs = dev.get("pull_requests") or dev.get("pullRequests")
        if isinstance(prs, list) and len(prs) > 0:
            return True
    links = issue.get("links") or issue.get("issuelinks") or []
    if isinstance(links, list):
        for link in links:
            text = str(link).lower()
            if "pull request" in text or "pull_request" in text or "/pull/" in text:
                return True
    return False


def jira_corroborates_pr_bottleneck(
    jira_data: Optional[Dict[str, Any]],
    *,
    org_id: Optional[str] = None,
    jira_connected: Optional[bool] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Return True when Jira corroborates the PR review bottleneck.

    Corroboration holds when Jira is connected AND at least one in-org Jira
    issue is unresolved, has been open for more than JIRA_OPEN_DAYS_THRESHOLD
    days, and has no linked pull request.

    `jira_connected` defaults to bool(jira_data) — matching how the runner
    derives sources_connected.jira. When Jira is disconnected (no payload) this
    always returns False, so confidence stays MEDIUM (AC2).
    """
    if jira_connected is None:
        jira_connected = bool(jira_data)
    if not jira_connected or not jira_data:
        return False

    issues = _extract_issues(jira_data)
    if not issues:
        return False

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=JIRA_OPEN_DAYS_THRESHOLD)

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if not _issue_in_org(issue, org_id):
            continue
        if _is_unresolved(issue) and _opened_before(issue, cutoff) and not _has_pr_linkage(issue):
            return True
    return False


# ── Scoring entry point ─────────────────────────────────────────────────────────

def score_github_engineering(
    dr: DetectorResult,
    *,
    jira_data: Optional[Dict[str, Any]] = None,
    org_id: Optional[str] = None,
    jira_connected: Optional[bool] = None,
) -> Dict[str, Any]:
    """Score a GitHub engineering signal DetectorResult.

    Unknown detector IDs receive safe default scoring instead of falling
    through silently. For GITHUB_PR_REVIEW_BOTTLENECK, confidence elevates
    MEDIUM -> HIGH when Jira corroboration holds (T7 / AT-191); the other two
    GitHub detectors are never affected. All corroboration arguments are
    optional and keyword-only, so existing callers (e.g. score_github_engineering(dr))
    remain backward compatible and simply keep the MEDIUM baseline.
    """
    known = is_github_engineering_detector(dr.detector_id)
    score = get_score(dr.detector_id)

    if not known:
        logger.warning(
            "score_github_engineering: unknown detector '%s' - returning default score. "
            "Check pack_config.py detector list.",
            dr.detector_id,
        )

    confidence = score["confidence"]
    corroborated = False
    corroboration_sources: List[str] = []
    confidence_note = (
        "MEDIUM - GitHub-only signal. Elevates to HIGH with Jira "
        "corroboration when unlinked open issues exceed 7 days (T7)."
    )

    if dr.detector_id == CORROBORATION_DETECTOR and jira_corroborates_pr_bottleneck(
        jira_data, org_id=org_id, jira_connected=jira_connected
    ):
        confidence = "HIGH"
        corroborated = True
        corroboration_sources = ["Jira"]
        confidence_note = (
            "HIGH - elevated by Jira corroboration: unresolved issues open "
            ">7d without PR linkage in the same time window (T7)."
        )
        logger.info(
            "%s: confidence elevated MEDIUM -> HIGH via Jira corroboration (org=%s)",
            dr.detector_id, org_id,
        )

    return {
        "tier": score["tier"],
        "impact": score["impact"],
        "effort": score["effort"],
        "effort_label": _EFFORT_LABEL.get(score["effort"], "Low"),
        "confidence": confidence,
        "roadmap_stage": score["roadmap_stage"],
        "corroborated": corroborated,
        "corroboration_sources": corroboration_sources,
        "score_debug": {
            "detector_id": dr.detector_id,
            "scorer": "github_engineering",
            "pack": "github_engineering",
            "metric_value": dr.metric_value,
            "threshold": dr.threshold,
            "signal_source": dr.signal_source,
            "base_impact": score["impact"],
            "final_impact": score["impact"],
            "base_confidence": score["confidence"],
            "confidence_note": confidence_note,
            **({"note": "unknown detector - default score applied"} if not known else {}),
        },
    }
