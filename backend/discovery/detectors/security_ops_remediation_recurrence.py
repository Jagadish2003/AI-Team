"""
SECOPS_REMEDIATION_RECURRENCE — MSP-B12 T2, Security Operations pack.

The patch-loop agent candidate: the SAME vulnerability class, on the SAME CI class,
remediated the SAME way, cycle after cycle. Reuses the recurrence PRINCIPLE from
``ops_recurrence`` (deterministic grouping by an exact structured signature, no
semantic similarity) applied to MSP-B11's Vulnerability-Response signal — whose
per-item ``remediation_signature`` is already the ordered (vulnerability_class,
ci_class, remediation_path) fingerprint (MSP-B11 T4).

Consumes ONLY ``sn_data['vulnerability_response']['vulnerable_items']``. For each
signature group meeting the ``min_cycles`` floor it reports:
  * recurrence_count      — vulnerable items sharing the signature,
  * observed_cycles       — distinct scan cycles the loop spans,
  * median time-in-state  — median opened→resolved seconds (the effort measure).

Groups / vulnerability classes / CI classes only — never a host, CVE, or person
(the aggregation floor is enforced at the pack boundary via the four-part
contract). Fails safe: no VR signal, or no signed items, → no findings.
"""
from __future__ import annotations

import hashlib
import json
from statistics import median
from typing import Any, Dict, List, Mapping, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import security_ops_finding as fc
from . import security_ops_common as common

try:
    from ..packs.security_ops_config import get_detector_thresholds
except Exception:  # pragma: no cover - config optional at import time
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "SECOPS_REMEDIATION_RECURRENCE"

DEFAULT_MIN_CYCLES = 3
_THRESHOLD_SECTION = "remediation_recurrence"

SIGNAL_METRICS: List[str] = [
    "recurrence_count",                 # metric_value — occurrences of the loop
    "observed_cycles",
    "median_time_in_state_seconds",
    "loops_found",
]


def _thresholds() -> Dict[str, Any]:
    defaults = {"min_cycles": DEFAULT_MIN_CYCLES}
    if get_detector_thresholds is None:
        return defaults
    return get_detector_thresholds(_THRESHOLD_SECTION, defaults)


def _components(item: Mapping[str, Any]) -> Dict[str, Any]:
    comp = item.get("remediation_signature_components")
    return dict(comp) if isinstance(comp, Mapping) else {}


def _cycle_marker(item: Mapping[str, Any]) -> Optional[str]:
    """A scan-cycle marker for one item: the date it was (last) seen by a scan."""
    for key in ("last_found", "first_found", "opened_at"):
        dt = common.parse_dt(item.get(key))
        if dt is not None:
            return dt.date().isoformat()
    return None


def _time_in_state_seconds(item: Mapping[str, Any]) -> Optional[float]:
    opened = common.parse_dt(item.get("opened_at") or item.get("first_found"))
    resolved = common.parse_dt(item.get("resolved_at") or item.get("closed_at"))
    if opened is None or resolved is None or resolved < opened:
        return None
    return (resolved - opened).total_seconds()


def _loops(sn_data: Optional[Mapping[str, Any]], org_id: Optional[str] = None) -> List[Dict[str, Any]]:
    block = common.vr_block(sn_data)
    effective = common.effective_org(block, org_id)
    items = [
        item for item in _items(block)
        if common.in_org(item, effective) and common._text(item.get("remediation_signature"))
    ]

    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item.get("remediation_signature")), []).append(item)

    min_cycles = int(_thresholds().get("min_cycles", DEFAULT_MIN_CYCLES) or DEFAULT_MIN_CYCLES)
    loops: List[Dict[str, Any]] = []
    for signature in sorted(groups):
        members = sorted(
            groups[signature],
            key=lambda m: (common.record_timestamp(m) or "", common._text(m.get("sys_id")) or ""),
        )
        if len(members) < min_cycles:
            continue
        comp = _components(members[0])
        ttrs = [t for t in (_time_in_state_seconds(m) for m in members) if t is not None]
        cycles = sorted({c for c in (_cycle_marker(m) for m in members) if c})
        bands: Dict[str, int] = {}
        for m in members:
            band = common.severity_band(m.get("severity"))
            bands[band] = bands.get(band, 0) + 1
        loops.append({
            "signature": signature,
            "vulnerability_class": comp.get("vulnerability_class") or "unclassified",
            "ci_class": comp.get("ci_class") or "unclassified",
            "remediation_path": list(comp.get("remediation_path") or []),
            "recurrence_count": len(members),
            "observed_cycles": len(cycles) or len(members),
            "median_time_in_state_seconds": float(median(ttrs)) if ttrs else None,
            "measured_ttr_count": len(ttrs),
            "severity_bands": bands,
            "severity_band": _dominant_band(bands),
            "org_id": effective,
            "members": members,
        })
    loops.sort(key=lambda l: (-l["recurrence_count"], l["signature"]))
    return loops


def _items(block: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    return common._records(block, "vulnerable_items")


def _dominant_band(bands: Mapping[str, int]) -> str:
    """The most severe band present (ties broken by severity, then count)."""
    present = [b for b in common.SEVERITY_BANDS if bands.get(b)]
    return present[0] if present else "unclassified"


def _build_result(loop: Dict[str, Any], min_cycles: int) -> DetectorResult:
    material = {"org_id": loop["org_id"] or "", "signature": loop["signature"]}
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]

    evidence = {
        "vulnerability_class": loop["vulnerability_class"],
        "ci_class": loop["ci_class"],
        "remediation_path": loop["remediation_path"],
        "remediation_signature": loop["signature"],
        "recurrence_count": loop["recurrence_count"],
        "observed_cycles": loop["observed_cycles"],
        "median_time_in_state_seconds": loop["median_time_in_state_seconds"],
        "measured_time_in_state_count": loop["measured_ttr_count"],
        "severity_band": loop["severity_band"],
        "severity_bands": loop["severity_bands"],
        "record_count": loop["recurrence_count"],
    }
    artifacts = common.pointer_artifacts(loop["members"], artifact_type="vulnerable_item")
    systems = [common.SOURCE_SYSTEM]
    confidence = fc.build_confidence(
        "MEDIUM",
        capped=True,
        eligible_for_high=False,
        cap_reason="Vulnerability-Response signal only — single-source recurrence.",
    )
    corroboration = fc.build_corroboration(
        fc.STATUS_SINGLE_SOURCE,
        sources=systems,
        label=fc.SINGLE_SOURCE_LABEL,
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
        metric_value=float(loop["recurrence_count"]),
        threshold=float(min_cycles),
        raw_evidence={
            "recurrence_count": loop["recurrence_count"],
            "observed_cycles": loop["observed_cycles"],
            "median_time_in_state_seconds": loop["median_time_in_state_seconds"] or 0.0,
            "remediation_signature": loop["signature"],
            "vulnerability_class": loop["vulnerability_class"],
            "ci_class": loop["ci_class"],
            "severity_band": loop["severity_band"],
            "confidence": confidence["level"],
            "corroborated": False,
            "corroboration_sources": systems,
            "finding_ref": f"servicenow:remediation-recurrence:{digest}",
            "finding_contract": contract,
        },
    )


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    loops = _loops(sn_data)
    min_cycles = int(_thresholds().get("min_cycles", DEFAULT_MIN_CYCLES) or DEFAULT_MIN_CYCLES)
    top = max((l["recurrence_count"] for l in loops), default=0)
    top_median = max(
        (l["median_time_in_state_seconds"] or 0.0 for l in loops), default=0.0
    )
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source=common.SOURCE_SYSTEM,
        metric_value=float(top),
        threshold=float(min_cycles),
        fired=bool(loops),
        raw_evidence={
            "recurrence_count": top,
            "observed_cycles": max((l["observed_cycles"] for l in loops), default=0),
            "median_time_in_state_seconds": top_median,
            "loops_found": len(loops),
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    min_cycles = int(_thresholds().get("min_cycles", DEFAULT_MIN_CYCLES) or DEFAULT_MIN_CYCLES)
    return [_build_result(loop, min_cycles) for loop in _loops(sn_data)]
