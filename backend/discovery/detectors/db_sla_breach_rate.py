"""DB_SLA_BREACH_RATE detector for the SQL Server Operational Signal Pack."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "DB_SLA_BREACH_RATE"
BREACH_THRESHOLD = 15.0
MIN_TICKET_VOLUME = 10

# Backward-compatible names used by the detector-specific branch tests.
THRESHOLD = BREACH_THRESHOLD
MIN_TICKETS = MIN_TICKET_VOLUME

SIGNAL_METRICS: List[str] = [
    "breach_rate_pct",
    "breached_count",
    "total_tickets_30d",
]


def evaluate(
    db_data: Optional[Dict[str, Any]],
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    sla = (db_data or {}).get("sla_breach") or {}

    degraded_signal = bool(sla.get("degraded_signal", False))
    breach_rate_pct = float(sla.get("breach_rate_pct", 0.0))
    breached_count = int(sla.get("breached_count", 0))
    total_tickets_30d = int(sla.get("total_tickets_30d", 0))
    schema_name = str((db_data or {}).get("schema_name", ""))
    table_name = str((db_data or {}).get("table_name", ""))

    fired = (
        not degraded_signal
        and total_tickets_30d >= MIN_TICKET_VOLUME
        and breach_rate_pct >= BREACH_THRESHOLD
    )

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="sqlserver",
        metric_value=round(breach_rate_pct, 4),
        threshold=BREACH_THRESHOLD,
        fired=fired,
        raw_evidence={
            "breach_rate_pct": round(breach_rate_pct, 4),
            "breached_count": breached_count,
            "total_tickets_30d": total_tickets_30d,
            "schema_name": schema_name,
            "table_name": table_name,
            "degraded_signal": degraded_signal,
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
