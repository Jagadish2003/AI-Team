"""
DISABILITY_REVIEW_BOTTLENECK detector - Sprint 5.2 ENG-STRS-2.

Fires when disability reviews are pending beyond the review threshold.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "DISABILITY_REVIEW_BOTTLENECK"

SIGNAL_METRICS = [
    "total_disability_cases", # disability case workload volume
    "pending_review_count",   # count of pending reviews
    "max_days_pending",       # strongest pending-review age signal
    "avg_days_pending",       # average pending-review age signal
]


def evaluate(
    sf_data: Dict[str, Any],
    sn_data=None,
    jira_data=None,
):
    strs = sf_data.get("strs_benefits") or sf_data
    metrics = strs.get("disability_metrics", {})

    pending_count = int(metrics.get("pending_review_count", 0))
    max_days_pending = float(metrics.get("max_days_pending", 0))
    avg_days_pending = float(metrics.get("avg_days_pending", 0))
    total = int(metrics.get("total_disability_cases", 0))
    threshold_days = int(metrics.get("review_threshold_days", 30))
    member_stopped_work = bool(metrics.get("member_stopped_work", False))
    compliance_override = member_stopped_work

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
        metric_value=float(max_days_pending),
        threshold=float(threshold_days),
        fired=bool(metrics) and pending_count > 0,
        raw_evidence={
            "total_disability_cases": total,
            "pending_review_count": pending_count,
            "max_days_pending": max_days_pending,
            "avg_days_pending": avg_days_pending,
            "review_threshold_days": threshold_days,
            "member_stopped_work": member_stopped_work,
            "compliance_override": compliance_override,
            "jira_corroborated": jira_corroborated,
            "sn_corroborated": sn_corroborated,
            "primary_object": "Case",
            "sme_note": metrics.get("sme_note", ""),
        },
    )


def detect(
    sf_data: Dict[str, Any],
    sn_data=None,
    jira_data=None,
) -> List[DetectorResult]:
    evaluation = evaluate(sf_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []
