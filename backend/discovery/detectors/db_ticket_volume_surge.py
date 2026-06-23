"""
DB_TICKET_VOLUME_SURGE detector for the SQL Server Operational Signal Pack.

Fires when the recent 7-day average ticket intake is at least 1.5x the
90-day average and the ingestor did not mark the signal as degraded.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "DB_TICKET_VOLUME_SURGE"
SURGE_THRESHOLD = 1.5

SIGNAL_METRICS: List[str] = [
    "recent_vs_baseline",
    "recent_7d_avg",
    "avg_daily_90d",
    "peak_daily",
    "total_90d",
]


def evaluate(
    db_data: Optional[Dict[str, Any]],
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    tv: Dict[str, Any] = (db_data or {}).get("ticket_volume") or {}

    degraded = bool(tv.get("degraded_signal", False))
    ratio = float(tv.get("recent_vs_baseline", 0.0))
    recent_7d_avg = float(tv.get("recent_7d_avg", 0.0))
    avg_daily_90d = float(tv.get("avg_daily", 0.0))
    peak_daily = int(tv.get("peak_daily", 0))
    total_90d = int(tv.get("total_90d", 0))
    peak_date = str(tv.get("peak_date", ""))
    schema_name = str((db_data or {}).get("schema_name", ""))
    table_name = str((db_data or {}).get("table_name", ""))
    signal_source = str((db_data or {}).get("connector_id", "sqlserver"))

    fired = (not degraded) and (ratio >= SURGE_THRESHOLD)

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source=signal_source,
        metric_value=round(ratio, 4),
        threshold=SURGE_THRESHOLD,
        fired=fired,
        raw_evidence={
            "recent_vs_baseline": round(ratio, 4),
            "recent_7d_avg": round(recent_7d_avg, 2),
            "avg_daily_90d": round(avg_daily_90d, 2),
            "peak_daily": peak_daily,
            "total_90d": total_90d,
            "peak_date": peak_date,
            "schema_name": schema_name,
            "table_name": table_name,
            "degraded_signal": degraded,
        },
    )


def detect(
    db_data: Optional[Dict[str, Any]],
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    evaluation = evaluate(db_data, sn_data, jira_data)
    if not evaluation.fired:
        return []
    return [detector_result_from_evaluation(evaluation)]
