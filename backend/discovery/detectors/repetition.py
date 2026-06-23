"""
D1 — REPETITIVE_AUTOMATION

Fires when: flow_activity_score > 0.6
            AND ProcessType = AutoLaunchedFlow
            AND avg_element_count < 15 (LOW complexity)

SF-1.3 thresholds:
    flow_activity_score threshold: 0.6
    element_count threshold: 15 (max for LOW complexity)
    min_cases_90d: 50 (insufficient signal guard)
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import DetectorResult

DETECTOR_ID = "REPETITIVE_AUTOMATION"
SCORE_THRESHOLD = 0.6
ELEMENT_THRESHOLD = 15
MIN_VOLUME = 50


def detect(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
) -> List[DetectorResult]:
    fi = sf_data.get("flow_inventory") or {}
    score = float(fi.get("flow_activity_score", 0.0))
    avg_elements = float(fi.get("avg_element_count", 0.0))
    records_90d = int((sf_data.get("case_metrics") or {}).get("total_cases_90d", 0))

    # Guard: insufficient signal
    if records_90d < MIN_VOLUME:
        return []

    # Guard: flows exist and are low-complexity AutoLaunched
    flows = fi.get("flows") or []
    auto_low = [
        f for f in flows
        if f.get("process_type") == "AutoLaunchedFlow"
        and int(f.get("element_count", 99)) < ELEMENT_THRESHOLD
    ]
    if not auto_low:
        return []

    if score <= SCORE_THRESHOLD:
        return []

    return [DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=round(score, 4),
        threshold=SCORE_THRESHOLD,
        raw_evidence={
            "flow_id": auto_low[0].get("flow_id", ""),
            "flow_label": auto_low[0].get("flow_label", ""),
            "trigger_object": fi.get("trigger_object", ""),
            "records_90d": records_90d,
            "element_count": int(auto_low[0].get("element_count", 0)),
            "active_flow_count_on_object": len(auto_low),
            "flow_activity_score": score,
        },
    )]
