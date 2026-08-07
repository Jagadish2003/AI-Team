"""2.0-A2 T6 - outcome surfaces assembled from stored artifacts.

Outcome reads are packaging, not measurement. The numbers here come from the
stored movement records produced by T3/T4/T5, and the route/report layers render
those stored facts with their run ids and caveats attached.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .opportunity_lifecycle import get_lifecycle, list_lifecycles
from .opportunity_lifecycle_states import MEASURABLE_STATES
from .opportunity_movement import get_movement_history, get_movements_for_run
from .opportunity_movement_record import VERDICT_COMPARABLE

OUTCOME_SURFACE_SCHEMA_VERSION = "1.0.0"

OUTCOME_EMPTY_NO_MOVEMENT = "no_stored_movement"

FORBIDDEN_OUTCOME_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "agent_iq_claimed_result",
        re.compile(
            r"\bagentiq\s+(delivered|drove|caused|generated|saved|reduced|improved)\b",
            re.IGNORECASE,
        ),
    ),
    ("causal_claim", re.compile(r"\b(caused|causing|because of|due to)\b", re.IGNORECASE)),
    ("credit_claim", re.compile(r"\b(credited|attributed to|took credit)\b", re.IGNORECASE)),
    ("financial_claim", re.compile(r"\b(savings|saved|roi)\b", re.IGNORECASE)),
    ("guarantee_claim", re.compile(r"\b(guaranteed|guarantees)\b", re.IGNORECASE)),
)


def _safe_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _round(value: Any) -> Optional[float]:
    number = _safe_float(value)
    if number is None:
        return None
    return int(number) if float(number).is_integer() else round(number, 6)


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list_of_dicts(value: Any) -> List[Dict[str, Any]]:
    return [dict(item) for item in (value or []) if isinstance(item, Mapping)]


def _primary_movement(record: Mapping[str, Any]) -> Dict[str, Any]:
    movements = _list_of_dicts(record.get("movements"))
    return (
        next((m for m in movements if m.get("role") == "movement"), None)
        or next(iter(movements), None)
        or {}
    )


def _confounder_summary(record: Mapping[str, Any]) -> Dict[str, Any]:
    summary = _dict(record.get("confounderSummary"))
    if summary:
        return {
            "count": int(summary.get("count") or 0),
            "materialCount": int(summary.get("materialCount") or 0),
            "advisoryCount": int(summary.get("advisoryCount") or 0),
            "byType": dict(summary.get("byType") or {}),
            "types": list(summary.get("types") or []),
        }
    confounders = _list_of_dicts(record.get("confounders"))
    by_type: Dict[str, int] = {}
    material = 0
    for caveat in confounders:
        caveat_type = str(caveat.get("type") or "unknown")
        by_type[caveat_type] = by_type.get(caveat_type, 0) + 1
        if caveat.get("severity") == "material":
            material += 1
    return {
        "count": len(confounders),
        "materialCount": material,
        "advisoryCount": max(0, len(confounders) - material),
        "byType": by_type,
        "types": sorted(by_type),
    }


def _measurement_has_caveat(record: Mapping[str, Any]) -> bool:
    comparability = _dict(record.get("comparability"))
    if comparability.get("verdict") and comparability.get("verdict") != VERDICT_COMPARABLE:
        return True
    return _confounder_summary(record)["count"] > 0


def _run_pair(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "opportunityIdentity": record.get("opportunityIdentity"),
        "baselineRunId": record.get("baselineRunId"),
        "currentRunId": record.get("currentRunId"),
    }


def _evidence_for_movement(
    record: Mapping[str, Any],
    movement: Mapping[str, Any],
) -> Dict[str, Any]:
    baseline = _dict(record.get("baseline"))
    current = _dict(record.get("current"))
    return {
        "opportunityIdentity": record.get("opportunityIdentity"),
        "signalName": movement.get("signalName"),
        "baselineRunId": record.get("baselineRunId"),
        "currentRunId": record.get("currentRunId"),
        "postActionRunIds": list(record.get("postActionRunIds") or []),
        "baseline": {
            "runId": baseline.get("runId") or record.get("baselineRunId"),
            "window": _dict(baseline.get("window")),
            "value": movement.get("baselineValue"),
        },
        "current": {
            "runId": current.get("runId") or record.get("currentRunId"),
            "window": _dict(current.get("window")),
            "value": movement.get("currentValue"),
        },
        "comparability": _dict(record.get("comparability")),
        "confounderSummary": _confounder_summary(record),
        "projectionValidation": _dict(record.get("projectionValidation")),
    }


def build_movement_number_refs(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    current_run_id = str(record.get("currentRunId") or "unknown")
    identity = str(record.get("opportunityIdentity") or "unknown")
    for movement in _list_of_dicts(record.get("movements")):
        signal = str(movement.get("signalName") or "signal")
        for field, label, unit in (
            ("baselineValue", "Baseline value", None),
            ("currentValue", "Current value", None),
            ("delta", "Movement against baseline", None),
            ("deltaPct", "Movement against baseline percent", "percent"),
        ):
            if movement.get(field) is None:
                continue
            refs.append(
                {
                    "id": f"{identity}:{current_run_id}:{signal}:{field}",
                    "label": label,
                    "value": _round(movement.get(field)),
                    "unit": unit,
                    "signalName": signal,
                    "field": field,
                    "evidence": _evidence_for_movement(record, movement),
                }
            )
    return refs


def _measurement_summary(record: Mapping[str, Any]) -> Dict[str, Any]:
    primary = _primary_movement(record)
    return {
        "opportunityIdentity": record.get("opportunityIdentity"),
        "detectorId": record.get("detectorId"),
        "actionDate": record.get("actionDate"),
        "measuredAt": record.get("measuredAt"),
        "baselineRunId": record.get("baselineRunId"),
        "currentRunId": record.get("currentRunId"),
        "primaryMovement": dict(primary),
        "movements": _list_of_dicts(record.get("movements")),
        "comparability": _dict(record.get("comparability")),
        "projectionValidation": _dict(record.get("projectionValidation")),
        "confounderSummary": _confounder_summary(record),
        "confounders": _list_of_dicts(record.get("confounders")),
        "numberRefs": build_movement_number_refs(record),
    }


def _count_by(records: Iterable[Mapping[str, Any]], getter) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for record in records:
        key = str(getter(record) or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _aggregate_number_ref(
    *,
    ref_id: str,
    label: str,
    value: int,
    records: Sequence[Mapping[str, Any]],
    lifecycles: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    run_pairs = [_run_pair(record) for record in records]
    run_ids = sorted(
        {
            str(run_id)
            for pair in run_pairs
            for run_id in (pair.get("baselineRunId"), pair.get("currentRunId"))
            if run_id
        }
    )
    lifecycle_refs = [
        {
            "opportunityIdentity": lifecycle.get("opportunityIdentity"),
            "state": lifecycle.get("state"),
            "actionDate": lifecycle.get("actionDate"),
            "lastRunId": lifecycle.get("lastRunId"),
        }
        for lifecycle in lifecycles
    ]
    run_ids = sorted(
        set(run_ids)
        | {
            str(lifecycle.get("lastRunId"))
            for lifecycle in lifecycles
            if lifecycle.get("lastRunId")
        }
    )
    return {
        "id": ref_id,
        "label": label,
        "value": value,
        "unit": "count",
        "evidence": {
            "measurementCount": len(records),
            "runIds": run_ids,
            "runPairs": run_pairs,
            "lifecycles": lifecycle_refs,
        },
    }


def build_outcome_aggregates(
    records: Sequence[Mapping[str, Any]],
    *,
    actioned_opportunity_count: int,
    actioned_evidence: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    caveated_records = [r for r in records if _measurement_has_caveat(r)]
    material_caveated_records = [
        r for r in records if _confounder_summary(r)["materialCount"] > 0
    ]
    by_direction = _count_by(records, lambda r: _primary_movement(r).get("direction"))
    by_comparability = _count_by(
        records, lambda r: _dict(r.get("comparability")).get("verdict")
    )
    by_projection = _count_by(
        records, lambda r: _dict(r.get("projectionValidation")).get("verdict")
    )
    return {
        "actionedOpportunityCount": actioned_opportunity_count,
        "measuredOpportunityCount": len(
            {str(r.get("opportunityIdentity")) for r in records if r.get("opportunityIdentity")}
        ),
        "measurementCount": len(records),
        "caveatedMeasurementCount": len(caveated_records),
        "materialCaveatMeasurementCount": len(material_caveated_records),
        "byDirection": by_direction,
        "byComparability": by_comparability,
        "byProjectionValidation": by_projection,
        "numberRefs": [
            _aggregate_number_ref(
                ref_id="aggregate:actionedOpportunityCount",
                label="Actioned opportunities",
                value=actioned_opportunity_count,
                records=records,
                lifecycles=actioned_evidence,
            ),
            _aggregate_number_ref(
                ref_id="aggregate:measuredOpportunityCount",
                label="Measured opportunities",
                value=len(
                    {
                        str(r.get("opportunityIdentity"))
                        for r in records
                        if r.get("opportunityIdentity")
                    }
                ),
                records=records,
            ),
            _aggregate_number_ref(
                ref_id="aggregate:measurementCount",
                label="Stored movement measurements",
                value=len(records),
                records=records,
            ),
            _aggregate_number_ref(
                ref_id="aggregate:caveatedMeasurementCount",
                label="Measurements carrying caveats",
                value=len(caveated_records),
                records=caveated_records,
            ),
        ],
    }


def build_opportunity_outcome_view(
    org_id: str,
    opportunity_identity: str,
    *,
    limit: int = 200,
) -> Optional[Dict[str, Any]]:
    lifecycle = get_lifecycle(org_id, opportunity_identity)
    history = get_movement_history(org_id, opportunity_identity, limit=limit)
    if lifecycle is None and not history:
        return None
    if (
        lifecycle is not None
        and not history
        and lifecycle.get("state") not in MEASURABLE_STATES
    ):
        return None

    summaries = [_measurement_summary(record) for record in history]
    latest = summaries[-1] if summaries else None
    caveated = sum(1 for record in history if _measurement_has_caveat(record))
    return {
        "schemaVersion": OUTCOME_SURFACE_SCHEMA_VERSION,
        "orgId": org_id,
        "opportunityIdentity": opportunity_identity,
        "lifecycle": lifecycle,
        "measurementCount": len(history),
        "caveatedMeasurementCount": caveated,
        "latestMeasurement": latest,
        "measurements": summaries,
        "numberRefs": [ref for summary in summaries for ref in summary["numberRefs"]],
        "emptyState": None
        if history
        else {
            "reason": OUTCOME_EMPTY_NO_MOVEMENT,
            "message": (
                "No stored movement measurement exists yet for this actioned "
                "opportunity."
            ),
        },
    }


def _projection_meta(record: Mapping[str, Any]) -> Dict[str, Any]:
    validation = _dict(record.get("projectionValidation"))
    projected = _dict(validation.get("projected"))
    return {
        "verdict": validation.get("verdict"),
        "packId": projected.get("packId"),
        "packVersion": projected.get("packVersion"),
        "confidence": str(projected.get("confidence") or "").upper() or None,
    }


def _matches(
    record: Mapping[str, Any],
    *,
    comparability_verdicts: Optional[Sequence[str]] = None,
    projection_verdicts: Optional[Sequence[str]] = None,
    pack_ids: Optional[Sequence[str]] = None,
    detector_ids: Optional[Sequence[str]] = None,
    confidences: Optional[Sequence[str]] = None,
) -> bool:
    comparability = _dict(record.get("comparability")).get("verdict")
    projection = _projection_meta(record)
    if comparability_verdicts and comparability not in comparability_verdicts:
        return False
    if projection_verdicts and projection["verdict"] not in projection_verdicts:
        return False
    if detector_ids and record.get("detectorId") not in detector_ids:
        return False
    if pack_ids and projection["packId"] not in pack_ids:
        return False
    if confidences and projection["confidence"] not in {c.upper() for c in confidences}:
        return False
    return True


def build_outcome_portfolio_view(
    org_id: str,
    *,
    comparability_verdicts: Optional[Sequence[str]] = None,
    projection_verdicts: Optional[Sequence[str]] = None,
    pack_ids: Optional[Sequence[str]] = None,
    detector_ids: Optional[Sequence[str]] = None,
    confidences: Optional[Sequence[str]] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    lifecycles = list_lifecycles(
        org_id,
        states=sorted(MEASURABLE_STATES),
        limit=limit,
    )
    has_filters = any(
        (
            comparability_verdicts,
            projection_verdicts,
            pack_ids,
            detector_ids,
            confidences,
        )
    )
    items: List[Dict[str, Any]] = []
    all_records: List[Dict[str, Any]] = []
    included_lifecycles: List[Dict[str, Any]] = []

    for lifecycle in lifecycles:
        identity = str(lifecycle.get("opportunityIdentity") or "")
        history = [
            record
            for record in get_movement_history(org_id, identity, limit=limit)
            if _matches(
                record,
                comparability_verdicts=comparability_verdicts,
                projection_verdicts=projection_verdicts,
                pack_ids=pack_ids,
                detector_ids=detector_ids,
                confidences=confidences,
            )
        ]
        if has_filters and not history:
            continue
        included_lifecycles.append(lifecycle)
        summaries = [_measurement_summary(record) for record in history]
        all_records.extend(history)
        items.append(
            {
                "opportunityIdentity": identity,
                "state": lifecycle.get("state"),
                "actionDate": lifecycle.get("actionDate"),
                "lastRunId": lifecycle.get("lastRunId"),
                "measurementCount": len(history),
                "caveatedMeasurementCount": sum(
                    1 for record in history if _measurement_has_caveat(record)
                ),
                "latestMeasurement": summaries[-1] if summaries else None,
                "measurements": summaries,
                "emptyState": None
                if history
                else {
                    "reason": OUTCOME_EMPTY_NO_MOVEMENT,
                    "message": (
                        "A recorded action exists, but no stored movement "
                        "measurement exists yet."
                    ),
                },
            }
        )

    return {
        "schemaVersion": OUTCOME_SURFACE_SCHEMA_VERSION,
        "orgId": org_id,
        "filters": {
            "comparabilityVerdict": list(comparability_verdicts or []),
            "projectionVerdict": list(projection_verdicts or []),
            "pack": list(pack_ids or []),
            "detector": list(detector_ids or []),
            "confidence": [c.upper() for c in (confidences or [])],
        },
        "aggregates": build_outcome_aggregates(
            all_records,
            actioned_opportunity_count=len(items),
            actioned_evidence=included_lifecycles,
        ),
        "count": len(items),
        "items": items,
    }


def build_empty_outcome_report_section(run_id: str) -> Dict[str, Any]:
    return {
        "schemaVersion": OUTCOME_SURFACE_SCHEMA_VERSION,
        "runId": run_id,
        "generatedFrom": "stored_movement_records",
        "summary": (
            "No stored movement measurements are available for this run yet."
        ),
        "aggregates": build_outcome_aggregates([], actioned_opportunity_count=0),
        "highlights": [],
        "numberRefs": [],
    }


def build_executive_outcome_section(
    org_id: str,
    run_id: str,
    *,
    max_highlights: int = 3,
) -> Dict[str, Any]:
    records = get_movements_for_run(org_id, run_id)
    if not records:
        return build_empty_outcome_report_section(run_id)
    summaries = [_measurement_summary(record) for record in records]
    aggregates = build_outcome_aggregates(
        records,
        actioned_opportunity_count=len({r.get("opportunityIdentity") for r in records}),
    )
    section = {
        "schemaVersion": OUTCOME_SURFACE_SCHEMA_VERSION,
        "runId": run_id,
        "generatedFrom": "stored_movement_records",
        "summary": (
            "Stored movement measurements are compared against baseline "
            "following recorded actions."
        ),
        "aggregates": aggregates,
        "highlights": summaries[: max(0, int(max_highlights))],
        "numberRefs": aggregates["numberRefs"]
        + [ref for summary in summaries for ref in summary["numberRefs"]],
    }
    violations = scan_outcome_vocabulary(section)
    if violations:
        raise ValueError(f"outcome vocabulary violation: {violations[0]}")
    return section


def _walk_strings(value: Any, path: str = "$") -> Iterable[Tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk_strings(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{path}[{index}]")


def scan_outcome_vocabulary(value: Any) -> List[Dict[str, str]]:
    violations: List[Dict[str, str]] = []
    for path, text in _walk_strings(value):
        for name, pattern in FORBIDDEN_OUTCOME_PATTERNS:
            match = pattern.search(text)
            if match:
                violations.append(
                    {
                        "path": path,
                        "pattern": name,
                        "match": match.group(0),
                        "text": text,
                    }
                )
    return violations


__all__ = [
    "FORBIDDEN_OUTCOME_PATTERNS",
    "OUTCOME_EMPTY_NO_MOVEMENT",
    "OUTCOME_SURFACE_SCHEMA_VERSION",
    "build_empty_outcome_report_section",
    "build_executive_outcome_section",
    "build_movement_number_refs",
    "build_opportunity_outcome_view",
    "build_outcome_aggregates",
    "build_outcome_portfolio_view",
    "scan_outcome_vocabulary",
]
