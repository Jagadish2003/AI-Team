"""
QUEUE_AGEING — MSP-B6 T2 (AT-737), Cloud-Operations pack.

Operational queues whose ageing profile departs from THEIR OWN baseline — the
stall detector, calibrated PER QUEUE, never global (MSP-B6 §1 / T2 AC4).

Each queue is judged only against its own baseline (the R16-B2 temporal line):
    departure = (current_avg_age - baseline_avg_age) / baseline_avg_age
A queue fires when its departure >= the configured fraction AND its baseline is
established (>= min_baseline_runs). The detector never mixes queues into a global
mean — a queue that is old in absolute terms but normal against its own baseline
does NOT fire, and a queue that is modest in absolute terms but elevated against
its own baseline DOES.

Sources & confidence shape: ITSM (ServiceNow) observed + the queue's own temporal
baseline (R16-B2) — SINGLE-SOURCE, capped MEDIUM, labelled as such.

Input (read from ``sn_data['cloud_ops'].queues``):
  [{queue, current_avg_age_hours, baseline_avg_age_hours, baseline_runs,
    open_count}]

Queues/services only — never an individual (MSP-B6 AC2/AC7).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import cloud_ops_finding as fc

try:
    from ..packs.cloud_ops_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "QUEUE_AGEING"

DEFAULT_BASELINE_DEPARTURE_PCT = 0.25
DEFAULT_MIN_BASELINE_RUNS = 3
_THRESHOLD_SECTION = "queue_ageing"

SIGNAL_METRICS: List[str] = [
    "departure_pct",          # metric_value — fractional departure from own baseline
    "current_avg_age_hours",
    "baseline_avg_age_hours",
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


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _current_age(q: Dict[str, Any]) -> float:
    return _num(
        q.get("current_avg_age_hours",
              q.get("avg_age_hours", q.get("current_age_hours", 0.0)))
    )


def _baseline_age(q: Dict[str, Any]) -> float:
    """This queue's OWN baseline — no global fallback (AC4)."""
    return _num(
        q.get("baseline_avg_age_hours",
              q.get("baseline_age_hours", q.get("baseline", 0.0)))
    )


def _departure(q: Dict[str, Any]) -> Optional[float]:
    """Fractional departure from the queue's own baseline, or None if unbaselined."""
    baseline = _baseline_age(q)
    if baseline <= 0.0:
        return None
    return round((_current_age(q) - baseline) / baseline, 4)


def _qualifies(q: Dict[str, Any], t: Dict[str, Any]) -> bool:
    if not isinstance(q, dict):
        return False
    if int(q.get("baseline_runs", 0) or 0) < int(t.get("min_baseline_runs", DEFAULT_MIN_BASELINE_RUNS)):
        return False
    departure = _departure(q)
    if departure is None:
        return False
    return departure >= float(t.get("baseline_departure_pct", DEFAULT_BASELINE_DEPARTURE_PCT))


def _build_result(q: Dict[str, Any], t: Dict[str, Any]) -> DetectorResult:
    departure = _departure(q) or 0.0
    current = _current_age(q)
    baseline = _baseline_age(q)
    queue_name = str(q.get("queue") or q.get("queue_name") or q.get("name") or "unassigned")
    open_count = int(q.get("open_count", 0) or 0)
    baseline_runs = int(q.get("baseline_runs", 0) or 0)
    threshold = float(t.get("baseline_departure_pct", DEFAULT_BASELINE_DEPARTURE_PCT))

    artifacts: List[Dict[str, Any]] = [
        {"type": "queue", "id": queue_name},
        {
            "type": "temporal_baseline",
            "id": f"{queue_name}:baseline",
            "baseline_avg_age_hours": baseline,
            "baseline_runs": baseline_runs,
        },
    ]

    evidence = {
        "queue": queue_name,
        "current_avg_age_hours": current,
        "baseline_avg_age_hours": baseline,
        "departure_pct": departure,
        "open_count": open_count,
        "baseline_runs": baseline_runs,
        "baseline_scope": "per_queue",  # AC4: never global
    }

    systems = ["servicenow"]
    confidence = fc.build_confidence(
        fc.CONFIDENCE_MEDIUM,
        capped=True,
        eligible_for_high=False,
        cap_reason="ITSM observed + this queue's own temporal baseline — single source.",
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
        metric_value=float(departure),
        threshold=threshold,
        raw_evidence={
            "departure_pct": departure,
            "current_avg_age_hours": current,
            "baseline_avg_age_hours": baseline,
            "open_count": open_count,
            "queue": queue_name,
            "baseline_scope": "per_queue",
            "confidence": confidence["level"],
            "corroborated": False,
            "corroboration_sources": systems,
            "finding_contract": contract,
        },
    )


def _qualifying(block: Dict[str, Any], t: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [q for q in (block.get("queues") or []) if _qualifies(q, t)]


def evaluate(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
):
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    t = _thresholds()
    qualifying = _qualifying(block, t)
    departures = [(_departure(q) or 0.0) for q in qualifying]
    top_departure = max(departures) if departures else 0.0
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="servicenow",
        metric_value=float(top_departure),
        threshold=float(t.get("baseline_departure_pct", DEFAULT_BASELINE_DEPARTURE_PCT)),
        fired=bool(qualifying),
        raw_evidence={
            "departure_pct": top_departure,
            "current_avg_age_hours": max((_current_age(q) for q in qualifying), default=0.0),
            "baseline_avg_age_hours": max((_baseline_age(q) for q in qualifying), default=0.0),
            "open_count": sum(int(q.get("open_count", 0) or 0) for q in qualifying),
            "queues_found": len(qualifying),
            "baseline_scope": "per_queue",
        },
    )


def detect(
    sf_data: Optional[Dict[str, Any]] = None,
    sn_data: Optional[Dict[str, Any]] = None,
    jira_data: Optional[Dict[str, Any]] = None,
) -> List[DetectorResult]:
    block = (sn_data or {}).get("cloud_ops", {}) or {}
    t = _thresholds()
    return [_build_result(q, t) for q in _qualifying(block, t)]
