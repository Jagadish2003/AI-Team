"""
NC-2 — LOAN_ORIGINATION_ROUTING_FRICTION detector (v2)

NC-1 used stage transition count as a proxy (owner/underwriter
handoff field was unconfirmed). NC-2 resolves this:

SME-confirmed (April 2026):
  LLC_BI__Loan__History.CreatedById = stage-level owner assignment.
  Real handoff = OwnerId change between consecutive stages on the same loan.

This detector now fires on EITHER:
  - max_owner_changes >= OWNER_CHANGE_THRESHOLD  (real handoffs — primary signal)
  OR
  - max_stage_transitions >= TRANSITION_THRESHOLD (retained as secondary signal)

Thresholds (NC-2 SME-confirmed):
  OWNER_CHANGE_THRESHOLD:  2  — 2+ owner changes in a loan lifecycle = friction
  TRANSITION_THRESHOLD:    4  — unchanged from NC-1

Backward compatibility:
  origination_metrics still contains avg_stage_transitions and
  max_stage_transitions. Both are read here so NC-1-only orgs
  (where OwnerId is absent) degrade gracefully.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import DetectorResult

DETECTOR_ID              = "LOAN_ORIGINATION_ROUTING_FRICTION"
OWNER_CHANGE_THRESHOLD   = 2    # SME-confirmed NC-2
TRANSITION_THRESHOLD     = 4    # NC-1 retained
SIGNAL_METRICS = {
    "max_owner_changes":     "Highest owner-change count on a single loan; primary friction signal",
    "avg_owner_changes":     "Average owner changes per loan; smoothed friction trend",
    "max_stage_transitions": "Highest stage transition count; secondary friction signal",
    "total_loans":           "Total loans evaluated; normalises friction counts as a rate",
    "high_friction_count":   "Number of loans exceeding both thresholds; severity volume trend",
}


def detect(
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any] = None,
    jira_data: Dict[str, Any] = None,
) -> List[DetectorResult]:
    ncino = sf_data.get("ncino") or sf_data
    metrics = ncino.get("origination_metrics", {})

    if not metrics:
        return []

    max_transitions  = int(metrics.get("max_stage_transitions", 0))
    avg_transitions  = float(metrics.get("avg_stage_transitions", 0.0))
    max_owner_changes = int(metrics.get("max_owner_changes", 0))
    avg_owner_changes = float(metrics.get("avg_owner_changes", 0.0))
    total_loans      = int(metrics.get("total_loans", 0))
    owner_change_source = metrics.get("owner_change_source", "UNKNOWN")
    high_friction    = metrics.get("high_friction_loans", [])

    fires_owner      = max_owner_changes >= OWNER_CHANGE_THRESHOLD
    fires_transition = max_stage_transitions = max_transitions >= TRANSITION_THRESHOLD

    if not (fires_owner or fires_transition):
        return []

    # Use owner changes as primary metric if confirmed source
    if fires_owner and owner_change_source == "LOAN_HISTORY_CREATEDBY":
        metric_value   = float(max_owner_changes)
        threshold_used = float(OWNER_CHANGE_THRESHOLD)
    else:
        metric_value   = float(max_transitions)
        threshold_used = float(TRANSITION_THRESHOLD)

    high_friction_count = len(high_friction)

    return [DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=metric_value,
        threshold=threshold_used,
        raw_evidence={
            "total_loans":            total_loans,
            "avg_stage_transitions":  avg_transitions,
            "max_stage_transitions":  max_transitions,
            "avg_owner_changes":      avg_owner_changes,
            "max_owner_changes":      max_owner_changes,
            "high_friction_count":    high_friction_count,
            "owner_change_source":    owner_change_source,  # NC-2: STAGE_OWNER_CONFIRMED
        },
    )]
