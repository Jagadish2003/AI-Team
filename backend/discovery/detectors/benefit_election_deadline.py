"""
BENEFIT_ELECTION_DEADLINE detector - Sprint 5.2 ENG-STRS-2.

Fires when approved benefit assignments are overdue for payment election.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "BENEFIT_ELECTION_DEADLINE"
SIGNAL_METRICS = {
    "overdue_election_count": "Number of elections past deadline; primary volume trend",
    "max_days_overdue":       "Worst-case days overdue; tracks severity of missed deadlines",
    "total_assignments":      "Total assignments evaluated; context for overdue rate",
}

SIGNAL_METRICS = [
    "total_assignments",       # benefit assignment workload volume
    "overdue_election_count",  # count of overdue benefit elections
    "max_days_overdue",        # strongest overdue-election age signal
]


def evaluate(
    sf_data: Dict[str, Any],
    sn_data=None,
    jira_data=None,
):
    strs = sf_data.get("strs_benefits") or sf_data
    metrics = strs.get("election_metrics", {})

    overdue_count = int(metrics.get("overdue_election_count", 0))
    max_days_overdue = float(metrics.get("max_days_overdue", 0))
    total = int(metrics.get("total_assignments", 0))
    deadline_days = int(metrics.get("election_deadline_days", 21))
    default_plan_risk = bool(metrics.get("default_plan_risk", False))

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
            "overdue_election_count": overdue_count,
            "max_days_overdue": max_days_overdue,
            "election_deadline_days": deadline_days,
            "default_plan_risk": default_plan_risk,
            "jira_corroborated": jira_corroborated,
            "sn_corroborated": sn_corroborated,
            "primary_object": "BenefitAssignment",
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
