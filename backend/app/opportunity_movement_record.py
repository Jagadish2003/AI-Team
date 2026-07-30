"""2.0-A2 T3 — the movement record and its comparability verdict, as pure logic.

Given T2's frozen baseline artifact and a post-action observation, produce an
honest comparison: baseline value, current value, delta, a comparability verdict,
and the run ids on both sides.

**Comparable window is the hard part.** A delta between a 30-day baseline window
and a 9-day post-action window is not a result, it is a category error. So this
module computes and CARRIES a verdict rather than silently normalising, and the
record still reports when comparability is poor — with the verdict attached —
matching the "never a blocked measurement" posture T4 takes. Four hazards are
assessed, all of them named in the subtask:

    1. unequal window lengths
    2. insufficient elapsed time since the action date (A1's projected horizon
       has not passed yet)
    3. gaps in run cadence leaving the post-action window sparsely sampled
    4. windows straddling a seasonality boundary

**Everything anchors on the action date.** Only observations strictly after it
count as post-action. There is deliberately no "most recent run" or "since the
finding was created" fallback — either would silently fold pre-action data into
the comparison and inflate or deflate the delta. Where the action date falls
mid-window in the source data, the record STATES how the boundary was handled
rather than picking one silently.

**Absence is not zero.** A caller with no qualifying post-action observation gets
``None``, never a zero-delta record: "we have not measured" and "we measured no
change" are different facts and conflating them is the single easiest way to
manufacture a false reassurance.

Pure: no DB, no clock read except the caller-supplied ``measured_at``. The
attribution discipline lives here too — this module computes movement and
comparison, and never a claim of credit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: Bumped when the record's shape changes in a way T4/T5/T6 must notice.
MOVEMENT_SCHEMA_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Comparability
# --------------------------------------------------------------------------

VERDICT_COMPARABLE = "comparable"
VERDICT_WEAK = "weakly_comparable"
VERDICT_NOT_COMPARABLE = "not_comparable"

#: Ordered worst-last so a verdict can be escalated but never quietly softened.
VERDICT_SEVERITY = {
    VERDICT_COMPARABLE: 0,
    VERDICT_WEAK: 1,
    VERDICT_NOT_COMPARABLE: 2,
}

# Reason codes. Stable strings — T4 surfaces them as labelled caveats and T6
# counts them, so renaming one is a breaking change.
REASON_WINDOW_LENGTH_MISMATCH = "window_length_mismatch"
REASON_HORIZON_NOT_ELAPSED = "horizon_not_elapsed"
REASON_SPARSE_SAMPLING = "sparse_post_action_sampling"
REASON_CADENCE_GAP = "run_cadence_gap"
REASON_SEASONALITY_MISMATCH = "seasonality_window_mismatch"
REASON_BOUNDARY_STRADDLE = "action_date_straddles_window"
REASON_WINDOW_UNKNOWN = "window_length_unknown"
REASON_HORIZON_UNKNOWN = "projected_horizon_unknown"

#: Window lengths differing by more than this fraction are weakly comparable;
#: beyond :data:`WINDOW_LENGTH_HARD_TOLERANCE` they are not comparable at all.
WINDOW_LENGTH_TOLERANCE = 0.25
WINDOW_LENGTH_HARD_TOLERANCE = 0.60

#: Fewer post-action observations than this leaves the window sparsely sampled.
MIN_POST_ACTION_OBSERVATIONS = 2

#: A gap larger than this between consecutive post-action runs means the window
#: was not continuously observed. Set above a normal monthly cadence on purpose:
#: at 21 days every monthly-cadence deployment would carry a permanent caveat,
#: and a caveat that is always present is one people learn to ignore. 35 days
#: catches a genuinely missed cycle rather than a routine one.
MAX_RUN_GAP_DAYS = 35

#: Two windows whose covered months overlap by less than this fraction sit in
#: different parts of the year and may be comparing different seasons.
MIN_SEASONAL_MONTH_OVERLAP = 0.5

#: How the action-date boundary was handled. Recorded, never implicit.
BOUNDARY_STRICTLY_AFTER = "observations_strictly_after_action_date"
BOUNDARY_NO_OBSERVATIONS = "no_observations_after_action_date"


@dataclass(frozen=True)
class ComparabilityAssessment:
    """The verdict and every reason behind it. Never null on a record."""

    verdict: str
    reasons: List[str]
    baseline_window_days: Optional[int]
    current_window_days: Optional[int]
    elapsed_days_since_action: Optional[int]
    projected_horizon_days: Optional[int]
    post_action_observation_count: int
    max_run_gap_days: Optional[int]
    seasonal_month_overlap: Optional[float]
    boundary_handling: str
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "baselineWindowDays": self.baseline_window_days,
            "currentWindowDays": self.current_window_days,
            "elapsedDaysSinceAction": self.elapsed_days_since_action,
            "projectedHorizonDays": self.projected_horizon_days,
            "postActionObservationCount": self.post_action_observation_count,
            "maxRunGapDays": self.max_run_gap_days,
            "seasonalMonthOverlap": self.seasonal_month_overlap,
            "boundaryHandling": self.boundary_handling,
            "notes": list(self.notes),
        }


def _escalate(current: str, candidate: str) -> str:
    """Verdicts only ever get worse. A later check cannot soften an earlier one."""
    return candidate if VERDICT_SEVERITY[candidate] > VERDICT_SEVERITY[current] else current


def _months_covered(start: Optional[datetime], end: Optional[datetime]) -> set:
    """Calendar months a window touches, as month-of-year numbers."""
    if not start or not end or end < start:
        return set()
    months, cursor = set(), start
    # Bounded: a window longer than ~3 years contributes every month anyway.
    for _ in range(40):
        months.add(cursor.month)
        if (cursor.year, cursor.month) == (end.year, end.month):
            break
        cursor = (
            cursor.replace(year=cursor.year + 1, month=1)
            if cursor.month == 12
            else cursor.replace(month=cursor.month + 1)
        )
    return months


def assess_comparability(
    *,
    baseline_window_days: Optional[int],
    current_window_days: Optional[int],
    baseline_window_start: Optional[datetime],
    baseline_window_end: Optional[datetime],
    current_window_start: Optional[datetime],
    current_window_end: Optional[datetime],
    action_date: Optional[date],
    measured_at: datetime,
    projected_horizon_days: Optional[int],
    post_action_observation_count: int,
    post_action_run_dates: Sequence[datetime] = (),
) -> ComparabilityAssessment:
    """Assess whether the two windows can honestly be compared.

    Always returns a verdict — there is no code path that leaves comparability
    null, because a null would be read as "fine" by anything that renders it.
    """
    verdict = VERDICT_COMPARABLE
    reasons: List[str] = []
    notes: List[str] = []

    # 1. Unequal window lengths.
    if baseline_window_days and current_window_days:
        longer = max(baseline_window_days, current_window_days)
        drift = abs(baseline_window_days - current_window_days) / longer
        if drift > WINDOW_LENGTH_HARD_TOLERANCE:
            reasons.append(REASON_WINDOW_LENGTH_MISMATCH)
            verdict = _escalate(verdict, VERDICT_NOT_COMPARABLE)
            notes.append(
                f"baseline window {baseline_window_days}d vs current "
                f"{current_window_days}d differ by {drift:.0%}"
            )
        elif drift > WINDOW_LENGTH_TOLERANCE:
            reasons.append(REASON_WINDOW_LENGTH_MISMATCH)
            verdict = _escalate(verdict, VERDICT_WEAK)
            notes.append(
                f"baseline window {baseline_window_days}d vs current "
                f"{current_window_days}d differ by {drift:.0%}"
            )
    else:
        # Not knowing a window length is itself a comparability problem, stated
        # rather than assumed away.
        reasons.append(REASON_WINDOW_UNKNOWN)
        verdict = _escalate(verdict, VERDICT_WEAK)

    # 2. Insufficient elapsed time since the action.
    elapsed_days: Optional[int] = None
    if action_date:
        elapsed_days = (measured_at.date() - action_date).days
        if projected_horizon_days:
            if elapsed_days < projected_horizon_days:
                reasons.append(REASON_HORIZON_NOT_ELAPSED)
                verdict = _escalate(verdict, VERDICT_NOT_COMPARABLE)
                notes.append(
                    f"{elapsed_days}d elapsed of the {projected_horizon_days}d "
                    "horizon the projection said movement would be observable over"
                )
        else:
            reasons.append(REASON_HORIZON_UNKNOWN)
            verdict = _escalate(verdict, VERDICT_WEAK)
            notes.append(
                "no projected horizon on record, so elapsed time cannot be judged "
                "against the window movement was expected over"
            )

    # 3. Sparse post-action sampling.
    if post_action_observation_count < MIN_POST_ACTION_OBSERVATIONS:
        reasons.append(REASON_SPARSE_SAMPLING)
        verdict = _escalate(verdict, VERDICT_WEAK)
        notes.append(
            f"{post_action_observation_count} post-action observation(s); "
            f"{MIN_POST_ACTION_OBSERVATIONS} is the minimum for a continuously "
            "observed window"
        )

    # 4. Gaps in run cadence.
    max_gap: Optional[int] = None
    ordered = sorted(d for d in post_action_run_dates if d is not None)
    if len(ordered) >= 2:
        max_gap = max(
            (ordered[i + 1] - ordered[i]).days for i in range(len(ordered) - 1)
        )
        if max_gap > MAX_RUN_GAP_DAYS:
            reasons.append(REASON_CADENCE_GAP)
            verdict = _escalate(verdict, VERDICT_WEAK)
            notes.append(
                f"a {max_gap}d gap between post-action runs leaves the window "
                "sparsely sampled"
            )

    # 5. Seasonality: are the two windows in comparable parts of the year?
    overlap: Optional[float] = None
    baseline_months = _months_covered(baseline_window_start, baseline_window_end)
    current_months = _months_covered(current_window_start, current_window_end)
    if baseline_months and current_months:
        overlap = len(baseline_months & current_months) / len(
            baseline_months | current_months
        )
        if overlap < MIN_SEASONAL_MONTH_OVERLAP:
            reasons.append(REASON_SEASONALITY_MISMATCH)
            verdict = _escalate(verdict, VERDICT_WEAK)
            notes.append(
                f"windows share {overlap:.0%} of their calendar months, so they may "
                "be comparing different seasons"
            )

    # 6. Boundary handling — always stated.
    if post_action_observation_count:
        boundary = BOUNDARY_STRICTLY_AFTER
    else:
        boundary = BOUNDARY_NO_OBSERVATIONS
    if action_date and baseline_window_end and baseline_window_end.date() > action_date:
        # The frozen baseline window extends past the action date, so the two
        # sides are not cleanly separated in the source data.
        reasons.append(REASON_BOUNDARY_STRADDLE)
        verdict = _escalate(verdict, VERDICT_WEAK)
        notes.append(
            "the baseline window ends after the action date, so pre- and "
            "post-action observations are not cleanly separated in the source data"
        )

    return ComparabilityAssessment(
        verdict=verdict,
        reasons=reasons,
        baseline_window_days=baseline_window_days,
        current_window_days=current_window_days,
        elapsed_days_since_action=elapsed_days,
        projected_horizon_days=projected_horizon_days,
        post_action_observation_count=post_action_observation_count,
        max_run_gap_days=max_gap,
        seasonal_month_overlap=round(overlap, 4) if overlap is not None else None,
        boundary_handling=boundary,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Movement
# --------------------------------------------------------------------------

DIRECTION_IMPROVED = "improved"
DIRECTION_WORSENED = "worsened"
DIRECTION_UNCHANGED = "unchanged"
DIRECTION_UNKNOWN = "unknown"

#: Absolute change smaller than this fraction of the baseline reads as unchanged
#: rather than as noise-level movement dressed up as a result.
UNCHANGED_TOLERANCE = 0.02


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


def _round(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else round(value, 6)


@dataclass(frozen=True)
class SignalMovement:
    """One signal's baseline → current comparison.

    ``direction`` is phrased as movement, never as attribution: "improved" states
    which way the number went against its own baseline, not that anything caused
    it.
    """

    signal_name: str
    role: Optional[str]
    baseline_value: Optional[float]
    current_value: Optional[float]
    delta: Optional[float]
    delta_pct: Optional[float]
    direction: str
    lower_is_better: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signalName": self.signal_name,
            "role": self.role,
            "baselineValue": _round(self.baseline_value),
            "currentValue": _round(self.current_value),
            "delta": _round(self.delta),
            "deltaPct": _round(self.delta_pct),
            "direction": self.direction,
            "lowerIsBetter": self.lower_is_better,
        }


def _direction(
    baseline: Optional[float], current: Optional[float], lower_is_better: bool
) -> str:
    if baseline is None or current is None:
        return DIRECTION_UNKNOWN
    delta = current - baseline
    if baseline != 0 and abs(delta) / abs(baseline) < UNCHANGED_TOLERANCE:
        return DIRECTION_UNCHANGED
    if delta == 0:
        return DIRECTION_UNCHANGED
    improved = delta < 0 if lower_is_better else delta > 0
    return DIRECTION_IMPROVED if improved else DIRECTION_WORSENED


def build_signal_movements(
    baseline_signals: Sequence[Mapping[str, Any]],
    current_values: Mapping[str, Any],
    *,
    lower_is_better_by_signal: Optional[Mapping[str, bool]] = None,
) -> List[SignalMovement]:
    """Compare each baseline signal against its re-measured current value.

    A signal with no current value is still emitted, with ``current_value=None``
    and ``direction='unknown'`` — dropping it would make the record look complete
    while quietly measuring fewer signals than the baseline froze.
    """
    lower_map = lower_is_better_by_signal or {}
    movements: List[SignalMovement] = []
    for signal in baseline_signals:
        name = str(signal.get("signalName") or "").strip()
        if not name:
            continue
        baseline_value = _safe_float(signal.get("value"))
        current_value = _safe_float(current_values.get(name))
        delta = (
            current_value - baseline_value
            if baseline_value is not None and current_value is not None
            else None
        )
        delta_pct = (
            (delta / abs(baseline_value)) * 100
            if delta is not None and baseline_value not in (None, 0)
            else None
        )
        lower_is_better = bool(lower_map.get(name, True))
        movements.append(
            SignalMovement(
                signal_name=name,
                role=signal.get("role"),
                baseline_value=baseline_value,
                current_value=current_value,
                delta=delta,
                delta_pct=delta_pct,
                direction=_direction(baseline_value, current_value, lower_is_better),
                lower_is_better=lower_is_better,
            )
        )
    return movements


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------

#: Fields T4/T5/T6 and 2.0-B1's evidence trace require. Asserted by test so a
#: shape change cannot quietly drop something a later subtask needs.
REQUIRED_MOVEMENT_FIELDS = (
    "schemaVersion",
    "orgId",
    "opportunityIdentity",
    "detectorId",
    "actionDate",
    "baselineRunId",
    "currentRunId",
    "movements",
    "comparability",
    "baseline",
    "current",
    "measuredAt",
)


def build_movement_record(
    *,
    org_id: str,
    opportunity_identity: str,
    detector_id: str,
    action_date: Optional[date],
    baseline_artifact: Mapping[str, Any],
    current_run_id: str,
    current_values: Mapping[str, Any],
    current_window_days: Optional[int],
    current_window_start: Optional[datetime],
    current_window_end: Optional[datetime],
    current_pack_version: Optional[str],
    post_action_run_ids: Sequence[str],
    post_action_run_dates: Sequence[datetime],
    projected_horizon_days: Optional[int],
    measured_at: datetime,
    lower_is_better_by_signal: Optional[Mapping[str, bool]] = None,
) -> Dict[str, Any]:
    """Assemble one movement record from a baseline and a post-action observation.

    Shaped to be TRACE-FRIENDLY rather than a flattened summary number: both run
    ids are first-class, each signal keeps its own before/after pair, and the
    baseline and current sides retain their windows and pack versions. 2.0-B1's
    evidence trace expands an outcome claim and expects to land on source
    records, so nothing here collapses to a single figure.
    """
    baseline_window = baseline_artifact.get("window") or {}
    baseline_signals = baseline_artifact.get("signals") or []

    def _dt(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    comparability = assess_comparability(
        baseline_window_days=baseline_window.get("days"),
        current_window_days=current_window_days,
        baseline_window_start=_dt(baseline_window.get("startedAt")),
        baseline_window_end=_dt(baseline_window.get("endedAt")),
        current_window_start=current_window_start,
        current_window_end=current_window_end,
        action_date=action_date,
        measured_at=measured_at,
        projected_horizon_days=projected_horizon_days,
        post_action_observation_count=len(post_action_run_ids),
        post_action_run_dates=post_action_run_dates,
    )

    movements = build_signal_movements(
        baseline_signals,
        current_values,
        lower_is_better_by_signal=lower_is_better_by_signal,
    )

    return {
        "schemaVersion": MOVEMENT_SCHEMA_VERSION,
        "orgId": org_id,
        "opportunityIdentity": opportunity_identity,
        "detectorId": detector_id,
        "actionDate": action_date.isoformat() if action_date else None,
        # AC7: both run ids are first-class, not buried in a nested blob. Cheap
        # now, expensive to retrofit.
        "baselineRunId": baseline_artifact.get("runId"),
        "currentRunId": current_run_id,
        "movements": [m.to_dict() for m in movements],
        "comparability": comparability.to_dict(),
        "baseline": {
            "runId": baseline_artifact.get("runId"),
            "capturedAt": baseline_artifact.get("capturedAt"),
            "packVersion": baseline_artifact.get("packVersion"),
            "window": dict(baseline_window),
            "values": dict(baseline_artifact.get("measuredValues") or {}),
        },
        "current": {
            "runId": current_run_id,
            "packVersion": current_pack_version,
            "window": {
                "days": current_window_days,
                "startedAt": current_window_start.isoformat()
                if current_window_start
                else None,
                "endedAt": current_window_end.isoformat() if current_window_end else None,
            },
            "values": {k: _round(_safe_float(v)) for k, v in current_values.items()
                       if _safe_float(v) is not None},
        },
        "postActionRunIds": list(post_action_run_ids),
        "measuredAt": measured_at.isoformat(),
    }


def missing_movement_fields(record: Optional[Mapping[str, Any]]) -> List[str]:
    """Which required fields a record lacks. Empty list means complete.

    ``actionDate`` may be present-but-null only in impossible states; a record
    without a comparability verdict is always a defect, so both are checked for
    presence and comparability additionally for a non-null verdict.
    """
    if not isinstance(record, Mapping):
        return list(REQUIRED_MOVEMENT_FIELDS)
    missing = [name for name in REQUIRED_MOVEMENT_FIELDS if name not in record]
    comparability = record.get("comparability")
    if not isinstance(comparability, Mapping) or not comparability.get("verdict"):
        missing.append("comparability.verdict")
    return missing


__all__ = [
    "MOVEMENT_SCHEMA_VERSION",
    "REQUIRED_MOVEMENT_FIELDS",
    "BOUNDARY_NO_OBSERVATIONS",
    "BOUNDARY_STRICTLY_AFTER",
    "DIRECTION_IMPROVED",
    "DIRECTION_UNCHANGED",
    "DIRECTION_UNKNOWN",
    "DIRECTION_WORSENED",
    "MAX_RUN_GAP_DAYS",
    "MIN_POST_ACTION_OBSERVATIONS",
    "MIN_SEASONAL_MONTH_OVERLAP",
    "REASON_BOUNDARY_STRADDLE",
    "REASON_CADENCE_GAP",
    "REASON_HORIZON_NOT_ELAPSED",
    "REASON_HORIZON_UNKNOWN",
    "REASON_SEASONALITY_MISMATCH",
    "REASON_SPARSE_SAMPLING",
    "REASON_WINDOW_LENGTH_MISMATCH",
    "REASON_WINDOW_UNKNOWN",
    "VERDICT_COMPARABLE",
    "VERDICT_NOT_COMPARABLE",
    "VERDICT_SEVERITY",
    "VERDICT_WEAK",
    "WINDOW_LENGTH_HARD_TOLERANCE",
    "WINDOW_LENGTH_TOLERANCE",
    "ComparabilityAssessment",
    "SignalMovement",
    "assess_comparability",
    "build_movement_record",
    "build_signal_movements",
    "missing_movement_fields",
]
