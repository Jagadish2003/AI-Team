"""FSC_SERVICING_REQUEST_RECURRENCE — 2.0-D1 T2, Financial Services Cloud pack.

The same servicing request recurring on a household's financial accounts, so
service teams re-handle work an unresolved cause keeps regenerating.

COMPOSED FROM: ``repetition.py``. That detector's shape — a repeated unit of work
crossing a volume floor, with a breadth qualifier so a single noisy record cannot
fire it — transfers directly. What does NOT transfer is its unit (Flow elements
org-wide) or its numbers (``MIN_VOLUME=50``, ``ELEMENT_THRESHOLD=15``), which are
org-wide Service Cloud scales. Here the unit is a SERVICE-PROCESS TYPE and the
breadth qualifier is distinct financial accounts.

Input: ``sf_data['fsc'].servicing_requests`` — aggregated by service-process type
by the FSC ingest, which has already applied the AC5 floor.

Thresholds are config-driven and PROVISIONAL (see the pack config's _meta).
Service-process types, queues and households-as-counts only — never an individual.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import DetectorResult, make_detector_evaluation
from ..packs import fsc_finding as fc

try:
    from ..packs.financial_services_cloud_config import get_detector_thresholds
except Exception:  # pragma: no cover
    get_detector_thresholds = None  # type: ignore

DETECTOR_ID = "FSC_SERVICING_REQUEST_RECURRENCE"

DEFAULT_MIN_RECURRENCE_COUNT = 4
DEFAULT_MIN_DISTINCT_FINANCIAL_ACCOUNTS = 2
DEFAULT_WINDOW_DAYS = 90
_THRESHOLD_SECTION = "servicing_request_recurrence"

SIGNAL_METRICS: List[str] = [
    "recurrence_count",
    "distinct_financial_accounts",
    "households_affected",
]


def _thresholds() -> Dict[str, Any]:
    defaults = {
        "min_recurrence_count": DEFAULT_MIN_RECURRENCE_COUNT,
        "min_distinct_financial_accounts": DEFAULT_MIN_DISTINCT_FINANCIAL_ACCOUNTS,
        "window_days": DEFAULT_WINDOW_DAYS,
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
    if _num(row.get("recurrence_count")) < _num(
        t.get("min_recurrence_count"), DEFAULT_MIN_RECURRENCE_COUNT
    ):
        return False
    return _num(row.get("distinct_financial_accounts")) >= _num(
        t.get("min_distinct_financial_accounts"), DEFAULT_MIN_DISTINCT_FINANCIAL_ACCOUNTS
    )


def _qualifying(block: Dict[str, Any], t: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in (block.get("servicing_requests") or []) if _qualifies(r, t)]


def _build_result(row: Dict[str, Any], t: Dict[str, Any]) -> DetectorResult:
    process_type = str(row.get("service_process_type") or "Unspecified")
    count = int(_num(row.get("recurrence_count")))
    accounts = int(_num(row.get("distinct_financial_accounts")))
    households = int(_num(row.get("households_affected")))
    threshold = _num(t.get("min_recurrence_count"), DEFAULT_MIN_RECURRENCE_COUNT)

    artifacts: List[Dict[str, Any]] = [
        {"type": "service_process_type", "id": process_type},
    ]
    for case_id in (row.get("case_ids") or [])[:20]:
        artifacts.append({"type": "case", "id": str(case_id)})
    # Households are opaque record-id POINTERS — never names (AC5 / pack config
    # aggregation.emit_household_names = false).
    for household_id in (row.get("household_ids") or [])[:20]:
        artifacts.append({"type": "household", "id": str(household_id)})
    for queue in (row.get("queues") or []):
        artifacts.append({"type": "queue", "id": str(queue)})

    statement = fc.build_concentration_statement(
        unit_label="repeat servicing requests",
        unit=process_type,
        count=count,
        measure="Servicing request volume",
    )

    evidence = {
        "service_process_type": process_type,
        "recurrence_count": count,
        "distinct_financial_accounts": accounts,
        "households_affected": households,
        "window_days": _num(row.get("window_days"), DEFAULT_WINDOW_DAYS),
        "first_seen": row.get("first_seen"),
        "last_seen": row.get("last_seen"),
        "statement": statement,
        "aggregation_unit": "service_process_type",
    }

    systems = ["salesforce"]
    confidence = fc.build_confidence(
        fc.CONFIDENCE_MEDIUM,
        capped=True,
        eligible_for_high=False,
        cap_reason=(
            "Salesforce FSC observed only — single source. What legitimately "
            "corroborates an FSC servicing finding is an outstanding SME question."
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
        metric_value=float(count),
        threshold=float(threshold),
        raw_evidence={
            "service_process_type": process_type,
            "recurrence_count": count,
            "distinct_financial_accounts": accounts,
            "households_affected": households,
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
    top = max((_num(r.get("recurrence_count")) for r in qualifying), default=0.0)
    return make_detector_evaluation(
        module_name=__name__,
        detector_id=DETECTOR_ID,
        signal_source="salesforce",
        metric_value=float(top),
        threshold=float(_num(t.get("min_recurrence_count"), DEFAULT_MIN_RECURRENCE_COUNT)),
        fired=bool(qualifying),
        raw_evidence={
            "recurrence_count": top,
            "distinct_financial_accounts": max(
                (_num(r.get("distinct_financial_accounts")) for r in qualifying), default=0.0
            ),
            "households_affected": sum(
                _num(r.get("households_affected")) for r in qualifying
            ),
            "service_process_types_found": len(qualifying),
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
