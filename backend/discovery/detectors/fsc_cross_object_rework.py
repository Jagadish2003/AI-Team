"""FSC_CROSS_OBJECT_REWORK — 2.0-D1 T2, Financial Services Cloud pack.

The same client fact maintained on BOTH the household record and its financial-
account records, so servicing teams correct one thing several times and the record
set can disagree with itself.

COMPOSED FROM: ``cross_system_echo.py``. Its idea — the same work appearing in two
places above a duplication rate, with a volume floor so a tiny sample cannot fire —
transfers directly, and its ``THRESHOLD = 0.15`` rate is carried forward UNCHANGED.
The axis is what changes: that detector echoes across SYSTEMS, this one across
OBJECTS inside one system. ``MIN_VOLUME = 30`` is lowered to ``min_records = 20``
because the population is field-history rows for one object pair rather than an
org-wide case count.

Input: ``sf_data['fsc'].cross_object_rework`` — aggregated by OBJECT PAIR, with
households contributing counts and opaque record-id pointers.

WORDING: this detector states co-occurrence, never causation — "maintenance
concentrates on ..." rather than "duplication causes disagreement". The statement is
built through the pack's causal gate, which validates its own output.

Object pairs and field groups only — never an individual.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import fsc_finding as fc

try:
    from ..packs.financial_services_cloud_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "FSC_CROSS_OBJECT_REWORK"

DEFAULT_DUPLICATE_RATE_THRESHOLD = 0.15
DEFAULT_MIN_RECORDS = 20
DEFAULT_MIN_REWORK_TOUCHES = 3
_THRESHOLD_SECTION = "cross_object_rework"

SIGNAL_METRICS: List[str] = [
    "duplicate_rate",
    "rework_touches",
    "households_with_rework",
    "records_considered",
]


def _thresholds() -> Dict[str, Any]:
    defaults = {
        "duplicate_rate_threshold": DEFAULT_DUPLICATE_RATE_THRESHOLD,
        "min_records": DEFAULT_MIN_RECORDS,
        "min_rework_touches": DEFAULT_MIN_REWORK_TOUCHES,
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
    if _num(row.get("records_considered")) < _num(t.get("min_records"), DEFAULT_MIN_RECORDS):
        return False
    if _num(row.get("rework_touches")) < _num(
        t.get("min_rework_touches"), DEFAULT_MIN_REWORK_TOUCHES
    ):
        return False
    return _num(row.get("duplicate_rate")) >= _num(
        t.get("duplicate_rate_threshold"), DEFAULT_DUPLICATE_RATE_THRESHOLD
    )


def _qualifying(block: Dict[str, Any], t: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in (block.get("cross_object_rework") or []) if _qualifies(r, t)]


def _build_result(row: Dict[str, Any], t: Dict[str, Any]) -> DetectorResult:
    object_pair = str(row.get("object_pair") or "unspecified")
    field_group = str(row.get("field_group") or "unspecified")
    duplicate_rate = _num(row.get("duplicate_rate"))
    rework_touches = int(_num(row.get("rework_touches")))
    with_rework = int(_num(row.get("households_with_rework")))
    considered = int(_num(row.get("households_considered")))
    records_considered = int(_num(row.get("records_considered")))
    threshold = _num(
        t.get("duplicate_rate_threshold"), DEFAULT_DUPLICATE_RATE_THRESHOLD
    )

    artifacts: List[Dict[str, Any]] = [
        {"type": "object_pair", "id": object_pair},
        {"type": "field_group", "id": field_group},
    ]
    for household_id in (row.get("household_ids") or [])[:20]:
        artifacts.append({"type": "household", "id": str(household_id)})

    statement = fc.build_concentration_statement(
        unit_label="duplicated maintenance touches",
        unit=object_pair,
        count=rework_touches,
        measure="Record maintenance effort",
    )

    evidence = {
        "object_pair": object_pair,
        "field_group": field_group,
        "duplicate_rate": duplicate_rate,
        "rework_touches": rework_touches,
        "households_with_rework": with_rework,
        "households_considered": considered,
        "records_considered": records_considered,
        "statement": statement,
        "aggregation_unit": "object_pair",
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
        metric_value=float(duplicate_rate),
        threshold=float(threshold),
        raw_evidence={
            "object_pair": object_pair,
            "field_group": field_group,
            "duplicate_rate": duplicate_rate,
            "rework_touches": rework_touches,
            "households_with_rework": with_rework,
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
    top = max((_num(r.get("duplicate_rate")) for r in qualifying), default=0.0)
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(top),
        threshold=float(
            _num(t.get("duplicate_rate_threshold"), DEFAULT_DUPLICATE_RATE_THRESHOLD)
        ),
        fired=bool(qualifying),
        raw_evidence={
            "duplicate_rate": top,
            "rework_touches": sum(_num(r.get("rework_touches")) for r in qualifying),
            "households_with_rework": sum(
                _num(r.get("households_with_rework")) for r in qualifying
            ),
            "records_considered": sum(
                _num(r.get("records_considered")) for r in qualifying
            ),
            "object_pairs_found": len(qualifying),
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
