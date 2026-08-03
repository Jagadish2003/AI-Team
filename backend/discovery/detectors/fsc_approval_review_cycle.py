"""FSC_APPROVAL_REVIEW_CYCLE — 2.0-D1 T2, Financial Services Cloud pack.

Service processes waiting in approval and compliance-review stages far longer than
they are worked, extending fulfilment for the household.

COMPOSED FROM: ``approval_delay.py`` and ``approval_bottleneck.py``. Both are
reused as ONE detector here rather than two, because on dev they collide — both
modules declare ``DETECTOR_ID = "APPROVAL_BOTTLENECK"``, and UI labels are keyed by
detector id, so registering both under FSC would collapse their labels into one
entry. Their two ideas (dwell time, and pending depth) are therefore carried as two
threshold legs of a single detector: ``dwell_days_threshold`` (from
``approval_delay``'s DELAY/SEVERE pair) and ``min_pending`` (from
``approval_bottleneck``'s PENDING_THRESHOLD).

Input: ``sf_data['fsc'].approval_reviews`` — PENDING ProcessInstances aggregated by
process-definition developer name (the review type).

REGULATED-MARKET POSTURE: this detector observes dwell time and escalates. It never
proposes an approval, a credit decision, or a suitability determination — the pack's
compliance_guardrail for this detector states that explicitly, and the label file
carries it to every screen the finding reaches.

Review types and queues only — the approver is never named.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import fsc_finding as fc

try:
    from ..packs.financial_services_cloud_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "FSC_APPROVAL_REVIEW_CYCLE"

DEFAULT_DWELL_DAYS_THRESHOLD = 5.0
DEFAULT_SEVERE_DWELL_DAYS = 10.0
DEFAULT_MIN_PENDING = 3
_THRESHOLD_SECTION = "approval_review_cycle"

SIGNAL_METRICS: List[str] = [
    "median_dwell_days",
    "max_dwell_days",
    "pending_count",
]


def _thresholds() -> Dict[str, Any]:
    defaults = {
        "dwell_days_threshold": DEFAULT_DWELL_DAYS_THRESHOLD,
        "severe_dwell_days": DEFAULT_SEVERE_DWELL_DAYS,
        "min_pending": DEFAULT_MIN_PENDING,
    }
    if get_detector_thresholds is None:
        return defaults
    return get_detector_thresholds(_THRESHOLD_SECTION, defaults)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _qualifies(row: Dict[str, Any], t: Dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if _num(row.get("pending_count")) < _num(t.get("min_pending"), DEFAULT_MIN_PENDING):
        return False
    return _num(row.get("median_dwell_days")) >= _num(
        t.get("dwell_days_threshold"), DEFAULT_DWELL_DAYS_THRESHOLD
    )


def _qualifying(block: Dict[str, Any], t: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in (block.get("approval_reviews") or []) if _qualifies(r, t)]


def _build_result(row: Dict[str, Any], t: Dict[str, Any]) -> DetectorResult:
    review_type = str(row.get("review_type") or "Unspecified")
    median_dwell = _num(row.get("median_dwell_days"))
    max_dwell = _num(row.get("max_dwell_days"))
    pending = int(_num(row.get("pending_count")))
    severe_threshold = _num(t.get("severe_dwell_days"), DEFAULT_SEVERE_DWELL_DAYS)
    threshold = _num(t.get("dwell_days_threshold"), DEFAULT_DWELL_DAYS_THRESHOLD)
    queues = [str(x) for x in (row.get("queues") or [])]

    artifacts: List[Dict[str, Any]] = [
        {"type": "review_type", "id": review_type},
    ]
    for instance_id in (row.get("process_instance_ids") or [])[:20]:
        artifacts.append({"type": "process_instance", "id": str(instance_id)})
    for target_id in (row.get("target_record_ids") or [])[:20]:
        artifacts.append({"type": "service_process", "id": str(target_id)})
    for queue in queues:
        artifacts.append({"type": "queue", "id": queue})

    statement = fc.build_concentration_statement(
        unit_label="pending reviews",
        unit=review_type,
        count=pending,
        measure="Approval and review waiting time",
    )

    evidence = {
        "review_type": review_type,
        "pending_count": pending,
        "median_dwell_days": median_dwell,
        "max_dwell_days": max_dwell,
        "severe": bool(median_dwell >= severe_threshold),
        "severe_dwell_days_threshold": severe_threshold,
        "queues_involved": len(queues),
        "statement": statement,
        "aggregation_unit": "review_type",
        "decision_posture": "observe_and_escalate_only",
    }

    systems = ["salesforce"]
    confidence = fc.build_confidence(
        fc.CONFIDENCE_MEDIUM,
        capped=True,
        eligible_for_high=False,
        cap_reason="Salesforce FSC observed only — single source.",
    )
    corroboration = fc.build_corroboration(
        fc.STATUS_SINGLE_SOURCE, sources=systems, label=fc.SINGLE_SOURCE_LABEL
    )
    contract = fc.build_finding_contract(
        evidence=evidence,
        confidence=confidence,
        corroboration=corroboration,
        source_trace=fc.build_source_trace(systems=systems, artifacts=artifacts),
    )

    return DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(median_dwell),
        threshold=float(threshold),
        raw_evidence={
            "review_type": review_type,
            "median_dwell_days": median_dwell,
            "max_dwell_days": max_dwell,
            "pending_count": pending,
            "confidence": confidence["level"],
            "corroborated": False,
            "corroboration_sources": systems,
            "finding_contract": contract,
        },
    )


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    block = (sf_data or {}).get("fsc", {}) or {}
    t = _thresholds()
    qualifying = _qualifying(block, t)
    top = max((_num(r.get("median_dwell_days")) for r in qualifying), default=0.0)
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(top),
        threshold=float(_num(t.get("dwell_days_threshold"), DEFAULT_DWELL_DAYS_THRESHOLD)),
        fired=bool(qualifying),
        raw_evidence={
            "median_dwell_days": top,
            "max_dwell_days": max(
                (_num(r.get("max_dwell_days")) for r in qualifying), default=0.0
            ),
            "pending_count": sum(_num(r.get("pending_count")) for r in qualifying),
            "review_types_found": len(qualifying),
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    block = (sf_data or {}).get("fsc", {}) or {}
    t = _thresholds()
    return [_build_result(r, t) for r in _qualifying(block, t)]
