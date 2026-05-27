"""
D3 — APPROVAL_BOTTLENECK

Fires when: (avg_delay_days > 3 AND bottleneck_score > 10)
            OR avg_delay_days > 7 (severe delay alone)

SF-1.3 thresholds:
    delay_threshold: 3 days
    bottleneck_threshold: 10
    severe_delay_threshold: 7 days
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import DetectorResult, make_detector_evaluation

DETECTOR_ID = "APPROVAL_BOTTLENECK"
DELAY_THRESHOLD = 3.0
BOTTLENECK_THRESHOLD = 10.0
SEVERE_DELAY = 7.0

SIGNAL_METRICS = [
    "process_count",        # number of approval processes evaluated
    "pending_count",        # total pending approval workload
    "max_avg_delay_days",   # strongest delay signal across processes
    "max_bottleneck_score", # strongest bottleneck score across processes
    "firing_process_count", # count of processes crossing the detector threshold
]


def evaluate(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
):
    approval_processes = sf_data.get("approval_processes") or []
    max_delay = 0.0
    max_bottleneck = 0.0
    pending_total = 0
    firing_count = 0

    for ap in approval_processes:
        delay = float(ap.get("avg_delay_days", 0.0))
        b_score = float(ap.get("bottleneck_score", 0.0))
        pending_total += int(ap.get("pending_count", 0))
        max_delay = max(max_delay, delay)
        max_bottleneck = max(max_bottleneck, b_score)
        if (delay > DELAY_THRESHOLD and b_score > BOTTLENECK_THRESHOLD) or delay > SEVERE_DELAY:
            firing_count += 1

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=round(max_delay, 2),
        threshold=DELAY_THRESHOLD,
        fired=firing_count > 0,
        raw_evidence={
            "process_count": len(approval_processes),
            "pending_count": pending_total,
            "max_avg_delay_days": max_delay,
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
        delay = float(ap.get("avg_delay_days", 0.0))
        b_score = float(ap.get("bottleneck_score", 0.0))
        pending = int(ap.get("pending_count", 0))

        combined_fires = delay > DELAY_THRESHOLD and b_score > BOTTLENECK_THRESHOLD
        severe_fires = delay > SEVERE_DELAY

        if not (combined_fires or severe_fires):
            continue

        results.append(DetectorResult(
            detector_id=DETECTOR_ID,
            signal_source="salesforce",
            metric_value=round(delay, 2),
            threshold=DELAY_THRESHOLD,
            raw_evidence={
                "process_name": ap.get("process_name", ""),
                "pending_count": pending,
                "avg_delay_days": delay,
                "approver_count": int(ap.get("approver_count", 0)),
                "bottleneck_score": b_score,
            },
        ))

    return results
