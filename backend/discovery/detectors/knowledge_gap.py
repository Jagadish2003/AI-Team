"""
D4 — KNOWLEDGE_GAP

Fires when: knowledge_gap_score > 0.40
            AND closed_cases_90d >= 30

SF-1.3 thresholds:
    gap_threshold: 0.40
    min_closed_cases: 30
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "KNOWLEDGE_GAP"
THRESHOLD = 0.40
MIN_CLOSED = 30

SIGNAL_METRICS = [
    "closed_cases_90d",     # volume of resolved cases eligible for knowledge reuse
    "cases_with_kb_link",   # coverage count for linked knowledge articles
    "knowledge_gap_score",  # primary trend score for knowledge gaps
]


def evaluate(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    cm = sf_data.get("case_metrics") or {}
    score = float(cm.get("knowledge_gap_score", 0.0))
    closed = int(cm.get("closed_cases_90d", 0))
    kb_linked = int(cm.get("cases_with_kb_link", 0))
    fired = closed >= MIN_CLOSED and score > THRESHOLD

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=round(score, 4),
        threshold=THRESHOLD,
        raw_evidence={
            "closed_cases_90d": closed,
            "cases_with_kb_link": kb_linked,
            "knowledge_gap_score": score,
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
