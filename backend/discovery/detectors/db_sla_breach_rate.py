"""
DB_SLA_BREACH_RATE — T2-S11-A detector.

Fires when the SLA breach rate over the last 30 days is >= 15% AND the
minimum volume guard of 10 tickets is met, and degraded_signal is False.

Signal source: SQL Server operational ingestor (sqlserver_opsignal pack).
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "DB_SLA_BREACH_RATE"
BREACH_THRESHOLD = 15.0   # percent — 15% of tickets breached SLA
MIN_TICKET_VOLUME = 10    # minimum volume guard

SIGNAL_METRICS = [
    "breach_rate_pct",      # primary metric — percent of tickets breached
    "breached_count",       # absolute breach count
    "total_tickets_30d",    # denominator / volume guard
]


def evaluate(
    db_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    sla = (db_data or {}).get("sla_breach", {})
    degraded = bool(sla.get("degraded_signal", True))
    rate = float(sla.get("breach_rate_pct", 0.0))
    total = int(sla.get("total_tickets_30d", 0))
    breached = int(sla.get("breached_count", 0))

    fired = (
        (not degraded)
        and (rate >= BREACH_THRESHOLD)
        and (total >= MIN_TICKET_VOLUME)
    )

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="sqlserver",
        metric_value=round(rate, 4),
        threshold=BREACH_THRESHOLD,
        fired=fired,
        raw_evidence={
            "breach_rate_pct": rate,
            "breached_count": breached,
            "total_tickets_30d": total,
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
