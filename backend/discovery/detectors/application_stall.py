"""
APPLICATION_STALL detector — Sprint 5.2 ENG-STRS-2

Object: IndividualApplication (PSS)
Confirmed fields (PSS metadata May 2026):
  Status (Picklist), AppliedDate, IsSubmitted

Fires when: stalled_count >= 1 AND max_days_stalled >= stall_threshold_days

Threshold: 30 days (Ohio Revised Code 3307 guideline).
SF-STRS-1 to confirm threshold and Status picklist values.

Signal: retirement applications that have been submitted but are not
progressing — stuck in review, returned to member with no response,
or stalled in an incomplete state beyond the processing deadline.
"""
from __future__ import annotations
from typing import Any, Dict, List
from ..models import DetectorResult

DETECTOR_ID = "APPLICATION_STALL"


def detect(
    sf_data: Dict[str, Any],
    sn_data=None,
    jira_data=None,
) -> List[DetectorResult]:
    strs = sf_data.get("strs_benefits") or sf_data
    metrics = strs.get("application_metrics", {})
    if not metrics:
        return []

    stalled_count     = int(metrics.get("stalled_count", 0))
    max_days_stalled  = float(metrics.get("max_days_stalled", 0))
    avg_days_stalled  = float(metrics.get("avg_days_stalled", 0))
    total             = int(metrics.get("total_applications", 0))
    threshold_days    = int(metrics.get("stall_threshold_days", 30))

    if stalled_count == 0:
        return []

    return [DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(max_days_stalled),
        threshold=float(threshold_days),
        raw_evidence={
            "total_applications":   total,
            "stalled_count":        stalled_count,
            "max_days_stalled":     max_days_stalled,
            "avg_days_stalled":     avg_days_stalled,
            "stall_threshold_days": threshold_days,
            "primary_object":       "IndividualApplication",
            "sme_note":             metrics.get("sme_note", ""),
        },
    )]
