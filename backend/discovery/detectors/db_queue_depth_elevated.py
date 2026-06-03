"""
DB_QUEUE_DEPTH_ELEVATED — T2-S11-A Task T4.

Fires when the number of open P1 + P2 tickets returned by the SQL Server
operational ingestor reaches or exceeds the configured threshold (default 20).

High-priority tickets in the backlog represent the highest operational risk:
they directly map to service disruption events and SLA breach escalations.
Keeping the P1/P2 queue below the threshold is a leading indicator of healthy
IT operations.

Signal source: sqlserver_opsignal pack via the SQL Server ingestor.
Threshold:    p1_p2_open >= 20 (configurable via scope declaration metadata).
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "DB_QUEUE_DEPTH_ELEVATED"
P1_P2_THRESHOLD = 20  # number of open P1+P2 tickets

SIGNAL_METRICS = [
    "p1_p2_open",           # primary metric — open P1+P2 ticket count
    "total_open",           # total open queue depth (all priorities)
    "oldest_ticket_hours",  # age of the oldest open ticket in hours
]


def evaluate(
    db_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    """Evaluate queue depth and return a DetectorEvaluation.

    Returns fired=True when:
      - degraded_signal is False (query completed cleanly)
      - p1_p2_open >= P1_P2_THRESHOLD
    """
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
    """Return a list containing one DetectorResult if the detector fires."""
    evaluation = evaluate(db_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []
