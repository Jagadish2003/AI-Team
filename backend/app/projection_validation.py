"""2.0-A2 T5 - validate measured movement against A1's stored projection.

This is calibration data, not a scorecard. A stored projection says "this signal
should move in this direction by this band over this horizon"; a movement record
says what actually moved. This module joins the two without claiming causation.

The important absences are named:

* no usable stored band -> ``not_projected``;
* horizon not elapsed -> ``too_early``;
* otherwise the measured movement is ``within_band``, ``above_band`` or
  ``below_band``.

Pure logic only. The movement pipeline is responsible for reading the projection
history and persisting the returned payload.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

PROJECTION_VALIDATION_SCHEMA_VERSION = "1.0.0"

VERDICT_WITHIN_BAND = "within_band"
VERDICT_ABOVE_BAND = "above_band"
VERDICT_BELOW_BAND = "below_band"
VERDICT_NOT_PROJECTED = "not_projected"
VERDICT_TOO_EARLY = "too_early"

PROJECTION_VALIDATION_VERDICTS: Tuple[str, ...] = (
    VERDICT_WITHIN_BAND,
    VERDICT_ABOVE_BAND,
    VERDICT_BELOW_BAND,
    VERDICT_NOT_PROJECTED,
    VERDICT_TOO_EARLY,
)

REASON_PROJECTION_MISSING = "projection_missing"
REASON_PROJECTION_BAND_MISSING = "projection_band_missing"
REASON_PROJECTION_BAND_INVALID = "projection_band_invalid"
REASON_PROJECTION_HORIZON_MISSING = "projection_horizon_missing"
REASON_PROJECTION_SIGNAL_MISSING = "projection_signal_missing"
REASON_MEASUREMENT_SIGNAL_MISSING = "measurement_signal_missing"
REASON_MOVEMENT_PERCENT_MISSING = "movement_percent_missing"
REASON_OBSERVATION_DATES_MISSING = "observation_dates_missing"
REASON_HORIZON_NOT_ELAPSED = "horizon_not_elapsed"
REASON_MOVEMENT_WITHIN_BAND = "movement_within_projected_band"
REASON_MOVEMENT_ABOVE_BAND = "movement_above_projected_band"
REASON_MOVEMENT_BELOW_BAND = "movement_below_projected_band"


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


def _as_int(value: Any) -> Optional[int]:
    number = _safe_float(value)
    return int(number) if number is not None else None


def _round(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else round(value, 6)


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed = _parse_dt(value)
    return parsed.date() if parsed else None


def _projection_provenance(projection: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not isinstance(projection, Mapping):
        return {}
    block = projection.get("provenance")
    return dict(block) if isinstance(block, Mapping) else {}


def _projection_block(
    projection: Optional[Mapping[str, Any]],
    projection_entry: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not isinstance(projection, Mapping):
        return None
    basis = projection.get("basis") if isinstance(projection.get("basis"), Mapping) else {}
    provenance = _projection_provenance(projection)
    band = projection.get("magnitudeBand")
    signal = projection.get("movementSignal")
    return {
        "runId": (
            (projection_entry or {}).get("runId")
            or provenance.get("runId")
            or basis.get("runId")
        ),
        "createdAt": (projection_entry or {}).get("createdAt")
        or provenance.get("createdAt"),
        "direction": projection.get("direction"),
        "magnitudeBand": dict(band) if isinstance(band, Mapping) else None,
        "observationHorizonDays": _as_int(projection.get("observationHorizonDays")),
        "movementSignal": dict(signal) if isinstance(signal, Mapping) else None,
        "packId": basis.get("packId") or provenance.get("packId"),
        "packVersion": basis.get("packVersion") or provenance.get("packVersion"),
        "detectorId": basis.get("detectorId"),
        "confidence": basis.get("confidence"),
        "projectionSchemaVersion": projection.get("schemaVersion")
        or provenance.get("projectionSchemaVersion"),
        "bandWidthModelVersion": basis.get("bandWidthModelVersion")
        or provenance.get("bandWidthModelVersion"),
    }


def _confounders(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [
        dict(item)
        for item in (record.get("confounders") or [])
        if isinstance(item, Mapping)
    ]


def _confounder_summary(record: Mapping[str, Any]) -> Dict[str, Any]:
    summary = record.get("confounderSummary")
    if isinstance(summary, Mapping):
        return dict(summary)
    confounders = _confounders(record)
    by_type: Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    for item in confounders:
        by_type[item.get("type", "unknown")] = by_type.get(item.get("type", "unknown"), 0) + 1
        by_severity[item.get("severity", "unknown")] = (
            by_severity.get(item.get("severity", "unknown"), 0) + 1
        )
    return {
        "count": len(confounders),
        "materialCount": by_severity.get("material", 0),
        "advisoryCount": by_severity.get("advisory", 0),
        "byType": by_type,
        "types": sorted(by_type),
    }


def _result(
    *,
    verdict: str,
    reason: str,
    record: Mapping[str, Any],
    projection: Optional[Mapping[str, Any]],
    projection_entry: Optional[Mapping[str, Any]] = None,
    measured: Optional[Mapping[str, Any]] = None,
    elapsed_days: Optional[int] = None,
    movement_pct: Optional[float] = None,
    notes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build the stable validation payload."""
    return {
        "schemaVersion": PROJECTION_VALIDATION_SCHEMA_VERSION,
        "verdict": verdict,
        "reason": reason,
        "opportunityIdentity": record.get("opportunityIdentity"),
        "detectorId": record.get("detectorId"),
        "baselineRunId": record.get("baselineRunId"),
        "currentRunId": record.get("currentRunId"),
        "actionDate": record.get("actionDate"),
        "measuredAt": record.get("measuredAt"),
        "elapsedDaysSinceAction": elapsed_days,
        "projected": _projection_block(projection, projection_entry),
        "measured": dict(measured) if isinstance(measured, Mapping) else None,
        "movementPct": _round(movement_pct),
        "confounderSummary": _confounder_summary(record),
        "confounders": _confounders(record),
        "notes": list(notes or []),
    }


def _band_bounds(projection: Mapping[str, Any]) -> Tuple[Optional[float], Optional[float], str]:
    band = projection.get("magnitudeBand")
    if not isinstance(band, Mapping):
        return None, None, REASON_PROJECTION_BAND_MISSING
    low = _safe_float(band.get("lowPct"))
    high = _safe_float(band.get("highPct"))
    if low is None or high is None or low > high:
        return None, None, REASON_PROJECTION_BAND_INVALID
    return low, high, ""


def _projection_signal_name(projection: Mapping[str, Any]) -> Optional[str]:
    signal = projection.get("movementSignal")
    if not isinstance(signal, Mapping):
        return None
    text = str(signal.get("signalName") or "").strip()
    return text or None


def _movement_for_signal(
    record: Mapping[str, Any], signal_name: str
) -> Optional[Mapping[str, Any]]:
    for movement in record.get("movements") or []:
        if (
            isinstance(movement, Mapping)
            and str(movement.get("signalName") or "") == signal_name
        ):
            return movement
    return None


def _movement_pct_in_expected_direction(movement: Mapping[str, Any]) -> Optional[float]:
    delta_pct = _safe_float(movement.get("deltaPct"))
    if delta_pct is None:
        return None
    lower_is_better = movement.get("lowerIsBetter")
    # T3 defaults to lower-is-better when the flag is absent, so do the same here.
    return delta_pct if lower_is_better is False else -delta_pct


def select_projection_entry_for_baseline(
    baseline_run_id: Optional[str], history: Sequence[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Return the projection stored on the baseline run, never a later one."""
    target = str(baseline_run_id or "").strip()
    if not target:
        return None
    for entry in history or ():
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("runId") or "") == target and isinstance(
            entry.get("projection"), Mapping
        ):
            return dict(entry)
    return None


def build_projection_validation(
    record: Mapping[str, Any],
    projection: Optional[Mapping[str, Any]],
    *,
    projection_entry: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Compare one T3 movement record with one A1 stored projection."""
    if not isinstance(projection, Mapping):
        return _result(
            verdict=VERDICT_NOT_PROJECTED,
            reason=REASON_PROJECTION_MISSING,
            record=record,
            projection=None,
            notes=["no stored projection was found for the baseline run"],
        )

    horizon = _as_int(projection.get("observationHorizonDays"))
    if horizon is None or horizon < 0:
        return _result(
            verdict=VERDICT_NOT_PROJECTED,
            reason=REASON_PROJECTION_HORIZON_MISSING,
            record=record,
            projection=projection,
            projection_entry=projection_entry,
            notes=["the stored projection has no machine-readable observation horizon"],
        )

    action_date = _parse_date(record.get("actionDate"))
    measured_at = _parse_dt(record.get("measuredAt"))
    if action_date is None or measured_at is None:
        return _result(
            verdict=VERDICT_NOT_PROJECTED,
            reason=REASON_OBSERVATION_DATES_MISSING,
            record=record,
            projection=projection,
            projection_entry=projection_entry,
            notes=["the measurement lacks the dates needed to judge the horizon"],
        )

    elapsed_days = (measured_at.date() - action_date).days
    if elapsed_days < horizon:
        return _result(
            verdict=VERDICT_TOO_EARLY,
            reason=REASON_HORIZON_NOT_ELAPSED,
            record=record,
            projection=projection,
            projection_entry=projection_entry,
            elapsed_days=elapsed_days,
            notes=[
                f"{elapsed_days}d elapsed; the stored projection horizon is {horizon}d"
            ],
        )

    low, high, band_reason = _band_bounds(projection)
    if low is None or high is None:
        return _result(
            verdict=VERDICT_NOT_PROJECTED,
            reason=band_reason,
            record=record,
            projection=projection,
            projection_entry=projection_entry,
            elapsed_days=elapsed_days,
            notes=["the stored projection has no usable machine-readable band"],
        )

    signal_name = _projection_signal_name(projection)
    if not signal_name:
        return _result(
            verdict=VERDICT_NOT_PROJECTED,
            reason=REASON_PROJECTION_SIGNAL_MISSING,
            record=record,
            projection=projection,
            projection_entry=projection_entry,
            elapsed_days=elapsed_days,
            notes=["the stored projection does not name a movement signal"],
        )

    measured = _movement_for_signal(record, signal_name)
    if not measured:
        return _result(
            verdict=VERDICT_NOT_PROJECTED,
            reason=REASON_MEASUREMENT_SIGNAL_MISSING,
            record=record,
            projection=projection,
            projection_entry=projection_entry,
            elapsed_days=elapsed_days,
            notes=[f"the measurement did not include projected signal {signal_name!r}"],
        )

    movement_pct = _movement_pct_in_expected_direction(measured)
    if movement_pct is None:
        return _result(
            verdict=VERDICT_NOT_PROJECTED,
            reason=REASON_MOVEMENT_PERCENT_MISSING,
            record=record,
            projection=projection,
            projection_entry=projection_entry,
            measured=measured,
            elapsed_days=elapsed_days,
            notes=["the measured movement cannot be expressed as a percent of baseline"],
        )

    if movement_pct > high:
        verdict, reason = VERDICT_ABOVE_BAND, REASON_MOVEMENT_ABOVE_BAND
    elif movement_pct < low:
        verdict, reason = VERDICT_BELOW_BAND, REASON_MOVEMENT_BELOW_BAND
    else:
        verdict, reason = VERDICT_WITHIN_BAND, REASON_MOVEMENT_WITHIN_BAND

    return _result(
        verdict=verdict,
        reason=reason,
        record=record,
        projection=projection,
        projection_entry=projection_entry,
        measured=measured,
        elapsed_days=elapsed_days,
        movement_pct=movement_pct,
        notes=[
            f"measured movement {_round(movement_pct)}% against projected band "
            f"{_round(low)}-{_round(high)}%"
        ],
    )


def validation_filter_values(
    validation: Optional[Mapping[str, Any]]
) -> Dict[str, Optional[Any]]:
    """Values promoted into columns so aggregate queries do not scrape JSON."""
    if not isinstance(validation, Mapping):
        return {
            "verdict": VERDICT_NOT_PROJECTED,
            "packId": None,
            "packVersion": None,
            "confidence": None,
        }
    projected = validation.get("projected")
    if not isinstance(projected, Mapping):
        projected = {}
    confidence = projected.get("confidence")
    return {
        "verdict": validation.get("verdict") or VERDICT_NOT_PROJECTED,
        "packId": projected.get("packId"),
        "packVersion": projected.get("packVersion"),
        "confidence": str(confidence).upper() if confidence else None,
    }


__all__ = [
    "PROJECTION_VALIDATION_SCHEMA_VERSION",
    "PROJECTION_VALIDATION_VERDICTS",
    "REASON_HORIZON_NOT_ELAPSED",
    "REASON_MEASUREMENT_SIGNAL_MISSING",
    "REASON_MOVEMENT_ABOVE_BAND",
    "REASON_MOVEMENT_BELOW_BAND",
    "REASON_MOVEMENT_PERCENT_MISSING",
    "REASON_MOVEMENT_WITHIN_BAND",
    "REASON_OBSERVATION_DATES_MISSING",
    "REASON_PROJECTION_BAND_INVALID",
    "REASON_PROJECTION_BAND_MISSING",
    "REASON_PROJECTION_HORIZON_MISSING",
    "REASON_PROJECTION_MISSING",
    "REASON_PROJECTION_SIGNAL_MISSING",
    "VERDICT_ABOVE_BAND",
    "VERDICT_BELOW_BAND",
    "VERDICT_NOT_PROJECTED",
    "VERDICT_TOO_EARLY",
    "VERDICT_WITHIN_BAND",
    "build_projection_validation",
    "select_projection_entry_for_baseline",
    "validation_filter_values",
]
