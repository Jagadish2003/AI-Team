"""
APPLICATION_STALL detector - Sprint 5.2 ENG-STRS-2

Object: IndividualApplication (PSS)
Fires when stalled retirement applications are present.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "APPLICATION_STALL"
SIGNAL_METRICS = {
    "stalled_count":      "Number of applications currently stalled; primary volume trend",
    "max_days_stalled":   "Worst-case staleness in days; tracks if backlog is getting older",
    "avg_days_stalled":   "Average days stalled; smoothed trend across all stalled apps",
    "total_applications": "Total applications evaluated; normalises stalled_count as a rate",
}

SIGNAL_METRICS = [
    "total_applications",  # application workload volume
    "stalled_count",       # count of stalled applications
    "max_days_stalled",    # strongest stalled-application age signal
    "avg_days_stalled",    # average stalled-application age signal
]


def evaluate(
    sf_data: Dict[str, Any],
    sn_data=None,
    jira_data=None,
):
    strs = sf_data.get("strs_benefits") or sf_data
    metrics = strs.get("application_metrics", {})

    stalled_count = int(metrics.get("stalled_count", 0))
    max_days_stalled = float(metrics.get("max_days_stalled", 0))
    avg_days_stalled = float(metrics.get("avg_days_stalled", 0))
    total = int(metrics.get("total_applications", 0))
    threshold_days = int(metrics.get("stall_threshold_days", 30))

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
        metric_value=float(max_days_stalled),
        threshold=float(threshold_days),
        fired=bool(metrics) and stalled_count > 0,
        raw_evidence={
            "total_applications": total,
            "stalled_count": stalled_count,
            "max_days_stalled": max_days_stalled,
            "avg_days_stalled": avg_days_stalled,
            "stall_threshold_days": threshold_days,
            "jira_corroborated": jira_corroborated,
            "sn_corroborated": sn_corroborated,
            "primary_object": "IndividualApplication",
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
