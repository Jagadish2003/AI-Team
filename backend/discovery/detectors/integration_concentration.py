"""
D5 — INTEGRATION_CONCENTRATION

Fires when: MAX(flow_reference_count across all named credentials) >= 3

SF-1.3 thresholds:
    min_flow_refs: 3 (3 or more distinct flows reference the same credential)
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import DetectorResult, make_detector_evaluation

DETECTOR_ID = "INTEGRATION_CONCENTRATION"
THRESHOLD = 3

SIGNAL_METRICS = [
    "credential_count",          # total integration credentials evaluated
    "max_flow_reference_count",  # strongest concentration signal
    "firing_credential_count",   # count of credentials crossing threshold
]


def evaluate(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    credentials = sf_data.get("named_credentials") or []
    ref_counts = [int(nc.get("flow_reference_count", 0)) for nc in credentials]
    max_ref_count = max(ref_counts, default=0)
    firing_count = sum(1 for count in ref_counts if count >= THRESHOLD)
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(max_ref_count),
        threshold=float(THRESHOLD),
        fired=firing_count > 0,
        raw_evidence={
            "credential_count": len(credentials),
            "max_flow_reference_count": max_ref_count,
            "firing_credential_count": firing_count,
        },
    )


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
