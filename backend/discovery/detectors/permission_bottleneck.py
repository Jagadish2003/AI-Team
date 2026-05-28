"""
D6 — PERMISSION_BOTTLENECK

Fires independently of D3. D3 fires on delay. D6 fires on concentration.

Fires when: bottleneck_score > 10
            AND approver_count > 0 (at least one approver identified)

SF-1.3 thresholds:
    bottleneck_threshold: 10
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import DetectorResult, make_detector_evaluation

DETECTOR_ID = "PERMISSION_BOTTLENECK"
THRESHOLD = 10.0

SIGNAL_METRICS = [
    "process_count",        # number of approval processes evaluated
    "pending_count",        # total pending approval workload
    "approver_count",       # total approver concentration
    "max_bottleneck_score", # strongest permission bottleneck score
    "firing_process_count", # count of processes crossing threshold
]


def evaluate(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    approval_processes = sf_data.get("approval_processes") or []
    pending_total = 0
    approver_total = 0
    max_bottleneck = 0.0
    firing_count = 0

    for ap in approval_processes:
        b_score = float(ap.get("bottleneck_score", 0.0))
        approver_count = int(ap.get("approver_count", 0))
        pending_total += int(ap.get("pending_count", 0))
        approver_total += approver_count
        max_bottleneck = max(max_bottleneck, b_score)
        if approver_count > 0 and b_score > THRESHOLD:
            firing_count += 1

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=round(max_bottleneck, 2),
        threshold=THRESHOLD,
        fired=firing_count > 0,
        raw_evidence={
            "process_count": len(approval_processes),
            "pending_count": pending_total,
            "approver_count": approver_total,
            "max_bottleneck_score": max_bottleneck,
            "firing_process_count": firing_count,
        },
    )


def detect(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
) -> List[DetectorResult]:
    approval_processes = sf_data.get("approval_processes") or []
    results = []

    for ap in approval_processes:
        b_score = float(ap.get("bottleneck_score", 0.0))
        approver_count = int(ap.get("approver_count", 0))

        if approver_count == 0:
            continue
        if b_score <= THRESHOLD:
            continue

        results.append(DetectorResult(
            detector_id=DETECTOR_ID,
            signal_source="salesforce",
            metric_value=round(b_score, 2),
            threshold=THRESHOLD,
            raw_evidence={
                "process_name": ap.get("process_name", ""),
                "pending_count": int(ap.get("pending_count", 0)),
                "approver_count": approver_count,
                "bottleneck_score": b_score,
            },
        ))

    return results
