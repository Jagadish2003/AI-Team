"""FSC_SERVICE_QUEUE_AGEING — 2.0-D1 T2, Financial Services Cloud pack.

Service processes ageing past their own baseline in specific team-owned queues, so
overdue servicing work concentrates in a few queues rather than spreading evenly.

THE ONE GENUINELY NEW DETECTOR IN THIS PACK. The story predicted this: queue ageing
has no equivalent among the Salesforce-facing detectors, and it does not. What it is
NOT, however, is a from-scratch design — the per-queue-own-baseline SHAPE is lifted
from ``cloud_ops_queue_ageing.py``:

    departure = (current_avg_age - baseline_avg_age) / baseline_avg_age

judged PER QUEUE against that queue's own baseline, never against a global mean. A
queue that is old in absolute terms but normal against its own baseline does not
fire; a queue that is modest in absolute terms but elevated against its own baseline
does. Only the domain and the units (days rather than hours, service processes rather
than incidents) differ, so this is a fifth instance of an existing idea rather than
new detector machinery.

Baselines come from the platform's existing temporal/baseline line, not from a query
this connector runs. A queue with no established baseline does NOT fire — an
unbaselined queue has nothing to be elevated against, and inventing a global
fallback is exactly the mistake the per-queue rule exists to prevent.

Input: ``sf_data['fsc'].service_queues``. Queues only — never an individual.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import fsc_finding as fc

try:
    from ..packs.financial_services_cloud_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "FSC_SERVICE_QUEUE_AGEING"

DEFAULT_BASELINE_DEPARTURE_PCT = 0.25
DEFAULT_MIN_BASELINE_RUNS = 3
DEFAULT_MIN_OPEN_COUNT = 5
_THRESHOLD_SECTION = "service_queue_ageing"

SIGNAL_METRICS: List[str] = [
    "departure_pct",
    "current_avg_age_days",
    "baseline_avg_age_days",
    "open_count",
]


def _thresholds() -> Dict[str, Any]:
    defaults = {
        "baseline_departure_pct": DEFAULT_BASELINE_DEPARTURE_PCT,
        "min_baseline_runs": DEFAULT_MIN_BASELINE_RUNS,
        "min_open_count": DEFAULT_MIN_OPEN_COUNT,
    }
    if get_detector_thresholds is None:
        return defaults
    return get_detector_thresholds(_THRESHOLD_SECTION, defaults)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _departure(row: Dict[str, Any]) -> Optional[float]:
    """Fractional departure from this queue's OWN baseline, or None if unbaselined."""
    baseline = _num(row.get("baseline_avg_age_days"))
    if baseline <= 0.0:
        return None
    return round((_num(row.get("current_avg_age_days")) - baseline) / baseline, 4)


def _qualifies(row: Dict[str, Any], t: Dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if _num(row.get("baseline_runs")) < _num(
        t.get("min_baseline_runs"), DEFAULT_MIN_BASELINE_RUNS
    ):
        return False
    if _num(row.get("open_count")) < _num(t.get("min_open_count"), DEFAULT_MIN_OPEN_COUNT):
        return False
    departure = _departure(row)
    if departure is None:
        return False
    return departure >= _num(
        t.get("baseline_departure_pct"), DEFAULT_BASELINE_DEPARTURE_PCT
    )


def _qualifying(block: Dict[str, Any], t: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in (block.get("service_queues") or []) if _qualifies(r, t)]


def _build_result(row: Dict[str, Any], t: Dict[str, Any]) -> DetectorResult:
    queue = str(row.get("queue") or "unassigned")
    departure = _departure(row) or 0.0
    current = _num(row.get("current_avg_age_days"))
    baseline = _num(row.get("baseline_avg_age_days"))
    baseline_runs = int(_num(row.get("baseline_runs")))
    open_count = int(_num(row.get("open_count")))
    threshold = _num(t.get("baseline_departure_pct"), DEFAULT_BASELINE_DEPARTURE_PCT)
    process_types = [str(x) for x in (row.get("service_process_types") or [])]

    artifacts: List[Dict[str, Any]] = [
        {"type": "queue", "id": queue},
        {
            "type": "temporal_baseline",
            "id": f"{queue}:baseline",
            "baseline_avg_age_days": baseline,
            "baseline_runs": baseline_runs,
        },
    ]
    for case_id in (row.get("case_ids") or [])[:20]:
        artifacts.append({"type": "service_process", "id": str(case_id)})
    for process_type in process_types:
        artifacts.append({"type": "service_process_type", "id": process_type})

    statement = fc.build_concentration_statement(
        unit_label="ageing service processes",
        unit=queue,
        count=open_count,
        measure="Overdue servicing work",
    )

    evidence = {
        "queue": queue,
        "current_avg_age_days": current,
        "baseline_avg_age_days": baseline,
        "departure_pct": departure,
        "open_count": open_count,
        "baseline_runs": baseline_runs,
        "baseline_scope": "per_queue",
        "service_process_types": len(process_types),
        "statement": statement,
        "aggregation_unit": "queue",
    }

    systems = ["salesforce"]
    confidence = fc.build_confidence(
        fc.CONFIDENCE_MEDIUM,
        capped=True,
        eligible_for_high=False,
        cap_reason=(
            "Salesforce FSC observed plus this queue's own temporal baseline — "
            "single source."
        ),
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
        metric_value=float(departure),
        threshold=float(threshold),
        raw_evidence={
            "queue": queue,
            "departure_pct": departure,
            "current_avg_age_days": current,
            "baseline_avg_age_days": baseline,
            "open_count": open_count,
            "baseline_scope": "per_queue",
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
    departures = [(_departure(r) or 0.0) for r in qualifying]
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(max(departures) if departures else 0.0),
        threshold=float(
            _num(t.get("baseline_departure_pct"), DEFAULT_BASELINE_DEPARTURE_PCT)
        ),
        fired=bool(qualifying),
        raw_evidence={
            "departure_pct": max(departures) if departures else 0.0,
            "current_avg_age_days": max(
                (_num(r.get("current_avg_age_days")) for r in qualifying), default=0.0
            ),
            "baseline_avg_age_days": max(
                (_num(r.get("baseline_avg_age_days")) for r in qualifying), default=0.0
            ),
            "open_count": sum(_num(r.get("open_count")) for r in qualifying),
            "queues_found": len(qualifying),
            "baseline_scope": "per_queue",
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
