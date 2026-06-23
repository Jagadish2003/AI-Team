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
from ..models import DetectorResult

DETECTOR_ID = "APPROVAL_BOTTLENECK"
DELAY_THRESHOLD = 3.0
BOTTLENECK_THRESHOLD = 10.0
SEVERE_DELAY = 7.0


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
