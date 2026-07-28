"""
SECOPS_SLA_DEFERRAL_AGEING — MSP-B12 T2, Security Operations pack.

The compliance-pressure signal a federal MSP answers for every month: open
remediation queues ageing against THEIR OWN historical baseline, reported by queue
and severity band, alongside deferral and exception volumes by justification class.

Consumes ONLY ``sn_data['vulnerability_response']['vulnerable_items']``. Each queue
(assignment group) × severity band is judged against its own baseline, never a
global mean — the baseline is the median time-in-state of that group's HISTORICALLY
RESOLVED items (the R16-B2 "own baseline" principle, derived from the B11 signal
itself). A group fires when its open items' median age departs from that baseline by
at least ``baseline_departure_pct`` AND the baseline is established
(≥ ``min_baseline_runs`` resolved items).

Queues / severity bands / justification classes only — never a host, CVE, or
person. Fails safe: no items, or an unestablished baseline, → no finding for that
group (an unbaselined queue is never guessed at).
"""
from __future__ import annotations

import hashlib
import json
from statistics import median
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..models import DetectorResult, make_detector_evaluation
from ..packs import security_ops_finding as fc
from . import security_ops_common as common

try:
    from ..packs.security_ops_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "SECOPS_SLA_DEFERRAL_AGEING"

DEFAULT_BASELINE_DEPARTURE_PCT = 0.25
DEFAULT_MIN_BASELINE_RUNS = 3
_THRESHOLD_SECTION = "sla_deferral_ageing"

SIGNAL_METRICS: List[str] = [
    "departure_pct",              # metric_value — departure from own baseline
    "current_avg_age_seconds",
    "baseline_avg_age_seconds",
    "open_count",
]


def _thresholds() -> Dict[str, Any]:
    defaults = {
        "baseline_departure_pct": DEFAULT_BASELINE_DEPARTURE_PCT,
        "min_baseline_runs": DEFAULT_MIN_BASELINE_RUNS,
    }
    if get_detector_thresholds is None:
        return defaults
    return get_detector_thresholds(_THRESHOLD_SECTION, defaults)


def _is_resolved(item: Mapping[str, Any]) -> bool:
    if common.parse_dt(item.get("resolved_at") or item.get("closed_at")) is not None:
        return True
    state = (common._text(item.get("state")) or "").lower()
    return state in ("closed", "resolved", "remediated")


def _resolved_age(item: Mapping[str, Any]) -> Optional[float]:
    opened = common.parse_dt(item.get("opened_at") or item.get("first_found"))
    resolved = common.parse_dt(item.get("resolved_at") or item.get("closed_at"))
    if opened is None or resolved is None or resolved < opened:
        return None
    return (resolved - opened).total_seconds()


def _open_age(item: Mapping[str, Any], as_of) -> Optional[float]:
    opened = common.parse_dt(item.get("opened_at") or item.get("first_found"))
    if opened is None or as_of is None or as_of < opened:
        return None
    return (as_of - opened).total_seconds()


def _as_of(items) -> Optional[Any]:
    stamps = [common.parse_dt(common.record_timestamp(i)) for i in items]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def _groups(sn_data: Optional[Mapping[str, Any]], org_id: Optional[str] = None) -> List[Dict[str, Any]]:
    block = common.vr_block(sn_data)
    effective = common.effective_org(block, org_id)
    items = [i for i in common._records(block, "vulnerable_items") if common.in_org(i, effective)]
    if not items:
        return []
    as_of = _as_of(items)

    t = _thresholds()
    departure_floor = float(t.get("baseline_departure_pct", DEFAULT_BASELINE_DEPARTURE_PCT))
    min_runs = int(t.get("min_baseline_runs", DEFAULT_MIN_BASELINE_RUNS) or DEFAULT_MIN_BASELINE_RUNS)

    # queue × severity band → {open_ages, baseline_ages, open_items, deferrals, exceptions}
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in items:
        queue = common._text(item.get("assignment_group")) or "unassigned"
        band = common.severity_band(item.get("severity"))
        bucket = buckets.setdefault((queue, band), {
            "open_ages": [], "baseline_ages": [], "open_items": [],
            "deferrals": {}, "exceptions": {},
        })
        if _is_resolved(item):
            age = _resolved_age(item)
            if age is not None:
                bucket["baseline_ages"].append(age)
        else:
            age = _open_age(item, as_of)
            if age is not None:
                bucket["open_ages"].append(age)
                bucket["open_items"].append(item)
        jc = common._text(item.get("justification_class")) or "unspecified"
        if common._text(item.get("deferral_category")):
            bucket["deferrals"][jc] = bucket["deferrals"].get(jc, 0) + 1
        if common._text(item.get("exception_category")):
            bucket["exceptions"][jc] = bucket["exceptions"].get(jc, 0) + 1

    groups: List[Dict[str, Any]] = []
    for (queue, band) in sorted(buckets):
        bucket = buckets[(queue, band)]
        if len(bucket["baseline_ages"]) < min_runs or not bucket["open_ages"]:
            continue  # fail safe: no established baseline, or nothing open
        baseline = float(median(bucket["baseline_ages"]))
        if baseline <= 0.0:
            continue
        current = float(median(bucket["open_ages"]))
        departure = round((current - baseline) / baseline, 4)
        if departure < departure_floor:
            continue
        groups.append({
            "queue": queue,
            "severity_band": band,
            "current_avg_age_seconds": round(current, 2),
            "baseline_avg_age_seconds": round(baseline, 2),
            "departure_pct": departure,
            "open_count": len(bucket["open_items"]),
            "baseline_sample": len(bucket["baseline_ages"]),
            "deferral_volumes_by_justification": dict(sorted(bucket["deferrals"].items())),
            "exception_volumes_by_justification": dict(sorted(bucket["exceptions"].items())),
            "open_items": bucket["open_items"],
            "org_id": effective,
        })
    groups.sort(key=lambda g: (-g["departure_pct"], g["queue"], g["severity_band"]))
    return groups


def _build_result(g: Dict[str, Any], departure_floor: float) -> DetectorResult:
    material = {"org_id": g["org_id"] or "", "queue": g["queue"], "severity_band": g["severity_band"]}
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]

    deferral_count = sum(g["deferral_volumes_by_justification"].values())
    exception_count = sum(g["exception_volumes_by_justification"].values())
    evidence = {
        "queue": g["queue"],
        "severity_band": g["severity_band"],
        "current_avg_age_seconds": g["current_avg_age_seconds"],
        "baseline_avg_age_seconds": g["baseline_avg_age_seconds"],
        "departure_pct": g["departure_pct"],
        "open_count": g["open_count"],
        "baseline_sample": g["baseline_sample"],
        "baseline_scope": "per_queue_severity",
        "deferral_count": deferral_count,
        "exception_count": exception_count,
        "deferral_volumes_by_justification": g["deferral_volumes_by_justification"],
        "exception_volumes_by_justification": g["exception_volumes_by_justification"],
    }
    artifacts = common.pointer_artifacts(g["open_items"], artifact_type="vulnerable_item")
    systems = [common.SOURCE_SYSTEM]
    confidence = fc.build_confidence(
        "MEDIUM",
        capped=True,
        eligible_for_high=False,
        cap_reason="ServiceNow VR queue observed against its own baseline — single source.",
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
        signal_source=common.SOURCE_SYSTEM,
        metric_value=float(g["departure_pct"]),
        threshold=float(departure_floor),
        raw_evidence={
            "departure_pct": g["departure_pct"],
            "current_avg_age_seconds": g["current_avg_age_seconds"],
            "baseline_avg_age_seconds": g["baseline_avg_age_seconds"],
            "open_count": g["open_count"],
            "queue": g["queue"],
            "severity_band": g["severity_band"],
            "deferral_count": deferral_count,
            "exception_count": exception_count,
            "baseline_scope": "per_queue_severity",
            "confidence": confidence["level"],
            "corroborated": False,
            "corroboration_sources": systems,
            "finding_ref": f"servicenow:sla-ageing:{digest}",
            "finding_contract": contract,
        },
    )


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    groups = _groups(sn_data)
    floor = float(_thresholds().get("baseline_departure_pct", DEFAULT_BASELINE_DEPARTURE_PCT))
    top = max((g["departure_pct"] for g in groups), default=0.0)
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source=common.SOURCE_SYSTEM,
        metric_value=float(top),
        threshold=float(floor),
        fired=bool(groups),
        raw_evidence={
            "departure_pct": top,
            "current_avg_age_seconds": max((g["current_avg_age_seconds"] for g in groups), default=0.0),
            "baseline_avg_age_seconds": max((g["baseline_avg_age_seconds"] for g in groups), default=0.0),
            "open_count": sum(g["open_count"] for g in groups),
            "queues_found": len(groups),
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    floor = float(_thresholds().get("baseline_departure_pct", DEFAULT_BASELINE_DEPARTURE_PCT))
    return [_build_result(g, floor) for g in _groups(sn_data)]
