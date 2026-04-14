from __future__ import annotations
from typing import Any, Dict, List
from ..types import DetectorResult

THRESHOLD = 1.5

def detect(sf_data: Dict[str, Any], sn_data: Dict[str, Any], jira_data: Dict[str, Any]) -> List[DetectorResult]:
    ingested = sf_data
    rows = ingested.get("salesforce_case_metrics") or []
    out: List[DetectorResult] = []
    for r in rows:
        volume = float(r.get("volume") or 0)
        avg_re = float(r.get("avg_reassignments") or 0)
        if volume < 50:
            continue
        if avg_re > THRESHOLD:
            out.append(DetectorResult(
                detector_id="HANDOFF_FRICTION",
                signal_source="Salesforce.CaseHistory",
                metric_name="avg_reassignments",
                metric_value=avg_re,
                threshold=THRESHOLD,
                label="HANDOFF_FRICTION",
                raw_evidence={"category": r.get("category"), "volume": volume, "avg_reassignments": avg_re}
            ))
    return out
