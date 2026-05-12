"""
DISABILITY_REVIEW_BOTTLENECK detector — Sprint 5.2 ENG-STRS-2

Object: Case (PSS — disability record type)
Confirmed fields (PSS metadata May 2026):
  Status, Type, CreatedDate, ClosedDate

Fires when: pending_review_count >= 1

COMPLIANCE OVERRIDE: when member_stopped_work = True, impact is
forced to 9. A disabled member who has stopped working has no income
while the review is pending. STRS has an obligation to process
disability applications within the Medical Review Board timeline.

member_stopped_work proxy: max_days_pending >= 30 days.
In a live STRS implementation this flag would come from employment
status in the pension system. SF-STRS-2 to confirm the exact
disability Case RecordType and whether a stopped-work field exists.
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import DetectorResult

DETECTOR_ID = "DISABILITY_REVIEW_BOTTLENECK"


def detect(
    sf_data: Dict[str, Any],
    sn_data=None,
    jira_data=None,
) -> List[DetectorResult]:
    strs = sf_data.get("strs_benefits") or sf_data
    metrics = strs.get("disability_metrics", {})
    if not metrics:
        return []

    pending_count        = int(metrics.get("pending_review_count", 0))
    max_days_pending     = float(metrics.get("max_days_pending", 0))
    avg_days_pending     = float(metrics.get("avg_days_pending", 0))
    total                = int(metrics.get("total_disability_cases", 0))
    threshold_days       = int(metrics.get("review_threshold_days", 30))
    member_stopped_work  = bool(metrics.get("member_stopped_work", False))
    # compliance_override when member has no income during review
    compliance_override  = member_stopped_work

    if pending_count == 0:
        return []

    return [DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(max_days_pending),
        threshold=float(threshold_days),
        raw_evidence={
            "total_disability_cases":  total,
            "pending_review_count":    pending_count,
            "max_days_pending":        max_days_pending,
            "avg_days_pending":        avg_days_pending,
            "review_threshold_days":   threshold_days,
            "member_stopped_work":     member_stopped_work,
            "compliance_override":     compliance_override,
            "primary_object":          "Case",
            "sme_note":                metrics.get("sme_note", ""),
        },
    )]
