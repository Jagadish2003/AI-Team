from __future__ import annotations
from typing import Any, Dict, List
from ..types import DetectorResult

THRESHOLD = 0.6

def detect(sf_data: Dict[str, Any], sn_data: Dict[str, Any], jira_data: Dict[str, Any]) -> List[DetectorResult]:
    # Ingestion contract:
    # Preferred: provide flow_activity_score per flow.
    # Fallback: provide records_per_day_proxy + active_flows_on_object to compute the score.

    ingested = sf_data

    flows = ingested.get("salesforce_flow_inventory") or []
    out: List[DetectorResult] = []
    for f in flows:
        if not f.get("is_active", True):
            continue
        ftype = (f.get("type") or "").lower()
        if ftype not in ("record-triggered", "record_triggered", "recordtriggered"):
            continue
        element_count = float(f.get("element_count") or 0)
        if element_count <= 0:
            continue
        score = float(f.get("flow_activity_score") or 0.0)
        if score == 0.0:
            records_per_day = float(f.get("records_per_day_proxy") or 0.0)
            active_flows_on_object = float(f.get("active_flows_on_object") or 1.0)
            score = records_per_day * (1.0 / element_count) * active_flows_on_object
        if score > THRESHOLD and element_count < 15:
            out.append(DetectorResult(
                detector_id="REPETITIVE_AUTOMATION",
                signal_source="Salesforce.FlowVersionView",
                metric_name="flow_activity_score",
                metric_value=score,
                threshold=THRESHOLD,
                label="REPETITIVE_AUTOMATION",
                raw_evidence={
                    "flowId": f.get("id"),
                    "flowName": f.get("name"),
                    "triggerObject": f.get("trigger_object"),
                    "elementCount": element_count,
                    "score": score,
                }
            ))
    return out
