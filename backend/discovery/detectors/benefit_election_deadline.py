"""
BENEFIT_ELECTION_DEADLINE detector — Sprint 5.2 ENG-STRS-2

Object: BenefitAssignment (PSS)
Confirmed fields (PSS metadata May 2026):
  Status, ApprovalStatus, AssignmentDate, NextPayoutDate

Fires when: overdue_election_count >= 1

Signal: benefit approved but member has not submitted payment election
within the required window. After the deadline the member is assigned
a default payment plan — an irreversible decision they may not want.

Threshold: 21 days from BenefitAssignment approval.
SF-STRS-1 to confirm exact ApprovalStatus and Status picklist values.
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import DetectorResult

DETECTOR_ID = "BENEFIT_ELECTION_DEADLINE"
SIGNAL_METRICS = {
    "overdue_election_count": "Number of elections past deadline; primary volume trend",
    "max_days_overdue":       "Worst-case days overdue; tracks severity of missed deadlines",
    "total_assignments":      "Total assignments evaluated; context for overdue rate",
}


def detect(
    sf_data: Dict[str, Any],
    sn_data=None,
    jira_data=None,
) -> List[DetectorResult]:
    strs = sf_data.get("strs_benefits") or sf_data
    metrics = strs.get("election_metrics", {})
    if not metrics:
        return []

    overdue_count      = int(metrics.get("overdue_election_count", 0))
    max_days_overdue   = float(metrics.get("max_days_overdue", 0))
    total              = int(metrics.get("total_assignments", 0))
    deadline_days      = int(metrics.get("election_deadline_days", 21))
    default_plan_risk  = bool(metrics.get("default_plan_risk", False))

    if overdue_count == 0:
        return []

    # ── ENG-STRS-CORR-1/2: Mark corroboration in raw_evidence ──────────────
    # runner.py reads these flags to set jira_corroborated/sn_corroborated.
    # jira_data and sn_data contain by_detector dicts from corroboration modules.
    jira_corroborated = bool(
        jira_data and
        isinstance(jira_data, dict) and
        jira_data.get("by_detector", {}).get("BENEFIT_ELECTION_DEADLINE")
    )
    sn_corroborated = bool(
        sn_data and
        isinstance(sn_data, dict) and
        sn_data.get("by_detector", {}).get("BENEFIT_ELECTION_DEADLINE")
    )

    return [DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(overdue_count),
        threshold=1.0,
        raw_evidence={
            "total_assignments":      total,
            "overdue_election_count": overdue_count,
            "max_days_overdue":       max_days_overdue,
            "election_deadline_days": deadline_days,
            "default_plan_risk":      default_plan_risk,
            "jira_corroborated":    jira_corroborated,
            "sn_corroborated":      sn_corroborated,
            "primary_object":         "BenefitAssignment",
            "sme_note":               metrics.get("sme_note", ""),
        },
    )]
