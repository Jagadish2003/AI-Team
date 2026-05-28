"""
APPROVAL_BOTTLENECK detector — v3 (confirmed objects)

Object: ProcessInstance (standard Salesforce approval workflow)
        Confirmed from real org: ProcessInstance.TargetObjectId
        links to LLC_BI__Loan__c records.

LLC_BI__Approval__c does NOT exist in this org.
LLC_BI__Credit_Decision__c is an automated credit scoring object
(has Behavioral_Score, Model_Version fields), NOT human approvals.

Fires when: pending_count >= 1  OR  max_cycle_days >= 7
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID      = "APPROVAL_BOTTLENECK"
PENDING_THRESHOLD = 1
CYCLE_THRESHOLD   = 7  # days

SIGNAL_METRICS = [
    "total_instances", # approval workflow workload volume
    "pending_count",   # current pending approval count
    "max_cycle_days",  # strongest approval-cycle age signal
    "avg_cycle_days",  # average approval-cycle age signal
]


def evaluate(sf_data: Dict[str, Any], sn_data=None, jira_data=None):
    ncino = sf_data.get("ncino") or sf_data
    metrics = ncino.get("approval_metrics", {})

    pending_count  = int(metrics.get("pending_count", 0))
    total          = int(metrics.get("total_instances", 0))
    max_cycle      = float(metrics.get("max_cycle_days", 0))
    avg_cycle      = float(metrics.get("avg_cycle_days", 0))

    fires_pending = pending_count >= PENDING_THRESHOLD
    fires_cycle   = max_cycle >= CYCLE_THRESHOLD

    metric_value = float(pending_count if fires_pending else max_cycle)
    threshold    = float(PENDING_THRESHOLD if fires_pending else CYCLE_THRESHOLD)

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=metric_value,
        threshold=threshold,
        raw_evidence={
            "total_instances": total,
            "pending_count":   pending_count,
            "max_cycle_days":  max_cycle,
            "avg_cycle_days":  avg_cycle,
            "primary_object":  "ProcessInstance",
            "approval_note":   "Standard Salesforce approval workflow. LLC_BI__Approval__c does not exist in this org.",
        },
        fired=bool(metrics) and (fires_pending or fires_cycle),
    )


def detect(sf_data: Dict[str, Any], sn_data=None, jira_data=None) -> List[DetectorResult]:
    evaluation = evaluate(sf_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []
