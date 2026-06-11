"""
ENT-5 / AT-263 (T2) — ENT_CHANGE_INCIDENT_CORRELATION detector.

Enterprise Operations Intelligence Pack — cross-system finding.

The finding: ServiceNow change requests are being approved and executed, and
incidents are spiking in the 72-hour window following those changes. The change
management process is not catching the changes that introduce instability. This
is the ITSM-layer analogue of the deployment-correlation detector, but it
operates on approved change records rather than application logs.

Signal:
    For each ServiceNow change in state=Implemented, incidents opened within the
    72 hours following the change's ``closed_at`` are counted. The post-change
    incident rate is compared to the 30-day baseline incident rate.
        post_change_incident_ratio = post_change_incident_rate / baseline_rate

Fires when (AC3):
    post_change_incident_ratio >= 2.0
    AND change_count_30d >= 3
    AND post_change_incidents >= 5
Confidence is HIGH whenever the detector fires (ratio >= 2.0 is a strong,
single-system corroborated signal — no external corroboration needed).

metric_value = post_change_incident_ratio (float, e.g. 2.8 = incidents nearly
triple after changes).

Baseline / history note (ENT-5 §6): a meaningful baseline needs ~30 days of
ServiceNow history. The firing floor for change volume is 3 records (AC3); the
detector emits ``insufficient_data`` when there are fewer than that, since a
correlation ratio cannot be computed at all. The ENT-5 architectural guidance
of "10 change records for a reliable baseline" is recorded in raw_evidence as
``baseline_reliable`` for the scorer/calibration layer to weigh — it is advisory
and does not override the AC3 firing gate.
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "ENT_CHANGE_INCIDENT_CORRELATION"

# Thresholds (Section 1b of ENT-5).
RATIO_THRESHOLD = 2.0
MIN_CHANGE_COUNT = 3
MIN_POST_CHANGE_INCIDENTS = 5

POST_CHANGE_WINDOW_HOURS = 72
BASELINE_WINDOW_DAYS = 30
# Advisory reliability floor from ENT-5 §6 — does not gate firing (AC3).
RELIABLE_BASELINE_MIN_CHANGES = 10

# Backward-compatible alias used by detector-specific branch tests.
THRESHOLD = RATIO_THRESHOLD

# Confidence is HIGH whenever the detector fires (ratio >= 2.0). No external
# corroboration is required; the change→incident linkage is single-system.
CONFIDENCE = "HIGH"

# State value (case-insensitive) that marks an executed change request.
_IMPLEMENTED_STATE = "implemented"

SIGNAL_METRICS: List[str] = [
    "post_change_incident_ratio",  # metric_value — post-change rate / baseline rate
    "change_count_30d",            # implemented changes analysed in the window
    "post_change_incidents",       # distinct incidents within 72h of a change
    "baseline_incident_rate",      # baseline incidents-per-day over the window
]


# ── date helpers ─────────────────────────────────────────────────────────────

def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse a ServiceNow date/datetime string into a datetime.

    Handles "YYYY-MM-DD HH:MM:SS", "YYYY-MM-DD HH:MM", "YYYY-MM-DD", and ISO
    8601 with a 'T' separator (optionally with a trailing 'Z'). Returns None
    when the value is missing or unparseable.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if not text:
        return None
    candidate = text.replace("T", " ").replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(candidate[: len(fmt) + 4], fmt)
        except ValueError:
            continue
    try:
        return datetime.strptime(candidate.split(" ")[0], "%Y-%m-%d")
    except ValueError:
        return None


def _resolve_as_of(block: Dict[str, Any], changes: List[datetime]) -> datetime:
    """Reference "as of" datetime that ends the baseline window.

    Prefers an explicit ``as_of`` / ``window_end`` from the ingestor for
    determinism; otherwise the latest change close, otherwise now.
    """
    explicit = _parse_dt(block.get("as_of") or block.get("window_end"))
    if explicit:
        return explicit
    if changes:
        return max(changes)
    return datetime.now()


# ── core computation ─────────────────────────────────────────────────────────

def _compute_metrics(sn_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Correlate implemented changes with following incidents and compute ratio."""
    block = (sn_data or {}).get("change_correlation") or {}
    degraded = bool(block.get("degraded_signal", False))
    baseline_window_days = int(block.get("baseline_window_days", BASELINE_WINDOW_DAYS)) or BASELINE_WINDOW_DAYS
    window_hours = int(block.get("post_change_window_hours", POST_CHANGE_WINDOW_HOURS)) or POST_CHANGE_WINDOW_HOURS

    # Implemented change closes (the only changes that can cause post-change incidents).
    change_closes: List[datetime] = []
    for change in block.get("changes") or []:
        state = str(change.get("state", "")).strip().lower()
        if state != _IMPLEMENTED_STATE:
            continue
        closed = _parse_dt(change.get("closed_at") or change.get("closed") or change.get("close_date"))
        if closed is not None:
            change_closes.append(closed)

    as_of = _resolve_as_of(block, change_closes)
    window_start = as_of - timedelta(days=baseline_window_days)

    # Changes within the 30-day baseline window.
    in_window_changes = [c for c in change_closes if window_start <= c <= as_of]
    change_count = len(in_window_changes)

    # Incident open timestamps (assumed already scoped to the window by ingest).
    incident_opens: List[datetime] = []
    for inc in block.get("incidents") or []:
        opened = _parse_dt(inc.get("opened_at") or inc.get("opened") or inc.get("open_date"))
        if opened is not None:
            incident_opens.append(opened)
    total_incidents = len(incident_opens)

    # Post-change incidents: opened within (close, close + 72h] of any change.
    delta = timedelta(hours=window_hours)
    post_change_open_set = set()
    for idx, opened in enumerate(incident_opens):
        for close in in_window_changes:
            if close < opened <= close + delta:
                post_change_open_set.add(idx)
                break
    post_change_incidents = len(post_change_open_set)

    # Baseline incidents-per-day over the window (or an explicit ingest value).
    if block.get("baseline_incident_rate") is not None:
        baseline_rate = float(block["baseline_incident_rate"])
    else:
        baseline_rate = total_incidents / baseline_window_days if baseline_window_days else 0.0

    # Post-change incidents-per-day: each change contributes a 72h exposure window.
    post_change_exposure_days = change_count * (window_hours / 24.0)
    post_change_rate = (
        post_change_incidents / post_change_exposure_days if post_change_exposure_days else 0.0
    )

    ratio = (post_change_rate / baseline_rate) if baseline_rate > 0 else 0.0

    insufficient_data = change_count < MIN_CHANGE_COUNT or baseline_rate <= 0.0

    return {
        "post_change_incident_ratio": round(ratio, 4),
        "change_count_30d": change_count,
        "post_change_incidents": post_change_incidents,
        "baseline_incident_rate": round(baseline_rate, 4),
        "total_incidents_30d": total_incidents,
        "degraded_signal": degraded,
        "insufficient_data": insufficient_data,
        "baseline_reliable": change_count >= RELIABLE_BASELINE_MIN_CHANGES,
        "confidence": CONFIDENCE,
    }


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    """Evaluate change→incident correlation and return a DetectorEvaluation.

    Matches the runner convention ``evaluate(sf_data, sn_data, jira_data)``. All
    inputs (change requests + incidents) come from ServiceNow via ``sn_data``;
    the other positionals are accepted for signature compatibility only.
    """
    metrics = _compute_metrics(sn_data)

    fired = (
        not metrics["degraded_signal"]
        and not metrics["insufficient_data"]
        and metrics["change_count_30d"] >= MIN_CHANGE_COUNT
        and metrics["post_change_incidents"] >= MIN_POST_CHANGE_INCIDENTS
        and metrics["post_change_incident_ratio"] >= RATIO_THRESHOLD
    )

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=metrics["post_change_incident_ratio"],
        threshold=RATIO_THRESHOLD,
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