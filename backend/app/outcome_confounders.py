"""2.0-A2 T4 — confounder detection: labelled caveats on a measurement.

This module is where A2's governing discipline becomes code. **AgentIQ measures
movement and shows the comparison honestly; it does not claim credit.** Confounders
are the mechanism for "honestly".

Two rules constrain every line here.

**Never a silent adjustment.** Nothing in this module scales, weights, or corrects
a delta. A number that has been quietly adjusted cannot be reproduced by a customer
holding the same source data, and the moment they discover it the entire outcome
story is dead. The raw movement is reported, and the confounder is reported beside
it. A structural test greps this module to prove no arithmetic touches a delta.

**Never a blocked measurement.** A detected confounder does not suppress the
result — AC3 requires the measurement still reports. Detection appends caveats; it
has no return path that means "do not publish".

Both failure modes come from the same instinct — make the number look clean — and
both are refused.

**Caveats are structured data, not prose.** Each carries a stable ``type``, a
``severity``, a ``detail`` dict of the actual values compared, and ``detectedAt``.
T6's portfolio view counts them by type; 2.0-B1's trace renders them. A prose
string would be countable by neither.

**Extensible without touching the comparison engine.** Detectors are registered
(:func:`register_confounder_detector`) and driven by one loop over the registry.
Adding a fifth confounder type means adding a detector and registering it — the
movement engine calls :func:`detect_confounders` once and never learns the list.
That matters because this list will grow once real outcome data exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .outcome_confounder_config import (
    SEASONALITY_CALENDAR_MONTH,
    SEASONALITY_DECLARED_PERIODS,
    SEASONALITY_DISABLED,
    SEASONALITY_FISCAL_QUARTER,
    SEVERITY_ADVISORY,
    SEVERITY_MATERIAL,
    ConfounderConfig,
    load_confounder_config,
)

logger = logging.getLogger(__name__)

#: Bumped when the caveat shape changes in a way T6/B1 must notice.
CONFOUNDER_SCHEMA_VERSION = "1.0.0"

# Stable type codes. T6 counts by these and B1 renders them, so renaming one is a
# breaking change.
CONFOUNDER_VOLUME_SHIFT = "volume_shift"
CONFOUNDER_CI_POPULATION_CHANGE = "ci_population_change"
CONFOUNDER_PACK_VERSION_CHANGE = "pack_version_change"
CONFOUNDER_SEASONALITY_MISMATCH = "seasonality_window_mismatch"


@dataclass(frozen=True)
class Confounder:
    """One labelled caveat on a measurement.

    Structured throughout: ``detail`` holds the values that were actually compared,
    so a reader can check the judgement rather than take the label on trust.
    """

    type: str
    severity: str
    label: str
    detail: Dict[str, Any]
    detected_at: str
    #: Which config section's threshold produced this, and how trustworthy that
    #: threshold is (measured / operationally_justified / provisional). Surfaced so
    #: a caveat driven by a first-guess number says so.
    threshold_basis: Optional[str] = None
    schema_version: str = CONFOUNDER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "type": self.type,
            "severity": self.severity,
            "label": self.label,
            "detail": dict(self.detail),
            "detectedAt": self.detected_at,
            "thresholdBasis": self.threshold_basis,
        }


@dataclass
class ConfounderContext:
    """Everything a detector may inspect about one comparison.

    A dataclass rather than loose arguments so a new detector can use a field an
    existing one ignores without changing any signature.
    """

    org_id: str
    opportunity_identity: str
    detected_at: str
    config: ConfounderConfig
    #: The frozen T2 baseline artifact.
    baseline: Mapping[str, Any] = field(default_factory=dict)
    #: The T3 movement record being annotated.
    movement: Mapping[str, Any] = field(default_factory=dict)
    #: Resolved entity keys behind each side, when available.
    baseline_entity_keys: Optional[Sequence[str]] = None
    current_entity_keys: Optional[Sequence[str]] = None
    #: A 2.0-B2 entity merge/unmerge touching this population, when known. An
    #: explicit resolution event must surface as a confounder rather than looking
    #: like organic drift.
    entity_resolution_events: Sequence[Mapping[str, Any]] = field(
        default_factory=tuple
    )


#: A detector takes the context and returns any caveats it found.
ConfounderDetector = Callable[[ConfounderContext], Sequence[Confounder]]

_REGISTRY: Dict[str, ConfounderDetector] = {}


def register_confounder_detector(
    name: str, detector: ConfounderDetector, *, replace: bool = False
) -> None:
    """Register a confounder detector.

    The extension point. Adding a confounder type never requires editing the
    comparison engine — it calls :func:`detect_confounders` and does not know what
    is registered.
    """
    key = str(name or "").strip()
    if not key:
        raise ValueError("a confounder detector needs a name")
    if key in _REGISTRY and not replace:
        raise ValueError(
            f"a confounder detector named {key!r} is already registered; pass "
            "replace=True to override it deliberately"
        )
    _REGISTRY[key] = detector


def registered_confounder_detectors() -> Tuple[str, ...]:
    """Every registered detector name, sorted — the audit surface."""
    return tuple(sorted(_REGISTRY))


def unregister_confounder_detector(name: str) -> None:
    """Remove a detector. Exists for tests; not used in the pipeline."""
    _REGISTRY.pop(name, None)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _volume_signal_name(baseline: Mapping[str, Any]) -> Optional[str]:
    """The signal that carries total volume — the population, else the instance count."""
    signals = baseline.get("signals") or []
    for role in ("population", "instance"):
        for signal in signals:
            if signal.get("role") == role and signal.get("signalName"):
                return str(signal["signalName"])
    return None


def _months_covered(start: Optional[datetime], end: Optional[datetime]) -> set:
    if not start or not end or end < start:
        return set()
    months, cursor = set(), start
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


def _fiscal_quarters(
    start: Optional[datetime], end: Optional[datetime], fiscal_start_month: int
) -> set:
    """Fiscal quarters a window touches, given the fiscal year's start month."""
    months = _months_covered(start, end)
    return {((m - fiscal_start_month) % 12) // 3 + 1 for m in months}


def _declared_periods(
    start: Optional[datetime],
    end: Optional[datetime],
    periods: Sequence[Mapping[str, Any]],
) -> set:
    """Which declared seasonal periods a window overlaps."""
    months = _months_covered(start, end)
    hit = set()
    for period in periods:
        try:
            first = int(period.get("startMonth"))
            last = int(period.get("endMonth"))
        except (TypeError, ValueError):
            continue
        span = (
            set(range(first, last + 1))
            if first <= last
            # A period wrapping the year end, e.g. Nov-Feb.
            else set(range(first, 13)) | set(range(1, last + 1))
        )
        if months & span:
            hit.add(str(period.get("label") or f"{first}-{last}"))
    return hit


# --------------------------------------------------------------------------
# Detector 1 — pack-version change (nearly free, ships first)
# --------------------------------------------------------------------------


def detect_pack_version_change(ctx: ConfounderContext) -> Sequence[Confounder]:
    """A pack-logic boundary crossed between the two measurements.

    Nearly free to detect — ``pack_version`` is already stamped on every instance
    and captured in T2's baseline — and it matters disproportionately. A pack's
    version bumps whenever its detector, scorer or corroboration-rule logic
    changes, so movement measured across a version boundary may reflect a
    threshold edit rather than any real-world improvement. That is precisely the
    caveat a CFO's analyst looks for, and if we do not surface it ourselves the
    discovery is fatal.
    """
    baseline_version = (ctx.movement.get("baseline") or {}).get("packVersion") or (
        ctx.baseline.get("packVersion")
    )
    current_version = (ctx.movement.get("current") or {}).get("packVersion")
    cfg = ctx.config.pack_version
    basis = ctx.config.basis_for("pack_version")

    if not baseline_version or not current_version:
        if not cfg.treat_missing_version_as_change:
            # An absence of information is not evidence of a change, so this is an
            # advisory rather than a material caveat.
            return [
                Confounder(
                    type=CONFOUNDER_PACK_VERSION_CHANGE,
                    severity=SEVERITY_ADVISORY,
                    label="Pack version unknown on one side of the comparison",
                    detail={
                        "baselinePackVersion": baseline_version,
                        "currentPackVersion": current_version,
                        "reason": "missing_version",
                    },
                    detected_at=ctx.detected_at,
                    threshold_basis=basis,
                )
            ]
        baseline_version = baseline_version or "unknown"
        current_version = current_version or "unknown"

    if str(baseline_version) == str(current_version):
        return []

    return [
        Confounder(
            type=CONFOUNDER_PACK_VERSION_CHANGE,
            severity=(
                SEVERITY_MATERIAL if cfg.any_change_is_material else SEVERITY_ADVISORY
            ),
            label="Pack version changed between the two measurements",
            detail={
                "baselinePackVersion": str(baseline_version),
                "currentPackVersion": str(current_version),
                "reason": "version_changed",
                "implication": (
                    "a pack version bumps when its detector, scorer or "
                    "corroboration-rule logic changes, so part of this movement may "
                    "reflect a pack-logic change rather than a change in the estate"
                ),
            },
            detected_at=ctx.detected_at,
            threshold_basis=basis,
        )
    ]


# --------------------------------------------------------------------------
# Detector 2 — total volume shift
# --------------------------------------------------------------------------


def detect_volume_shift(ctx: ConfounderContext) -> Sequence[Confounder]:
    """Total volume moved materially between the two measurements.

    A signal computed over a materially different total volume is not measuring
    the same thing, even when the rate looks similar. The threshold is
    configuration, because this is the tuning parameter most likely to be wrong
    on first guess.
    """
    signal_name = _volume_signal_name(ctx.baseline)
    if not signal_name:
        return []

    baseline_value = _safe_float(
        (ctx.movement.get("baseline") or {}).get("values", {}).get(signal_name)
    )
    current_value = _safe_float(
        (ctx.movement.get("current") or {}).get("values", {}).get(signal_name)
    )
    if baseline_value is None or current_value is None or baseline_value == 0:
        return []

    shift = (current_value - baseline_value) / abs(baseline_value)
    magnitude = abs(shift)
    cfg = ctx.config.volume_shift
    basis = ctx.config.basis_for("volume_shift")

    if magnitude < cfg.advisory_shift_fraction:
        return []

    severity = (
        SEVERITY_MATERIAL
        if magnitude >= cfg.material_shift_fraction
        else SEVERITY_ADVISORY
    )
    direction = "increased" if shift > 0 else "decreased"
    return [
        Confounder(
            type=CONFOUNDER_VOLUME_SHIFT,
            severity=severity,
            label=f"Total volume {direction} {magnitude:.0%} between measurements",
            detail={
                "signalName": signal_name,
                "baselineValue": baseline_value,
                "currentValue": current_value,
                "shiftFraction": round(shift, 6),
                "direction": direction,
                "materialThreshold": cfg.material_shift_fraction,
                "advisoryThreshold": cfg.advisory_shift_fraction,
                "implication": (
                    "the two measurements are computed over materially different "
                    "total volumes, so part of the movement may reflect the change "
                    "in volume rather than a change in the underlying pattern"
                ),
            },
            detected_at=ctx.detected_at,
            threshold_basis=basis,
        )
    ]


# --------------------------------------------------------------------------
# Detector 3 — CI/service population change (the subtlest)
# --------------------------------------------------------------------------


def detect_ci_population_change(ctx: ConfounderContext) -> Sequence[Confounder]:
    """The resolved entity set behind the signal changed.

    MSP-B3 CI entities and dependency edges underpin several operational signals,
    so a signal computed over a service set that quietly grew or shrank between
    measurements is not measuring the same thing.

    Interacts with 2.0-B2 deliberately: an entity merge or unmerge legitimately
    changes the population, so a recorded resolution event is surfaced HERE as a
    confounder rather than being left to look like organic drift.
    """
    if ctx.baseline_entity_keys is None or ctx.current_entity_keys is None:
        # Not knowing the population is not evidence it changed. Silence beats a
        # fabricated caveat.
        return []

    baseline_set = {str(k) for k in ctx.baseline_entity_keys if str(k).strip()}
    current_set = {str(k) for k in ctx.current_entity_keys if str(k).strip()}
    added = sorted(current_set - baseline_set)
    removed = sorted(baseline_set - current_set)
    if not added and not removed:
        return _resolution_event_confounders(ctx)

    cfg = ctx.config.ci_population
    basis = ctx.config.basis_for("ci_population")
    changed = len(added) + len(removed)
    baseline_size = len(baseline_set)

    # Below the minimum population a fraction is meaningless — one service out of
    # three is a 33% swing — so an absolute-count rule applies instead.
    if baseline_size < cfg.min_population_for_fraction:
        material = changed >= cfg.material_change_absolute
        fraction = None
        rule = "absolute_count"
    else:
        fraction = changed / baseline_size
        material = fraction >= cfg.material_change_fraction
        if fraction < cfg.advisory_change_fraction:
            return _resolution_event_confounders(ctx)
        rule = "fraction_of_baseline_population"

    confounders = [
        Confounder(
            type=CONFOUNDER_CI_POPULATION_CHANGE,
            severity=SEVERITY_MATERIAL if material else SEVERITY_ADVISORY,
            label=(
                f"Underlying service/CI population changed "
                f"({len(added)} added, {len(removed)} removed)"
            ),
            detail={
                "baselinePopulationSize": baseline_size,
                "currentPopulationSize": len(current_set),
                "addedCount": len(added),
                "removedCount": len(removed),
                # Bounded samples, not the whole set: a caveat is a summary, and an
                # unbounded list would make the record unrenderable on a large estate.
                "addedSample": added[:10],
                "removedSample": removed[:10],
                "changeFraction": round(fraction, 6) if fraction is not None else None,
                "rule": rule,
                "materialThreshold": cfg.material_change_fraction,
                "minPopulationForFraction": cfg.min_population_for_fraction,
                "implication": (
                    "the two measurements describe different populations, so the "
                    "movement is not a like-for-like comparison"
                ),
            },
            detected_at=ctx.detected_at,
            threshold_basis=basis,
        )
    ]
    confounders.extend(_resolution_event_confounders(ctx))
    return confounders


def _resolution_event_confounders(
    ctx: ConfounderContext,
) -> List[Confounder]:
    """Surface 2.0-B2 entity merge/unmerge events touching this population.

    A resolution event is a legitimate reason the population changed — and
    precisely because it is legitimate it must be labelled, or it reads as organic
    drift in the estate.
    """
    events = [e for e in (ctx.entity_resolution_events or ()) if isinstance(e, Mapping)]
    if not events:
        return []
    return [
        Confounder(
            type=CONFOUNDER_CI_POPULATION_CHANGE,
            severity=SEVERITY_ADVISORY,
            label=(
                f"{len(events)} entity resolution event(s) changed the population "
                "between measurements"
            ),
            detail={
                "reason": "entity_resolution",
                "eventCount": len(events),
                "events": [
                    {
                        "kind": str(e.get("kind") or "unknown"),
                        "entityKey": e.get("entityKey"),
                        "occurredAt": e.get("occurredAt"),
                    }
                    for e in events[:10]
                ],
                "implication": (
                    "an entity merge or unmerge legitimately changes the population; "
                    "it is labelled here so it is not read as organic drift"
                ),
            },
            detected_at=ctx.detected_at,
            threshold_basis=ctx.config.basis_for("ci_population"),
        )
    ]


# --------------------------------------------------------------------------
# Detector 4 — seasonality window mismatch
# --------------------------------------------------------------------------


def detect_seasonality_mismatch(ctx: ConfounderContext) -> Sequence[Confounder]:
    """The two windows sit in different parts of the year.

    The window definition comes from CONFIG, not from a hardcoded calendar: a
    fixed assumption is wrong for a large share of the customer base — retail,
    education, public-sector fiscal years and southern-hemisphere operations all
    disagree about what "the same part of the year" means. An operation with no
    seasonal pattern sets ``mode: disabled``.
    """
    cfg = ctx.config.seasonality
    if not cfg.enabled:
        return []

    basis = ctx.config.basis_for("seasonality")
    baseline_window = (ctx.movement.get("baseline") or {}).get("window") or {}
    current_window = (ctx.movement.get("current") or {}).get("window") or {}

    b_start = _parse_dt(baseline_window.get("startedAt"))
    b_end = _parse_dt(baseline_window.get("endedAt"))
    c_start = _parse_dt(current_window.get("startedAt"))
    c_end = _parse_dt(current_window.get("endedAt"))
    if not all((b_start, b_end, c_start, c_end)):
        return []

    if cfg.mode == SEASONALITY_CALENDAR_MONTH:
        baseline_set = _months_covered(b_start, b_end)
        current_set = _months_covered(c_start, c_end)
        unit = "calendar_month"
    elif cfg.mode == SEASONALITY_FISCAL_QUARTER:
        baseline_set = _fiscal_quarters(b_start, b_end, cfg.fiscal_year_start_month)
        current_set = _fiscal_quarters(c_start, c_end, cfg.fiscal_year_start_month)
        unit = "fiscal_quarter"
    elif cfg.mode == SEASONALITY_DECLARED_PERIODS:
        baseline_set = _declared_periods(b_start, b_end, cfg.declared_periods)
        current_set = _declared_periods(c_start, c_end, cfg.declared_periods)
        unit = "declared_period"
    else:  # pragma: no cover - guarded by config validation
        return []

    if not baseline_set or not current_set:
        return []

    union = baseline_set | current_set
    overlap = len(baseline_set & current_set) / len(union) if union else 1.0
    if overlap >= cfg.min_month_overlap:
        return []

    return [
        Confounder(
            type=CONFOUNDER_SEASONALITY_MISMATCH,
            severity=(
                SEVERITY_MATERIAL if overlap == 0 else SEVERITY_ADVISORY
            ),
            label=f"Windows share only {overlap:.0%} of their {unit.replace('_', ' ')}s",
            detail={
                "mode": cfg.mode,
                "unit": unit,
                "overlapFraction": round(overlap, 6),
                "minOverlapThreshold": cfg.min_month_overlap,
                "baselineUnits": sorted(str(u) for u in baseline_set),
                "currentUnits": sorted(str(u) for u in current_set),
                "fiscalYearStartMonth": cfg.fiscal_year_start_month,
                "implication": (
                    "the two windows cover different parts of the year, so seasonal "
                    "variation may account for part of the movement"
                ),
            },
            detected_at=ctx.detected_at,
            threshold_basis=basis,
        )
    ]


# --------------------------------------------------------------------------
# The one entry point
# --------------------------------------------------------------------------


def detect_confounders(
    *,
    org_id: str,
    opportunity_identity: str,
    baseline: Mapping[str, Any],
    movement: Mapping[str, Any],
    baseline_entity_keys: Optional[Sequence[str]] = None,
    current_entity_keys: Optional[Sequence[str]] = None,
    entity_resolution_events: Sequence[Mapping[str, Any]] = (),
    config: Optional[ConfounderConfig] = None,
    detected_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run every registered detector and return their caveats.

    The comparison engine calls this once and never learns which detectors exist.

    A detector that raises is logged and skipped: one broken detector must not
    cost the measurement its other caveats, and it must certainly not block the
    measurement — the alternative would be a result published with FEWER caveats
    than were actually detectable, which is the failure this whole subtask exists
    to prevent.
    """
    ctx = ConfounderContext(
        org_id=org_id,
        opportunity_identity=opportunity_identity,
        detected_at=detected_at or _now_iso(),
        config=config or load_confounder_config(),
        baseline=baseline or {},
        movement=movement or {},
        baseline_entity_keys=baseline_entity_keys,
        current_entity_keys=current_entity_keys,
        entity_resolution_events=tuple(entity_resolution_events or ()),
    )

    found: List[Dict[str, Any]] = []
    for name in registered_confounder_detectors():
        detector = _REGISTRY[name]
        try:
            results = detector(ctx) or ()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Confounder detector %r failed for %s (skipped): %s",
                name,
                opportunity_identity,
                exc,
            )
            continue
        for confounder in results:
            if isinstance(confounder, Confounder):
                found.append(confounder.to_dict())
    return found


def summarise_confounders(confounders: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Counts by type and severity — what T6's portfolio aggregate needs.

    An aggregate that hides how many of its inputs were caveated is exactly the
    kind of number that destroys trust when a customer audits it, so the counts
    travel with the record rather than being recomputed by each consumer.
    """
    by_type: Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    for confounder in confounders or ():
        by_type[confounder.get("type", "unknown")] = (
            by_type.get(confounder.get("type", "unknown"), 0) + 1
        )
        by_severity[confounder.get("severity", "unknown")] = (
            by_severity.get(confounder.get("severity", "unknown"), 0) + 1
        )
    return {
        "count": len(confounders or ()),
        "materialCount": by_severity.get(SEVERITY_MATERIAL, 0),
        "advisoryCount": by_severity.get(SEVERITY_ADVISORY, 0),
        "byType": by_type,
        "types": sorted(by_type),
    }


# The four in-scope detectors. Registered here, at import, so the comparison
# engine never enumerates them.
register_confounder_detector(
    CONFOUNDER_PACK_VERSION_CHANGE, detect_pack_version_change, replace=True
)
register_confounder_detector(
    CONFOUNDER_VOLUME_SHIFT, detect_volume_shift, replace=True
)
register_confounder_detector(
    CONFOUNDER_CI_POPULATION_CHANGE, detect_ci_population_change, replace=True
)
register_confounder_detector(
    CONFOUNDER_SEASONALITY_MISMATCH, detect_seasonality_mismatch, replace=True
)


__all__ = [
    "CONFOUNDER_CI_POPULATION_CHANGE",
    "CONFOUNDER_PACK_VERSION_CHANGE",
    "CONFOUNDER_SCHEMA_VERSION",
    "CONFOUNDER_SEASONALITY_MISMATCH",
    "CONFOUNDER_VOLUME_SHIFT",
    "Confounder",
    "ConfounderContext",
    "detect_ci_population_change",
    "detect_confounders",
    "detect_pack_version_change",
    "detect_seasonality_mismatch",
    "detect_volume_shift",
    "register_confounder_detector",
    "registered_confounder_detectors",
    "summarise_confounders",
    "unregister_confounder_detector",
]
