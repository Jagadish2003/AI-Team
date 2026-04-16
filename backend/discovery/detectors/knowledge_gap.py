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
from ..models import DetectorResult

DETECTOR_ID = "KNOWLEDGE_GAP"
THRESHOLD = 0.40
MIN_CLOSED = 30


def detect(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
) -> List[DetectorResult]:
    cm = sf_data.get("case_metrics") or {}
    score = float(cm.get("knowledge_gap_score", 0.0))
    closed = int(cm.get("closed_cases_90d", 0))
    kb_linked = int(cm.get("cases_with_kb_link", 0))

    if closed < MIN_CLOSED:
        return []
    if score <= THRESHOLD:
        return []

    return [DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=round(score, 4),
        threshold=THRESHOLD,
        raw_evidence={
            "closed_cases_90d": closed,
            "cases_with_kb_link": kb_linked,
            "knowledge_gap_score": score,
        },
    )]
