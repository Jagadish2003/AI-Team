"""2.0-A3 T3 — what AgentIQ may never say about a learned ranking adjustment.

The A1 T5 precedent, applied to a different failure. ``discovery/projection/
vocabulary.py`` blocks guarantee language and point-estimate savings claims
because "this will reduce cost by 40%" overclaims. Learning-reason copy fails the
same way through a different door: *"we learned this is more important for you"*
asserts that the platform knows better, when all it observed was that a team
clicked accept four times.

**Three prohibited categories**, each a distinct overclaim:

1. **Knowledge claims** — the platform asserting understanding, learning, or
   judgement of its own ("we learned", "AgentIQ understands", "we know your
   priorities"). What actually happened is that decisions were counted.
2. **Importance claims** — asserting a finding matters more, is more valuable, or
   should be prioritised. The layer changed an ORDER; it did not discover worth.
3. **Credibility implications** — the subtle one, and the reason this module
   exists rather than a shorter word list. AC3 forbids the adjustment touching
   evidence, confidence or corroboration. Copy saying "we are more confident in
   this" or "your decisions corroborate this finding" would violate the SPIRIT of
   that criterion while passing its letter, because a reader would reasonably
   conclude the learned signal contributed to the finding's credibility. It did
   not, and must never appear to.

**What is deliberately NOT prohibited**, following A1 T5's rule that a guard
which flags the evidence trains people to ignore it:

* the counts themselves — "4 decisions", "1 measured outcome";
* what the customer did — "your team accepted", "your team dismissed". That is an
  OBSERVATION about the customer, not a claim by the platform;
* what was measured — "delivered measured improvement", "did not move as far as
  projected". A2 measured those; reporting a measurement is not overclaiming;
* what was done — "ranked higher", "moved up 2 places", "the cap limited this".

The guard runs at BUILD time on our own templates (a template must be clean by
construction) and at SERVE time on the payload, because a template that is clean
today is not a control over what someone adds tomorrow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

LEARNING_VOCABULARY_VERSION = "1.0.0"

CATEGORY_KNOWLEDGE_CLAIM = "knowledge_claim"
CATEGORY_IMPORTANCE_CLAIM = "importance_claim"
CATEGORY_CREDIBILITY_IMPLICATION = "credibility_implication"


class ProhibitedLearningCopyError(ValueError):
    """Raised when our own reason copy would overclaim."""


@dataclass(frozen=True)
class LearningCopyViolation:
    """One prohibited phrase in one piece of reason copy."""

    category: str
    rule: str
    matched_text: str
    path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "rule": self.rule,
            "matchedText": self.matched_text,
            "path": self.path,
        }

    def __str__(self) -> str:  # pragma: no cover - diagnostic only
        where = f" at {self.path}" if self.path else ""
        return f"{self.category} ({self.rule}){where}: {self.matched_text!r}"


# --------------------------------------------------------------------------
# 1. Knowledge claims — the platform asserting it learned or understands
# --------------------------------------------------------------------------

_KNOWLEDGE_RULES: Tuple[Tuple[str, str], ...] = (
    # "we learned", "we have learned", "AgentIQ learned", "the system learned"
    (
        "learned_that",
        r"\b(?:we|agentiq|the (?:system|platform|model))\b[^.;]{0,20}\b"
        r"(?:learned|learnt|have learned|has learned)\b",
    ),
    # "we know", "we understand", "we've noticed", "we believe"
    (
        "platform_knows",
        r"\b(?:we|agentiq|the (?:system|platform|model))\b[^.;]{0,20}\b"
        r"(?:know|knows|understand|understands|believe|believes|think|thinks|"
        r"noticed|recognise|recognize|realised|realized)\b",
    ),
    # "based on what we know about you", "your preferences", "tailored to you"
    ("personalisation_claim", r"\b(?:personalis|personaliz|tailor)\w*\b"),
    ("smarter_claim", r"\b(?:smarter|gets smarter|more intelligent|learns from you)\b"),
    ("prediction_claim", r"\b(?:we (?:predict|expect|anticipate)|predicted for you)\b"),
)

# --------------------------------------------------------------------------
# 2. Importance claims — asserting worth rather than reporting an order change
# --------------------------------------------------------------------------

_IMPORTANCE_RULES: Tuple[Tuple[str, str], ...] = (
    (
        "more_important",
        r"\b(?:more|most|less|least|highly|especially)\s+"
        r"(?:important|valuable|relevant|significant|critical|urgent)\b",
    ),
    ("matters_more", r"\b(?:matters?|counts?)\s+(?:more|most|less)\b",),
    (
        "should_prioritise",
        r"\b(?:should|must|need to|ought to)\s+"
        r"(?:be\s+)?(?:prioriti[sz]e|prioriti[sz]ed|focus|address|tackle|do)\b",
    ),
    (
        "best_for_you",
        r"\b(?:best|better|right|ideal|optimal)\s+(?:for (?:you|your team)|choice|option)\b",
    ),
    ("top_priority", r"\b(?:top|highest)\s+priority\b"),
    ("we_recommend", r"\bwe\s+(?:recommend|suggest|advise)\b"),
)

# --------------------------------------------------------------------------
# 3. Credibility implications — the AC3-spirit rule
# --------------------------------------------------------------------------

_CREDIBILITY_RULES: Tuple[Tuple[str, str], ...] = (
    (
        "confidence_implication",
        r"\b(?:more|higher|increased|greater|raised|stronger)\s+confiden\w*\b"
        r"|\bconfiden\w*\s+(?:is\s+)?(?:higher|increased|raised|improved)\b",
    ),
    (
        "corroboration_implication",
        r"\bcorroborat\w*\b",
    ),
    (
        "evidence_strength_implication",
        r"\b(?:stronger|better|more|additional|further)\s+evidence\b"
        r"|\bevidence\s+(?:is\s+)?(?:stronger|improved)\b",
    ),
    (
        # Requires an OBJECT, not a bare verb. "Prove value fast with low-effort
        # quick wins" is the SHIPPED Next-30-Days roadmap summary — ordinary
        # business copy this guard does not own. Flagging it would be exactly the
        # false positive A1 T5 warns trains people to ignore the guard. The
        # failure actually being caught is a claim about a FINDING.
        "verification_claim",
        r"\b(?:confirms?|confirmed|validates?|validated|proves?|proven|verifies|"
        r"verified)\s+(?:this|that|it|the (?:finding|pattern|opportunity))\b"
        r"|\b(?:confirmed|validated|proven|verified)\s+by\b",
    ),
    (
        "reliability_claim",
        r"\b(?:more|less)\s+(?:reliable|trustworthy|certain|accurate)\b",
    ),
)

_ALL_RULES: Tuple[Tuple[str, str, str], ...] = tuple(
    [(CATEGORY_KNOWLEDGE_CLAIM, name, pattern) for name, pattern in _KNOWLEDGE_RULES]
    + [(CATEGORY_IMPORTANCE_CLAIM, name, pattern) for name, pattern in _IMPORTANCE_RULES]
    + [
        (CATEGORY_CREDIBILITY_IMPLICATION, name, pattern)
        for name, pattern in _CREDIBILITY_RULES
    ]
)

_COMPILED: Tuple[Tuple[str, str, "re.Pattern[str]"], ...] = tuple(
    (category, name, re.compile(pattern, re.IGNORECASE))
    for category, name, pattern in _ALL_RULES
)


def scan_text(text: Optional[str], path: str = "") -> List[LearningCopyViolation]:
    """Every prohibited phrase in one string, in a stable order."""
    if not text or not isinstance(text, str):
        return []
    violations: List[LearningCopyViolation] = []
    for category, rule, pattern in _COMPILED:
        for match in pattern.finditer(text):
            violations.append(
                LearningCopyViolation(
                    category=category,
                    rule=rule,
                    matched_text=match.group(0),
                    path=path,
                )
            )
    return violations


def contains_prohibited(text: Optional[str]) -> bool:
    return bool(scan_text(text))


#: Keys whose values are machine identifiers or measured numbers, never prose.
#: Skipped when sweeping a payload — a verdict code like ``within_band`` or a run
#: id is not customer-facing copy, and flagging it would train people to ignore
#: the guard (A1 T5's lesson, applied here).
_NON_PROSE_KEYS = frozenset(
    {
        "id",
        "feedbackId",
        "opportunityId",
        "opportunityIdentity",
        "detectorId",
        "packId",
        "runId",
        "currentRunId",
        "baselineRunId",
        "actorId",
        "verdict",
        "action",
        "reasonCode",
        "cappedBy",
        "direction",
        "measuredDirection",
        "comparabilityVerdict",
        "schemaVersion",
        "kind",
        "evidenceStrength",
    }
)


def scan_payload(
    value: Any, path: str = "", skip_keys: Iterable[str] = ()
) -> List[LearningCopyViolation]:
    """Recursively scan a reason payload or API response for prohibited copy."""
    skip = _NON_PROSE_KEYS | set(skip_keys)
    violations: List[LearningCopyViolation] = []

    if isinstance(value, str):
        violations.extend(scan_text(value, path))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in skip:
                continue
            child = f"{path}.{key}" if path else str(key)
            violations.extend(scan_payload(item, child, skip_keys))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child = f"{path}[{index}]"
            violations.extend(scan_payload(item, child, skip_keys))
    return violations


def assert_clean(text: Optional[str], where: str = "reason") -> None:
    """Raise when our own copy overclaims. Called at build time.

    Our templates must be clean by construction; a violation here is a bug in
    the template, not something to scrub at the edge.
    """
    violations = scan_text(text, where)
    if violations:
        raise ProhibitedLearningCopyError(
            f"{where} contains prohibited learning vocabulary: "
            + "; ".join(str(v) for v in violations)
        )


__all__ = [
    "CATEGORY_CREDIBILITY_IMPLICATION",
    "CATEGORY_IMPORTANCE_CLAIM",
    "CATEGORY_KNOWLEDGE_CLAIM",
    "LEARNING_VOCABULARY_VERSION",
    "LearningCopyViolation",
    "ProhibitedLearningCopyError",
    "assert_clean",
    "contains_prohibited",
    "scan_payload",
    "scan_text",
]
