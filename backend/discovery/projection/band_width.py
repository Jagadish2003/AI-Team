"""2.0-A1 T4 — deterministic band-width calculation.

This module is the SINGLE source of truth for how wide a projection band is.
Band width comes from **evidence quality**, never from a hand-set number, a
per-pack knob, or an operator-tunable config value — that is 2.0-A1 AC2, and it
is why every constant here is a module-level literal rather than an env var or
a config file entry.  There is deliberately no ``set_band_width(...)`` and no
override parameter anywhere in the public surface.

Four inputs, and only these four:

    1. **sample size**            — how many observed instances the finding rests on
    2. **recurrence stability**   — steady vs bursty, from the temporal series
    3. **corroboration status**   — single-source / supporting-only / corroborated / triple
    4. **confidence cap status**  — is this finding's confidence capped for want
                                    of corroboration (the single-source case)

Each input maps to a penalty in ``[0.0, 1.0]``; the four penalties are combined
with fixed weights that sum to 1.0 into one *evidence penalty*, and the band's
half-width is a straight linear function of it.  So:

    * the same inputs always produce the same band (AC2, AC5);
    * thinner evidence produces a strictly wider band, never a narrower one;
    * stronger corroboration produces a strictly narrower band;
    * a capped-confidence finding is widened on TWO axes (corroboration and the
      cap itself), so it can never present a narrower band than an otherwise
      identical corroborated finding.

**Projection strength** (the comparable scalar the roadmap orders and displays
with) is the inverse of that penalty.  2.0-A1 AC4 requires that a capped
(single-source) finding never out-ranks a corroborated equivalent *on projection
strength alone*, so strength carries a hard structural guard, not just a smaller
number: :func:`projection_rank_key` sorts every capped projection below every
uncapped one regardless of the scalar, and the scalar itself is additionally
clamped to :data:`CAPPED_STRENGTH_CEILING`.  Either mechanism alone would
satisfy AC4; both are present because the scalar is what a UI renders and the
rank key is what an ordering uses, and neither may quietly disagree with the
other.

Pure and dependency-free: no DB, no ``app`` import, no clock read, no LLM, no
randomness.  Everything returned is JSON-ready and rounded, so a stored
projection compares byte-for-byte with a freshly computed one.

The rules are documented for humans in ``docs/projection_band_width.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

#: Bumped when the band-width computation changes in a way that moves a band for
#: unchanged evidence.  Stamped onto every band so 2.0-A2 can tell "the evidence
#: moved" from "the model moved" when it validates a stored projection against a
#: measured outcome.
BAND_WIDTH_MODEL_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Band geometry
# --------------------------------------------------------------------------

#: Midpoint of the band before any widening — the share of identified recurring
#: instances an agent is expected to handle when evidence is strong.  Well below
#: "all of them": an agent handles the identified cases, and the residual
#: requires judgment.
BASE_MIDPOINT = 0.40

#: Half-width at full evidence strength (strong sample, steady, triple
#: corroborated, uncapped).  Even the best-evidenced projection is a band.
MIN_HALF_WIDTH = 0.15

#: Half-width added when the evidence is at its weakest on all four axes.
MAX_ADDITIONAL_HALF_WIDTH = 0.30

#: Hard bounds on the band.  A projection never claims 0% (that is
#: "no material change", a different direction) and never claims 100%.
BAND_FLOOR = 0.05
BAND_CEILING = 0.90

# --------------------------------------------------------------------------
# Axis 1 — sample size
# --------------------------------------------------------------------------

SAMPLE_STRONG = 100
SAMPLE_MODERATE = 30
SAMPLE_THIN = 10

SAMPLE_TIER_STRONG = "strong"
SAMPLE_TIER_MODERATE = "moderate"
SAMPLE_TIER_THIN = "thin"
SAMPLE_TIER_MINIMAL = "minimal"

#: Below this many observed instances the finding is too thin to project a
#: direction of improvement at all.  Exported so the projection model and this
#: module cannot disagree about what "too thin to project" means.
MIN_INSTANCES_FOR_DIRECTION = 3

# --------------------------------------------------------------------------
# Axis 2 — recurrence stability
# --------------------------------------------------------------------------

#: Coefficient-of-variation thresholds for recurrence stability, computed from
#: the temporal ``recent_values`` series.
CV_STEADY = 0.25
CV_VARIABLE = 0.60

STABILITY_STEADY = "steady"
STABILITY_VARIABLE = "variable"
STABILITY_BURSTY = "bursty"
STABILITY_UNKNOWN = "unknown"

#: Fewer than this many observations cannot distinguish steady from bursty.
MIN_OBSERVATIONS_FOR_STABILITY = 3

# --------------------------------------------------------------------------
# Axis 3 — corroboration status
# --------------------------------------------------------------------------

CORROBORATION_TRIPLE = "triple"
CORROBORATION_CORROBORATED = "corroborated"
CORROBORATION_SUPPORTING_ONLY = "supporting_only"
CORROBORATION_SINGLE_SOURCE = "single_source"

#: The two non-elevating corroboration rules — the 2.0-A1 AC4 capped-confidence
#: case.  They are NOT interchangeable: COR-08 means the finding stands on ONE
#: source (nothing corroborates it), while COR-05 means a conversation source
#: (Slack/Teams) corroborates it but cannot elevate confidence on its own.
#: COR-08 is therefore the weaker state and must win when both are present.
RULE_SINGLE_SOURCE = "COR-08"
RULE_SUPPORTING_ONLY = "COR-05"
NON_ELEVATING_RULE_IDS = frozenset({RULE_SUPPORTING_ONLY, RULE_SINGLE_SOURCE})

#: Substrings the corroboration engine puts in a source label when the source
#: cannot elevate confidence on its own.
NON_ELEVATING_SOURCE_MARKERS = ("supporting only", "single source")

#: Corroboration states that cap confidence for want of corroboration.
CAPPING_CORROBORATION_STATES = frozenset(
    {CORROBORATION_SINGLE_SOURCE, CORROBORATION_SUPPORTING_ONLY}
)

# --------------------------------------------------------------------------
# Axis weights and penalty tables
# --------------------------------------------------------------------------

AXIS_SAMPLE_SIZE = "sample_size"
AXIS_RECURRENCE_STABILITY = "recurrence_stability"
AXIS_CORROBORATION = "corroboration_status"
AXIS_CONFIDENCE_CAP = "confidence_cap"

#: How much each axis can widen the band.  Sample size leads because it is the
#: only axis that measures how much was actually observed; the confidence cap is
#: smallest because it deliberately overlaps the corroboration axis — it is a
#: second, explicit charge for the same weakness, not an independent one.  The
#: four weights sum to exactly 1.0, which is what bounds the widening.
AXIS_WEIGHTS: Dict[str, float] = {
    AXIS_SAMPLE_SIZE: 0.35,
    AXIS_RECURRENCE_STABILITY: 0.25,
    AXIS_CORROBORATION: 0.25,
    AXIS_CONFIDENCE_CAP: 0.15,
}

AXIS_LABELS: Dict[str, str] = {
    AXIS_SAMPLE_SIZE: "Sample size",
    AXIS_RECURRENCE_STABILITY: "Recurrence stability",
    AXIS_CORROBORATION: "Corroboration status",
    AXIS_CONFIDENCE_CAP: "Confidence cap status",
}

SAMPLE_PENALTY: Dict[str, float] = {
    SAMPLE_TIER_STRONG: 0.0,
    SAMPLE_TIER_MODERATE: 0.35,
    SAMPLE_TIER_THIN: 0.70,
    SAMPLE_TIER_MINIMAL: 1.0,
}
STABILITY_PENALTY: Dict[str, float] = {
    STABILITY_STEADY: 0.0,
    STABILITY_VARIABLE: 0.50,
    STABILITY_BURSTY: 1.0,
    # Unknown is penalised heavily but not maximally: absent history is not
    # evidence of instability, and assuming steady would narrow a band on
    # evidence that does not exist.
    STABILITY_UNKNOWN: 0.70,
}
CORROBORATION_PENALTY: Dict[str, float] = {
    CORROBORATION_TRIPLE: 0.0,
    CORROBORATION_CORROBORATED: 0.30,
    CORROBORATION_SUPPORTING_ONLY: 0.85,
    CORROBORATION_SINGLE_SOURCE: 1.0,
}
CONFIDENCE_CAP_PENALTY: Dict[bool, float] = {False: 0.0, True: 1.0}

# --------------------------------------------------------------------------
# Derived labels
# --------------------------------------------------------------------------

#: Band-width tiers, by the band's span in percentage points.  Read from the
#: computed width rather than from the inputs, so the label can never disagree
#: with the band it describes.
BAND_TIER_NARROW = "narrow"
BAND_TIER_MODERATE = "moderate"
BAND_TIER_WIDE = "wide"
BAND_TIER_VERY_WIDE = "very_wide"

_BAND_TIER_THRESHOLDS: Sequence[Tuple[int, str]] = (
    (32, BAND_TIER_NARROW),
    (48, BAND_TIER_MODERATE),
    (64, BAND_TIER_WIDE),
)

_BAND_TIER_LABELS: Dict[str, str] = {
    BAND_TIER_NARROW: "Narrow band",
    BAND_TIER_MODERATE: "Moderate band",
    BAND_TIER_WIDE: "Wide band",
    BAND_TIER_VERY_WIDE: "Very wide band",
}

EVIDENCE_TIER_STRONG = "strong"
EVIDENCE_TIER_ADEQUATE = "adequate"
EVIDENCE_TIER_LIMITED = "limited"
EVIDENCE_TIER_THIN = "thin"

_EVIDENCE_TIER_THRESHOLDS: Sequence[Tuple[float, str]] = (
    (0.75, EVIDENCE_TIER_STRONG),
    (0.50, EVIDENCE_TIER_ADEQUATE),
    (0.25, EVIDENCE_TIER_LIMITED),
)

_EVIDENCE_TIER_LABELS: Dict[str, str] = {
    EVIDENCE_TIER_STRONG: "Strong evidence",
    EVIDENCE_TIER_ADEQUATE: "Adequate evidence",
    EVIDENCE_TIER_LIMITED: "Limited evidence",
    EVIDENCE_TIER_THIN: "Thin evidence",
}

STRENGTH_TIER_STRONG = "strong"
STRENGTH_TIER_MODERATE = "moderate"
STRENGTH_TIER_WEAK = "weak"

_STRENGTH_TIER_THRESHOLDS: Sequence[Tuple[float, str]] = (
    (0.70, STRENGTH_TIER_STRONG),
    (0.40, STRENGTH_TIER_MODERATE),
)

#: 2.0-A1 AC4 — a capped (single-source) finding's projection strength is
#: clamped here so the rendered scalar agrees with the rank key's structural
#: demotion.  A capped projection therefore reads as at most "moderate" strength
#: however large its sample.
CAPPED_STRENGTH_CEILING = 0.50

#: The label a capped projection carries wherever its strength is shown.  AC4
#: requires the cap to be *labelled*, not merely applied.
CAPPED_STRENGTH_LABEL = "Capped — single-source confidence"

#: Wording used when a projection carries no band at all (direction is
#: "no material change"), so callers never render a bare "None".
NO_BAND_STRENGTH_LABEL = "Not projected — evidence below the projection floor"


# --------------------------------------------------------------------------
# Input classification
# --------------------------------------------------------------------------


def _safe_float(value: Any) -> Optional[float]:
    """Coerce to float, rejecting bools, NaN, inf and non-numerics."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def classify_sample_tier(sample_size: Optional[float]) -> str:
    """Axis 1 — bucket the observed sample size.

    An absent sample is ``minimal``, never assumed adequate: the widest band is
    the honest answer when nothing was counted.
    """
    if sample_size is None:
        return SAMPLE_TIER_MINIMAL
    if sample_size >= SAMPLE_STRONG:
        return SAMPLE_TIER_STRONG
    if sample_size >= SAMPLE_MODERATE:
        return SAMPLE_TIER_MODERATE
    if sample_size >= SAMPLE_THIN:
        return SAMPLE_TIER_THIN
    return SAMPLE_TIER_MINIMAL


def classify_recurrence_stability(recent_values: Sequence[Any]) -> str:
    """Axis 2 — steady vs bursty, as the coefficient of variation of the series.

    Fewer than :data:`MIN_OBSERVATIONS_FOR_STABILITY` observations is honestly
    ``unknown`` rather than assumed steady — assuming steady would narrow the
    band on absent evidence.
    """
    values = [v for v in (_safe_float(x) for x in recent_values or ()) if v is not None]
    if len(values) < MIN_OBSERVATIONS_FOR_STABILITY:
        return STABILITY_UNKNOWN
    mean = sum(values) / len(values)
    if mean <= 0:
        return STABILITY_UNKNOWN
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    cv = (variance**0.5) / mean
    if cv <= CV_STEADY:
        return STABILITY_STEADY
    if cv <= CV_VARIABLE:
        return STABILITY_VARIABLE
    return STABILITY_BURSTY


def classify_corroboration_status(opp: Mapping[str, Any]) -> str:
    """Axis 3 — classify corroboration status for band-width purposes.

    Mirrors the states ``corroboration_engine`` can produce, read defensively so
    a legacy stored opportunity without the ENT-2 fields is treated as
    single-source (the widest band) rather than crashing or being flattered.
    """
    if bool(opp.get("triple_corroboration")):
        return CORROBORATION_TRIPLE

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
        if not any(marker in s.lower() for marker in NON_ELEVATING_SOURCE_MARKERS)
    ]

    # COR-08 is the single-source rule: nothing corroborates the finding at all.
    # Checked first so it is never flattered into the (stronger) supporting-only
    # state when the engine stamps it alongside a conversation rule.
    if RULE_SINGLE_SOURCE in rule_ids and not elevating_sources:
        return CORROBORATION_SINGLE_SOURCE
    if rule_ids and rule_ids <= NON_ELEVATING_RULE_IDS:
        return CORROBORATION_SUPPORTING_ONLY
    if not elevating_sources:
        # Sources present but all non-elevating => supporting only.
        return CORROBORATION_SUPPORTING_ONLY if sources else CORROBORATION_SINGLE_SOURCE
    return CORROBORATION_CORROBORATED


def classify_confidence_cap(opp: Mapping[str, Any], corroboration_status: str) -> bool:
    """Axis 4 — True when the finding's confidence is capped.

    Two ways a finding gets here, and both are real:

    * its corroboration state cannot elevate confidence (single-source, or a
      conversation source that is capped at MEDIUM by the standing ceiling);
    * the pipeline already recorded LOW confidence for it.

    2.0-A1 AC4: such a projection is labelled, widened on this axis in addition
    to the corroboration axis, and structurally demoted in any ordering that
    uses projection strength.
    """
    if corroboration_status in CAPPING_CORROBORATION_STATES:
        return True
    return str(opp.get("confidence", "")).strip().upper() == "LOW"


# --------------------------------------------------------------------------
# Result shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BandWidthInputs:
    """The four — and only four — inputs to band width.

    Constructed by :func:`band_width_inputs_from_opportunity` from values the
    opportunity already carries.  No caller may supply a width directly; there
    is deliberately no field here for one.
    """

    sample_size: Optional[float]
    recurrence_stability: str
    corroboration_status: str
    confidence_capped: bool

    @property
    def sample_tier(self) -> str:
        return classify_sample_tier(self.sample_size)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sampleTier": self.sample_tier,
            "sampleSize": _as_number(self.sample_size),
            "recurrenceStability": self.recurrence_stability,
            "corroborationStatus": self.corroboration_status,
            "confidenceCapped": self.confidence_capped,
        }


@dataclass(frozen=True)
class BandWidthDriver:
    """One axis's contribution to the band's width — the audit trail.

    Rendered so an analyst can see *which* weakness widened a band and by how
    much, rather than being handed an opaque number.
    """

    axis: str
    label: str
    value: str
    penalty: float
    weight: float
    widens_by_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "axis": self.axis,
            "label": self.label,
            "value": self.value,
            "penalty": self.penalty,
            "weight": self.weight,
            "widensByPct": self.widens_by_pct,
        }


@dataclass(frozen=True)
class BandWidth:
    """A computed band plus everything needed to explain and compare it."""

    low_pct: int
    high_pct: int
    width_pct: int
    half_width: float
    evidence_penalty: float
    evidence_quality: float
    evidence_tier: str
    evidence_label: str
    band_tier: str
    band_label: str
    thin_evidence: bool
    confidence_capped: bool
    strength: float
    strength_tier: str
    strength_label: str
    strength_capped: bool
    rationale: str
    drivers: List[BandWidthDriver]
    inputs: BandWidthInputs
    model_version: str = BAND_WIDTH_MODEL_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "modelVersion": self.model_version,
            "lowPct": self.low_pct,
            "highPct": self.high_pct,
            "widthPct": self.width_pct,
            "halfWidth": self.half_width,
            "evidencePenalty": self.evidence_penalty,
            "evidenceQuality": self.evidence_quality,
            "evidenceTier": self.evidence_tier,
            "evidenceLabel": self.evidence_label,
            "bandTier": self.band_tier,
            "bandLabel": self.band_label,
            "thinEvidence": self.thin_evidence,
            "confidenceCapped": self.confidence_capped,
            "rationale": self.rationale,
            "drivers": [d.to_dict() for d in self.drivers],
            "inputs": self.inputs.to_dict(),
        }

    def strength_to_dict(self) -> Dict[str, Any]:
        """The comparable projection-strength scalar, as served.

        Separate from :meth:`to_dict` because strength is what an ORDERING uses
        and a band is what a READER sees; keeping them distinct on the wire
        stops a UI from accidentally ranking on a band edge.
        """
        return {
            "value": self.strength,
            "tier": self.strength_tier,
            "label": self.strength_label,
            "capped": self.strength_capped,
            "cappedLabel": CAPPED_STRENGTH_LABEL if self.strength_capped else None,
            "comparableWithCapped": not self.strength_capped,
        }


# --------------------------------------------------------------------------
# Computation
# --------------------------------------------------------------------------


def _as_number(value: Optional[float]) -> Optional[float]:
    """Render a float as an int when it is integral, for stable JSON."""
    if value is None:
        return None
    return int(value) if float(value).is_integer() else round(value, 4)


def _tier_from_thresholds(
    value: float, thresholds: Sequence[Tuple[float, str]], fallback: str
) -> str:
    for threshold, tier in thresholds:
        if value >= threshold:
            return tier
    return fallback


def _band_tier(width_pct: int) -> str:
    for threshold, tier in _BAND_TIER_THRESHOLDS:
        if width_pct <= threshold:
            return tier
    return BAND_TIER_VERY_WIDE


def _rationale(inputs: BandWidthInputs, band_tier: str, evidence_tier: str) -> str:
    """Plain-language reason the band is as wide as it is.

    Deliberately descriptive, never predictive: it explains a width, it does not
    promise a result.
    """
    sample = (
        f"{_as_number(inputs.sample_size)} observed instances"
        if inputs.sample_size is not None
        else "no counted instance population"
    )
    return (
        f"{_BAND_TIER_LABELS[band_tier]}: computed from {sample} "
        f"({inputs.sample_tier} sample), {inputs.recurrence_stability} recurrence, "
        f"{inputs.corroboration_status.replace('_', ' ')} corroboration"
        + (", capped confidence" if inputs.confidence_capped else "")
        + f" — {_EVIDENCE_TIER_LABELS[evidence_tier].lower()}."
    )


def compute_band_width(inputs: BandWidthInputs) -> BandWidth:
    """Compute the band deterministically from the four evidence inputs.

    The only entry point.  Given the same :class:`BandWidthInputs` it returns an
    identical result on every call, in every process, forever — which is what
    makes a stored projection comparable to a recomputed one (AC5).
    """
    sample_tier = inputs.sample_tier
    penalties: Dict[str, float] = {
        AXIS_SAMPLE_SIZE: SAMPLE_PENALTY[sample_tier],
        AXIS_RECURRENCE_STABILITY: STABILITY_PENALTY.get(
            inputs.recurrence_stability, STABILITY_PENALTY[STABILITY_UNKNOWN]
        ),
        AXIS_CORROBORATION: CORROBORATION_PENALTY.get(
            inputs.corroboration_status,
            CORROBORATION_PENALTY[CORROBORATION_SINGLE_SOURCE],
        ),
        AXIS_CONFIDENCE_CAP: CONFIDENCE_CAP_PENALTY[bool(inputs.confidence_capped)],
    }

    evidence_penalty = sum(AXIS_WEIGHTS[axis] * p for axis, p in penalties.items())
    half_width = MIN_HALF_WIDTH + MAX_ADDITIONAL_HALF_WIDTH * evidence_penalty

    low = max(BAND_FLOOR, BASE_MIDPOINT - half_width)
    high = min(BAND_CEILING, BASE_MIDPOINT + half_width)
    low_pct = int(round(low * 100))
    high_pct = int(round(high * 100))
    if high_pct <= low_pct:  # never collapse to a point estimate
        high_pct = low_pct + 1
    width_pct = high_pct - low_pct

    evidence_quality = round(1.0 - evidence_penalty, 4)
    evidence_tier = _tier_from_thresholds(
        evidence_quality, _EVIDENCE_TIER_THRESHOLDS, EVIDENCE_TIER_THIN
    )
    band_tier = _band_tier(width_pct)

    # ``thin_evidence`` is the analyst-facing "this band is wider because the
    # evidence is limited" flag.  It is TRUE whenever any axis is materially
    # weak, which is a lower bar than the evidence tier — a strong sample can
    # carry a capped-confidence finding into "adequate" overall while the reason
    # for the extra width still needs saying.
    thin_evidence = (
        sample_tier in (SAMPLE_TIER_THIN, SAMPLE_TIER_MINIMAL)
        or inputs.recurrence_stability in (STABILITY_BURSTY, STABILITY_UNKNOWN)
        or bool(inputs.confidence_capped)
    )

    evidence_label = _EVIDENCE_TIER_LABELS[evidence_tier]
    if thin_evidence and evidence_tier != EVIDENCE_TIER_THIN:
        evidence_label = f"{evidence_label} — band widened"

    strength_capped = bool(inputs.confidence_capped)
    strength = evidence_quality
    if strength_capped:
        strength = min(strength, CAPPED_STRENGTH_CEILING)
    strength = round(strength, 4)
    strength_tier = _tier_from_thresholds(
        strength, _STRENGTH_TIER_THRESHOLDS, STRENGTH_TIER_WEAK
    )
    strength_label = (
        CAPPED_STRENGTH_LABEL
        if strength_capped
        else f"{strength_tier.capitalize()} projection strength"
    )

    drivers = [
        BandWidthDriver(
            axis=axis,
            label=AXIS_LABELS[axis],
            value=_driver_value(axis, inputs),
            penalty=round(penalties[axis], 4),
            weight=AXIS_WEIGHTS[axis],
            widens_by_pct=round(
                AXIS_WEIGHTS[axis]
                * penalties[axis]
                * MAX_ADDITIONAL_HALF_WIDTH
                * 2
                * 100,
                2,
            ),
        )
        for axis in (
            AXIS_SAMPLE_SIZE,
            AXIS_RECURRENCE_STABILITY,
            AXIS_CORROBORATION,
            AXIS_CONFIDENCE_CAP,
        )
    ]

    return BandWidth(
        low_pct=low_pct,
        high_pct=high_pct,
        width_pct=width_pct,
        half_width=round(half_width, 4),
        evidence_penalty=round(evidence_penalty, 4),
        evidence_quality=evidence_quality,
        evidence_tier=evidence_tier,
        evidence_label=evidence_label,
        band_tier=band_tier,
        band_label=_BAND_TIER_LABELS[band_tier],
        thin_evidence=thin_evidence,
        confidence_capped=bool(inputs.confidence_capped),
        strength=strength,
        strength_tier=strength_tier,
        strength_label=strength_label,
        strength_capped=strength_capped,
        rationale=_rationale(inputs, band_tier, evidence_tier),
        drivers=drivers,
        inputs=inputs,
    )


def _driver_value(axis: str, inputs: BandWidthInputs) -> str:
    if axis == AXIS_SAMPLE_SIZE:
        size = _as_number(inputs.sample_size)
        return f"{inputs.sample_tier} ({size} observed)" if size is not None else (
            f"{inputs.sample_tier} (population not counted)"
        )
    if axis == AXIS_RECURRENCE_STABILITY:
        return inputs.recurrence_stability
    if axis == AXIS_CORROBORATION:
        return inputs.corroboration_status
    return "capped" if inputs.confidence_capped else "not capped"


def band_width_inputs_from_opportunity(
    opp: Mapping[str, Any], sample_size: Optional[float]
) -> BandWidthInputs:
    """Read the four inputs off an opportunity record.

    ``sample_size`` is passed in rather than read here because only the
    projection model knows which of a detector's measured fields is a countable
    population (a rate is not a sample size) — that decision belongs with the
    detector signal profile, not with the width model.
    """
    corroboration_status = classify_corroboration_status(opp)
    return BandWidthInputs(
        sample_size=sample_size,
        recurrence_stability=classify_recurrence_stability(
            opp.get("recent_values") or ()
        ),
        corroboration_status=corroboration_status,
        confidence_capped=classify_confidence_cap(opp, corroboration_status),
    )


# --------------------------------------------------------------------------
# Projection strength — comparison and ordering (AC4)
# --------------------------------------------------------------------------


def projection_strength_of(projection: Optional[Mapping[str, Any]]) -> Optional[float]:
    """The strength scalar off a serialized projection, or None when absent."""
    strength = (projection or {}).get("projectionStrength")
    if not isinstance(strength, Mapping):
        return None
    return _safe_float(strength.get("value"))


def projection_is_capped(projection: Optional[Mapping[str, Any]]) -> bool:
    """True when a serialized projection carries capped (single-source) confidence.

    Reads the strength block first and falls back to the top-level flag, so a
    projection stored before T4 is still correctly recognised as capped.
    """
    if not isinstance(projection, Mapping):
        return False
    strength = projection.get("projectionStrength")
    if isinstance(strength, Mapping) and "capped" in strength:
        return bool(strength.get("capped"))
    return bool(projection.get("confidenceCapped"))


def projection_rank_key(projection: Optional[Mapping[str, Any]]) -> Tuple[int, float]:
    """Ascending sort key ordering projections strongest-first.

    2.0-A1 AC4 is enforced *structurally* by the leading element: every capped
    projection sorts after every uncapped one, so a capped finding can never
    out-rank a corroborated equivalent on projection strength alone — regardless
    of how large its sample is or how the scalar happens to compare.

    A projection that carries no band (direction "no material change") has no
    strength to compare and sorts last within its group rather than being
    treated as maximally strong.
    """
    capped = 1 if projection_is_capped(projection) else 0
    strength = projection_strength_of(projection)
    return (capped, -strength if strength is not None else 1.0)


def order_by_projection_strength(
    items: Sequence[Any],
    projection_of: Callable[[Any], Optional[Mapping[str, Any]]],
) -> List[Any]:
    """Stable strongest-first ordering of ``items`` by projection strength.

    Stable by construction: items with equal keys keep their incoming relative
    order, so this narrows an existing ranking rather than replacing it.
    """
    return sorted(items, key=lambda item: projection_rank_key(projection_of(item)))


def demote_capped_projections(
    items: Sequence[Any],
    projection_of: Callable[[Any], Optional[Mapping[str, Any]]],
) -> List[Any]:
    """Move capped-confidence findings below uncapped ones, order else unchanged.

    The conservative half of :func:`order_by_projection_strength`: it applies
    AC4's rule (a capped finding never presents above a corroborated equivalent)
    WITHOUT letting the strength scalar re-rank anything else.  Used where an
    existing ordering already encodes a deliberate decision — the roadmap's
    approved-before-unreviewed ordering, for instance — and only the capped
    demotion is wanted on top of it.
    """
    return sorted(items, key=lambda item: 1 if projection_is_capped(projection_of(item)) else 0)


__all__ = [
    "BAND_WIDTH_MODEL_VERSION",
    "BASE_MIDPOINT",
    "MIN_HALF_WIDTH",
    "MAX_ADDITIONAL_HALF_WIDTH",
    "BAND_FLOOR",
    "BAND_CEILING",
    "MIN_INSTANCES_FOR_DIRECTION",
    "AXIS_SAMPLE_SIZE",
    "AXIS_RECURRENCE_STABILITY",
    "AXIS_CORROBORATION",
    "AXIS_CONFIDENCE_CAP",
    "AXIS_WEIGHTS",
    "CAPPED_STRENGTH_CEILING",
    "CAPPED_STRENGTH_LABEL",
    "NO_BAND_STRENGTH_LABEL",
    "SAMPLE_TIER_STRONG",
    "SAMPLE_TIER_MODERATE",
    "SAMPLE_TIER_THIN",
    "SAMPLE_TIER_MINIMAL",
    "STABILITY_STEADY",
    "STABILITY_VARIABLE",
    "STABILITY_BURSTY",
    "STABILITY_UNKNOWN",
    "CORROBORATION_TRIPLE",
    "CORROBORATION_CORROBORATED",
    "CORROBORATION_SUPPORTING_ONLY",
    "CORROBORATION_SINGLE_SOURCE",
    "BandWidth",
    "BandWidthDriver",
    "BandWidthInputs",
    "band_width_inputs_from_opportunity",
    "classify_confidence_cap",
    "classify_corroboration_status",
    "classify_recurrence_stability",
    "classify_sample_tier",
    "compute_band_width",
    "demote_capped_projections",
    "order_by_projection_strength",
    "projection_is_capped",
    "projection_rank_key",
    "projection_strength_of",
]
