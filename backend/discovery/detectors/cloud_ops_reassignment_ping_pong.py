"""
REASSIGNMENT_PING_PONG — MSP-B6 T2 (AT-737), Cloud-Operations pack.

Wraps MSP-B4's oscillation records as findings: routing friction where an
incident bounces between assignment GROUPS/QUEUES, hop-counted (MSP-B6 §1).

Groups and queues ONLY — never individuals (MSP-B4's AC carried through the
pack; MSP-B6 T2 AC2 / AC7). This detector reads only the group/queue fields of
each hop and builds its own clean hop list, so any person field present on the
raw record is never surfaced.

Sources & confidence shape: ITSM (ServiceNow) observed — a routing loop seen in
ITSM alone is SINGLE-SOURCE, capped MEDIUM, and labelled as such.

Fires for an oscillation record when hop_count >= min_hops.

Input (read from ``sn_data['cloud_ops'].oscillation_records``):
  [{signature, incident_id, hop_count, groups_involved: [...],
    hops: [{from_group, to_group}, ...], affected_service}]
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import cloud_ops_finding as fc

try:
    from ..packs.cloud_ops_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "REASSIGNMENT_PING_PONG"

DEFAULT_MIN_HOPS = 3
_THRESHOLD_SECTION = "reassignment_ping_pong"

SIGNAL_METRICS: List[str] = [
    "hop_count",          # metric_value — reassignment hops for the incident
    "groups_involved",    # count of distinct groups/queues in the loop
]


def _thresholds() -> Dict[str, Any]:
    defaults = {"min_hops": DEFAULT_MIN_HOPS}
    if get_detector_thresholds is None:
        return defaults
    return get_detector_thresholds(_THRESHOLD_SECTION, defaults)


def _hop_count(rec: Dict[str, Any]) -> int:
    if rec.get("hop_count") is not None:
        return int(rec.get("hop_count") or 0)
    return len(rec.get("hops") or [])


def _clean_hops(rec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract group->group hops ONLY. Any person field on a hop is dropped."""
    clean: List[Dict[str, str]] = []
    for hop in rec.get("hops") or []:
        if not isinstance(hop, dict):
            continue
        frm = hop.get("from_group") or hop.get("from_queue") or hop.get("from")
        to = hop.get("to_group") or hop.get("to_queue") or hop.get("to")
        if frm or to:
            clean.append({"from_group": str(frm or ""), "to_group": str(to or "")})
    return clean


def _groups_involved(rec: Dict[str, Any], hops: List[Dict[str, str]]) -> List[str]:
    declared = rec.get("groups_involved")
    if isinstance(declared, (list, tuple)) and declared:
        return [str(g) for g in declared]
    groups: List[str] = []
    for hop in hops:
        for g in (hop["from_group"], hop["to_group"]):
            if g and g not in groups:
                groups.append(g)
    return groups


def _build_result(rec: Dict[str, Any], min_hops: int) -> DetectorResult:
    hops = _clean_hops(rec)
    hop_count = _hop_count(rec)
    groups = _groups_involved(rec, hops)
    service = str(rec.get("affected_service") or rec.get("service") or "")
    incident_id = str(rec.get("incident_id") or rec.get("incident") or "")

    artifacts: List[Dict[str, Any]] = [
        {"type": "oscillation_signature", "id": str(rec.get("signature", ""))},
    ]
    if incident_id:
        artifacts.append({"type": "incident", "id": incident_id})
    for g in groups:
        artifacts.append({"type": "group", "id": g})
    if service:
        artifacts.append({"type": "service", "id": service})

    evidence = {
        "hop_count": hop_count,
        "groups_involved": len(groups),
        "groups": groups,
        "hops": hops,
        "affected_service": service,
    }

    systems = ["servicenow"]
    confidence = fc.build_confidence(
        fc.CONFIDENCE_MEDIUM,
        capped=True,
        eligible_for_high=False,
        cap_reason="ITSM-only routing signal — single source.",
    )
    corroboration = fc.build_corroboration(
        fc.STATUS_SINGLE_SOURCE,
        sources=systems,
        label=fc.SINGLE_SOURCE_LABEL,
        window_gated=False,
    )

    contract = fc.build_finding_contract(
        evidence=evidence,
        confidence=confidence,
        corroboration=corroboration,
        source_trace=fc.build_source_trace(systems=systems, artifacts=artifacts),
    )

    return DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(hop_count),
        threshold=float(min_hops),
        raw_evidence={
            "hop_count": hop_count,
            "groups_involved": len(groups),
            "signature": str(rec.get("signature", "")),
            "confidence": confidence["level"],
            "corroborated": False,
            "corroboration_sources": systems,
            "finding_contract": contract,
        },
    )


def _qualifying(block: Dict[str, Any], min_hops: int) -> List[Dict[str, Any]]:
    out = []
    for rec in block.get("oscillation_records") or []:
        if isinstance(rec, dict) and _hop_count(rec) >= min_hops:
            out.append(rec)
    return out


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    min_hops = int(_thresholds().get("min_hops", DEFAULT_MIN_HOPS))
    qualifying = _qualifying(block, min_hops)
    hop_counts = [_hop_count(r) for r in qualifying]
    top_hops = max(hop_counts) if hop_counts else 0
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(top_hops),
        threshold=float(min_hops),
        fired=bool(qualifying),
        raw_evidence={
            "hop_count": top_hops,
            "groups_involved": max(
                (len(_groups_involved(r, _clean_hops(r))) for r in qualifying), default=0
            ),
            "loops_found": len(qualifying),
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    min_hops = int(_thresholds().get("min_hops", DEFAULT_MIN_HOPS))
    return [_build_result(rec, min_hops) for rec in _qualifying(block, min_hops)]
