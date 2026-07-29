"""FSC_REFERRAL_HANDOFF_FRICTION — 2.0-D1 T2, Financial Services Cloud pack.

Referrals bouncing between banking, lending and wealth teams, extending
time-to-first-contact and leaving relationship-group opportunities unworked.

COMPOSED FROM: ``handoff_friction.py``. Its shape — average reassignments per unit
of work, above a floor, with a worst-case qualifier — transfers directly, and its
``THRESHOLD = 1.5`` average is carried forward UNCHANGED as ``min_avg_hops``. What
does not transfer is ``MIN_CASES = 50``: the unit here is a referral TYPE (a
team-pair route), of which an org has a handful, so a 50-item floor would mean the
detector never fires. That rescaling is engineering judgement, not measurement.

Input: ``sf_data['fsc'].referral_handoffs`` — referrals aggregated by referral type
with hop counts derived from FinServ__Referral__History owner changes.

Teams are QUEUE names only. A user-owned referral contributes its hop count but no
owner, so the finding never names the person who handled it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import fsc_finding as fc

try:
    from ..packs.financial_services_cloud_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "FSC_REFERRAL_HANDOFF_FRICTION"

DEFAULT_MIN_HOPS = 3
DEFAULT_MIN_REFERRALS = 3
DEFAULT_MIN_AVG_HOPS = 1.5
_THRESHOLD_SECTION = "referral_handoff_friction"

SIGNAL_METRICS: List[str] = [
    "avg_hops",
    "max_hops",
    "total_hops",
    "referral_count",
]


def _thresholds() -> Dict[str, Any]:
    defaults = {
        "min_hops": DEFAULT_MIN_HOPS,
        "min_referrals": DEFAULT_MIN_REFERRALS,
        "min_avg_hops": DEFAULT_MIN_AVG_HOPS,
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
    if _num(row.get("referral_count")) < _num(t.get("min_referrals"), DEFAULT_MIN_REFERRALS):
        return False
    if _num(row.get("avg_hops")) < _num(t.get("min_avg_hops"), DEFAULT_MIN_AVG_HOPS):
        return False
    # A route only counts as friction if at least one referral genuinely bounced.
    return _num(row.get("max_hops")) >= _num(t.get("min_hops"), DEFAULT_MIN_HOPS)


def _qualifying(block: Dict[str, Any], t: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in (block.get("referral_handoffs") or []) if _qualifies(r, t)]


def _build_result(row: Dict[str, Any], t: Dict[str, Any]) -> DetectorResult:
    referral_type = str(row.get("referral_type") or "Unspecified")
    avg_hops = _num(row.get("avg_hops"))
    max_hops = int(_num(row.get("max_hops")))
    total_hops = int(_num(row.get("total_hops")))
    referral_count = int(_num(row.get("referral_count")))
    teams = [str(x) for x in (row.get("teams") or [])]
    threshold = _num(t.get("min_avg_hops"), DEFAULT_MIN_AVG_HOPS)

    artifacts: List[Dict[str, Any]] = [
        {"type": "referral_type", "id": referral_type},
    ]
    for referral_id in (row.get("referral_ids") or [])[:20]:
        artifacts.append({"type": "referral", "id": str(referral_id)})
    for team in teams:
        artifacts.append({"type": "team", "id": team})
    for household_id in (row.get("household_ids") or [])[:20]:
        artifacts.append({"type": "household", "id": str(household_id)})

    statement = fc.build_concentration_statement(
        unit_label="referral handoffs",
        unit=referral_type,
        count=total_hops,
        measure="Reassignment volume",
    )

    evidence = {
        "referral_type": referral_type,
        "referral_count": referral_count,
        "total_hops": total_hops,
        "avg_hops": avg_hops,
        "max_hops": max_hops,
        "teams_involved": len(teams),
        "statement": statement,
        "aggregation_unit": "referral_type",
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
        metric_value=float(avg_hops),
        threshold=float(threshold),
        raw_evidence={
            "referral_type": referral_type,
            "avg_hops": avg_hops,
            "max_hops": max_hops,
            "total_hops": total_hops,
            "referral_count": referral_count,
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
    top = max((_num(r.get("avg_hops")) for r in qualifying), default=0.0)
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(top),
        threshold=float(_num(t.get("min_avg_hops"), DEFAULT_MIN_AVG_HOPS)),
        fired=bool(qualifying),
        raw_evidence={
            "avg_hops": top,
            "max_hops": max((_num(r.get("max_hops")) for r in qualifying), default=0.0),
            "total_hops": sum(_num(r.get("total_hops")) for r in qualifying),
            "referral_count": sum(_num(r.get("referral_count")) for r in qualifying),
            "referral_types_found": len(qualifying),
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
