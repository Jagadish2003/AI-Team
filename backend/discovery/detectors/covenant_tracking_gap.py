"""
COVENANT_TRACKING_GAP detector — v3 (confirmed objects)

Object: LLC_BI__Covenant_Compliance__c (per-loan covenant link)
        joined to LLC_BI__Covenant2__c (modern covenant standard)

Confirmed fields (real org metadata May 2026):
  LLC_BI__Covenant2__r.LLC_BI__Overdue__c  — boolean overdue flag
  LLC_BI__Covenant2__r.LLC_BI__Breached__c — boolean breach flag
  LLC_BI__Covenant2__r.LLC_BI__Days_Past_Next_Evaluation__c — days overdue

Fires when: overdue_count >= 1  OR  breached_count >= 1
Compliance override: breached_count >= 1 → impact = 9
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID = "COVENANT_TRACKING_GAP"

SIGNAL_METRICS = [
    "total_covenants",          # covenant workload volume
    "overdue_count",            # count of overdue covenant evaluations
    "breached_count",           # count of breached covenants
    "max_days_past_evaluation", # strongest overdue-age signal
]


def evaluate(sf_data: Dict[str, Any], sn_data=None, jira_data=None):
    ncino = sf_data.get("ncino") or sf_data
    metrics = ncino.get("covenant_metrics", {})

    overdue_count  = int(metrics.get("overdue_count", 0))
    breached_count = int(metrics.get("breached_count", 0))
    total          = int(metrics.get("total_covenants", 0))
    compliance_override = bool(metrics.get("compliance_override", False))
    max_days_past  = float(metrics.get("max_days_past_evaluation", 0))

    metric_value = float(breached_count if breached_count > 0 else overdue_count)
    threshold    = 1.0

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=metric_value,
        threshold=threshold,
        raw_evidence={
            "total_covenants":          total,
            "overdue_count":            overdue_count,
            "breached_count":           breached_count,
            "compliance_override":      compliance_override,
            "max_days_past_evaluation": max_days_past,
            "primary_object":           "LLC_BI__Covenant_Compliance__c",
            "covenant_object":          "LLC_BI__Covenant2__c",
        },
        fired=bool(metrics) and (overdue_count > 0 or breached_count > 0),
    )


def detect(sf_data: Dict[str, Any], sn_data=None, jira_data=None) -> List[DetectorResult]:
    evaluation = evaluate(sf_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []
