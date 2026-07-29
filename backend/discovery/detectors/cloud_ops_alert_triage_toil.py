"""
ALERT_TRIAGE_TOIL — MSP-B6 T2 (AT-737), Cloud-Operations pack.

High-volume event signatures that reliably produce incidents resolved TRIVIALLY
(short TTR, same close code) — the "a human acknowledges this every day" pattern,
the purest triage-agent candidate (MSP-B6 §1).

Sources & confidence shape: Events + ITSM — TWO sources by construction; the join
is window-gated (MSP-B7). A qualifying signature is therefore CORROBORATED and
HIGH-eligible. The four-part contract (evidence, confidence, corroboration,
source trace) is populated here (T2 AC3).

Fires for an event signature when ALL of:
  * incident_count >= min_daily_volume        (high volume)
  * median_ttr_minutes <= max_resolve_minutes  (resolved trivially / short TTR)
  * distinct_close_codes == 1                  (same close code every time)
  * window_overlap is true                     (B7 window-gated join)

Input (read from ``sn_data['cloud_ops'].event_signatures``):
  [{signature, event_count, incident_count, median_ttr_minutes, close_code,
    distinct_close_codes, window_overlap, assignment_group|queue,
    affected_services: [...], incident_ids: [...]}]

Groups/queues/services only — never an individual (MSP-B6 AC2/AC7).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import cloud_ops_finding as fc

try:
    from ..packs.cloud_ops_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "ALERT_TRIAGE_TOIL"

DEFAULT_MAX_RESOLVE_MINUTES = 30.0
DEFAULT_MIN_DAILY_VOLUME = 5
_THRESHOLD_SECTION = "alert_triage_toil"

SIGNAL_METRICS: List[str] = [
    "incident_volume",       # metric_value — incidents produced by the signature
    "median_ttr_minutes",    # short TTR = trivially resolved
    "distinct_close_codes",  # 1 = same close code every time
    "event_count",
]


def _thresholds() -> Dict[str, Any]:
    defaults = {
        "max_resolve_minutes": DEFAULT_MAX_RESOLVE_MINUTES,
        "min_daily_volume": DEFAULT_MIN_DAILY_VOLUME,
    }
    if get_detector_thresholds is None:
        return defaults
    return get_detector_thresholds(_THRESHOLD_SECTION, defaults)


def _group_of(ev: Dict[str, Any]) -> str:
    return str(ev.get("assignment_group") or ev.get("queue") or ev.get("group") or "unassigned")


def _distinct_close_codes(ev: Dict[str, Any]) -> int:
    if ev.get("distinct_close_codes") is not None:
        return int(ev.get("distinct_close_codes") or 0)
    codes = ev.get("close_codes")
    if isinstance(codes, (list, tuple, set)):
        return len({str(c) for c in codes})
    # A single close_code field implies one distinct code.
    return 1 if ev.get("close_code") else 0


def _qualifies(ev: Dict[str, Any], t: Dict[str, Any]) -> bool:
    if not isinstance(ev, dict):
        return False
    volume = int(ev.get("incident_count", ev.get("event_count", 0)) or 0)
    ttr = float(ev.get("median_ttr_minutes", 0.0) or 0.0)
    window_ok = bool(ev.get("window_overlap", ev.get("window_gated", False)))
    return (
        window_ok
        and volume >= int(t.get("min_daily_volume", DEFAULT_MIN_DAILY_VOLUME))
        and 0.0 < ttr <= float(t.get("max_resolve_minutes", DEFAULT_MAX_RESOLVE_MINUTES))
        and _distinct_close_codes(ev) == 1
    )


def _build_result(ev: Dict[str, Any], t: Dict[str, Any]) -> DetectorResult:
    volume = int(ev.get("incident_count", ev.get("event_count", 0)) or 0)
    ttr = float(ev.get("median_ttr_minutes", 0.0) or 0.0)
    event_count = int(ev.get("event_count", volume) or 0)
    group = _group_of(ev)
    services = [str(s) for s in (ev.get("affected_services") or [])]
    incident_ids = [str(i) for i in (ev.get("incident_ids") or [])]
    close_code = str(ev.get("close_code", "")) or (
        str((ev.get("close_codes") or [""])[0]) if ev.get("close_codes") else ""
    )

    artifacts: List[Dict[str, Any]] = [
        {"type": "event_signature", "id": str(ev.get("signature", "")), "group": group},
    ]
    for inc in incident_ids:
        artifacts.append({"type": "incident", "id": inc})
    for svc in services:
        artifacts.append({"type": "service", "id": svc})

    evidence = {
        "incident_volume": volume,
        "event_count": event_count,
        "median_ttr_minutes": ttr,
        "distinct_close_codes": _distinct_close_codes(ev),
        "close_code": close_code,
        "group": group,
        "affected_services": services,
    }

    systems = ["events", "servicenow"]
    confidence = fc.build_confidence(
        fc.CONFIDENCE_HIGH,
        capped=False,
        eligible_for_high=True,
        note=(
            "Events + ITSM by construction; high-volume signature resolved "
            "trivially with a single close code, joined within window (MSP-B7)."
        ),
    )
    corroboration = fc.build_corroboration(
        fc.STATUS_CORROBORATED,
        sources=systems,
        label="Corroborated by event stream + ITSM (window-gated)",
        window_gated=True,
        # 2.0-B1 (trace graph engine): carry the actual join(s) MSP-B7 recorded
        # for this event signature so the trace can surface join type + window.
        correlation_windows=ev.get("correlation_windows"),
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
        metric_value=float(volume),
        threshold=float(t.get("min_daily_volume", DEFAULT_MIN_DAILY_VOLUME)),
        raw_evidence={
            "incident_volume": volume,
            "median_ttr_minutes": ttr,
            "distinct_close_codes": _distinct_close_codes(ev),
            "event_count": event_count,
            "signature": str(ev.get("signature", "")),
            "confidence": confidence["level"],
            "corroborated": True,
            "corroboration_sources": systems,
            "finding_contract": contract,
        },
    )


def _qualifying(block: Dict[str, Any], t: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [ev for ev in (block.get("event_signatures") or []) if _qualifies(ev, t)]


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    t = _thresholds()
    qualifying = _qualifying(block, t)
    volumes = [int(ev.get("incident_count", ev.get("event_count", 0)) or 0) for ev in qualifying]
    top_volume = max(volumes) if volumes else 0
    top_ttr = min(
        (float(ev.get("median_ttr_minutes", 0.0) or 0.0) for ev in qualifying),
        default=0.0,
    )
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(top_volume),
        threshold=float(t.get("min_daily_volume", DEFAULT_MIN_DAILY_VOLUME)),
        fired=bool(qualifying),
        raw_evidence={
            "incident_volume": top_volume,
            "median_ttr_minutes": top_ttr,
            "distinct_close_codes": 1 if qualifying else 0,
            "event_count": sum(int(ev.get("event_count", 0) or 0) for ev in qualifying),
            "signatures_found": len(qualifying),
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    t = _thresholds()
    return [_build_result(ev, t) for ev in _qualifying(block, t)]
