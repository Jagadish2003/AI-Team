"""
DB_TICKET_VOLUME_SURGE — T2-S11-A detector.

Fires when the recent 7-day average ticket intake is >= 1.5x the 90-day
average, and degraded_signal is False.

Signal source: SQL Server operational ingestor (sqlserver_opsignal pack).
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "DB_TICKET_VOLUME_SURGE"
SURGE_THRESHOLD = 1.5  # recent 7d avg is 50% above 90d baseline

SIGNAL_METRICS = [
    "recent_vs_baseline",   # ratio — primary trending metric
    "recent_7d_avg",        # recent intake volume (absolute context)
    "avg_daily_90d",        # baseline denominator
    "peak_daily",           # single-day spike context
    "total_90d",            # volume scale context
]


def evaluate(
    db_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    tv = (db_data or {}).get("ticket_volume", {})
    degraded = bool(tv.get("degraded_signal", True))
    ratio = float(tv.get("recent_vs_baseline", 0.0))
    fired = (not degraded) and (ratio >= SURGE_THRESHOLD)

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="sqlserver",
        metric_value=round(ratio, 4),
        threshold=SURGE_THRESHOLD,
        fired=fired,
        raw_evidence={
            "recent_vs_baseline": ratio,
            "recent_7d_avg": tv.get("recent_7d_avg", 0.0),
            "avg_daily_90d": tv.get("avg_daily", 0.0),
            "peak_daily": tv.get("peak_daily", 0),
            "peak_date": tv.get("peak_date", ""),
            "total_90d": tv.get("total_90d", 0),
            "schema_name": (db_data or {}).get("schema_name", ""),
            "table_name": (db_data or {}).get("table_name", ""),
            "degraded_signal": degraded,
        },
    )


def detect(
    db_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
) -> List[DetectorResult]:
    evaluation = evaluate(db_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []
