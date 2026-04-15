from __future__ import annotations
from typing import Any, Dict, List
from ..types import DetectorResult

THRESHOLD = 0.15

def detect(sf_data: Dict[str, Any], sn_data: Dict[str, Any], jira_data: Dict[str, Any]) -> List[DetectorResult]:
    ingested = sf_data
    sf = ingested.get("salesforce_cross_system_references") or {}
    sn = ingested.get("servicenow_cross_system_references") or {}
    score = float(sf.get("echo_score") or 0.0)
    total = float(sf.get("total_count") or 0)
    match = float(sf.get("match_count") or 0)
    out: List[DetectorResult] = []
    if total > 0 and score > THRESHOLD:
        out.append(DetectorResult(
            detector_id="CROSS_SYSTEM_ECHO",
            signal_source="Salesforce.Case",
            metric_name="echo_score",
            metric_value=score,
            threshold=THRESHOLD,
            label="CROSS_SYSTEM_ECHO",
            raw_evidence={
                "match_count": match,
                "total_count": total,
                "echo_score": score,
                "servicenow_echo_score": float(sn.get("echo_score") or 0.0),
            }
        ))
    return out
