"""
DB_SLA_BREACH_RATE detector — T2-S11-A

Signal source: SQL Server ingestor sla_breach section.
Fires when: breach_rate_pct >= 15.0 AND total_tickets_30d >= 10.

Volume guard: does not fire for small sample sizes (total_tickets_30d < 10).
Degraded guard: does not fire when signal is missing or marked degraded.

Helps AgentIQ identify SLA risk early and recommend monitoring before
escalations happen.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "DB_SLA_BREACH_RATE"
THRESHOLD = 15.0
MIN_TICKETS = 10

SIGNAL_METRICS = [
    "breach_rate_pct",    # percentage of tickets breaching SLA over 30 days
    "breached_count",     # raw count of breached tickets
    "total_tickets_30d",  # total ticket volume (volume guard denominator)
]


def evaluate(
    db_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    sla = db_data.get("sla_breach") or {}

    if not sla:
        return make_detector_evaluation(
            module_name=__name__,
            detector_id=DETECTOR_ID,
            signal_source="sqlserver",
            metric_value=0.0,
            threshold=THRESHOLD,
            fired=False,
            raw_evidence={"breach_rate_pct": 0.0, "breached_count": 0, "total_tickets_30d": 0},
        )

    degraded = bool(sla.get("degraded", False))
    breach_rate_pct = float(sla.get("breach_rate_pct", 0.0))
    breached_count = int(sla.get("breached_count", 0))
    total_tickets_30d = int(sla.get("total_tickets_30d", 0))
    schema_name = sla.get("schema_name", "")
    table_name = sla.get("table_name", "")

    fired = (
        not degraded
        and total_tickets_30d >= MIN_TICKETS
        and breach_rate_pct >= THRESHOLD
    )

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="sqlserver",
        metric_value=round(breach_rate_pct, 4),
        threshold=THRESHOLD,
        fired=fired,
        raw_evidence={
            "breach_rate_pct": breach_rate_pct,
            "breached_count": breached_count,
            "total_tickets_30d": total_tickets_30d,
            "schema_name": schema_name,
            "table_name": table_name,
            "degraded": degraded,
        },
    )


def detect(
    db_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
) -> List[DetectorResult]:
    evaluation = evaluate(db_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []
