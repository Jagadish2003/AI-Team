from __future__ import annotations
from typing import Any, Dict, List
from ..types import DetectorResult

THRESHOLD = 10.0

def detect(ingested: Dict[str, Any]) -> List[DetectorResult]:
    rows = ingested.get("salesforce_permission_bottlenecks") or []
    out: List[DetectorResult] = []
    for r in rows:
        score = float(r.get("bottleneck_score") or 0.0)
        if score > THRESHOLD:
            out.append(DetectorResult(
                detector_id="PERMISSION_BOTTLENECK",
                signal_source="Salesforce.ProcessNode",
                metric_name="bottleneck_score",
                metric_value=score,
                threshold=THRESHOLD,
                label="PERMISSION_BOTTLENECK",
                raw_evidence={
                    "step_name": r.get("step_name"),
                    "pending_count": r.get("pending_count"),
                    "approver_count": r.get("approver_count"),
                    "bottleneck_score": score,
                }
            ))
    return out
