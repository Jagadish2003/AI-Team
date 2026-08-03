"""2.0-A3 T3 — why a finding moved, as structured data.

**The reason is data; the sentence is rendered from it.** This follows A2 T4's
confounder precedent exactly: each caveat there carries ``type``/``severity``/
``detail``/``detectedAt`` so T6 can COUNT them and B1 can RENDER them, and a
prose string would serve neither. The same applies here — a reason composed as
a string at the point of adjustment could not be counted, filtered, aggregated
across a portfolio, or re-rendered in another language or surface.

So :class:`AdjustmentReason` carries the contributing decision count and its
breakdown, the contributing outcome count and its verdicts, the direction and
magnitude of the movement, whether a cap bound it, and the IDENTIFIERS of every
contributing decision and outcome. :func:`render_reason` turns that into the
customer-facing sentence, and every surface renders from the same structure
rather than composing its own.

**Links, not just counts (AC2).** "Links to the contributing decisions and
outcomes" means identifiers must survive into the record. Outcomes are
straightforward — A2's movement records carry ``baselineRunId`` and
``currentRunId`` as first-class columns precisely so a measured number resolves
to the runs that produced it. Decisions resolve by ``feedbackId``, which is why
T1 gave every decision a stable id rather than widening a mutable enum.

**The wording is guarded, not trusted.** ``learning_reason_vocabulary`` blocks
knowledge claims, importance claims, and — the subtle one — copy that would imply
the learned signal contributed to a finding's credibility. See that module for
why the third category is the one this subtask most needs.

**The reason explains the ORDERING and nothing else.** It is namespaced under
the finding's ``_ranking`` annotation and must never appear inside or beside
``confidence``, ``corroboration_*`` or the evidence trace. AC3 forbids the
adjustment touching those; explainability copy sitting among them would suggest
the learned signal contributed to the finding's credibility, which would violate
the spirit of AC3 while passing its letter. A contract test pins the placement.

**Honest about thin evidence.** An adjustment resting on three decisions and one
outcome says so, and says that is limited. The counts are already here, so this
is a rendering decision rather than extra machinery — and it is the difference
between a feature that survives scrutiny and one that invites it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .learning_reason_vocabulary import assert_clean

REASON_SCHEMA_VERSION = "1.0.0"

DIRECTION_UP = "up"
DIRECTION_DOWN = "down"
DIRECTION_NONE = "none"

#: How much evidence the adjustment rests on. Rendered as a plain-language
#: qualifier so a customer is told when it is thin, rather than discovering it.
STRENGTH_MINIMAL = "minimal"
STRENGTH_LIMITED = "limited"
STRENGTH_MODERATE = "moderate"
STRENGTH_SUBSTANTIAL = "substantial"

#: Thresholds for the qualifier above. A measured outcome counts double here for
#: the same reason it does in T1's weighting — but this only affects how the
#: sentence HEDGES, never the adjustment itself.
#:
#: Set so that "three decisions and one measured outcome" (weighted 5) reads as
#: LIMITED and says so. T1's cold start needs 10 weighted signals before learning
#: activates for an org at all, but a single finding TYPE can rest on far fewer,
#: and that is precisely the case the customer should be told about.
_SUBSTANTIAL_SIGNALS = 10
_MODERATE_SIGNALS = 6

_ACTION_VERB = {
    "accept": "accepted",
    "dismiss": "dismissed",
    "defer": "deferred",
}

#: Plain language for each A2 projection-validation verdict. Reports what was
#: measured; asserts nothing about what it means for this finding's credibility.
_VERDICT_PHRASE = {
    "within_band": "delivered measured improvement within the projected range",
    "above_band": "delivered measured improvement beyond the projected range",
    "below_band": "did not move as far as projected after the change was made",
    "not_projected": "was measured after action, with no projection to compare against",
    "too_early": "is being measured, but its projected horizon has not yet elapsed",
}

_DIRECTION_PHRASE = {
    "improved": "moved in the intended direction after the change was made",
    "worsened": "moved the wrong way after the change was made",
    "unchanged": "did not move after the change was made",
}

_NUMBER_WORD = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def _count_phrase(count: int, singular: str, plural: Optional[str] = None) -> str:
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{_NUMBER_WORD.get(count, count)} {noun}"


# --------------------------------------------------------------------------
# The links (AC2)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContributingDecision:
    """One analyst decision behind an adjustment, resolvable by id.

    ``feedback_id`` resolves against
    ``GET /api/learning/feedback/entry/{feedbackId}``. A decision with no stable
    id could not be linked to at all, which is why T1 stored decisions in their
    own append-only record rather than widening a mutable enum.
    """

    feedback_id: Optional[str]
    action: Optional[str]
    opportunity_identity: Optional[str] = None
    reason_code: Optional[str] = None
    actor_id: Optional[str] = None
    recorded_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "decision",
            "feedbackId": self.feedback_id,
            "action": self.action,
            "opportunityIdentity": self.opportunity_identity,
            "reasonCode": self.reason_code,
            "actorId": self.actor_id,
            "recordedAt": self.recorded_at,
            "href": (
                f"/api/learning/feedback/entry/{self.feedback_id}"
                if self.feedback_id
                else None
            ),
        }


@dataclass(frozen=True)
class ContributingOutcome:
    """One measured outcome behind an adjustment, resolvable by identity + run.

    Both run ids travel because A2 made them first-class columns for exactly
    this: an outcome number that cannot be traced to the runs that produced it
    is a number a customer cannot audit.
    """

    opportunity_identity: Optional[str]
    verdict: Optional[str]
    current_run_id: Optional[str] = None
    baseline_run_id: Optional[str] = None
    measured_direction: Optional[str] = None
    comparability_verdict: Optional[str] = None
    measured_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "outcome",
            "opportunityIdentity": self.opportunity_identity,
            "verdict": self.verdict,
            "currentRunId": self.current_run_id,
            "baselineRunId": self.baseline_run_id,
            "measuredDirection": self.measured_direction,
            "comparabilityVerdict": self.comparability_verdict,
            "measuredAt": self.measured_at,
            "href": (
                f"/api/opportunity-movement/{self.opportunity_identity}"
                if self.opportunity_identity
                else None
            ),
        }


# --------------------------------------------------------------------------
# The structured reason
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AdjustmentReason:
    """Why one finding moved — countable, filterable, and renderable.

    Deliberately carries no prose. The sentence is produced by
    :func:`render_reason` from these fields, so a caller can equally count
    reasons by direction, filter to capped ones, or aggregate verdicts across a
    portfolio — none of which is possible against a string.
    """

    direction: str
    ranks_moved: int
    base_rank: int
    adjusted_rank: int
    decision_count: int
    decisions_by_action: Dict[str, int]
    outcome_count: int
    outcomes_by_verdict: Dict[str, int]
    has_outcome_evidence: bool
    was_capped: bool
    capped_by: Optional[str]
    evidence_strength: str
    contributing_decisions: Tuple[ContributingDecision, ...] = ()
    contributing_outcomes: Tuple[ContributingOutcome, ...] = ()

    @property
    def total_signals(self) -> int:
        return self.decision_count + self.outcome_count

    @property
    def is_thin(self) -> bool:
        """Whether the rendered sentence should say the evidence is limited."""
        return self.evidence_strength in (STRENGTH_MINIMAL, STRENGTH_LIMITED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": REASON_SCHEMA_VERSION,
            "direction": self.direction,
            "ranksMoved": self.ranks_moved,
            "baseRank": self.base_rank,
            "adjustedRank": self.adjusted_rank,
            "decisionCount": self.decision_count,
            "decisionsByAction": dict(self.decisions_by_action),
            "outcomeCount": self.outcome_count,
            "outcomesByVerdict": dict(self.outcomes_by_verdict),
            "hasOutcomeEvidence": self.has_outcome_evidence,
            "wasCapped": self.was_capped,
            "cappedBy": self.capped_by,
            "evidenceStrength": self.evidence_strength,
            "totalSignals": self.total_signals,
            "contributingDecisions": [d.to_dict() for d in self.contributing_decisions],
            "contributingOutcomes": [o.to_dict() for o in self.contributing_outcomes],
            # Rendered from the fields above, never composed independently. Kept
            # on the payload so every surface shows identical wording rather than
            # each writing its own — the A1 T5 discipline.
            "summary": render_reason(self),
        }


def _strength(decision_count: int, outcome_count: int) -> str:
    """How much evidence this rests on. Outcomes count double, for hedging only.

    This affects only how strongly the sentence hedges. It never affects the
    adjustment — the weighting that does live in T1's config, and duplicating it
    here would let the two disagree about what an outcome is worth.
    """
    weighted = decision_count + (outcome_count * 2)
    if weighted >= _SUBSTANTIAL_SIGNALS:
        return STRENGTH_SUBSTANTIAL
    if weighted >= _MODERATE_SIGNALS:
        return STRENGTH_MODERATE
    if weighted >= 2:
        return STRENGTH_LIMITED
    return STRENGTH_MINIMAL


def build_reason(adjustment: Any) -> Optional[AdjustmentReason]:
    """Build the structured reason for one :class:`OpportunityAdjustment`.

    Returns ``None`` when the finding did not move: a reason with nothing to
    explain is noise, and rendering "this did not move because…" on every
    unadjusted finding would bury the ones that did.
    """
    if adjustment is None:
        return None
    moved = getattr(adjustment, "moved", 0)
    if not moved:
        return None

    decisions: List[ContributingDecision] = []
    outcomes: List[ContributingOutcome] = []
    for ref in getattr(adjustment, "contributing_refs", ()) or ():
        if not isinstance(ref, Mapping):
            continue
        kind = str(ref.get("kind") or "").strip().lower()
        if kind == "decision":
            decisions.append(
                ContributingDecision(
                    feedback_id=ref.get("feedbackId"),
                    action=ref.get("action"),
                    opportunity_identity=ref.get("opportunityIdentity"),
                    reason_code=ref.get("reasonCode"),
                    actor_id=ref.get("actorId"),
                    recorded_at=ref.get("recordedAt"),
                )
            )
        elif kind == "outcome":
            outcomes.append(
                ContributingOutcome(
                    opportunity_identity=ref.get("opportunityIdentity"),
                    verdict=ref.get("verdict"),
                    current_run_id=ref.get("currentRunId"),
                    baseline_run_id=ref.get("baselineRunId"),
                    measured_direction=ref.get("measuredDirection"),
                    comparability_verdict=ref.get("comparabilityVerdict"),
                    measured_at=ref.get("measuredAt"),
                )
            )

    by_action: Dict[str, int] = {}
    for decision in decisions:
        action = str(decision.action or "unknown").strip().lower()
        by_action[action] = by_action.get(action, 0) + 1

    by_verdict: Dict[str, int] = {}
    for outcome in outcomes:
        verdict = str(outcome.verdict or "unknown").strip().lower()
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1

    return AdjustmentReason(
        direction=DIRECTION_UP if moved < 0 else DIRECTION_DOWN,
        ranks_moved=abs(moved),
        base_rank=getattr(adjustment, "base_rank", 0),
        adjusted_rank=getattr(adjustment, "adjusted_rank", 0),
        decision_count=len(decisions),
        decisions_by_action=by_action,
        outcome_count=len(outcomes),
        outcomes_by_verdict=by_verdict,
        has_outcome_evidence=bool(getattr(adjustment, "has_outcome_evidence", False)),
        was_capped=bool(getattr(adjustment, "was_capped", False)),
        capped_by=getattr(adjustment, "capped_by", None),
        evidence_strength=_strength(len(decisions), len(outcomes)),
        contributing_decisions=tuple(decisions),
        contributing_outcomes=tuple(outcomes),
    )


# --------------------------------------------------------------------------
# Rendering — the sentence, composed from the structure
# --------------------------------------------------------------------------


def _decision_clause(reason: AdjustmentReason) -> Optional[str]:
    """"your team accepted 4 similar findings" — an observation, not a claim."""
    parts: List[str] = []
    for action in ("accept", "dismiss", "defer"):
        count = reason.decisions_by_action.get(action, 0)
        if not count:
            continue
        parts.append(
            f"{_ACTION_VERB[action]} {_count_phrase(count, 'similar finding')}"
        )
    if not parts:
        return None
    if len(parts) == 1:
        return f"your team {parts[0]}"
    return "your team " + ", ".join(parts[:-1]) + f" and {parts[-1]}"


def _outcome_clause(
    reason: AdjustmentReason, *, standalone: bool = True
) -> Optional[str]:
    """"one delivered measured improvement" — reporting A2's measurement.

    ``standalone=False`` drops the repeated "similar finding" subject when a
    decision clause has already established it, so the sentence reads as the
    story writes it rather than restating the noun twice.
    """
    if not reason.outcome_count:
        return None

    phrases: List[str] = []
    for verdict, count in sorted(reason.outcomes_by_verdict.items()):
        phrase = _VERDICT_PHRASE.get(verdict)
        if phrase is None:
            # An unrecognised verdict is reported as a bare measurement rather
            # than guessed at. Inventing wording for a verdict this code does
            # not know is how an explanation starts saying something untrue.
            phrase = "was measured after action"
        subject = (
            _NUMBER_WORD.get(count, str(count))
            if standalone is False
            else _count_phrase(count, "similar finding")
        )
        phrases.append(f"{subject} {phrase}")

    if len(phrases) == 1:
        return phrases[0]
    return ", ".join(phrases[:-1]) + f" and {phrases[-1]}"


def _basis_clause(reason: AdjustmentReason) -> str:
    """The honesty clause. Always present, and says so when evidence is thin."""
    counted: List[str] = []
    if reason.decision_count:
        counted.append(_count_phrase(reason.decision_count, "decision"))
    if reason.outcome_count:
        counted.append(_count_phrase(reason.outcome_count, "measured outcome"))
    basis = " and ".join(counted) if counted else "no recorded signals"

    sentence = f"Based on {basis}"
    if reason.is_thin:
        # Said plainly rather than implied. A customer told the evidence is
        # limited is prepared for the adjustment to change as more arrives; one
        # given a confident-sounding summary is not.
        sentence += ", which is limited evidence and may change as more arrives"
    return sentence + "."


def render_reason(reason: Optional[AdjustmentReason]) -> Optional[str]:
    """The customer-facing sentence, composed from the structured fields.

    Describes what was OBSERVED (decisions counted, outcomes measured) and what
    was DONE (ranks moved, cap applied). It never asserts that the platform knows
    better, and never implies the learned signal contributed to the finding's
    credibility — ``learning_reason_vocabulary`` enforces both, and this function
    checks its own output before returning it, because our templates must be
    clean by construction rather than scrubbed at the edge.
    """
    if reason is None:
        return None

    headline = (
        f"Ranked higher: moved up {_count_phrase(reason.ranks_moved, 'place')}"
        if reason.direction == DIRECTION_UP
        else f"Ranked lower: moved down {_count_phrase(reason.ranks_moved, 'place')}"
    )

    decision_clause = _decision_clause(reason)
    outcome_clause = _outcome_clause(reason, standalone=decision_clause is None)
    clauses = [c for c in (decision_clause, outcome_clause) if c]
    if clauses:
        headline += " because " + " and ".join(clauses)
    headline += "."

    parts = [headline, _basis_clause(reason)]

    if reason.was_capped:
        # The tension case, stated rather than hidden: the learned signal wanted
        # to move this further than the cap allows.
        parts.append(
            "The adjustment limit stopped this moving further than "
            f"{_count_phrase(reason.ranks_moved, 'place')}."
        )

    sentence = " ".join(parts)
    assert_clean(sentence, where="learned ranking reason")
    return sentence


def describe_adjustment(adjustment: Any) -> Optional[Dict[str, Any]]:
    """The full structured reason for one adjustment, ready to serve."""
    reason = build_reason(adjustment)
    return reason.to_dict() if reason is not None else None


#: Fields the reason must never be placed inside or beside. AC3 forbids the
#: adjustment touching a finding's evidence, confidence or corroboration; copy
#: sitting among them would tell a reader the learned signal contributed to the
#: finding's credibility, which is the AC3-spirit violation this subtask must
#: not commit.
CREDIBILITY_FIELDS = (
    "confidence",
    "corroboration_sources",
    "corroboration_label",
    "corroboration_rule_ids",
    "triple_corroboration",
    "evidenceIds",
    "evidence_ids",
    "evidenceItems",
    "aiRationale",
    "projection",
)


def reason_placement_violations(finding: Mapping[str, Any]) -> List[str]:
    """Every place a learned reason has leaked outside the ranking namespace.

    The serve-time half of the boundary. A structural test can prove the code
    does not write these today; this proves the SERVED payload does not carry
    them, which is what a customer actually sees — and it catches a reason that
    arrived by some path nobody thought to guard.
    """
    if not isinstance(finding, Mapping):
        return []

    violations: List[str] = []
    for field_name in CREDIBILITY_FIELDS:
        value = finding.get(field_name)
        if isinstance(value, Mapping) and (
            "reason" in value or "_ranking" in value or "learned" in value
        ):
            violations.append(field_name)
        elif isinstance(value, str) and _reads_as_learned_copy(value):
            violations.append(field_name)
    return violations


def _reads_as_learned_copy(text: str) -> bool:
    """Whether a credibility field has had ranking-reason copy written into it."""
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in ("ranked higher", "ranked lower", "similar findings", "moved up")
    )


__all__ = [
    "DIRECTION_DOWN",
    "DIRECTION_NONE",
    "DIRECTION_UP",
    "REASON_SCHEMA_VERSION",
    "STRENGTH_LIMITED",
    "STRENGTH_MINIMAL",
    "STRENGTH_MODERATE",
    "STRENGTH_SUBSTANTIAL",
    "CREDIBILITY_FIELDS",
    "AdjustmentReason",
    "ContributingDecision",
    "ContributingOutcome",
    "build_reason",
    "describe_adjustment",
    "reason_placement_violations",
    "render_reason",
]
