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
from ..models import DetectorResult

DETECTOR_ID    = "SPREADING_BOTTLENECK"
MIN_DAYS_OPEN  = 14  # unlocked for 14+ days = bottleneck
SIGNAL_METRICS = {
    "unlocked_count":    "Number of spread periods open 14+ days; primary bottleneck volume",
    "max_days_unlocked": "Worst-case days unlocked; tracks severity of longest open period",
    "avg_days_unlocked": "Average days unlocked; smoothed trend across all open periods",
    "total_periods":     "Total periods evaluated; normalises unlocked_count as a rate",
}

def detect(sf_data: Dict[str, Any], sn_data=None, jira_data=None) -> List[DetectorResult]:
    ncino = sf_data.get("ncino") or sf_data
    metrics = ncino.get("spreading_metrics", {})
    if not metrics:
        return []

    unlocked_count  = int(metrics.get("unlocked_count", 0))
    total           = int(metrics.get("total_periods", 0))
    max_days        = float(metrics.get("max_days_unlocked", 0))
    avg_days        = float(metrics.get("avg_days_unlocked", 0))

    if unlocked_count == 0:
        return []

    return [DetectorResult(
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
    )]
