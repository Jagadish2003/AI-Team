"""
DB_TICKET_VOLUME_SURGE — T2-S11-A  |  SQL Server Operational Signal Pack
AgentIQ 2.0  |  Track 2 — Enterprise Technology  |  Sprint 11

Detector: DB_TICKET_VOLUME_SURGE
Signal source: sqlserver
Data source: ticket_volume section returned by the SQL Server ingestor.

Fires when:
  recent_vs_baseline >= 1.5   (recent 7-day average is ≥ 50% above the 90-day
                                daily average)
  AND degraded_signal is False

Does NOT fire when:
  - ratio < SURGE_THRESHOLD
  - db_data is None or missing 'ticket_volume'
  - degraded_signal is True (ingestor marked the signal as unreliable)
  - ticket_volume section is empty

SIGNAL_METRICS defines the numeric fields exposed to T3 snapshot / trend
detection.  Every metric listed here must appear as a numeric value in
raw_evidence.  Do not exceed 8 entries (Track 3 constraint).

Signal logic (doc reference: T2-S11-A Section 2b):
  recent_7d_avg    = average daily ticket count over the most recent 7 days
  avg_daily_90d    = average daily ticket count over the last 90 days (baseline)
  recent_vs_baseline = recent_7d_avg / avg_daily_90d
  Threshold: 1.5 — 50% above the 90-day baseline
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

# ── Constants ─────────────────────────────────────────────────────────────────

DETECTOR_ID: str = "DB_TICKET_VOLUME_SURGE"

#: Fires when recent 7-day average / 90-day baseline >= SURGE_THRESHOLD.
#: 1.5 = recent volume is 50% above the long-term daily average.
SURGE_THRESHOLD: float = 1.5

#: Numeric metrics exposed to T3-S10-A snapshot and T3-S11-A trend detection.
#: Each name must be a key in raw_evidence with a numeric value.
#: Must not exceed 8 entries.
SIGNAL_METRICS: List[str] = [
    "recent_vs_baseline",  # primary ratio — the trending signal
    "recent_7d_avg",       # absolute recent volume — context for analysts
    "avg_daily_90d",       # baseline denominator
    "peak_daily",          # single-day spike — context for incident review
    "total_90d",           # scale of the dataset
]


# ── Evaluation ────────────────────────────────────────────────────────────────


def evaluate(
    db_data: Optional[Dict[str, Any]],
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    """
    Build a DetectorEvaluation for temporal capture (T3-S10-A Option A pattern).

    Called by snapshot_signals() on every run regardless of whether the
    detector fires, so trend data is always captured.

    Parameters
    ----------
    db_data:
        Dict returned by the SQL Server ingestor.  Must contain a
        'ticket_volume' section.  None and empty are safe.
    sn_data, jira_data:
        Unused in Sprint 11 — reserved for cross-system corroboration in
        T2-S16-A normalisation layer.

    Returns
    -------
    DetectorEvaluation
        Always returned.  evaluation.fired determines whether detect()
        converts it to an opportunity.
    """
    tv: Dict[str, Any] = (db_data or {}).get("ticket_volume") or {}

    # Guard: missing or degraded signal → do not fire
    degraded: bool = bool(tv.get("degraded_signal", False))

    ratio: float = float(tv.get("recent_vs_baseline", 0.0))
    recent_7d_avg: float = float(tv.get("recent_7d_avg", 0.0))
    avg_daily_90d: float = float(tv.get("avg_daily", 0.0))
    peak_daily: int = int(tv.get("peak_daily", 0))
    total_90d: int = int(tv.get("total_90d", 0))
    peak_date: str = str(tv.get("peak_date", ""))
    schema_name: str = str((db_data or {}).get("schema_name", ""))
    table_name: str = str((db_data or {}).get("table_name", ""))

    fired: bool = (not degraded) and (ratio >= SURGE_THRESHOLD)

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="sqlserver",
        metric_value=round(ratio, 4),
        threshold=SURGE_THRESHOLD,
        fired=fired,
        raw_evidence={
            # SIGNAL_METRICS keys — all numeric
            "recent_vs_baseline": round(ratio, 4),
            "recent_7d_avg": round(recent_7d_avg, 2),
            "avg_daily_90d": round(avg_daily_90d, 2),
            "peak_daily": peak_daily,
            "total_90d": total_90d,
            # Context fields — not in SIGNAL_METRICS but included for analyst view
            "peak_date": peak_date,
            "schema_name": schema_name,
            "table_name": table_name,
            "degraded_signal": degraded,
        },
    )


# ── Public detect() ───────────────────────────────────────────────────────────


def detect(
    db_data: Optional[Dict[str, Any]],
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    """
    Return a list of DetectorResult when a ticket volume surge is detected.

    Returns an empty list (no opportunity) when:
      - db_data is None or empty
      - ticket_volume section is missing
      - degraded_signal is True
      - recent_vs_baseline < SURGE_THRESHOLD (1.5)

    Returns a single-element list when all firing conditions are met.
    Detectors never return more than one result per call.

    Parameters
    ----------
    db_data:
        Output dict from the SQL Server ingestor (Section 1d return shape).
        Expected keys under 'ticket_volume':
            recent_7d_avg, avg_daily, recent_vs_baseline,
            peak_daily, peak_date, total_90d, degraded_signal.
    sn_data, jira_data:
        Passed through for API compatibility with existing runner.
        Unused until T2-S16-A cross-system corroboration.
    """
    evaluation = evaluate(db_data, sn_data, jira_data)
    if not evaluation.fired:
        return []
    return [detector_result_from_evaluation(evaluation)]
