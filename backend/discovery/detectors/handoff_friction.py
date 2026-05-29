"""
D2 — HANDOFF_FRICTION

Fires when: handoff_score > 1.5
            AND total_cases_90d >= 50 (volume guard)

SF-1.3 thresholds:
    handoff_score threshold: 1.5
    min_cases: 50
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "HANDOFF_FRICTION"
THRESHOLD = 1.5
MIN_CASES = 50

SIGNAL_METRICS = [
    "owner_changes_90d",  # direct operational handoff volume
    "total_cases_90d",   # workload denominator for the handoff score
    "handoff_score",     # primary trend score for handoff friction
]


def evaluate(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    cm = sf_data.get("case_metrics") or {}
    score = float(cm.get("handoff_score", 0.0))
    total_cases = int(cm.get("total_cases_90d", 0))
    owner_changes = int(cm.get("owner_changes_90d", 0))
    fired = total_cases >= MIN_CASES and score > THRESHOLD

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=round(score, 4),
        threshold=THRESHOLD,
        raw_evidence={
            "owner_changes_90d": owner_changes,
            "total_cases_90d": total_cases,
            "handoff_score": score,
            "top_categories": [
                {"category": c.get("category"), "handoff_score": c.get("handoff_score", 0)}
                for c in (cm.get("category_breakdown") or [])
                if float(c.get("handoff_score", 0)) > THRESHOLD
            ],
        },
        fired=fired,
    )


def detect(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
) -> List[DetectorResult]:
    evaluation = evaluate(sf_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []
