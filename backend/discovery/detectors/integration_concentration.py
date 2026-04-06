from __future__ import annotations
from typing import Any, Dict, List
from ..types import DetectorResult

THRESHOLD = 3

def detect(ingested: Dict[str, Any]) -> List[DetectorResult]:
    creds = ingested.get("salesforce_named_credentials") or []
    out: List[DetectorResult] = []
    for c in creds:
        ref_count = int(c.get("flow_reference_count") or 0)
        if ref_count >= THRESHOLD:
            out.append(DetectorResult(
                detector_id="INTEGRATION_CONCENTRATION",
                signal_source="Salesforce.NamedCredential",
                metric_name="flow_reference_count",
                metric_value=float(ref_count),
                threshold=float(THRESHOLD),
                label="INTEGRATION_CONCENTRATION",
                raw_evidence={
                    "named_credential": c.get("name"),
                    "endpoint": c.get("endpoint"),
                    "flow_reference_count": ref_count,
                    "referencing_flow_ids": c.get("referencing_flow_ids") or [],
                }
            ))
    return out
