from __future__ import annotations
from typing import Any, Dict, List
from ..types import DetectorResult

THRESHOLD_DAYS = 3.0

def detect(sf_data: Dict[str, Any], sn_data: Dict[str, Any], jira_data: Dict[str, Any]) -> List[DetectorResult]:
    ingested = sf_data
    steps = ingested.get("salesforce_approval_pending") or []
    out: List[DetectorResult] = []
    for s in steps:
        pending = float(s.get("pending_count") or 0)
        avg_age = float(s.get("avg_age_days") or 0.0)
        if pending <= 0:
            continue
        if avg_age > THRESHOLD_DAYS:
            out.append(DetectorResult(
                detector_id="APPROVAL_BOTTLENECK",
                signal_source="Salesforce.ProcessInstance",
                metric_name="avg_pending_age_days",
                metric_value=avg_age,
                threshold=THRESHOLD_DAYS,
                label="APPROVAL_BOTTLENECK",
                raw_evidence={
                    "process": s.get("process_name"),
                    "step": s.get("step_name"),
                    "pending_count": pending,
                    "avg_age_days": avg_age,
                    "approver_count": s.get("approver_count"),
                }
            ))
    return out
