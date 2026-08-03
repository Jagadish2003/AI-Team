"""2.0-A3 T1 — the learning signal set.

This module defines **what the learning layer is allowed to learn from**, and
establishes the weighting principle that separates this feature from ordinary
click-tracking. It produces signals; it applies no adjustment. The bounded
ranking adjustment is T2's job, and keeping the two apart is deliberate: a
signal set that also adjusted would be impossible to inspect without also
running the thing it feeds.

**Two sources, and only two.**

* Analyst decisions — accept / dismiss / defer-with-reason, from
  ``learning_feedback.py``. Evidence about a JUDGEMENT.
* Outcome results — 2.0-A2's post-action movement measurements, from
  ``opportunity_movement.py``. Evidence about the WORLD.

Nothing else. In particular, nothing from ``telemetry.py``: page views, dwell
time, expand-clicks and every other engagement signal are excluded by
construction, because a ranking layer trained on what was clicked is a
recommendation engine wearing an evidence platform's clothes. A structural test
(``tests/unit/test_learning_signal_isolation.py``) fails the build if any module
in this layer so much as imports it.

**An outcome outweighs an opinion.** The weights live in
``config/learning_signals.json``, and the loader REFUSES a config in which any
decision weight meets or exceeds any non-zero outcome weight — the invariant is
enforced, not documented, because it is exactly the kind of relationship that
gets tuned away in good faith with nothing in the product looking different
afterwards.

**The join is ``opportunity_identity``.** Both sources key on it, which is the
only reason this works at all: it is computed from run-invariant inputs
(``discovery/opportunity_identity.py``), so a decision made on one run and an
outcome measured three runs later resolve to the same problem.

**Similarity is conservative and declared.** Two findings are similar by
detector, pack, and signal concept — the run-invariant dimensions already stamped
on every finding. Nothing is inferred from titles or narrative text. A
name-similarity match is exactly the silent fuzzy inference 2.0-B2 refuses for
entity resolution, and it is refused here for the same reason: a claim of
similarity the customer disagrees with makes the whole explainability surface
untrustworthy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .learning_feedback import ACTION_DEFER, latest_feedback_by_identity
from .projection_validation import VERDICT_NOT_PROJECTED
from .learning_signal_config import (
    DIRECTION_NEGATIVE,
    DIRECTION_NEUTRAL,
    DIRECTION_POSITIVE,
    LearningSignalConfig,
    load_config,
)

logger = logging.getLogger(__name__)

SIGNAL_SET_SCHEMA_VERSION = "1.0.0"

SOURCE_DECISION = "decision"
SOURCE_OUTCOME = "outcome"
SIGNAL_SOURCES = (SOURCE_DECISION, SOURCE_OUTCOME)

#: Why a collected input carries no weight. Recorded rather than dropped: a
#: signal that vanishes silently is one nobody can ask about later.
EXCLUDED_NEUTRAL_VERDICT = "neutral_verdict"
EXCLUDED_UNKNOWN_ACTION = "unknown_action"
EXCLUDED_NO_IDENTITY = "no_opportunity_identity"
EXCLUDED_UNWEIGHTED_DEFER_REASON = "defer_reason_carries_no_signal"


# --------------------------------------------------------------------------
# One signal
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LearningSignal:
    """One weighted piece of evidence about one opportunity.

    ``evidence_ref`` is the handle AC2's "links to the contributing decisions and
    outcomes" resolves against — a ``feedbackId`` for a decision, an
    ``(identity, runId)`` pair for an outcome. It is carried on every signal,
    weighted or not, so an explanation can always name its sources.
    """

    source: str
    opportunity_identity: str
    detector_id: Optional[str]
    pack_id: Optional[str]
    signal_concept: Optional[str]
    direction: str
    #: Configured base weight before any multiplier.
    base_weight: float
    #: After recency decay, comparability, confounders and reason multipliers.
    weight: float
    evidence_ref: Dict[str, Any]
    recorded_at: Optional[str]
    #: Human-readable, for the T2/T3 explainability surface. Plain language on
    #: purpose: this string is destined for a customer-facing "why", not a log.
    label: str
    #: Set when the signal was collected but carries no weight.
    excluded_reason: Optional[str] = None
    #: Every multiplier applied, named. An unexplainable weight is not usable in
    #: an explainability feature, so the derivation travels with the number.
    multipliers: Dict[str, float] = field(default_factory=dict)

    @property
    def is_outcome(self) -> bool:
        return self.source == SOURCE_OUTCOME

    @property
    def signed_weight(self) -> float:
        """Weight with its direction applied. Neutral signals contribute zero."""
        if self.direction == DIRECTION_POSITIVE:
            return self.weight
        if self.direction == DIRECTION_NEGATIVE:
            return -self.weight
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "opportunityIdentity": self.opportunity_identity,
            "detectorId": self.detector_id,
            "packId": self.pack_id,
            "signalConcept": self.signal_concept,
            "direction": self.direction,
            "baseWeight": round(self.base_weight, 4),
            "weight": round(self.weight, 4),
            "signedWeight": round(self.signed_weight, 4),
            "evidenceRef": dict(self.evidence_ref),
            "recordedAt": self.recorded_at,
            "label": self.label,
            "excludedReason": self.excluded_reason,
            "multipliers": {k: round(v, 4) for k, v in self.multipliers.items()},
        }


# --------------------------------------------------------------------------
# Similarity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SimilarityKey:
    """The run-invariant dimensions on which two findings may be called similar."""

    detector_id: Optional[str]
    pack_id: Optional[str]
    signal_concept: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detectorId": self.detector_id,
            "packId": self.pack_id,
            "signalConcept": self.signal_concept,
        }


def _norm(value: Any) -> Optional[str]:
    text = str(value).strip().lower() if value is not None else ""
    return text or None


def signal_concept_for(detector_id: Optional[str]) -> Optional[str]:
    """The measured concept behind a detector, from the A1 signal registry.

    Reusing A1's registry rather than inventing a second mapping is the point:
    the concept two detectors share is the field they actually measure, which the
    registry already records. A detector with no profile has no concept — it is
    then similar only to itself, which is the honest answer.
    """
    if not detector_id:
        return None
    try:
        from discovery.projection.signal_registry import get_detector_profile

        profile = get_detector_profile(str(detector_id))
    except Exception:  # noqa: BLE001 - an unprofiled detector is not an error
        return None
    if profile is None:
        return None
    return _norm(getattr(profile, "movement_signal", None))


def similarity_key(
    detector_id: Optional[str],
    pack_id: Optional[str],
    signal_concept: Optional[str] = None,
) -> SimilarityKey:
    detector = _norm(detector_id)
    return SimilarityKey(
        detector_id=detector,
        pack_id=_norm(pack_id),
        signal_concept=_norm(signal_concept) or signal_concept_for(detector),
    )


def similarity_score(
    left: SimilarityKey,
    right: SimilarityKey,
    config: Optional[LearningSignalConfig] = None,
) -> float:
    """How similar two findings are, in [0, 1]. Strongest matching rule wins.

    There is deliberately no "same pack" tier. Two findings that share only a
    pack have nothing meaningful in common, and calling them similar in a
    customer-facing explanation would be indefensible.
    """
    cfg = config or load_config()
    if left.detector_id and left.detector_id == right.detector_id:
        if left.pack_id and left.pack_id == right.pack_id:
            return cfg.similarity.same_detector_same_pack
        return cfg.similarity.same_detector_other_pack
    if left.signal_concept and left.signal_concept == right.signal_concept:
        return cfg.similarity.same_signal_concept
    return 0.0


def are_similar(
    left: SimilarityKey,
    right: SimilarityKey,
    config: Optional[LearningSignalConfig] = None,
) -> bool:
    cfg = config or load_config()
    return similarity_score(left, right, cfg) >= cfg.similarity.minimum_score


# --------------------------------------------------------------------------
# Recency
# --------------------------------------------------------------------------


def _parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recency_multiplier(
    recorded_at: Any,
    *,
    now: Optional[datetime] = None,
    config: Optional[LearningSignalConfig] = None,
) -> float:
    """Exponential decay towards a floor, never to zero.

    An old outcome is weak evidence, not disproven evidence. A signal that
    decayed to exactly zero would silently leave the explainability surface — the
    customer sees a reason cite four decisions, then three, with nothing having
    changed and no event to point at.
    """
    cfg = config or load_config()
    stamp = _parse_ts(recorded_at)
    if stamp is None:
        # An undated signal is treated as fully decayed rather than fresh: the
        # conservative direction, since the alternative rewards missing data.
        return cfg.recency.floor
    reference = now or datetime.now(timezone.utc)
    age_days = max(0.0, (reference - stamp).total_seconds() / 86400.0)
    half_life = cfg.recency.half_life_days
    if half_life <= 0:
        return 1.0
    decayed = 0.5 ** (age_days / half_life)
    return max(cfg.recency.floor, min(1.0, decayed))


# --------------------------------------------------------------------------
# Building signals from each source
# --------------------------------------------------------------------------


_ACTION_LABELS = {
    "accept": "your team accepted this finding",
    "dismiss": "your team dismissed this finding",
    "defer": "your team deferred this finding",
}

#: The role whose direction IS the outcome. A population/denominator signal
#: moving says nothing about whether the intervention worked.
MOVEMENT_ROLE = "movement"

_DIRECTION_LABELS = {
    "improved": "improved measurably after the change was made",
    "worsened": "moved the wrong way after the change was made",
    "unchanged": "did not move after the change was made",
}

_VERDICT_LABELS = {
    "within_band": "delivered measured improvement within the projected range",
    "above_band": "delivered measured improvement beyond the projected range",
    "below_band": "did not move as far as projected after the change was made",
    "not_projected": "was measured after action, with no projection to compare against",
    "too_early": "is being measured, but its projected horizon has not yet elapsed",
}


def decision_signal(
    record: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    config: Optional[LearningSignalConfig] = None,
) -> Optional[LearningSignal]:
    """One analyst decision as a weighted signal."""
    cfg = config or load_config()
    identity = _norm(record.get("opportunityIdentity"))
    action = _norm(record.get("action"))
    if not identity:
        return None

    weighted = cfg.decision_weight(action or "")
    detector = _norm(record.get("detectorId"))
    concept = _norm(record.get("signalConcept")) or signal_concept_for(detector)
    recorded_at = record.get("recordedAt")
    label = _ACTION_LABELS.get(action or "", "your team recorded a decision")

    excluded: Optional[str] = None
    multipliers: Dict[str, float] = {}
    base = weighted.weight

    if weighted.weight <= 0 or weighted.direction == DIRECTION_NEUTRAL:
        excluded = EXCLUDED_UNKNOWN_ACTION

    reason_code = _norm(record.get("reasonCode"))
    if action == ACTION_DEFER:
        reason_multiplier = cfg.defer_multiplier(reason_code)
        multipliers["deferReason"] = reason_multiplier
        if reason_multiplier <= 0:
            # A deferral for want of capacity, a blocking dependency, or pending
            # approval is a fact about the team's calendar, not a judgement about
            # the finding. Counted and shown; never learned from.
            excluded = excluded or EXCLUDED_UNWEIGHTED_DEFER_REASON

    decay = recency_multiplier(recorded_at, now=now, config=cfg)
    multipliers["recency"] = decay

    weight = 0.0
    if not excluded:
        weight = base * decay * multipliers.get("deferReason", 1.0)

    return LearningSignal(
        source=SOURCE_DECISION,
        opportunity_identity=identity,
        detector_id=detector,
        pack_id=_norm(record.get("packId")),
        signal_concept=concept,
        direction=weighted.direction if not excluded else DIRECTION_NEUTRAL,
        base_weight=base,
        weight=weight,
        evidence_ref={
            "kind": SOURCE_DECISION,
            "feedbackId": record.get("feedbackId"),
            "opportunityIdentity": record.get("opportunityIdentity"),
            "actorId": record.get("actorId"),
            "reasonCode": record.get("reasonCode"),
        },
        recorded_at=str(recorded_at) if recorded_at else None,
        label=label,
        excluded_reason=excluded,
        multipliers=multipliers,
    )


def _measured_direction(record: Mapping[str, Any]) -> Optional[str]:
    """The direction of the signal the finding is actually about.

    Prefers the ``movement``-role signal: a population/denominator signal moving
    says nothing about whether the intervention worked, so taking "the first
    signal" would sometimes read a total case count as the outcome. Falls back to
    the first signal carrying a usable direction when no role is marked.
    """
    movements = record.get("movements")
    if not isinstance(movements, Sequence) or isinstance(movements, (str, bytes)):
        return None

    fallback: Optional[str] = None
    for entry in movements:
        if not isinstance(entry, Mapping):
            continue
        direction = _norm(entry.get("direction"))
        if not direction:
            continue
        if _norm(entry.get("role")) == MOVEMENT_ROLE:
            return direction
        if fallback is None:
            fallback = direction
    return fallback


def outcome_signal(
    record: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    config: Optional[LearningSignalConfig] = None,
) -> Optional[LearningSignal]:
    """One A2 movement measurement as a weighted signal.

    Caveated measurements are DOWN-WEIGHTED, never dropped. 2.0-A2 T3's rule is
    "carried, never silently normalised" and T4's is "never a blocked
    measurement"; a learning layer that discarded caveated outcomes would
    re-introduce exactly the blocking those subtasks refused, one layer up.
    """
    cfg = config or load_config()
    identity = _norm(record.get("opportunityIdentity"))
    if not identity:
        return None

    validation = record.get("projectionValidation")
    verdict = None
    if isinstance(validation, Mapping):
        verdict = _norm(validation.get("verdict"))
    weighted = cfg.outcome_weight(verdict or "")
    label = _VERDICT_LABELS.get(verdict or "", "was measured after action")
    direction_used: Optional[str] = None

    # No projection to validate against, but a real measurement all the same.
    # The verdict answers "was our model right?"; the measured DIRECTION answers
    # "did the action help?" — and ranking cares about the second. Falling back
    # to it is what stops every finding created before 2.0-A1 shipped from being
    # permanently unlearnable. Deliberately NOT applied when a projection exists:
    # the band verdict already incorporates the direction and knows what was
    # expected, so using both would count one measurement twice.
    if verdict == VERDICT_NOT_PROJECTED:
        direction_used = _measured_direction(record)
        directional = cfg.movement_direction_weight(direction_used or "")
        if directional.weight > 0:
            weighted = directional
            label = _DIRECTION_LABELS.get(
                direction_used or "", "was measured after action"
            )

    base = weighted.weight

    multipliers: Dict[str, float] = {}
    excluded: Optional[str] = None
    if base <= 0 or weighted.direction == DIRECTION_NEUTRAL:
        # too_early, or not_projected with no determinable direction: a real
        # measurement with nothing to learn from yet. Counted in the signal set
        # so it stays visible rather than vanishing.
        excluded = EXCLUDED_NEUTRAL_VERDICT

    comparability = record.get("comparability")
    comparability_verdict = (
        _norm(comparability.get("verdict")) if isinstance(comparability, Mapping) else None
    )
    comparability_multiplier = cfg.comparability_multiplier(comparability_verdict)
    multipliers["comparability"] = comparability_multiplier

    summary = record.get("confounderSummary")
    confounder_multiplier = 1.0
    if isinstance(summary, Mapping):
        # Applied once per severity PRESENT, not once per caveat, so a
        # measurement with six advisory caveats is not weighted into oblivion.
        if int(summary.get("materialCount") or 0) > 0:
            confounder_multiplier *= cfg.material_caveat_multiplier
        if int(summary.get("advisoryCount") or 0) > 0:
            confounder_multiplier *= cfg.advisory_caveat_multiplier
    multipliers["confounders"] = confounder_multiplier

    measured_at = record.get("measuredAt")
    decay = recency_multiplier(measured_at, now=now, config=cfg)
    multipliers["recency"] = decay

    weight = 0.0
    if not excluded:
        # The caveat multipliers order outcomes against EACH OTHER; the floor
        # stops them pushing one across the evidence-class boundary. Without it,
        # a below-band, weakly-comparable, doubly-caveated measurement lands
        # below a clean analyst accept — and since caveats are common in real
        # measurement, opinions would quietly win most of the time with nothing
        # in the config revealing it.
        #
        # Floored BEFORE decay, deliberately: decay is identical for both
        # classes, so at equal age an outcome always outweighs a decision, while
        # a stale measurement and a stale opinion fade together. Flooring after
        # decay would claim a three-year-old measurement about a since-rebuilt
        # system beats a fresh judgement about today's.
        caveated = base * comparability_multiplier * confounder_multiplier
        floored = max(caveated, cfg.outcome_floor)
        if floored > caveated:
            multipliers["outcomeFloor"] = cfg.outcome_floor
        weight = floored * decay

    detector = _norm(record.get("detectorId"))
    # The pack lives at projectionValidation.projected.packId — NOT at the top
    # level of the validation block (see projection_validation._projection_block).
    # Reading the wrong level does not fail loudly: every outcome would simply
    # carry pack_id=None, silently bucketing them apart from the decisions about
    # the same findings and dropping their similarity from 1.0 to the weaker
    # "same detector, other pack" tier.
    pack_id = None
    if isinstance(validation, Mapping):
        projected = validation.get("projected")
        if isinstance(projected, Mapping):
            pack_id = _norm(projected.get("packId"))
        if not pack_id:
            pack_id = _norm(validation.get("packId"))
    if not pack_id:
        baseline = record.get("baseline")
        if isinstance(baseline, Mapping):
            pack_id = _norm(baseline.get("packId"))

    return LearningSignal(
        source=SOURCE_OUTCOME,
        opportunity_identity=identity,
        detector_id=detector,
        pack_id=pack_id,
        signal_concept=signal_concept_for(detector),
        direction=weighted.direction if not excluded else DIRECTION_NEUTRAL,
        base_weight=base,
        weight=weight,
        evidence_ref={
            "kind": SOURCE_OUTCOME,
            "opportunityIdentity": record.get("opportunityIdentity"),
            "currentRunId": record.get("currentRunId"),
            "baselineRunId": record.get("baselineRunId"),
            "verdict": verdict,
            "measuredDirection": direction_used,
            "comparabilityVerdict": comparability_verdict,
        },
        recorded_at=str(measured_at) if measured_at else None,
        label=label,
        excluded_reason=excluded,
        multipliers=multipliers,
    )


# --------------------------------------------------------------------------
# The set
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalSet:
    """Every learning signal available to one org, and whether it is enough.

    ``is_active`` is AC4's gate. T2 must consult it and apply nothing when it is
    False — "no pretending to personalise from three data points".
    """

    org_id: str
    signals: Tuple[LearningSignal, ...]
    config_version: str
    minimum_signals: int
    minimum_distinct_identities: int
    collected_at: str

    @property
    def weighted(self) -> Tuple[LearningSignal, ...]:
        return tuple(s for s in self.signals if s.weight > 0)

    @property
    def distinct_identities(self) -> int:
        return len({s.opportunity_identity for s in self.weighted})

    @property
    def outcome_count(self) -> int:
        return sum(1 for s in self.weighted if s.is_outcome)

    @property
    def decision_count(self) -> int:
        return sum(1 for s in self.weighted if not s.is_outcome)

    @property
    def is_active(self) -> bool:
        """Both thresholds must be met.

        The distinct-identity rule is what stops a single enthusiastically
        reviewed opportunity from switching learning on for a whole org.
        """
        return (
            len(self.weighted) >= self.minimum_signals
            and self.distinct_identities >= self.minimum_distinct_identities
        )

    @property
    def inactive_reason(self) -> Optional[str]:
        """Plain language, for the UI's "learning not yet active" state."""
        if self.is_active:
            return None
        if len(self.weighted) < self.minimum_signals:
            return (
                f"Learning is not yet active: {len(self.weighted)} of "
                f"{self.minimum_signals} decisions and measured outcomes recorded."
            )
        return (
            f"Learning is not yet active: signals so far cover "
            f"{self.distinct_identities} of {self.minimum_distinct_identities} "
            "distinct findings."
        )

    def for_identity(self, opportunity_identity: str) -> Tuple[LearningSignal, ...]:
        identity = _norm(opportunity_identity)
        return tuple(s for s in self.signals if s.opportunity_identity == identity)

    def similar_to(
        self,
        key: SimilarityKey,
        *,
        config: Optional[LearningSignalConfig] = None,
        exclude_identity: Optional[str] = None,
    ) -> Tuple[Tuple[LearningSignal, float], ...]:
        """Weighted signals from findings similar to ``key``, with their scores.

        ``exclude_identity`` drops an opportunity's own history so a caller
        asking "what do similar findings say about this one?" does not get the
        finding's own decisions back as evidence about itself.
        """
        cfg = config or load_config()
        excluded = _norm(exclude_identity)
        out = []
        for signal in self.weighted:
            if excluded and signal.opportunity_identity == excluded:
                continue
            score = similarity_score(
                similarity_key(signal.detector_id, signal.pack_id, signal.signal_concept),
                key,
                cfg,
            )
            if score >= cfg.similarity.minimum_score:
                out.append((signal, score))
        return tuple(out)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": SIGNAL_SET_SCHEMA_VERSION,
            "orgId": self.org_id,
            "configVersion": self.config_version,
            "collectedAt": self.collected_at,
            "isActive": self.is_active,
            "inactiveReason": self.inactive_reason,
            "counts": {
                "total": len(self.signals),
                "weighted": len(self.weighted),
                "outcomes": self.outcome_count,
                "decisions": self.decision_count,
                "distinctIdentities": self.distinct_identities,
            },
            "thresholds": {
                "minimumSignals": self.minimum_signals,
                "minimumDistinctIdentities": self.minimum_distinct_identities,
            },
            "signals": [s.to_dict() for s in self.signals],
        }


def collect_learning_signals(
    org_id: str,
    *,
    now: Optional[datetime] = None,
    config: Optional[LearningSignalConfig] = None,
    decision_records: Optional[Sequence[Mapping[str, Any]]] = None,
    outcome_records: Optional[Sequence[Mapping[str, Any]]] = None,
    limit: int = 2000,
) -> SignalSet:
    """Every learning signal available to one org.

    Both stores are read org-scoped at the SQL layer, never filtered afterwards:
    AC6's isolation has to hold in the query or it does not hold. The record
    sequences are injectable so the pure weighting logic is testable without a
    database.
    """
    cfg = config or load_config()
    org = str(org_id or "").strip()
    reference = now or datetime.now(timezone.utc)

    if decision_records is None:
        try:
            decision_records = list(latest_feedback_by_identity(org, limit=limit).values())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read learning decisions for %s: %s", org, exc)
            decision_records = []

    if outcome_records is None:
        try:
            from .opportunity_movement import list_movements

            outcome_records = list_movements(org, limit=min(limit, 1000))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read learning outcomes for %s: %s", org, exc)
            outcome_records = []

    signals: List[LearningSignal] = []
    for record in outcome_records or ():
        if not isinstance(record, Mapping):
            continue
        signal = outcome_signal(record, now=reference, config=cfg)
        if signal is not None:
            signals.append(signal)
    for record in decision_records or ():
        if not isinstance(record, Mapping):
            continue
        signal = decision_signal(record, now=reference, config=cfg)
        if signal is not None:
            signals.append(signal)

    return SignalSet(
        org_id=org,
        signals=tuple(signals),
        config_version=cfg.config_version,
        minimum_signals=cfg.cold_start.minimum_signals,
        minimum_distinct_identities=cfg.cold_start.minimum_distinct_identities,
        collected_at=reference.isoformat(),
    )


# --------------------------------------------------------------------------
# Aggregation — what T2's adjustment layer consumes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SimilarityGroup:
    """The accumulated evidence for one finding type.

    ``net_weight`` is signed: positive means the team's decisions and measured
    outcomes favour this finding type, negative means they do not. T2 turns this
    into a BOUNDED rank adjustment; T1 asserts nothing about how far it may move
    anything, because that cap is the other subtask's entire subject.
    """

    key: SimilarityKey
    signals: Tuple[LearningSignal, ...]

    @property
    def net_weight(self) -> float:
        return sum(s.signed_weight for s in self.signals)

    @property
    def outcome_weight(self) -> float:
        return sum(s.signed_weight for s in self.signals if s.is_outcome)

    @property
    def decision_weight(self) -> float:
        return sum(s.signed_weight for s in self.signals if not s.is_outcome)

    @property
    def has_outcome_evidence(self) -> bool:
        return any(s.is_outcome and s.weight > 0 for s in self.signals)

    @property
    def contributing_refs(self) -> Tuple[Dict[str, Any], ...]:
        """AC2's links, in the order they should be shown: outcomes first."""
        ordered = sorted(
            self.signals, key=lambda s: (0 if s.is_outcome else 1, -abs(s.signed_weight))
        )
        return tuple(dict(s.evidence_ref) for s in ordered)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key.to_dict(),
            "netWeight": round(self.net_weight, 4),
            "outcomeWeight": round(self.outcome_weight, 4),
            "decisionWeight": round(self.decision_weight, 4),
            "hasOutcomeEvidence": self.has_outcome_evidence,
            "signalCount": len(self.signals),
            "contributingRefs": [dict(r) for r in self.contributing_refs],
        }


def group_by_similarity(signal_set: SignalSet) -> Tuple[SimilarityGroup, ...]:
    """Accumulate weighted signals into finding-type groups.

    Grouped on the strongest similarity dimension available (detector + pack), so
    every member of a group is similar to every other at the highest score the
    config defines. Cross-group transfer at the weaker tiers is
    :meth:`SignalSet.similar_to`'s job — kept separate so a group is always a set
    of things the customer would agree are the same finding type.
    """
    buckets: Dict[Tuple[Optional[str], Optional[str]], List[LearningSignal]] = {}
    concepts: Dict[Tuple[Optional[str], Optional[str]], Optional[str]] = {}
    for signal in signal_set.weighted:
        bucket = (signal.detector_id, signal.pack_id)
        buckets.setdefault(bucket, []).append(signal)
        if signal.signal_concept and not concepts.get(bucket):
            concepts[bucket] = signal.signal_concept

    groups = [
        SimilarityGroup(
            key=SimilarityKey(
                detector_id=detector,
                pack_id=pack,
                signal_concept=concepts.get((detector, pack)),
            ),
            signals=tuple(members),
        )
        for (detector, pack), members in buckets.items()
    ]
    # Deterministic order: strongest evidence first, then by name so two groups
    # with equal weight never swap between reads.
    return tuple(
        sorted(
            groups,
            key=lambda g: (
                -abs(g.net_weight),
                g.key.detector_id or "",
                g.key.pack_id or "",
            ),
        )
    )


def describe_signal_set(signal_set: SignalSet) -> Dict[str, Any]:
    """The inspectable summary — the read model for the API and for T4's audit."""
    groups = group_by_similarity(signal_set)
    payload = signal_set.to_dict()
    payload["groups"] = [g.to_dict() for g in groups]
    return payload


__all__ = [
    "EXCLUDED_NEUTRAL_VERDICT",
    "EXCLUDED_NO_IDENTITY",
    "EXCLUDED_UNKNOWN_ACTION",
    "EXCLUDED_UNWEIGHTED_DEFER_REASON",
    "MOVEMENT_ROLE",
    "SIGNAL_SET_SCHEMA_VERSION",
    "SIGNAL_SOURCES",
    "SOURCE_DECISION",
    "SOURCE_OUTCOME",
    "LearningSignal",
    "SignalSet",
    "SimilarityGroup",
    "SimilarityKey",
    "are_similar",
    "collect_learning_signals",
    "decision_signal",
    "describe_signal_set",
    "group_by_similarity",
    "outcome_signal",
    "recency_multiplier",
    "signal_concept_for",
    "similarity_key",
    "similarity_score",
]
