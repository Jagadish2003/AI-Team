"""2.0-A1 T5 — recommendation text in intervention language.

The story's rule, and the reason this module exists:

    "Recommendation text in intervention language ('agent handles the N
    recurring cases; the residual requires judgment'), never in
    guaranteed-savings language."

"This agent will reduce cost by 40%" is a lie generator — it gets quoted in a
board paper and measured against reality in ninety days.  The honest form names
what is actually being proposed and what it rests on, so every recommendation
this module builds states five things and no more:

    1. **what the agent handles**    — the manual step it takes over
    2. **which cases are in scope**  — the N measured, recurring instances
    3. **what remains manual**       — the residual that still needs judgement
    4. **which signal should move**  — a real measured field, named
    5. **band and horizon**          — a range and a window, never a point

Deterministic and pure: no DB, no ``app`` import, no LLM, no clock.  The same
opportunity always produces the same recommendation, so it is reproducible
alongside the projection it is built from (AC5) and storable with it (AC6).

Everything generated here is checked against ``vocabulary.py`` before it is
returned — not as a test, but at build time.  A template in this repo that
somehow acquires guarantee language fails loudly rather than reaching a customer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .signal_registry import DetectorSignalProfile, get_detector_profile
from .vocabulary import assert_clean

#: Bumped when the recommendation wording changes shape (not on copy tweaks).
RECOMMENDATION_SCHEMA_VERSION = "1.0.0"

#: The five parts every recommendation must carry. A recommendation missing any
#: of them is not a recommendation — it is a claim with the caveats removed.
PART_AGENT_HANDLES = "agent_handles"
PART_CASES_IN_SCOPE = "cases_in_scope"
PART_REMAINS_MANUAL = "remains_manual"
PART_SIGNAL_TO_MOVE = "signal_expected_to_move"
PART_BAND_AND_HORIZON = "band_and_horizon"

REQUIRED_PARTS = (
    PART_AGENT_HANDLES,
    PART_CASES_IN_SCOPE,
    PART_REMAINS_MANUAL,
    PART_SIGNAL_TO_MOVE,
    PART_BAND_AND_HORIZON,
)

_PART_LABELS: Dict[str, str] = {
    PART_AGENT_HANDLES: "What the agent handles",
    PART_CASES_IN_SCOPE: "Cases in scope",
    PART_REMAINS_MANUAL: "What remains manual",
    PART_SIGNAL_TO_MOVE: "Signal expected to move",
    PART_BAND_AND_HORIZON: "Projection band and horizon",
}

#: Wording used when the finding projects no material change. Still a complete
#: recommendation — "not enough evidence to project" is a useful answer, and
#: hiding it would leave the analyst guessing why a band is missing.
_NO_BAND_STATEMENT = (
    "No magnitude band is projected: the measured evidence is below the "
    "threshold this platform requires before projecting a direction."
)


@dataclass(frozen=True)
class RecommendationPart:
    """One named part of the recommendation, with its rendered sentence."""

    id: str
    label: str
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "text": self.text}


@dataclass(frozen=True)
class Recommendation:
    """Intervention-language recommendation text for one opportunity."""

    headline: str
    parts: List[RecommendationPart]
    next_steps: List[str] = field(default_factory=list)
    schema_version: str = RECOMMENDATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "headline": self.headline,
            "parts": [p.to_dict() for p in self.parts],
            "nextSteps": list(self.next_steps),
            # Flattened for surfaces that want one string (PDF, exec summary).
            "summary": self.summary,
        }

    @property
    def summary(self) -> str:
        return " ".join([self.headline] + [p.text for p in self.parts])

    def part(self, part_id: str) -> Optional[RecommendationPart]:
        return next((p for p in self.parts if p.id == part_id), None)


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------


def _count_phrase(count: Optional[float], noun: str) -> str:
    """"the 240 recurring reassignment cases", or an honest hedge with no count."""
    if count is None:
        return f"the identified {noun}"
    rendered = int(count) if float(count).is_integer() else round(float(count), 1)
    return f"the {rendered:,} {noun}" if isinstance(rendered, int) else f"the {rendered} {noun}"


def _movement_phrase(signal: Mapping[str, Any]) -> str:
    """"reassignment hops (owner_changes_90d), currently 240" — measured, named."""
    label = str(signal.get("conceptLabel") or signal.get("concept") or "the measured signal")
    name = signal.get("signalName")
    current = signal.get("currentValue")
    unit = signal.get("unit") or ""

    phrase = f"{label.lower()}"
    if name:
        phrase += f" ({name})"
    if isinstance(current, (int, float)) and not isinstance(current, bool):
        rendered = int(current) if float(current).is_integer() else round(float(current), 2)
        suffix = {
            "days": " days",
            "hours": " hours",
            "seconds": " seconds",
            "pct": "%",
        }.get(str(unit), "")
        phrase += f", currently {rendered:,}{suffix}" if isinstance(rendered, int) else (
            f", currently {rendered}{suffix}"
        )
    # Direction is stated as a direction, not as a benefit: "a lower value is
    # the expected direction of movement" says which way to watch without
    # implying the platform is promising that movement.
    direction = (
        "lower" if str(signal.get("directionOfImprovement", "decrease")) == "decrease" else "higher"
    )
    return f"{phrase}; a {direction} value is the expected direction of movement"


def _band_phrase(projection: Mapping[str, Any]) -> str:
    band = projection.get("magnitudeBand")
    horizon = projection.get("observationHorizonDays")
    if not isinstance(band, Mapping):
        return _NO_BAND_STATEMENT

    label = band.get("label") or (
        f"{band.get('lowPct')}–{band.get('highPct')}% {band.get('basisUnit', '')}".strip()
    )
    sentence = f"Projected movement is a band of {label}"
    if isinstance(horizon, int):
        sentence += f", observable over about {horizon} days"
    sentence += "."

    # The band's own honesty caveat travels with it, so the range is never read
    # as a forecast the platform stands behind.
    strength = projection.get("projectionStrength")
    if isinstance(strength, Mapping) and strength.get("capped"):
        sentence += (
            " Confidence on this finding is capped for want of corroboration, so "
            "the band is wider and is not ranked against corroborated findings."
        )
    band_width = projection.get("bandWidth")
    if isinstance(band_width, Mapping) and band_width.get("thinEvidence"):
        sentence += " The band is wide because the underlying evidence is limited."
    return sentence


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def build_recommendation(
    opp: Mapping[str, Any], projection: Optional[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Build the intervention-language recommendation, or None if not applicable.

    ``None`` — deliberately, rather than generic filler — when the opportunity
    has no detector profile or no projection to describe. Silence is honest; a
    recommendation about an unmeasured signal is not.
    """
    if not isinstance(projection, Mapping) or not projection:
        return None

    debug = opp.get("_debug") or {}
    detector_id = str(opp.get("detector_id") or debug.get("detector_id") or "")
    profile = get_detector_profile(detector_id)
    if profile is None:
        return None

    basis = projection.get("basis") if isinstance(projection.get("basis"), Mapping) else {}
    movement = (
        projection.get("movementSignal")
        if isinstance(projection.get("movementSignal"), Mapping)
        else {}
    )

    instances = basis.get("observedInstances")
    if instances is None:
        instances = basis.get("observedPopulation")
    window = basis.get("observationWindowDays") or basis.get("baselineWindowDays")

    scope = _count_phrase(instances, profile.case_noun)

    # The story's canonical construction: "agent handles the N recurring cases;
    # the residual requires judgment". Naming the residual parenthetically keeps
    # the sentence grammatical for every profile — inlining it as the subject
    # would need singular/plural agreement guessed from an arbitrary noun
    # phrase, which is a losing game and reads wrong when it loses. Only the
    # head phrase is used; a trailing relative clause belongs in the
    # "what remains manual" part.
    residual_head = profile.residual.split(",")[0].strip()
    headline = f"Agent handles {scope}; the residual requires judgement ({residual_head})."

    cases_sentence = f"In scope: {scope}"
    if window:
        cases_sentence += f", measured over the observed {window}-day window"
    cases_sentence += "."

    parts = [
        RecommendationPart(
            PART_AGENT_HANDLES,
            _PART_LABELS[PART_AGENT_HANDLES],
            f"The agent takes over {profile.manual_step}.",
        ),
        RecommendationPart(
            PART_CASES_IN_SCOPE, _PART_LABELS[PART_CASES_IN_SCOPE], cases_sentence
        ),
        RecommendationPart(
            PART_REMAINS_MANUAL,
            _PART_LABELS[PART_REMAINS_MANUAL],
            f"Remaining manual: {profile.residual}.",
        ),
        RecommendationPart(
            PART_SIGNAL_TO_MOVE,
            _PART_LABELS[PART_SIGNAL_TO_MOVE],
            f"The signal expected to move is {_movement_phrase(movement)}.",
        ),
        RecommendationPart(
            PART_BAND_AND_HORIZON,
            _PART_LABELS[PART_BAND_AND_HORIZON],
            _band_phrase(projection),
        ),
    ]

    recommendation = Recommendation(
        headline=headline, parts=parts, next_steps=_next_steps(profile, scope)
    )

    # Build-time enforcement, not a test: our own copy must be clean by
    # construction, and a template that drifts should fail loudly here rather
    # than reach a board paper.
    assert_clean(recommendation.headline, "recommendation.headline")
    for part in recommendation.parts:
        assert_clean(part.text, f"recommendation.parts.{part.id}")
    for index, step in enumerate(recommendation.next_steps):
        assert_clean(step, f"recommendation.nextSteps[{index}]")

    return recommendation.to_dict()


def _next_steps(profile: DetectorSignalProfile, scope: str) -> List[str]:
    """Concrete, intervention-shaped next steps — actions, never outcomes.

    Deliberately about *validating and scoping the intervention*, because those
    are the steps a team can actually take before anything is built. None of
    them promises a result.
    """
    return [
        f"Confirm with the owning team that {scope} match the pattern described here.",
        f"Agree the boundary for {profile.residual}, so the agent's scope is explicit before build.",
        (
            f"Record the current value of {profile.movement_signal} as the baseline to "
            "re-measure against after the agent is live."
        ),
    ]


__all__ = [
    "RECOMMENDATION_SCHEMA_VERSION",
    "REQUIRED_PARTS",
    "PART_AGENT_HANDLES",
    "PART_CASES_IN_SCOPE",
    "PART_REMAINS_MANUAL",
    "PART_SIGNAL_TO_MOVE",
    "PART_BAND_AND_HORIZON",
    "Recommendation",
    "RecommendationPart",
    "build_recommendation",
]
