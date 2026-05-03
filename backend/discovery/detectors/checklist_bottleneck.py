"""
CHECKLIST_BOTTLENECK detector — v3 (confirmed objects)

Object: LLC_BI__Checklist__c (confirmed from real org metadata May 2026)

Confirmed fields:
  LLC_BI__Actual_Duration_Days__c   — actual time taken
  LLC_BI__Expected_Duration_Days__c — benchmark time
  LLC_BI__Status__c                 — stall states: 'To Do', 'Under Review', 'On Hold' (confirmed from Org 2 SF-NC-3)
  LLC_BI__Loan__c                   — loan link

NOTE: This object tracks WORKFLOW TASK CHECKLISTS, not document counts.
      No Required_Count / Received_Count fields exist on LLC_BI__Checklist__c.
      Signal is duration overrun or stalled status, not document gaps.

Fires when: overrun_count >= 1  OR  stalled_count >= 1
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import DetectorResult

DETECTOR_ID = "CHECKLIST_BOTTLENECK"
OVERRUN_THRESHOLD = 1   # any overrun fires
STALL_DAYS        = 14  # Incomplete status for 14+ days = stalled

# SF-NC-3 confirmed: actual stall-worthy statuses from Org 2 (May 2026)
# 'To Do' is the primary stall state.
# 'Under Review' and 'On Hold' also indicate no active progress.
# 'In Progress', 'Complete', 'Rejected' are NOT stall states.
STALL_STATUSES = frozenset(["To Do", "Under Review", "On Hold"])

def detect(sf_data: Dict[str, Any], sn_data=None, jira_data=None) -> List[DetectorResult]:
    ncino = sf_data.get("ncino") or sf_data
    metrics = ncino.get("checklist_metrics", {})
    if not metrics:
        return []

    overrun_count = int(metrics.get("overrun_count", 0))
    stalled_count = int(metrics.get("stalled_count", 0))
    total         = int(metrics.get("total_checklists", 0))
    max_overrun   = float(metrics.get("max_overrun_days", 0))

    if overrun_count == 0 and stalled_count == 0:
        return []

    metric_value = float(max_overrun if overrun_count > 0 else stalled_count)
    threshold    = 1.0

    return [DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=metric_value,
        threshold=threshold,
        raw_evidence={
            "total_checklists": total,
            "overrun_count":    overrun_count,
            "stalled_count":    stalled_count,
            "max_overrun_days": max_overrun,
            "avg_overrun_days": metrics.get("avg_overrun_days", 0),
            "primary_object":   "LLC_BI__Checklist__c",
        },
    )]
