"""
DISBURSEMENT_OVERDUE detector — Sprint 5.2 ENG-STRS-2

Object: BenefitAssignment (PSS) — NextPayoutDate proxy
Confirmed fields (PSS metadata May 2026):
  Status, NextPayoutDate

Fires when: overdue_count >= 1

COMPLIANCE OVERRIDE: any overdue disbursement forces impact = 9.
STRS has a legal obligation to pay benefits on the scheduled date
per Ohio Revised Code 3307. A missed payment is a regulatory event,
not an operational inconvenience.

Proxy design: BenefitDisbursement not in PSS metadata for this org.
Using BenefitAssignment.NextPayoutDate as disbursement proxy:
  NextPayoutDate < TODAY AND Status is active = disbursement overdue.
SF-STRS-3 to confirm or replace with BenefitDisbursement if available
in the PSS trial org.
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import DetectorResult

DETECTOR_ID = "DISBURSEMENT_OVERDUE"
SIGNAL_METRICS = {
    "overdue_count":     "Number of overdue disbursements; primary compliance volume trend",
    "max_days_overdue":  "Worst-case days overdue; regulatory severity indicator",
    "avg_days_overdue":  "Average days overdue; smoothed trend across all overdue payments",
    "total_assignments": "Total assignments evaluated; normalises overdue_count as a rate",
}


def detect(
    sf_data: Dict[str, Any],
    sn_data=None,
    jira_data=None,
) -> List[DetectorResult]:
    strs = sf_data.get("strs_benefits") or sf_data
    metrics = strs.get("disbursement_metrics", {})
    if not metrics:
        return []

    overdue_count     = int(metrics.get("overdue_count", 0))
    max_days_overdue  = float(metrics.get("max_days_overdue", 0))
    avg_days_overdue  = float(metrics.get("avg_days_overdue", 0))
    total             = int(metrics.get("total_assignments", 0))
    # compliance_override is always True for this detector
    # Any overdue disbursement = regulatory obligation breach
    compliance_override = True

    if overdue_count == 0:
        return []

    # ── ENG-STRS-CORR-1/2: Mark corroboration in raw_evidence ──────────────
    # runner.py reads these flags to set jira_corroborated/sn_corroborated.
    # jira_data and sn_data contain by_detector dicts from corroboration modules.
    jira_corroborated = bool(
        jira_data and
        isinstance(jira_data, dict) and
        jira_data.get("by_detector", {}).get("DISBURSEMENT_OVERDUE")
    )
    sn_corroborated = bool(
        sn_data and
        isinstance(sn_data, dict) and
        sn_data.get("by_detector", {}).get("DISBURSEMENT_OVERDUE")
    )

    return [DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(overdue_count),
        threshold=1.0,
        raw_evidence={
            "total_assignments":    total,
            "overdue_count":        overdue_count,
            "max_days_overdue":     max_days_overdue,
            "avg_days_overdue":     avg_days_overdue,
            "compliance_override":  compliance_override,
            "proxy_field":          metrics.get("proxy_field", "BenefitAssignment.NextPayoutDate"),
            "proxy_note":           metrics.get("proxy_note", ""),
            "jira_corroborated":    jira_corroborated,
            "sn_corroborated":      sn_corroborated,
            "primary_object":       "BenefitAssignment",
        },
    )]
