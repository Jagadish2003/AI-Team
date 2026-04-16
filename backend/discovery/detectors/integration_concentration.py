"""
D5 — INTEGRATION_CONCENTRATION

Fires when: MAX(flow_reference_count across all named credentials) >= 3

SF-1.3 thresholds:
    min_flow_refs: 3 (3 or more distinct flows reference the same credential)
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import DetectorResult

DETECTOR_ID = "INTEGRATION_CONCENTRATION"
THRESHOLD = 3


def detect(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
) -> List[DetectorResult]:
    credentials = sf_data.get("named_credentials") or []
    results = []

    for nc in credentials:
        ref_count = int(nc.get("flow_reference_count", 0))
        if ref_count < THRESHOLD:
            continue

        results.append(DetectorResult(
            detector_id=DETECTOR_ID,
            signal_source="salesforce",
            metric_value=float(ref_count),
            threshold=float(THRESHOLD),
            raw_evidence={
                "credential_name": nc.get("credential_name", ""),
                "credential_developer_name": nc.get("credential_developer_name", ""),
                "flow_reference_count": ref_count,
                "referencing_flow_ids": nc.get("referencing_flow_ids", []),
                "match_type": nc.get("match_type", "name"),
            },
        ))

    return results
