"""
RECURRING_RESOLUTION_LOOP — MSP-B6 T2 (AT-737), Cloud-Operations pack.

Wraps MSP-B4's RecurrenceRecord as a finding: the same incident kind resolved
the same way, N times, with the effort measured (count x median time-to-resolve).

Sources & confidence shape (MSP-B6 §1):
  * ITSM (ServiceNow) observed — a recurrence seen only in ITSM is SINGLE-SOURCE,
    capped MEDIUM, and labelled as such (nothing dropped).
  * Joined to a recurring ``event_signature`` via MSP-B7's windows → CORROBORATED
    (ServiceNow + events), window-gated, and HIGH-eligible.

So two otherwise-identical recurrences rank differently: the event-corroborated
one carries higher confidence than the ITSM-only one, and BOTH are emitted
(MSP-B6 T2 AC1). Confidence/corroboration/evidence/source-trace — the four-part
contract — are populated here (AC3); the ops-impact scorer (T4) reads them.

Input (read from ``sn_data['cloud_ops']`` — the ITSM signal block):
  recurrence_records: [{
      signature, incident_kind, resolution, count, median_ttr_minutes,
      assignment_group | queue,            # GROUP/QUEUE only, never a person
      affected_services: [...],
      close_code, event_signature (opt),   # link to the B0/B7 event stream
      incident_ids: [...],                 # source-trace artifacts
  }, ...]
  event_signatures: [{signature, recurring|event_count, window_overlap, ...}]

No detector references an individual (MSP-B6 AC2/AC7) — only group/queue/service
fields are read.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import cloud_ops_finding as fc

try:
    from ..packs.cloud_ops_config import get_detector_thresholds
except Exception:  # pragma: no cover - config is optional at import time
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "RECURRING_RESOLUTION_LOOP"

# Documented defaults; the external pack config (cloud_ops_pack_config.json) wins.
DEFAULT_MIN_OCCURRENCES = 3
DEFAULT_WINDOW_DAYS = 30
_THRESHOLD_SECTION = "recurring_resolution_loop"

SIGNAL_METRICS: List[str] = [
    "recurrence_count",      # metric_value — occurrences of the same resolution loop
    "effort_score",          # count x median TTR (minutes) — the effort measure
    "median_ttr_minutes",
    "corroborated_count",    # how many emitted loops were event-corroborated
]


def _thresholds() -> Dict[str, Any]:
    defaults = {"min_occurrences": DEFAULT_MIN_OCCURRENCES, "window_days": DEFAULT_WINDOW_DAYS}
    if get_detector_thresholds is None:
        return defaults
    return get_detector_thresholds(_THRESHOLD_SECTION, defaults)


def _group_of(rec: Dict[str, Any]) -> str:
    """Return the owning group/queue (never an individual)."""
    return str(rec.get("assignment_group") or rec.get("queue") or rec.get("group") or "unassigned")


def _recurring_event_index(block: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Index event signatures that are recurring AND window-overlapping (B7 gate)."""
    index: Dict[str, Dict[str, Any]] = {}
    for ev in block.get("event_signatures") or []:
        if not isinstance(ev, dict):
            continue
        sig = ev.get("signature")
        if not sig:
            continue
        window_ok = bool(ev.get("window_overlap", ev.get("window_gated", False)))
        recurring = bool(ev.get("recurring", int(ev.get("event_count", 0) or 0) > 1))
        if window_ok and recurring:
            index[str(sig)] = ev
    return index


def _runbook_match_for(rec: Dict[str, Any], block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The B5 runbook match for this recurrence, if any.

    Prefers a per-record ``runbook_match``; falls back to the run-level
    ``runbook_matching.matches`` keyed by the recurrence signature (both shapes
    MSP-B5 may supply). Returns None when B5 provided no match for this record.
    """
    direct = rec.get("runbook_match")
    if isinstance(direct, dict) and direct:
        return direct
    rb = block.get("runbook_matching")
    if isinstance(rb, dict):
        matches = rb.get("matches")
        if isinstance(matches, dict):
            hit = matches.get(str(rec.get("signature", "")))
            if isinstance(hit, dict) and hit:
                return hit
    return None


def _runbook_available_for(rec: Dict[str, Any], block: Dict[str, Any]) -> bool:
    """Per-recurrence B5 availability, falling back to the run-level flag."""
    if "runbook_matching_available" in rec:
        return bool(rec.get("runbook_matching_available"))
    return fc.runbook_matching_available(block)


def _build_result(
    rec: Dict[str, Any],
    event_index: Dict[str, Dict[str, Any]],
    threshold: int,
    *,
    b5_available: bool = False,
    runbook_match: Optional[Dict[str, Any]] = None,
) -> DetectorResult:
    count = int(rec.get("count", 0) or 0)
    median_ttr = float(rec.get("median_ttr_minutes", 0.0) or 0.0)
    effort = round(count * median_ttr, 2)
    group = _group_of(rec)
    services = [str(s) for s in (rec.get("affected_services") or [])]
    incident_ids = [str(i) for i in (rec.get("incident_ids") or [])]

    ev_sig = rec.get("event_signature")
    matched_event = event_index.get(str(ev_sig)) if ev_sig else None

    artifacts: List[Dict[str, Any]] = [
        {"type": "recurrence_signature", "id": str(rec.get("signature", "")), "group": group},
    ]
    for inc in incident_ids:
        artifacts.append({"type": "incident", "id": inc})
    for svc in services:
        artifacts.append({"type": "service", "id": svc})

    # MSP-B6 T6 (AC2): the composite "documented-repeated-manual" leg. With MSP-B5
    # (runbook matching) present and a runbook matched, this is the full composite;
    # with B5 absent it degrades to repeated-manual only, carrying the explicit
    # "runbook match unavailable" label — never silently narrower.
    runbook_leg = fc.build_runbook_leg(runbook_match=runbook_match, b5_available=b5_available)

    evidence = {
        "incident_kind": str(rec.get("incident_kind", "")),
        "resolution": str(rec.get("resolution", "")),
        "recurrence_count": count,
        "median_ttr_minutes": median_ttr,
        "effort_score": effort,
        "close_code": str(rec.get("close_code", "")),
        "group": group,
        "affected_services": services,
        # The composite/degradation leg travels on the evidence so the four-part
        # contract stays complete either way (documented composite or labelled
        # repeated-manual).
        "composite": runbook_leg,
        "finding_kind": runbook_leg["kind"],
    }

    if matched_event is not None:
        systems = ["servicenow", "events"]
        artifacts.append({"type": "event_signature", "id": str(ev_sig)})
        confidence = fc.build_confidence(
            fc.CONFIDENCE_HIGH,
            capped=False,
            eligible_for_high=True,
            note=(
                "Corroborated by a recurring event signature joined within the "
                "same window (MSP-B7)."
            ),
        )
        corroboration = fc.build_corroboration(
            fc.STATUS_CORROBORATED,
            sources=systems,
            label="Corroborated by recurring event signature (window-gated)",
            window_gated=True,
        )
    else:
        systems = ["servicenow"]
        confidence = fc.build_confidence(
            fc.CONFIDENCE_MEDIUM,
            capped=True,
            eligible_for_high=False,
            cap_reason="ITSM-only recurrence — no corroborating event signature in window.",
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

    raw_evidence = {
        "recurrence_count": count,
        "effort_score": effort,
        "median_ttr_minutes": median_ttr,
        "signature": str(rec.get("signature", "")),
        # Flat mirrors for scorer convenience + the structured four-part contract.
        "confidence": confidence["level"],
        "corroborated": corroboration["status"] == fc.STATUS_CORROBORATED,
        "corroboration_sources": corroboration["sources"],
        # T6 composite/degradation mirrors (visible on the finding, never silent).
        "finding_kind": runbook_leg["kind"],
        "runbook_documented": runbook_leg["documented"],
        "runbook_match_available": bool(b5_available and runbook_match),
        "finding_contract": contract,
    }

    return DetectorResult(
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(count),
        threshold=float(threshold),
        raw_evidence=raw_evidence,
    )


def _qualifying(block: Dict[str, Any], threshold: int) -> List[Dict[str, Any]]:
    return [
        rec for rec in (block.get("recurrence_records") or [])
        if isinstance(rec, dict) and int(rec.get("count", 0) or 0) >= threshold
    ]


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    """Aggregate evaluation for temporal capture (fired if any loop qualifies)."""
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    threshold = int(_thresholds().get("min_occurrences", DEFAULT_MIN_OCCURRENCES))
    event_index = _recurring_event_index(block)
    qualifying = _qualifying(block, threshold)

    counts = [int(r.get("count", 0) or 0) for r in qualifying]
    top_count = max(counts) if counts else 0
    corroborated = sum(
        1 for r in qualifying if r.get("event_signature") and str(r["event_signature"]) in event_index
    )
    top_median = max((float(r.get("median_ttr_minutes", 0.0) or 0.0) for r in qualifying), default=0.0)

    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(top_count),
        threshold=float(threshold),
        fired=bool(qualifying),
        raw_evidence={
            "recurrence_count": top_count,
            "effort_score": round(top_count * top_median, 2),
            "median_ttr_minutes": top_median,
            "corroborated_count": corroborated,
            "loops_found": len(qualifying),
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    """Emit one finding per qualifying recurrence loop (corroborated + ITSM-only)."""
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    threshold = int(_thresholds().get("min_occurrences", DEFAULT_MIN_OCCURRENCES))
    event_index = _recurring_event_index(block)
    return [
        _build_result(
            rec, event_index, threshold,
            b5_available=_runbook_available_for(rec, block),
            runbook_match=_runbook_match_for(rec, block),
        )
        for rec in _qualifying(block, threshold)
    ]
