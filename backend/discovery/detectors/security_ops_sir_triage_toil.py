"""
SECOPS_SIR_TRIAGE_TOIL — MSP-B12 T2, Security Operations pack.

The enrichment/triage agent candidate on the SOC side: a high-volume security-
incident category reliably closed with the SAME classification in a short time —
"a human dispositions this the same way every day". Reuses MSP-B6's alert-triage-toil
pattern (high volume + trivial resolution + single close code), pointed at MSP-B11's
SIR signal.

Consumes ONLY ``sn_data['secops']['security_incidents']``. Groups by
(category, subcategory, close-classification) and fires when ALL of:
  * incident volume ≥ ``min_daily_volume``,
  * exactly ONE distinct close classification (closed the same way every time),
  * median close time ≤ ``max_close_minutes`` (short period).

Categories / classifications / queues only — never a caller, assignee, or free-text
note (B11 already strips notes; the pack boundary re-enforces no individuals). Fails
safe: no SIR signal → no findings.
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

DETECTOR_ID = "SECOPS_SIR_TRIAGE_TOIL"

DEFAULT_MAX_CLOSE_MINUTES = 30.0
DEFAULT_MIN_DAILY_VOLUME = 5
_THRESHOLD_SECTION = "sir_triage_toil"

SIGNAL_METRICS: List[str] = [
    "incident_volume",        # metric_value — incidents in the toil category
    "median_close_minutes",
    "distinct_classifications",
    "category_count",
]


def _thresholds() -> Dict[str, Any]:
    defaults = {
        "max_close_minutes": DEFAULT_MAX_CLOSE_MINUTES,
        "min_daily_volume": DEFAULT_MIN_DAILY_VOLUME,
    }
    if get_detector_thresholds is None:
        return defaults
    return get_detector_thresholds(_THRESHOLD_SECTION, defaults)


def _classification(inc: Mapping[str, Any]) -> str:
    return (
        common._text(inc.get("close_code"))
        or common._text(inc.get("resolution_code"))
        or "unspecified"
    )


def _close_minutes(inc: Mapping[str, Any]) -> Optional[float]:
    opened = common.parse_dt(inc.get("opened_at") or inc.get("created_at"))
    closed = common.parse_dt(inc.get("closed_at") or inc.get("resolved_at"))
    if opened is None or closed is None or closed < opened:
        return None
    return (closed - opened).total_seconds() / 60.0


def _categories(sn_data: Optional[Mapping[str, Any]], org_id: Optional[str] = None) -> List[Dict[str, Any]]:
    block = common.secops_block(sn_data)
    effective = common.effective_org(block, org_id)
    incidents = [i for i in common._records(block, "security_incidents") if common.in_org(i, effective)]

    t = _thresholds()
    min_volume = int(t.get("min_daily_volume", DEFAULT_MIN_DAILY_VOLUME) or DEFAULT_MIN_DAILY_VOLUME)
    max_minutes = float(t.get("max_close_minutes", DEFAULT_MAX_CLOSE_MINUTES) or DEFAULT_MAX_CLOSE_MINUTES)

    # (category, subcategory) → members (only closed incidents count as toil).
    buckets: Dict[Tuple[str, str], List[Mapping[str, Any]]] = {}
    for inc in incidents:
        closed = common.parse_dt(inc.get("closed_at") or inc.get("resolved_at"))
        if closed is None:
            continue  # only resolved incidents are triage toil
        category = common._text(inc.get("category")) or "uncategorized"
        subcategory = common._text(inc.get("subcategory")) or ""
        buckets.setdefault((category, subcategory), []).append(inc)

    categories: List[Dict[str, Any]] = []
    for (category, subcategory) in sorted(buckets):
        members = buckets[(category, subcategory)]
        if len(members) < min_volume:
            continue
        classifications = sorted({_classification(m) for m in members})
        if len(classifications) != 1:
            continue  # closed the SAME way every time
        close_minutes = [m for m in (_close_minutes(x) for x in members) if m is not None]
        if not close_minutes:
            continue
        median_close = float(median(close_minutes))
        if median_close > max_minutes:
            continue
        bands: Dict[str, int] = {}
        for m in members:
            band = common.severity_band(m.get("severity"))
            bands[band] = bands.get(band, 0) + 1
        queues = sorted({common._text(m.get("assignment_group")) or "unassigned" for m in members})
        categories.append({
            "category": category,
            "subcategory": subcategory,
            "classification": classifications[0],
            "incident_volume": len(members),
            "median_close_minutes": round(median_close, 2),
            "severity_bands": bands,
            "severity_band": _dominant_band(bands),
            "queues": queues,
            "members": members,
            "org_id": effective,
        })
    categories.sort(key=lambda c: (-c["incident_volume"], c["category"], c["subcategory"]))
    return categories


def _dominant_band(bands: Mapping[str, int]) -> str:
    present = [b for b in common.SEVERITY_BANDS if bands.get(b)]
    return present[0] if present else "unclassified"


def _build_result(c: Dict[str, Any], min_volume: int) -> DetectorResult:
    material = {
        "org_id": c["org_id"] or "", "category": c["category"],
        "subcategory": c["subcategory"], "classification": c["classification"],
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]

    evidence = {
        "category": c["category"],
        "subcategory": c["subcategory"],
        "classification": c["classification"],
        "incident_volume": c["incident_volume"],
        "median_close_minutes": c["median_close_minutes"],
        "distinct_classifications": 1,
        "severity_band": c["severity_band"],
        "severity_bands": c["severity_bands"],
        "queues": c["queues"],
        "record_count": c["incident_volume"],
    }
    artifacts = common.pointer_artifacts(c["members"], artifact_type="security_incident")
    systems = [common.SOURCE_SYSTEM]
    confidence = fc.build_confidence(
        "MEDIUM",
        capped=True,
        eligible_for_high=False,
        cap_reason="ServiceNow SIR signal only — single-source triage-toil category.",
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
        metric_value=float(c["incident_volume"]),
        threshold=float(min_volume),
        raw_evidence={
            "incident_volume": c["incident_volume"],
            "median_close_minutes": c["median_close_minutes"],
            "distinct_classifications": 1,
            "category": c["category"],
            "classification": c["classification"],
            "severity_band": c["severity_band"],
            "confidence": confidence["level"],
            "corroborated": False,
            "corroboration_sources": systems,
            "finding_ref": f"servicenow:sir-triage-toil:{digest}",
            "finding_contract": contract,
        },
    )


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    categories = _categories(sn_data)
    min_volume = int(_thresholds().get("min_daily_volume", DEFAULT_MIN_DAILY_VOLUME) or DEFAULT_MIN_DAILY_VOLUME)
    top = max((c["incident_volume"] for c in categories), default=0)
    best_close = min((c["median_close_minutes"] for c in categories), default=0.0)
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source=common.SOURCE_SYSTEM,
        metric_value=float(top),
        threshold=float(min_volume),
        fired=bool(categories),
        raw_evidence={
            "incident_volume": top,
            "median_close_minutes": best_close,
            "distinct_classifications": 1 if categories else 0,
            "category_count": len(categories),
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    min_volume = int(_thresholds().get("min_daily_volume", DEFAULT_MIN_DAILY_VOLUME) or DEFAULT_MIN_DAILY_VOLUME)
    return [_build_result(c, min_volume) for c in _categories(sn_data)]
