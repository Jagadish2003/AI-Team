"""OPS_RUNBOOK_DOCUMENTATION_GAP — MSP-B5 inverse Cloud Operations finding.

MSP-B5 emits a documentation-gap record only after both matching paths complete,
no active lifecycle match remains, and the recurrence crosses its high-frequency
floor. This adapter gives that already-conservative record the Cloud Operations
pack's four-part finding contract.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import cloud_ops_finding as fc

DETECTOR_ID = "OPS_RUNBOOK_DOCUMENTATION_GAP"

SIGNAL_METRICS: List[str] = [
    "recurrence_count",
    "recurrence_floor",
    "documentation_gap_count",
]


def _items(block: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [
        dict(item)
        for item in (block.get("documentation_gaps") or ())
        if isinstance(item, Mapping)
        and int(item.get("recurrence_count", 0) or 0)
        >= int(item.get("recurrence_floor", 1) or 1)
    ]


def _build_result(item: Mapping[str, Any]) -> DetectorResult:
    count = int(item.get("recurrence_count", 0) or 0)
    floor = int(item.get("recurrence_floor", 1) or 1)
    recurrence_id = str(item.get("recurrence_id") or "")
    incident_evidence = [
        pointer
        for pointer in (item.get("incident_evidence") or ())
        if isinstance(pointer, Mapping)
    ]
    artifacts: List[Dict[str, Any]] = [
        {"type": "recurrence_signature", "id": recurrence_id},
    ]
    artifacts.extend(
        {
            "type": "incident",
            "id": str(pointer.get("source_artifact") or ""),
        }
        for pointer in incident_evidence
        if pointer.get("source_artifact")
    )

    evidence = {
        "loop_name": str(item.get("loop_name") or "repeated resolution loop"),
        "recurrence_count": count,
        "recurrence_floor": floor,
        "evaluated_window": dict(item.get("evaluated_window") or {}),
        "grouped_signatures": dict(item.get("grouped_signatures") or {}),
        "search_outcome": dict(item.get("search_outcome") or {}),
        "confidence_cap": float(item.get("confidence_cap", 0.65) or 0.65),
    }
    confidence = fc.build_confidence(
        fc.CONFIDENCE_MEDIUM,
        capped=True,
        eligible_for_high=False,
        cap_reason=(
            "Documentation absence is inferred from a completed search; "
            "confidence is capped by MSP-B5."
        ),
    )
    corroboration = fc.build_corroboration(
        fc.STATUS_SINGLE_SOURCE,
        sources=["servicenow"],
        label=(
            "Repeated ITSM work; explicit citation and runbook-library searches "
            "completed without an active match"
        ),
        window_gated=False,
    )
    contract = fc.build_finding_contract(
        evidence=evidence,
        confidence=confidence,
        corroboration=corroboration,
        source_trace=fc.build_source_trace(
            systems=["servicenow", "runbook_library"],
            artifacts=artifacts,
        ),
    )
    return DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(count),
        threshold=float(floor),
        raw_evidence={
            "recurrence_count": count,
            "recurrence_floor": floor,
            "signature": recurrence_id,
            "confidence": confidence["level"],
            "corroborated": False,
            "corroboration_sources": ["servicenow"],
            "documentation_gap": dict(item),
            "finding_contract": contract,
        },
        provenance_type="inferred",
    )


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    qualifying = _items(block)
    top_count = max(
        (int(item.get("recurrence_count", 0) or 0) for item in qualifying),
        default=0,
    )
    floor = min(
        (int(item.get("recurrence_floor", 1) or 1) for item in qualifying),
        default=1,
    )
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(top_count),
        threshold=float(floor),
        fired=bool(qualifying),
        raw_evidence={
            "recurrence_count": top_count,
            "recurrence_floor": floor,
            "documentation_gap_count": len(qualifying),
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    return [_build_result(item) for item in _items(block)]


__all__ = ["DETECTOR_ID", "SIGNAL_METRICS", "detect", "evaluate"]
