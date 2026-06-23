"""
ENT-5 / AT-262 (T1) — ENT_INCIDENT_RESOLUTION_LAG detector.

Enterprise Operations Intelligence Pack — cross-system finding.

The finding: ServiceNow incidents are being closed, but the underlying Jira
issues that caused them are still open. The gap between incident closure and
issue resolution is where operational debt accumulates. This pattern is
invisible if you only look at ServiceNow (the incident looks closed) or only
Jira (the issue looks like ordinary backlog). Only cross-system analysis of
the two systems together reveals it.

Signal:
    ServiceNow closed incidents that carry a Jira issue reference are joined to
    the referenced Jira issue's status / resolution date.
        unresolved_pct = closed incidents whose linked Jira issue is still open
                         / closed incidents (with a Jira reference) in the window
        avg_lag_days   = average days from incident close to Jira issue close
                         (for still-open issues, to the window's "as of" date)

Fires when (AC1):
    unresolved_pct >= 0.30
    AND avg_lag_days >= 14
    AND incident_count_30d >= 10   (minimum volume guard — AC2)

metric_value = unresolved_pct (float, e.g. 0.47 = 47% of closed incidents have
unresolved root-cause issues).

Confidence: MEDIUM standalone. Elevates to HIGH when the Slack escalation
pattern (COR-06 from the ENT-2 corroboration engine) also fires in the same
window. Confidence elevation is applied by the enterprise_ops pack scorer
(ENT-5 T4/T5), not by this detector — the detector only emits the signal.
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Any, Dict, List, Optional

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "ENT_INCIDENT_RESOLUTION_LAG"

# Thresholds (Section 1a of ENT-5).
UNRESOLVED_PCT_THRESHOLD = 0.30
AVG_LAG_DAYS_THRESHOLD = 14.0
MIN_INCIDENT_VOLUME = 10

# Backward-compatible alias used by detector-specific branch tests.
THRESHOLD = UNRESOLVED_PCT_THRESHOLD

# Confidence is MEDIUM standalone; the enterprise_ops scorer elevates to HIGH
# when COR-06 (Slack escalation) corroborates in the same window.
CONFIDENCE_STANDALONE = "MEDIUM"

# Jira statuses that count as a resolved root-cause issue.
_RESOLVED_STATUSES = {"done", "closed", "resolved", "complete", "completed"}

SIGNAL_METRICS: List[str] = [
    "unresolved_pct",       # metric_value — fraction of closed incidents w/ open Jira issue
    "avg_lag_days",         # mean days from incident close to Jira issue close/now
    "max_lag_days",         # longest single incident-to-issue resolution gap
    "incident_count_30d",   # closed incidents (with Jira ref) analysed in the window
    "unresolved_count",     # closed incidents whose linked Jira issue is still open
]


# ── date helpers ─────────────────────────────────────────────────────────────

def _parse_date(value: Any) -> Optional[date]:
    """Parse a ServiceNow/Jira date or datetime string into a date.

    Handles the common shapes: "YYYY-MM-DD", "YYYY-MM-DD HH:MM:SS",
    and ISO 8601 with a 'T' separator (optionally with a trailing 'Z').
    Returns None when the value is missing or unparseable.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Normalise the ISO 'T' separator and trailing 'Z'.
    candidate = text.replace("T", " ").replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        token = candidate[: len(fmt) + 4]
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    # Last resort: take the leading date token.
    try:
        return datetime.strptime(candidate.split(" ")[0], "%Y-%m-%d").date()
    except ValueError:
        return None


def _resolve_as_of(incident_block: Dict[str, Any]) -> date:
    """Return the reference "as of" date used to measure lag on open issues.

    Prefers an explicit ``as_of`` / ``window_end`` provided by the ingestor so
    runs stay deterministic; falls back to today only when none is supplied.
    """
    as_of = _parse_date(incident_block.get("as_of") or incident_block.get("window_end"))
    return as_of or datetime.now().date()


# ── Jira issue normalisation ─────────────────────────────────────────────────

def _normalise_jira_issues(jira_data: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build a ``{issue_key: {resolved, resolved_at}}`` map from jira_data.

    Accepts both the dedicated ``issue_resolution.issues`` block produced for
    this pack and the standard ``issue_metrics`` ingest shape (where each issue
    carries ``fields.status.name`` and ``resolutiondate``).
    """
    issues: Dict[str, Dict[str, Any]] = {}
    if not isinstance(jira_data, dict):
        return issues

    def _record(key: Any, status: Any, resolved_at: Any, resolved_flag: Any) -> None:
        if not key:
            return
        status_text = str(status or "").strip().lower()
        parsed_resolved_at = _parse_date(resolved_at)
        if resolved_flag is None:
            resolved = status_text in _RESOLVED_STATUSES or parsed_resolved_at is not None
        else:
            resolved = bool(resolved_flag)
        issues[str(key)] = {"resolved": resolved, "resolved_at": parsed_resolved_at}

    # 1) Dedicated pack block: issue_resolution.issues (dict or list).
    ir = jira_data.get("issue_resolution") or {}
    container = ir.get("issues")
    if isinstance(container, dict):
        for key, info in container.items():
            info = info or {}
            _record(
                key,
                info.get("status"),
                info.get("resolved_at") or info.get("resolution_date"),
                info.get("resolved"),
            )
    elif isinstance(container, list):
        for info in container:
            info = info or {}
            _record(
                info.get("issue_key") or info.get("key"),
                info.get("status"),
                info.get("resolved_at") or info.get("resolution_date"),
                info.get("resolved"),
            )

    # 2) Standard ingest shape: issue_metrics.{issues,recent_issues}.
    im = jira_data.get("issue_metrics") or {}
    for list_key in ("issues", "recent_issues"):
        for info in im.get(list_key) or []:
            info = info or {}
            key = info.get("key") or info.get("issue_key")
            if not key or key in issues:
                continue
            fields = info.get("fields") or {}
            status = (fields.get("status") or {}).get("name") or info.get("status")
            resolved_at = (
                fields.get("resolutiondate")
                or info.get("resolutiondate")
                or info.get("resolved_at")
            )
            _record(key, status, resolved_at, info.get("resolved"))

    return issues


def _incident_jira_key(incident: Dict[str, Any]) -> Optional[str]:
    """Return the Jira issue key an incident references, if any."""
    for field in ("jira_issue_key", "jira_issue", "jira_key", "jira_ref", "issue_key"):
        value = incident.get(field)
        if value:
            return str(value)
    return None


# ── core computation ─────────────────────────────────────────────────────────

def _compute_metrics(
    sn_data: Optional[Dict[str, Any]],
    jira_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Join closed incidents to their Jira issues and compute the lag metrics."""
    incident_block = (sn_data or {}).get("incident_resolution") or {}
    closed_incidents = incident_block.get("closed_incidents") or []
    as_of = _resolve_as_of(incident_block)
    jira_issues = _normalise_jira_issues(jira_data)

    # Only closed incidents that actually carry a Jira reference are analysed —
    # this matches the data source ("closed incidents with Jira issue reference
    # fields") and is the population unresolved_pct is measured against.
    referenced = [inc for inc in closed_incidents if _incident_jira_key(inc)]
    incident_count = len(referenced)

    unresolved_count = 0
    lags: List[float] = []
    for inc in referenced:
        key = _incident_jira_key(inc)
        closed_at = _parse_date(
            inc.get("closed_at") or inc.get("closed") or inc.get("close_date")
        )
        issue = jira_issues.get(key)

        # A referenced issue missing from the Jira pull is treated as unresolved
        # — we cannot confirm the root cause was closed.
        resolved = bool(issue and issue.get("resolved"))
        if not resolved:
            unresolved_count += 1

        if closed_at is None:
            continue
        if resolved:
            resolved_at = issue.get("resolved_at") if issue else None
            end = resolved_at or closed_at  # resolved without a date → no lag
        else:
            end = as_of
        lags.append(max(0.0, float((end - closed_at).days)))

    unresolved_pct = (unresolved_count / incident_count) if incident_count else 0.0
    avg_lag_days = (sum(lags) / len(lags)) if lags else 0.0
    max_lag_days = max(lags) if lags else 0.0

    return {
        "unresolved_pct": round(unresolved_pct, 4),
        "avg_lag_days": round(avg_lag_days, 2),
        "max_lag_days": round(max_lag_days, 2),
        "incident_count_30d": incident_count,
        "unresolved_count": unresolved_count,
        "degraded_signal": bool(incident_block.get("degraded_signal", False)),
    }


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    """Evaluate the incident-issue resolution gap and return a DetectorEvaluation.

    Matches the runner calling convention ``evaluate(sf_data, sn_data, jira_data)``.
    This is a cross-system detector — it reads ServiceNow closed incidents from
    ``sn_data`` and joins them to Jira issue status from ``jira_data``; the
    ``sf_data`` positional is accepted for signature compatibility only.
    """
    metrics = _compute_metrics(sn_data, jira_data)

    fired = (
        not metrics["degraded_signal"]
        and metrics["incident_count_30d"] >= MIN_INCIDENT_VOLUME
        and metrics["unresolved_pct"] >= UNRESOLVED_PCT_THRESHOLD
        and metrics["avg_lag_days"] >= AVG_LAG_DAYS_THRESHOLD
    )

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=metrics["unresolved_pct"],
        threshold=UNRESOLVED_PCT_THRESHOLD,
        fired=fired,
        raw_evidence=metrics,
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    """Return a list with one DetectorResult when the detector fires, else []."""
    evaluation = evaluate(sf_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []