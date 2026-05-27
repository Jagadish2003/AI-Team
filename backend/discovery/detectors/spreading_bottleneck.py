"""
SPREADING_BOTTLENECK detector — v3.1 (corrected from real org metadata)

Objects used:
  LLC_BI__Spread_Statement_Period__c — the period record (locked/unlocked signal)
  LLC_BI__Spread__c                  — spread header (holds LLC_BI__Loan__c)

Confirmed fields on LLC_BI__Spread_Statement_Period__c:
  LLC_BI__Is_Locked__c   — False = not finalised (the confirmed bottleneck signal)
  LLC_BI__Is_Annual__c   — confirmed exists
  LLC_BI__Spread__c      — parent spread header (use to resolve loan via two hops)
  LLC_BI__Analyst__c     — analyst assignment
  CreatedById            — fallback analyst proxy
  CreatedDate            — period age calculation

Corrected fields (were assumed, do NOT exist):
  LLC_BI__Loan__c        — NOT on period, is on LLC_BI__Spread__c (parent)
  LLC_BI__Statement_Date__c — NOT in FIELDS(ALL)
  LLC_BI__Source__c      — NOT in FIELDS(ALL)
  LLC_BI__Year__c        — NOT in FIELDS(ALL)

Loan resolution (two hops):
  SpreadStatementPeriod.LLC_BI__Spread__c → Spread.LLC_BI__Loan__c

Fires when: unlocked_count >= 1 (periods open for 14+ days)
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import (
    DetectorResult,
    detector_result_from_evaluation,
    make_detector_evaluation,
)

DETECTOR_ID    = "SPREADING_BOTTLENECK"
MIN_DAYS_OPEN  = 14  # unlocked for 14+ days = bottleneck

SIGNAL_METRICS = [
    "total_periods",      # spread statement period workload volume
    "unlocked_count",     # count of unfinished spread periods
    "max_days_unlocked",  # strongest open-period age signal
    "avg_days_unlocked",  # average open-period age signal
]


def evaluate(sf_data: Dict[str, Any], sn_data=None, jira_data=None):
    ncino = sf_data.get("ncino") or sf_data
    metrics = ncino.get("spreading_metrics", {})

    unlocked_count  = int(metrics.get("unlocked_count", 0))
    total           = int(metrics.get("total_periods", 0))
    max_days        = float(metrics.get("max_days_unlocked", 0))
    avg_days        = float(metrics.get("avg_days_unlocked", 0))

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(unlocked_count),
        threshold=1.0,
        raw_evidence={
            "total_periods":       total,
            "unlocked_count":      unlocked_count,
            "max_days_unlocked":   max_days,
            "avg_days_unlocked":   avg_days,
            "analyst_bottlenecks": metrics.get("analyst_bottlenecks", []),
            "primary_object":      "LLC_BI__Spread_Statement_Period__c",
            "loan_join_via":       "LLC_BI__Spread__c.LLC_BI__Loan__c",
            "analyst_field":       "LLC_BI__Analyst__c, fallback CreatedById",
            "signal_field":        "LLC_BI__Is_Locked__c",
        },
        fired=bool(metrics) and unlocked_count > 0,
    )


def detect(sf_data: Dict[str, Any], sn_data=None, jira_data=None) -> List[DetectorResult]:
    evaluation = evaluate(sf_data, sn_data, jira_data)
    return [detector_result_from_evaluation(evaluation)] if evaluation.fired else []
