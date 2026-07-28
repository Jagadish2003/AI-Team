"""2.0-A1 T1 — the projection model per opportunity.

Pure and deterministic: no DB access, no ``app`` import, no LLM, no clock read.
Everything a projection says is computed from values already on the stored
opportunity (its ``_debug.score_debug`` / raw evidence numbers, its confidence
and corroboration status, and its temporal fields when temporal enrichment has
run).  The same input always yields the same projection — that is 2.0-A1 AC5,
and it is what lets 2.0-A2 compare a stored projection against a later
measurement.

The discipline, restated because it governs every line here:

    A projection is a DIRECTION and a MAGNITUDE BAND on specific MEASURED
    signals — never a point estimate, never a guarantee, never a savings claim.

Band width is a deterministic function of three inputs (2.0-A1 AC2):

    1. sample size        — how many observed instances the finding rests on
    2. recurrence stability — steady vs bursty, from temporal history
    3. corroboration status — single-source vs corroborated vs triple

Thinner evidence widens the band; it never narrows it.  Width is never a
hand-set number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .signal_registry import (
    SIGNAL_CONCEPTS,
    DetectorSignalProfile,
    get_detector_profile,
)

#: Bumped when the projection computation changes in a way that makes a stored
#: projection non-comparable with a freshly computed one.  2.0-A2 reads this to
#: know whether a stored projection is still comparable to a new measurement.
PROJECTION_SCHEMA_VERSION = "1.0.0"

# --- Direction -------------------------------------------------------------

DIRECTION_IMPROVES = "improves"
DIRECTION_NO_MATERIAL_CHANGE = "no_material_change"

# --- Observation horizon ---------------------------------------------------

HORIZON_30 = 30
HORIZON_60 = 60
HORIZON_90 = 90

#: Roadmap stage -> horizon days.  A finding staged for the next 30 days is
#: observable sooner than one staged at 90; the horizon follows the stage the
#: scorer already assigned rather than inventing a second schedule.
_STAGE_HORIZON: Dict[str, int] = {
    "NEXT_30": HORIZON_30,
    "NEXT_60": HORIZON_60,
    "NEXT_90": HORIZON_90,
}

# --- Band-width model ------------------------------------------------------
#
# The band is centred on a base midpoint and widened by three deterministic
# penalties.  All values are fractions of the affected instance population.

#: Midpoint of the band before any widening — the share of identified recurring
#: instances an agent is expected to handle when evidence is strong.  Chosen to
#: sit well below "all of them": an agent handles the identified cases, and the
#: residual requires judgment.
_BASE_MIDPOINT = 0.40

#: Half-width at full evidence strength (strong sample, steady, corroborated).
_MIN_HALF_WIDTH = 0.15

#: Half-width added when the evidence is at its weakest on all three axes.
_MAX_ADDITIONAL_HALF_WIDTH = 0.30

#: Hard bounds on the band.  A projection never claims 0% (that is
#: "no material change", a different direction) and never claims 100%.
_BAND_FLOOR = 0.05
_BAND_CEILING = 0.90

# Sample-size tiers (count of observed instances behind the finding).
_SAMPLE_STRONG = 100
_SAMPLE_MODERATE = 30
_SAMPLE_THIN = 10

#: Below this many observed instances the finding is too thin to project a
#: direction of improvement at all.
_MIN_INSTANCES_FOR_DIRECTION = 3

#: Coefficient-of-variation thresholds for recurrence stability, computed from
#: the temporal ``recent_values`` series.
_CV_STEADY = 0.25
_CV_VARIABLE = 0.60

# Per-axis widening contributions.  Each is a fraction of
# _MAX_ADDITIONAL_HALF_WIDTH, and the three weights sum to 1.0.
_W_SAMPLE = 0.40
_W_STABILITY = 0.30
_W_CORROBORATION = 0.30

_STABILITY_STEADY = "steady"
_STABILITY_VARIABLE = "variable"
_STABILITY_BURSTY = "bursty"
_STABILITY_UNKNOWN = "unknown"

_CORROBORATION_TRIPLE = "triple"
_CORROBORATION_CORROBORATED = "corroborated"
_CORROBORATION_SUPPORTING_ONLY = "supporting_only"
_CORROBORATION_SINGLE_SOURCE = "single_source"

#: The two non-elevating corroboration rules — the 2.0-A1 AC4 capped-confidence
#: case.  They are NOT interchangeable: COR-08 means the finding stands on ONE
#: source (nothing corroborates it), while COR-05 means a conversation source
#: (Slack/Teams) corroborates it but cannot elevate confidence on its own.
#: COR-08 is therefore the weaker state and must win when both are present.
_RULE_SINGLE_SOURCE = "COR-08"
_RULE_SUPPORTING_ONLY = "COR-05"
_NON_ELEVATING_RULE_IDS = frozenset({_RULE_SUPPORTING_ONLY, _RULE_SINGLE_SOURCE})

#: Substrings the corroboration engine puts in a source label when the source
#: cannot elevate confidence on its own.
_NON_ELEVATING_SOURCE_MARKERS = ("supporting only", "single source")

# Axis penalty tables — a plain lookup keeps the computation auditable.
_SAMPLE_PENALTY = {
    "strong": 0.0,
    "moderate": 0.35,
    "thin": 0.70,
    "minimal": 1.0,
}
_STABILITY_PENALTY = {
    _STABILITY_STEADY: 0.0,
    _STABILITY_VARIABLE: 0.50,
    _STABILITY_BURSTY: 1.0,
    _STABILITY_UNKNOWN: 0.70,
}
_CORROBORATION_PENALTY = {
    _CORROBORATION_TRIPLE: 0.0,
    _CORROBORATION_CORROBORATED: 0.30,
    _CORROBORATION_SUPPORTING_ONLY: 0.85,
    _CORROBORATION_SINGLE_SOURCE: 1.0,
}


# --------------------------------------------------------------------------
# Result shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MagnitudeBand:
    """A magnitude band expressed as a share of affected instances.

    Never a point estimate: ``low_pct`` is always strictly below ``high_pct``.
    """

    low_pct: int
    high_pct: int
    basis_unit: str = "of the recurring instances"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lowPct": self.low_pct,
            "highPct": self.high_pct,
            "basisUnit": self.basis_unit,
            "label": self.label,
        }

    @property
    def label(self) -> str:
        return f"{self.low_pct}–{self.high_pct}% {self.basis_unit}"


@dataclass(frozen=True)
class ProjectedSignal:
    """The measured signal that should move if the agent is implemented."""

    concept: str
    concept_label: str
    signal_name: str
    unit: str
    current_value: Optional[float] = None
    direction_of_improvement: str = "decrease"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concept": self.concept,
            "conceptLabel": self.concept_label,
            "signalName": self.signal_name,
            "unit": self.unit,
            "currentValue": self.current_value,
            "directionOfImprovement": self.direction_of_improvement,
        }


@dataclass(frozen=True)
class Projection:
    """A per-opportunity intervention projection.

    Every field below is required by 2.0-A1 AC1; a projection missing any part
    fails the contract test.
    """

    direction: str
    magnitude_band: Optional[MagnitudeBand]
    observation_horizon_days: int
    manual_step_replaced: str
    movement_signal: ProjectedSignal
    affected_signals: List[ProjectedSignal] = field(default_factory=list)
    basis: Dict[str, Any] = field(default_factory=dict)
    band_width_inputs: Dict[str, Any] = field(default_factory=dict)
    confidence_capped: bool = False
    schema_version: str = PROJECTION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "direction": self.direction,
            "magnitudeBand": (
                self.magnitude_band.to_dict() if self.magnitude_band else None
            ),
            "observationHorizonDays": self.observation_horizon_days,
            "manualStepReplaced": self.manual_step_replaced,
            "movementSignal": self.movement_signal.to_dict(),
            "affectedSignals": [s.to_dict() for s in self.affected_signals],
            "basis": dict(self.basis),
            "bandWidthInputs": dict(self.band_width_inputs),
            "confidenceCapped": self.confidence_capped,
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _safe_float(value: Any) -> Optional[float]:
    """Coerce to float, rejecting bools and non-numerics."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return result


def _raw_evidence(opp: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the detector's raw evidence numbers, wherever they survived.

    The Track A adapter drops ``raw_evidence`` but keeps ``score_debug`` under
    ``_debug``.  Callers inside the pipeline (which still hold the runner-shaped
    opportunity) get the real thing; a stored opportunity falls back to what the
    adapter kept.
    """
    for candidate in (
        opp.get("raw_evidence"),
        (opp.get("_debug") or {}).get("raw_evidence"),
    ):
        if isinstance(candidate, Mapping) and candidate:
            return dict(candidate)
    return {}


def _lookup_signal(raw: Mapping[str, Any], name: Optional[str]) -> Optional[float]:
    if not name:
        return None
    return _safe_float(raw.get(name))


def _detector_id(opp: Mapping[str, Any]) -> str:
    debug = opp.get("_debug") or {}
    return str(opp.get("detector_id") or debug.get("detector_id") or "")


def _metric_value(opp: Mapping[str, Any]) -> Optional[float]:
    debug = opp.get("_debug") or {}
    for candidate in (
        opp.get("current_value"),
        opp.get("metric_value"),
        debug.get("metric_value"),
    ):
        value = _safe_float(candidate)
        if value is not None:
            return value
    return None


def _sample_tier(instance_count: Optional[float]) -> str:
    if instance_count is None:
        return "minimal"
    if instance_count >= _SAMPLE_STRONG:
        return "strong"
    if instance_count >= _SAMPLE_MODERATE:
        return "moderate"
    if instance_count >= _SAMPLE_THIN:
        return "thin"
    return "minimal"


def _recurrence_stability(recent_values: Sequence[Any]) -> str:
    """Classify recurrence stability from the temporal value series.

    Steady vs bursty is the coefficient of variation of the observed series.
    Fewer than three observations is honestly ``unknown`` rather than assumed
    steady — assuming steady would narrow the band on absent evidence.
    """
    values = [v for v in (_safe_float(x) for x in recent_values or ()) if v is not None]
    if len(values) < 3:
        return _STABILITY_UNKNOWN
    mean = sum(values) / len(values)
    if mean <= 0:
        return _STABILITY_UNKNOWN
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    cv = (variance**0.5) / mean
    if cv <= _CV_STEADY:
        return _STABILITY_STEADY
    if cv <= _CV_VARIABLE:
        return _STABILITY_VARIABLE
    return _STABILITY_BURSTY


def _corroboration_state(opp: Mapping[str, Any]) -> str:
    """Classify corroboration status for band-width purposes.

    Mirrors the states ``corroboration_engine`` can produce, read defensively so
    a legacy stored opportunity without the ENT-2 fields is treated as
    single-source (the widest band) rather than crashing or being flattered.
    """
    if bool(opp.get("triple_corroboration")):
        return _CORROBORATION_TRIPLE

    sources = opp.get("corroboration_sources")
    sources = [str(s) for s in sources] if isinstance(sources, (list, tuple)) else []

    rule_ids = opp.get("corroboration_rule_ids")
    rule_ids = (
        {str(r).strip().upper() for r in rule_ids}
        if isinstance(rule_ids, (list, tuple))
        else set()
    )

    elevating_sources = [
        s
        for s in sources
        if not any(marker in s.lower() for marker in _NON_ELEVATING_SOURCE_MARKERS)
    ]

    # COR-08 is the single-source rule: nothing corroborates the finding at all.
    # Checked first so it is never flattered into the (stronger) supporting-only
    # state when the engine stamps it alongside a conversation rule.
    if _RULE_SINGLE_SOURCE in rule_ids and not elevating_sources:
        return _CORROBORATION_SINGLE_SOURCE
    if rule_ids and rule_ids <= _NON_ELEVATING_RULE_IDS:
        return _CORROBORATION_SUPPORTING_ONLY
    if not elevating_sources:
        # Sources present but all non-elevating => supporting only.
        return (
            _CORROBORATION_SUPPORTING_ONLY if sources else _CORROBORATION_SINGLE_SOURCE
        )
    return _CORROBORATION_CORROBORATED


def _is_confidence_capped(opp: Mapping[str, Any], corroboration_state: str) -> bool:
    """True when the finding's confidence is capped for want of corroboration.

    2.0-A1 AC4: such a projection is labelled, and its band is widened by the
    corroboration axis so it cannot out-rank a corroborated equivalent on
    projection strength alone.
    """
    if corroboration_state in (
        _CORROBORATION_SINGLE_SOURCE,
        _CORROBORATION_SUPPORTING_ONLY,
    ):
        return True
    return str(opp.get("confidence", "")).strip().upper() == "LOW"


def _horizon_days(opp: Mapping[str, Any], stability: str) -> int:
    """Observation horizon in days — 30 / 60 / 90.

    Starts from the roadmap stage the scorer already assigned, then extends one
    step when recurrence is bursty or unknown: a signal that moves erratically
    needs a longer window before movement is distinguishable from noise.
    """
    debug = opp.get("_debug") or {}
    stage = str(opp.get("roadmap_stage") or debug.get("roadmap_stage") or "").upper()
    base = _STAGE_HORIZON.get(stage, HORIZON_60)
    if stability in (_STABILITY_BURSTY, _STABILITY_UNKNOWN):
        if base == HORIZON_30:
            return HORIZON_60
        return HORIZON_90
    return base


#: What the band's percentages are a share OF, per movement-signal unit. A band
#: must never describe itself as a share of "recurring instances" when the signal
#: it moves is a rate or a duration — that would misstate what was measured.
_BASIS_UNIT_BY_SIGNAL_UNIT = {
    "count": "of the recurring instances",
    "days": "of the observed delay",
    "hours": "of the observed delay",
    "ratio": "of the observed rate",
    "pct": "of the observed rate",
}


def _band(
    sample_tier: str,
    stability: str,
    corroboration_state: str,
    signal_unit: str = "count",
) -> MagnitudeBand:
    """Compute the magnitude band deterministically from the three axes."""
    penalty = (
        _W_SAMPLE * _SAMPLE_PENALTY[sample_tier]
        + _W_STABILITY * _STABILITY_PENALTY[stability]
        + _W_CORROBORATION * _CORROBORATION_PENALTY[corroboration_state]
    )
    half_width = _MIN_HALF_WIDTH + _MAX_ADDITIONAL_HALF_WIDTH * penalty

    low = max(_BAND_FLOOR, _BASE_MIDPOINT - half_width)
    high = min(_BAND_CEILING, _BASE_MIDPOINT + half_width)

    low_pct = int(round(low * 100))
    high_pct = int(round(high * 100))
    if high_pct <= low_pct:  # never collapse to a point estimate
        high_pct = low_pct + 1
    return MagnitudeBand(
        low_pct=low_pct,
        high_pct=high_pct,
        basis_unit=_BASIS_UNIT_BY_SIGNAL_UNIT.get(
            signal_unit, "of the recurring instances"
        ),
    )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def build_projection(opp: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Build the projection for one opportunity, or None if not projectable.

    Returns a plain dict (API-shaped, camelCase) so it can be stored directly on
    the opportunity record and served without translation.

    ``None`` is returned — deliberately, rather than a hedged projection — when
    the detector has no signal profile or the finding carries no measured
    instance count.  Silence is honest; a projection about an unmeasured signal
    is not.
    """
    detector_id = _detector_id(opp)
    profile = get_detector_profile(detector_id)
    if profile is None:
        return None

    raw = _raw_evidence(opp)

    instance_count = _lookup_signal(raw, profile.instance_field)
    volume_count = _lookup_signal(raw, profile.volume_signal)
    movement_value = _lookup_signal(raw, profile.movement_signal)
    if movement_value is None:
        # The primary metric IS the movement signal for most detectors.
        movement_value = _metric_value(opp)

    # A ratio/percentage is a RATE, not a population — it can never serve as a
    # sample size or an instance count. Several detectors project a rate
    # (breach rate, surge-vs-baseline, top-author share); counting "0.42" as
    # 0.42 observed instances would read a real finding as minimal-evidence and
    # wrongly flip its direction to "no material change".
    if profile.unit in ("ratio", "pct"):
        if profile.instance_field == profile.movement_signal:
            instance_count = None
        if profile.volume_signal == profile.movement_signal:
            volume_count = None

    # Sample size is the observed population the finding rests on: prefer the
    # denominator (how many things were looked at), then the affected count, then
    # the primary metric — the last only when it is itself a count.
    sample_size = volume_count
    if sample_size is None:
        sample_size = instance_count
    if sample_size is None and profile.unit not in ("ratio", "pct"):
        sample_size = _metric_value(opp)

    stability = _recurrence_stability(opp.get("recent_values") or ())
    corroboration_state = _corroboration_state(opp)
    sample_tier = _sample_tier(sample_size)
    confidence_capped = _is_confidence_capped(opp, corroboration_state)
    horizon = _horizon_days(opp, stability)

    affected = _affected_signals(profile, raw, movement_value)
    movement_signal = affected[0]

    # Direction. A finding whose measured instance COUNT is below the minimum
    # cannot honestly claim improvement — it projects no material change, with no
    # band at all rather than a wide one.
    #
    # A rate-based finding (breach rate, surge ratio) legitimately has no
    # countable population on the record; the detector firing on a crossed
    # threshold IS its evidence. Such a finding still projects improvement, but
    # with the widest band the axes allow — its sample tier is "minimal" because
    # the sample genuinely is unknown, not because it is small.
    effective_instances = instance_count if instance_count is not None else sample_size
    rate_only = effective_instances is None and profile.unit in ("ratio", "pct")

    if rate_only:
        direction = DIRECTION_IMPROVES
        band: Optional[MagnitudeBand] = _band(
            sample_tier, stability, corroboration_state, profile.unit
        )
    elif (
        effective_instances is None
        or effective_instances < _MIN_INSTANCES_FOR_DIRECTION
    ):
        direction = DIRECTION_NO_MATERIAL_CHANGE
        band = None
    else:
        direction = DIRECTION_IMPROVES
        band = _band(sample_tier, stability, corroboration_state, profile.unit)

    basis: Dict[str, Any] = {
        "detectorId": detector_id,
        "observedInstances": _as_number(instance_count),
        "observedPopulation": _as_number(volume_count),
        "instanceSignal": profile.instance_field,
        "populationSignal": profile.volume_signal,
        "baselineMean": _safe_float(opp.get("baseline_mean")),
        "baselineStddev": _safe_float(opp.get("baseline_stddev")),
        "baselineWindowDays": _as_int(opp.get("baseline_window_days")),
        "observedRunCount": _as_int(opp.get("run_count")),
        "signalKey": opp.get("signal_key"),
        "confidence": str(opp.get("confidence", "")).strip().upper() or None,
        "corroborationStatus": corroboration_state,
        "corroborationSources": list(opp.get("corroboration_sources") or []),
        "packId": opp.get("packId") or opp.get("pack_id"),
        "packVersion": opp.get("packVersion"),
        "evidenceIds": list(opp.get("evidenceIds") or []),
    }

    band_width_inputs = {
        "sampleTier": sample_tier,
        "sampleSize": _as_number(sample_size),
        "recurrenceStability": stability,
        "corroborationStatus": corroboration_state,
        "thinEvidence": sample_tier in ("thin", "minimal")
        or stability in (_STABILITY_BURSTY, _STABILITY_UNKNOWN)
        or confidence_capped,
    }

    projection = Projection(
        direction=direction,
        magnitude_band=band,
        observation_horizon_days=horizon,
        manual_step_replaced=profile.manual_step,
        movement_signal=movement_signal,
        affected_signals=affected,
        basis=basis,
        band_width_inputs=band_width_inputs,
        confidence_capped=confidence_capped,
    )
    return projection.to_dict()


def _affected_signals(
    profile: DetectorSignalProfile,
    raw: Mapping[str, Any],
    movement_value: Optional[float],
) -> List[ProjectedSignal]:
    """The measured signals this finding touches, movement signal first."""
    direction_of_improvement = "decrease" if profile.lower_is_better else "increase"
    primary = ProjectedSignal(
        concept=profile.concept,
        concept_label=SIGNAL_CONCEPTS.get(profile.concept, profile.concept),
        signal_name=profile.movement_signal,
        unit=profile.unit,
        current_value=movement_value,
        direction_of_improvement=direction_of_improvement,
    )
    signals = [primary]

    # The instance and population signals are also touched, when they are
    # distinct fields the detector actually measured.
    for name, concept in (
        (profile.instance_field, profile.concept),
        (profile.volume_signal, profile.concept),
    ):
        if not name or name == profile.movement_signal:
            continue
        if any(s.signal_name == name for s in signals):
            continue
        signals.append(
            ProjectedSignal(
                concept=concept,
                concept_label=SIGNAL_CONCEPTS.get(concept, concept),
                signal_name=name,
                unit="count",
                current_value=_lookup_signal(raw, name),
                direction_of_improvement=direction_of_improvement,
            )
        )
    return signals


def _as_number(value: Optional[float]) -> Optional[float]:
    """Render a float as an int when it is integral, for stable JSON."""
    if value is None:
        return None
    return int(value) if float(value).is_integer() else value


def _as_int(value: Any) -> Optional[int]:
    number = _safe_float(value)
    return int(number) if number is not None else None


def project_opportunities(opps: Sequence[Any]) -> int:
    """Attach ``projection`` to each opportunity in place. Non-blocking.

    Mirrors ``temporal_enrichment.enrich_opportunities_with_temporal_context``:
    an opportunity that cannot be projected is left untouched rather than
    dropped, and no exception escapes — a projection failure must never lose an
    opportunity or fail a run.

    Returns the number of opportunities that received a projection.
    """
    projected = 0
    for opp in opps or ():
        if not isinstance(opp, dict):
            continue
        try:
            result = build_projection(opp)
        except Exception:  # noqa: BLE001 - never break a run over a projection
            continue
        if result is not None:
            opp["projection"] = result
            projected += 1
    return projected


__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "DIRECTION_IMPROVES",
    "DIRECTION_NO_MATERIAL_CHANGE",
    "HORIZON_30",
    "HORIZON_60",
    "HORIZON_90",
    "MagnitudeBand",
    "ProjectedSignal",
    "Projection",
    "build_projection",
    "project_opportunities",
]
