"""2.0-B3 T5 — the conversation MEDIUM ceiling, carried into assembly (AC5).

The standing rule: **conversation-derived content, on its own, never lifts a
finding above MEDIUM.** Chat is where people speculate, vent, and misremember. It
corroborates; it does not establish. That rule has held since R16-A2 at the
CORROBORATION layer, as COR-05 (Slack/Teams alone stays MEDIUM) plus the R16-C1 T3
clamp in ``corroboration_engine.apply_corroboration_confidence``, and this module
does not replace or duplicate either — ``discovery/packs/corroboration_rules.py``
remains the single source of the confidence vocabulary, imported here.

**Why the rule needs a second enforcement point in 2.0-B3.** COR-05 governs the
DETECTOR signal path: a Slack escalation pattern firing beside a finding. It knows
nothing about the retrieval substrate, which since R18-A4 indexes Slack and Teams
threads as ``conversation``-typed chunks that reach a finding through
``context_assembly`` — a path that did not exist when the ceiling was written and
that COR-05 never sees. 2.0-B3 T1 then made precedence **editable configuration**,
so ``source_type_ranks`` can be reordered to rank conversation first. Both are
legitimate; neither may be allowed to route around a safety rule. So the ceiling is
computed where the evidence is actually composed, and it is derived from the
evidence itself rather than from any policy the deployment can edit — which is what
makes "reordering the declaration cannot defeat the ceiling" true by construction
rather than by convention.

**What "on its own" means here, precisely.** The ceiling looks at EVIDENCE — the
retrieved chunks that support the claim — and applies when there is at least one
conversation-derived chunk and no evidence of any other source type. Graph entities
and relationships are deliberately not counted as the "other source": they are the
finding's subject, not an independent source agreeing with it, and treating the
mere presence of the entity a thread mentions as corroboration would let any
conversation-only finding clear the ceiling by naming something. That is the same
reasoning COR-08 applies when it refuses to let a single system self-corroborate.

**The ceiling caps; it never promotes and never lowers.** A LOW finding stays LOW.
Applying it to a finding already at or below MEDIUM is a no-op. It only ever removes
an elevation the evidence does not support.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple

try:  # Repo-root import style (tests add both roots to sys.path).
    from backend.app.assembly_policy_config import SOURCE_TYPE_CONVERSATION
    from backend.discovery.packs.corroboration_rules import (
        CONFIDENCE_MEDIUM,
        CONFIDENCE_ORDER,
    )
except ModuleNotFoundError:  # Runtime inside backend/ where the packages are top-level.
    from app.assembly_policy_config import SOURCE_TYPE_CONVERSATION
    from discovery.packs.corroboration_rules import (
        CONFIDENCE_MEDIUM,
        CONFIDENCE_ORDER,
    )

logger = logging.getLogger(__name__)

#: The ceiling itself. One definition, imported by every enforcement point — the
#: corroboration clamp, this module, and their tests — so no surface can hold a
#: different idea of how high conversation-only evidence may reach.
CONVERSATION_CEILING = CONFIDENCE_MEDIUM

#: The source types the retrieval substrate assigns to chat threads. Slack and Teams
#: both land on ``conversation`` (R18-A4's shared model), so a third chat platform
#: inherits the ceiling with no change here. ``chat`` is accepted as a defensive
#: synonym: a producer that labels a thread ``chat`` must not thereby escape the
#: ceiling, and the failure mode of over-matching (a ceiling applied where the
#: evidence was already MEDIUM) is strictly safer than under-matching.
CONVERSATION_SOURCE_TYPES: Tuple[str, ...] = (SOURCE_TYPE_CONVERSATION, "chat")

#: Explanation attached when the ceiling binds. Rendered from here, not composed at
#: the call site, so every surface says the same thing.
CEILING_REASON = (
    "Capped at MEDIUM: the supporting evidence for this finding is conversation "
    "content only. Conversation corroborates a finding; on its own it does not "
    "establish one."
)

KIND_EVIDENCE = "evidence"


def _source_type_of(item: Any) -> str:
    if isinstance(item, dict):
        raw = item.get("source_type") or item.get("content_type") or ""
    else:
        raw = getattr(item, "source_type", "") or getattr(item, "content_type", "") or ""
    return str(raw).strip().lower()


def is_conversation_source_type(value: Any) -> bool:
    """True iff ``value`` names a conversation-derived source type."""
    return str(value or "").strip().lower() in CONVERSATION_SOURCE_TYPES


@dataclass(frozen=True)
class CeilingAssessment:
    """Whether conversation-only evidence caps this finding, and the evidence for it.

    Deliberately reports the counts it decided from. A ceiling that fires with no
    visible basis is indistinguishable from a bug, and an analyst who cannot see why
    a finding stopped at MEDIUM will assume the platform is being arbitrary.
    """

    applies: bool
    conversation_evidence: int = 0
    other_evidence: int = 0
    ceiling: Optional[str] = None
    reason: Optional[str] = None

    @property
    def total_evidence(self) -> int:
        return self.conversation_evidence + self.other_evidence

    def to_dict(self) -> dict:
        return {
            "applies": self.applies,
            "ceiling": self.ceiling,
            "reason": self.reason,
            "conversation_evidence": self.conversation_evidence,
            "other_evidence": self.other_evidence,
        }


def _no_ceiling(conversation: int, other: int) -> CeilingAssessment:
    return CeilingAssessment(
        applies=False, conversation_evidence=conversation, other_evidence=other
    )


def assess_evidence(evidence: Iterable[Any]) -> CeilingAssessment:
    """Assess a sequence of evidence items (chunks, or evidence candidates).

    Untyped evidence counts as OTHER, not as conversation: the ceiling must fire on
    positive knowledge that the support is chat, never on a producer's silence. An
    unlabelled chunk making a finding non-conversation-only is the conservative
    direction — the alternative would cap findings for a missing metadata field.
    """
    conversation = 0
    other = 0
    for item in evidence or ():
        if is_conversation_source_type(_source_type_of(item)):
            conversation += 1
        else:
            other += 1
    if conversation and not other:
        return CeilingAssessment(
            applies=True,
            conversation_evidence=conversation,
            other_evidence=other,
            ceiling=CONVERSATION_CEILING,
            reason=CEILING_REASON,
        )
    return _no_ceiling(conversation, other)


def assess_candidates(candidates: Sequence[Any]) -> CeilingAssessment:
    """Assess assembly candidates, considering the EVIDENCE kind only.

    Entities and relationships are filtered out here rather than counted as "other
    evidence" — see the module docstring: the graph is the finding's subject, not an
    independent source that agrees with it.
    """
    return assess_evidence(
        [c for c in (candidates or ()) if str(getattr(c, "kind", "")) == KIND_EVIDENCE]
    )


def assess_package(package: Any) -> CeilingAssessment:
    """Assess an assembled :class:`~app.context_assembly.ContextPackage`."""
    return assess_evidence(getattr(package, "evidence", None) or [])


def apply_ceiling(
    confidence: Optional[str], assessment: CeilingAssessment
) -> Tuple[str, bool]:
    """Return ``(confidence, was_capped)`` after applying the ceiling.

    Caps only downward and only from above MEDIUM. An unknown or empty confidence is
    returned untouched: this function's job is to remove an unsupported elevation,
    not to invent a level for a caller that supplied none.
    """
    level = str(confidence or "").strip().upper()
    if not assessment.applies or level not in CONFIDENCE_ORDER:
        return (level or str(confidence or ""), False)
    if CONFIDENCE_ORDER[level] <= CONFIDENCE_ORDER[CONVERSATION_CEILING]:
        return (level, False)
    logger.warning(
        "conversation ceiling enforced — confidence clamped %s -> %s "
        "(conversation evidence=%d, other evidence=%d)",
        level, CONVERSATION_CEILING,
        assessment.conversation_evidence, assessment.other_evidence,
    )
    return (CONVERSATION_CEILING, True)


def cap_confidence(confidence: Optional[str], evidence: Iterable[Any]) -> Tuple[str, bool]:
    """Convenience: assess ``evidence`` and apply the ceiling in one call."""
    return apply_ceiling(confidence, assess_evidence(evidence))


def enforcement_points() -> List[str]:
    """Every place the ceiling is enforced, named.

    Exists so the regression suite can assert the list is complete rather than
    trusting a comment, and so a future author adding a fourth path is prompted to
    register it here.
    """
    return [
        "discovery.packs.corroboration_rules:COR-05 (conversation source alone "
        "never elevates)",
        "app.corroboration_engine.apply_corroboration_confidence (R16-C1 T3 clamp)",
        "app.conversation_ceiling.apply_ceiling (2.0-B3 T5, assembled evidence)",
    ]


__all__ = [
    "CEILING_REASON",
    "CONVERSATION_CEILING",
    "CONVERSATION_SOURCE_TYPES",
    "CeilingAssessment",
    "apply_ceiling",
    "assess_candidates",
    "assess_evidence",
    "assess_package",
    "cap_confidence",
    "enforcement_points",
    "is_conversation_source_type",
]
