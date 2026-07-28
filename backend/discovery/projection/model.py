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

Band width is a deterministic function of four evidence inputs (2.0-A1 AC2/AC4):

    1. sample size          — how many observed instances the finding rests on
    2. recurrence stability — steady vs bursty, from temporal history
    3. corroboration status — single-source vs corroborated vs triple
    4. confidence cap status — is confidence capped for want of corroboration

Thinner evidence widens the band; it never narrows it.  Width is never a
hand-set number.  The whole computation lives in ``band_width.py`` — this module
decides WHICH measured values feed it and never computes a width itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .band_width import (
    BAND_WIDTH_MODEL_VERSION,
    MIN_INSTANCES_FOR_DIRECTION,
    NO_BAND_STRENGTH_LABEL,
    STABILITY_BURSTY,
    STABILITY_UNKNOWN,
    BandWidth,
    band_width_inputs_from_opportunity,
    compute_band_width,
)
from .signal_registry import (
    SIGNAL_CONCEPTS,
    DetectorSignalProfile,
    get_detector_profile,
)

#: Bumped when the projection computation changes in a way that makes a stored
#: projection non-comparable with a freshly computed one.  2.0-A2 reads this to
#: know whether a stored projection is still comparable to a new measurement.
#: 1.1.0 — T4 moved band width onto the four-input deterministic model, so bands
#: computed before it are not comparable with bands computed after it.
PROJECTION_SCHEMA_VERSION = "1.1.0"

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
# Band width is NOT computed here.  ``band_width.py`` owns it end to end — the
# four evidence inputs, the penalty tables, the weights, the geometry, and the
# projection-strength scalar.  This module's only job on that front is to decide
# which of a detector's measured fields is a countable sample size (a rate is
# not a population), which is signal-profile knowledge the width model must not
# need.
#
# Only the values this module still reasons about are named here; the tiers and
# corroboration states themselves belong to the width model.

_MIN_INSTANCES_FOR_DIRECTION = MIN_INSTANCES_FOR_DIRECTION
_STABILITY_BURSTY = STABILITY_BURSTY
_STABILITY_UNKNOWN = STABILITY_UNKNOWN


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
class ProjectionAssumption:
    """One explicit assumption carried with the projection."""

    id: str
    label: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
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
    assumption_ledger: List[ProjectionAssumption]
    affected_signals: List[ProjectedSignal] = field(default_factory=list)
    basis: Dict[str, Any] = field(default_factory=dict)
    band_width_inputs: Dict[str, Any] = field(default_factory=dict)
    confidence_capped: bool = False
    #: T4 — the full band-width derivation (inputs, per-axis drivers, labels).
    #: None when the finding carries no band at all.
    band_width: Optional[Dict[str, Any]] = None
    #: T4 — the comparable projection-strength scalar and its cap label.
    projection_strength: Optional[Dict[str, Any]] = None
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
            "assumptionLedger": [a.to_dict() for a in self.assumption_ledger],
            "affectedSignals": [s.to_dict() for s in self.affected_signals],
            "basis": dict(self.basis),
            "bandWidthInputs": dict(self.band_width_inputs),
            "bandWidth": dict(self.band_width) if self.band_width else None,
            "projectionStrength": (
                dict(self.projection_strength) if self.projection_strength else None
            ),
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


# The four input classifiers live in ``band_width`` and are reached through
# ``band_width_inputs_from_opportunity``, so the width model and the projection
# payload can never disagree about what "thin" or "single source" means.


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


def _band(band_width: BandWidth, signal_unit: str = "count") -> MagnitudeBand:
    """Render the computed band width as the analyst-facing magnitude band.

    Pure presentation: the numbers are decided in ``band_width.py``; the only
    thing chosen here is what the percentages are a share OF, which depends on
    the movement signal's unit.
    """
    return MagnitudeBand(
        low_pct=band_width.low_pct,
        high_pct=band_width.high_pct,
        basis_unit=_BASIS_UNIT_BY_SIGNAL_UNIT.get(
            signal_unit, "of the recurring instances"
        ),
    )


def _assumption_ledger(
    manual_step_replaced: str,
    movement_signal: ProjectedSignal,
    horizon_days: int,
) -> List[ProjectionAssumption]:
    """Build the explicit assumptions every projection must carry."""
    signal_label = movement_signal.concept_label.lower()
    return [
        ProjectionAssumption(
            id="agent_handles_identified_cases",
            label="Agent handles the identified recurring cases",
            description=(
                "The projection assumes the agent handles the cases represented "
                f"by this finding and takes over the manual step: {manual_step_replaced}."
            ),
        ),
        ProjectionAssumption(
            id="adoption_complete_for_cases",
            label="Adoption is complete for those cases",
            description=(
                "The projection applies after the identified cases are routed "
                "through the agent path instead of the current manual path."
            ),
        ),
        ProjectionAssumption(
            id="upstream_volume_within_observed_range",
            label="Upstream volume remains within its observed range",
            description=(
                "The projection assumes incoming volume stays comparable to the "
                "observed baseline and does not materially change the case mix."
            ),
        ),
        ProjectionAssumption(
            id="residual_requires_human_judgement",
            label="Residual cases still require human judgement",
            description=(
                "The projection does not assume the agent handles exceptions, "
                "ambiguous cases, or work that still needs human review."
            ),
        ),
        ProjectionAssumption(
            id="limited_to_signal_and_horizon",
            label="Projection applies only to the measured signal and horizon shown",
            description=(
                f"The projection is limited to {signal_label} over the "
                f"{horizon_days}-day observation horizon shown with this opportunity."
            ),
        ),
    ]


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

    # The four band-width inputs, and the band they deterministically produce.
    # Computed once, up front: both the band and every label derived from it
    # (evidence strength, projection strength, the thin-evidence flag) read from
    # this single result, so a rendered label can never disagree with the band
    # it sits beside.
    width_inputs = band_width_inputs_from_opportunity(opp, sample_size)
    band_width = compute_band_width(width_inputs)

    stability = width_inputs.recurrence_stability
    corroboration_state = width_inputs.corroboration_status
    sample_tier = width_inputs.sample_tier
    confidence_capped = width_inputs.confidence_capped
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
        band: Optional[MagnitudeBand] = _band(band_width, profile.unit)
    elif (
        effective_instances is None
        or effective_instances < _MIN_INSTANCES_FOR_DIRECTION
    ):
        direction = DIRECTION_NO_MATERIAL_CHANGE
        band = None
    else:
        direction = DIRECTION_IMPROVES
        band = _band(band_width, profile.unit)

    baseline_mean = _safe_float(opp.get("baseline_mean"))
    baseline_window_days = _as_int(opp.get("baseline_window_days"))
    thin_evidence = band_width.thin_evidence

    band_width_inputs = {
        **width_inputs.to_dict(),
        "thinEvidence": thin_evidence,
    }

    # A finding with no band has no width to explain and no strength to compare.
    # Both blocks are None rather than zeroed: a zero would sort and render as a
    # real, very weak projection instead of an absent one.
    band_width_block = band_width.to_dict() if band is not None else None
    strength_block = band_width.strength_to_dict() if band is not None else None
    if strength_block is None:
        strength_block = {
            "value": None,
            "tier": None,
            "label": NO_BAND_STRENGTH_LABEL,
            "capped": confidence_capped,
            "cappedLabel": None,
            "comparableWithCapped": False,
        }

    basis: Dict[str, Any] = {
        "detectorId": detector_id,
        "observedInstances": _as_number(instance_count),
        "observedPopulation": _as_number(volume_count),
        "observationWindowDays": baseline_window_days,
        "instanceSignal": profile.instance_field,
        "populationSignal": profile.volume_signal,
        "signalUsed": {
            "signalName": movement_signal.signal_name,
            "concept": movement_signal.concept,
            "conceptLabel": movement_signal.concept_label,
            "unit": movement_signal.unit,
        },
        "baselineValue": baseline_mean,
        "baselineMean": baseline_mean,
        "baselineStddev": _safe_float(opp.get("baseline_stddev")),
        "baselineWindowDays": baseline_window_days,
        "observedRunCount": _as_int(opp.get("run_count")),
        "signalKey": opp.get("signal_key"),
        "confidence": str(opp.get("confidence", "")).strip().upper() or None,
        "corroborationStatus": corroboration_state,
        "corroborationSources": list(opp.get("corroboration_sources") or []),
        "evidenceStrength": "thin" if thin_evidence else "strong",
        "thinEvidence": thin_evidence,
        # T4 — the analyst-facing evidence label and the band-width tier that
        # produced it, on the basis block so the Opportunity Review basis panel
        # can show the band's width and its reason side by side.
        "evidenceTier": band_width.evidence_tier,
        "evidenceLabel": band_width.evidence_label,
        "bandTier": band_width.band_tier,
        "bandLabel": band_width.band_label,
        "bandWidthRationale": band_width.rationale,
        "bandWidthModelVersion": BAND_WIDTH_MODEL_VERSION,
        "packId": opp.get("packId") or opp.get("pack_id"),
        "packVersion": opp.get("packVersion"),
        "evidenceIds": list(opp.get("evidenceIds") or []),
    }

    projection = Projection(
        direction=direction,
        magnitude_band=band,
        observation_horizon_days=horizon,
        manual_step_replaced=profile.manual_step,
        movement_signal=movement_signal,
        assumption_ledger=_assumption_ledger(
            profile.manual_step, movement_signal, horizon
        ),
        affected_signals=affected,
        basis=basis,
        band_width_inputs=band_width_inputs,
        band_width=band_width_block,
        projection_strength=strength_block,
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
    "BAND_WIDTH_MODEL_VERSION",
    "DIRECTION_IMPROVES",
    "DIRECTION_NO_MATERIAL_CHANGE",
    "HORIZON_30",
    "HORIZON_60",
    "HORIZON_90",
    "MagnitudeBand",
    "ProjectedSignal",
    "ProjectionAssumption",
    "Projection",
    "build_projection",
    "project_opportunities",
]
