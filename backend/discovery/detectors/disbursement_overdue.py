"""
DISBURSEMENT_OVERDUE detector - Sprint 5.2 ENG-STRS-2.

Fires when benefit disbursements are overdue.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "DISBURSEMENT_OVERDUE"
SIGNAL_METRICS = {
    "overdue_count":     "Number of overdue disbursements; primary compliance volume trend",
    "max_days_overdue":  "Worst-case days overdue; regulatory severity indicator",
    "avg_days_overdue":  "Average days overdue; smoothed trend across all overdue payments",
    "total_assignments": "Total assignments evaluated; normalises overdue_count as a rate",
}

SIGNAL_METRICS = [
    "total_assignments", # benefit assignment workload volume
    "overdue_count",     # count of overdue disbursements
    "max_days_overdue",  # strongest overdue-disbursement age signal
    "avg_days_overdue",  # average overdue-disbursement age signal
]


def evaluate(
    sf_data: Dict[str, Any],
    sn_data=None,
    jira_data=None,
):
    strs = sf_data.get("strs_benefits") or sf_data
    metrics = strs.get("disbursement_metrics", {})

    overdue_count = int(metrics.get("overdue_count", 0))
    max_days_overdue = float(metrics.get("max_days_overdue", 0))
    avg_days_overdue = float(metrics.get("avg_days_overdue", 0))
    total = int(metrics.get("total_assignments", 0))
    compliance_override = True

    jira_corroborated = bool(
        jira_data
        and isinstance(jira_data, dict)
        and jira_data.get("by_detector", {}).get(DETECTOR_ID)
    )
    sn_corroborated = bool(
        sn_data
        and isinstance(sn_data, dict)
        and sn_data.get("by_detector", {}).get(DETECTOR_ID)
    )

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(overdue_count),
        threshold=1.0,
        fired=bool(metrics) and overdue_count > 0,
        raw_evidence={
            "total_assignments": total,
            "overdue_count": overdue_count,
            "max_days_overdue": max_days_overdue,
            "avg_days_overdue": avg_days_overdue,
            "compliance_override": compliance_override,
            "proxy_field": metrics.get("proxy_field", "BenefitAssignment.NextPayoutDate"),
            "proxy_note": metrics.get("proxy_note", ""),
            "jira_corroborated": jira_corroborated,
            "sn_corroborated": sn_corroborated,
            "primary_object": "BenefitAssignment",
        },
    )


def detect(
    sf_data: Dict[str, Any],
    sn_data=None,
    jira_data=None,
) -> List[DetectorResult]:
    evaluation = evaluate(sf_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []
