"""
DB_QUEUE_DEPTH_ELEVATED — T2-S11-A detector.

Fires when the total number of open P1 + P2 tickets is >= 20, and
degraded_signal is False.

Signal source: SQL Server operational ingestor (sqlserver_opsignal pack).
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "DB_QUEUE_DEPTH_ELEVATED"
P1_P2_THRESHOLD = 20  # open P1+P2 tickets

SIGNAL_METRICS = [
    "p1_p2_open",           # primary metric — critical open ticket count
    "total_open",           # total open queue depth
    "oldest_ticket_hours",  # age of oldest open ticket
]


def evaluate(
    db_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    qd = (db_data or {}).get("queue_depth", {})
    degraded = bool(qd.get("degraded_signal", True))
    p1_p2 = int(qd.get("p1_p2_open", 0))
    total_open = int(qd.get("total_open", 0))
    oldest = float(qd.get("oldest_ticket_hours", 0.0))

    fired = (not degraded) and (p1_p2 >= P1_P2_THRESHOLD)

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="sqlserver",
        metric_value=float(p1_p2),
        threshold=float(P1_P2_THRESHOLD),
        fired=fired,
        raw_evidence={
            "p1_p2_open": p1_p2,
            "total_open": total_open,
            "oldest_ticket_hours": oldest,
            "by_priority": qd.get("by_priority", {}),
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
