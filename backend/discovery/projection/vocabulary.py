"""2.0-A1 T5 — the projection vocabulary guard.

AC3, stated in full: *"No projection output — API, UI, report, or export —
contains a point-estimate savings claim or guarantee language; template-level
check over the projection vocabulary."*

This module is that check.  It is the single place that decides what AgentIQ is
not allowed to say about a projection, and it is deliberately a runtime guard
rather than only a test: prompts drift, models improvise, and a customer-facing
report must not depend on an LLM's good manners.  The pre-existing note in
``llm_enrichment.py`` — *"post-generation validation of prohibited phrases is
deferred"* — is what this closes.

Two families are prohibited.

**Guarantee language.**  "will reduce", "will save", "guarantees", "eliminates",
"ensures".  A projection is a direction and a band; a promise is a different
kind of claim and AgentIQ does not make it.

**Point-estimate savings claims.**  "reduce cost by 40%", "40% cost reduction",
"saves 12 hours per week".  The problem is not the number — it is a *single*
number attached to a saving.  A magnitude BAND ("23–57% of the recurring
instances") is exactly what the platform is supposed to say, so the detector
below is careful to permit ranges and to fire only when a lone figure is bound
to savings/reduction vocabulary.

What is deliberately NOT prohibited:

* bands and ranges — "23–57%", "25 to 55 percent";
* measured, observed facts — "240 owner changes across 800 cases", "reassignment
  rate 2.4", "Impact 8/10".  These are measurements, not claims about the
  future, and stripping them would gut the evidence story;
* the word "reduce" in a *descriptive* clause with no promise and no figure
  ("cases are re-routed to reduce handling steps" is unhelpful but not a
  guarantee) — the guard fires on the promise forms, not on the verb alone.

Pure and dependency-free: no DB, no ``app`` import, no LLM, no clock.  The same
text always yields the same verdict, which is what lets a contract test sweep an
entire API payload and get a stable answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Bumped when the prohibited set changes in a way that would newly flag (or
#: newly permit) existing copy.
VOCABULARY_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Violation categories
# --------------------------------------------------------------------------

CATEGORY_GUARANTEE = "guarantee_language"
CATEGORY_POINT_ESTIMATE = "point_estimate_savings_claim"

#: Replacement used when a prohibited sentence is stripped from generated text.
#: Says what happened rather than silently deleting — a reader must never be
#: shown a doctored paragraph that reads as if it were written that way.
#: Note the wording: the notice must itself survive :func:`scan_text`, so it
#: describes what was removed WITHOUT restating any prohibited term.
REDACTION_NOTICE = (
    "[A projected-benefit claim was removed here: AgentIQ reports a magnitude "
    "band on measured signals, not a promised outcome.]"
)


# --------------------------------------------------------------------------
# Guarantee language
# --------------------------------------------------------------------------

#: Verbs that turn a projection into a promise when bound to a future auxiliary.
_PROMISE_VERBS = r"(?:save|saves|reduce|reduces|cut|cuts|eliminate|eliminates|remove|removes|deliver|delivers|achieve|achieves|guarantee|guarantees|ensure|ensures)"

#: Future/assertive auxiliaries that make a claim a commitment.
_PROMISE_AUXILIARIES = r"(?:will|shall|is going to|are going to|guaranteed to|is expected to deliver)"

_GUARANTEE_PATTERNS: Sequence[Tuple[str, "re.Pattern[str]"]] = (
    (
        "future-tense promise",
        re.compile(rf"\b{_PROMISE_AUXILIARIES}\s+{_PROMISE_VERBS}\b", re.IGNORECASE),
    ),
    (
        "guarantee wording",
        re.compile(r"\bguarantee(?:s|d|ing)?\b", re.IGNORECASE),
    ),
    (
        # "eliminate" claims TOTALITY — all of the thing, gone. No magnitude band
        # supports that, under any modal: "would eliminate the escalation cycles"
        # over-claims exactly as badly as "will eliminate" does, and coordinated
        # forms ("would reduce X and eliminate Y") put arbitrary distance between
        # the modal and the verb. So the whole family is prohibited in projection
        # copy rather than chased through grammar.
        #
        # Scope note: this guard only ever runs over payload strings and this
        # repo's own template constants — never over source comments — so the
        # ban costs nothing outside customer-facing copy.
        "absolute-outcome claim",
        re.compile(r"\b(?:eliminat|eradicat)(?:e|es|ed|ing|ion)\b", re.IGNORECASE),
    ),
    (
        "totality claim",
        re.compile(
            r"\b(?:will|would|shall|can|could|may|might)\s+"
            r"(?:completely|entirely|fully|totally)\s+\w+",
            re.IGNORECASE,
        ),
    ),
    (
        "assertive assurance",
        re.compile(r"\bensur(?:es|ing)\b", re.IGNORECASE),
    ),
    (
        "savings framing",
        re.compile(r"\b(?:cost )?savings?\b", re.IGNORECASE),
    ),
    (
        "return-on-investment framing",
        re.compile(r"\b(?:roi|return on investment|payback period)\b", re.IGNORECASE),
    ),
    (
        "risk-free framing",
        re.compile(r"\b(?:risk[- ]free|no[- ]risk|proven to)\b", re.IGNORECASE),
    ),
)


# --------------------------------------------------------------------------
# Point-estimate savings claims
# --------------------------------------------------------------------------

#: A range/band. Matched FIRST and masked out, so a legitimate band is never
#: mistaken for a point estimate. Covers "23-57%", "23–57%", "23 to 57%",
#: "between 23% and 57%".
_RANGE_RE = re.compile(
    r"""
    (?:between\s+)?
    \d+(?:\.\d+)?\s*%?
    \s*(?:-|–|—|to|and)\s*
    \d+(?:\.\d+)?\s*%
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Savings/benefit nouns and verbs a figure must not be bound to.
_SAVINGS_TERMS = (
    r"(?:sav\w*|reduc\w*|cut\w*|decreas\w*|improv\w*|gain\w*|uplift|efficienc\w*|"
    r"faster|quicker|cheaper|lower cost|less cost|productivity)"
)

#: A single figure — percentage, currency, or a quantity with a time unit.
_FIGURE = (
    r"(?:\d+(?:\.\d+)?\s*%"
    r"|[$£€]\s*\d[\d,]*(?:\.\d+)?\s*(?:k|m|bn|million|billion)?"
    r"|\d+(?:\.\d+)?\s*(?:hours?|hrs?|days?|weeks?|months?|fte)\b)"
)

#: Benefit NOUN/adjective forms — the shapes that turn a bare figure into a
#: claim ("40% reduction", "12 hours saved", "30% faster"). Deliberately
#: narrower than :data:`_SAVINGS_TERMS`: a *measured* value sitting near the
#: word "improvement" ("currently 120 days, and lower is the improvement") is a
#: description of direction, not a claimed benefit, and must not be flagged.
_BENEFIT_NOUNS = (
    r"(?:sav(?:ing|ings|ed)|reduction|decrease|improvement|gain|uplift|"
    r"faster|quicker|cheaper)"
)

_POINT_ESTIMATE_PATTERNS: Sequence[Tuple[str, "re.Pattern[str]"]] = (
    (
        # "reduce cost by 40%", "saves 12 hours", "savings of $120,000".
        # The claim leads, the figure follows within a short window.
        "figure bound to a savings claim",
        re.compile(rf"{_SAVINGS_TERMS}\W+(?:\w+\W+){{0,3}}?{_FIGURE}", re.IGNORECASE),
    ),
    (
        # "40% reduction", "12 hours saved", "30% faster". The figure leads and
        # the benefit noun QUALIFIES it, so at most one word may intervene —
        # any looser and a measured value in the same sentence as the word
        # "improvement" is wrongly read as a savings claim.
        "savings claim bound to a figure",
        re.compile(rf"{_FIGURE}\W+(?:\w+\W+)?{_BENEFIT_NOUNS}\b", re.IGNORECASE),
    ),
)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VocabularyViolation:
    """One prohibited phrase found in one piece of text."""

    category: str
    rule: str
    matched_text: str
    #: Where it was found — a dotted path when scanning a payload, else "".
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
# Scanning
# --------------------------------------------------------------------------


def _mask_ranges(text: str) -> str:
    """Blank out ranges so a band is never read as a point estimate.

    Replaced with same-length filler rather than removed, so any offsets a
    caller derives from the masked text still line up with the original.
    """
    return _RANGE_RE.sub(lambda m: "#" * len(m.group(0)), text)


def scan_text(text: Optional[str], path: str = "") -> List[VocabularyViolation]:
    """Every prohibited phrase in one string, in a stable order.

    Guarantee language is checked against the raw text; point-estimate claims
    are checked against the range-masked text so a legitimate band survives.
    """
    if not text or not isinstance(text, str):
        return []

    violations: List[VocabularyViolation] = []
    for rule, pattern in _GUARANTEE_PATTERNS:
        for match in pattern.finditer(text):
            violations.append(
                VocabularyViolation(CATEGORY_GUARANTEE, rule, match.group(0), path)
            )

    masked = _mask_ranges(text)
    for rule, pattern in _POINT_ESTIMATE_PATTERNS:
        for match in pattern.finditer(masked):
            # Report the ORIGINAL span, not the masked one, so the message is
            # readable and points at real copy.
            original = text[match.start() : match.end()]
            violations.append(
                VocabularyViolation(CATEGORY_POINT_ESTIMATE, rule, original, path)
            )
    return violations


def contains_prohibited(text: Optional[str]) -> bool:
    """True when ``text`` carries guarantee or point-estimate savings language."""
    return bool(scan_text(text))


#: Keys whose values are measured numbers or machine identifiers, never prose.
#: Skipped when sweeping a payload: a detector id like ``COST_REDUCTION_GAP`` or
#: a signal name is not customer-facing copy, and flagging it would train people
#: to ignore the guard.
_NON_PROSE_KEYS = frozenset(
    {
        "id",
        "oppId",
        "runId",
        "detectorId",
        "detector_id",
        "signalName",
        "signal_name",
        "signalKey",
        "signal_key",
        "instanceSignal",
        "populationSignal",
        "packId",
        "packVersion",
        "schemaVersion",
        "modelVersion",
        "evidenceIds",
        "axis",
        "concept",
    }
)


def scan_payload(
    value: Any,
    path: str = "",
    skip_keys: Iterable[str] = (),
) -> List[VocabularyViolation]:
    """Recursively scan an API/report payload for prohibited vocabulary.

    This is the "template-level check" AC3 asks for: point it at a whole
    serialized response, a report dict, or an export model and it returns every
    place the platform would have made a claim it must not make.
    """
    skip = _NON_PROSE_KEYS | set(skip_keys)
    violations: List[VocabularyViolation] = []

    if isinstance(value, str):
        violations.extend(scan_text(value, path))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if key in skip:
                continue
            violations.extend(
                scan_payload(item, f"{path}.{key}" if path else str(key), skip)
            )
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            violations.extend(scan_payload(item, f"{path}[{index}]", skip))
    return violations


# --------------------------------------------------------------------------
# Enforcement
# --------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def sanitize_text(text: Optional[str], notice: str = REDACTION_NOTICE) -> str:
    """Drop the sentences that carry prohibited claims, keep the rest.

    Sentence-level rather than whole-field: an LLM paragraph is usually three
    good sentences and one over-claiming one, and throwing away the analysis to
    punish one clause serves nobody.

    When every sentence offends, the notice alone is returned — an explicit
    "something was removed here" always beats silently emitting an empty field
    that reads as "the model had nothing to say".
    """
    if not text or not isinstance(text, str):
        return ""
    if not contains_prohibited(text):
        return text

    kept = [s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s and not contains_prohibited(s)]
    if not kept:
        return notice
    cleaned = " ".join(kept).strip()
    # A sentence carrying the claim may have been the only one; if what is left
    # is too thin to be an analysis, say so rather than shipping a fragment.
    return cleaned if len(cleaned) >= 20 else f"{cleaned} {notice}".strip()


def sanitize_bullets(
    bullets: Optional[Sequence[Any]], notice: str = REDACTION_NOTICE
) -> List[str]:
    """Drop bullets carrying prohibited claims.

    Bullets are dropped whole rather than rewritten: a bullet is already one
    claim, so a partially-redacted bullet is a fragment, not a finding.
    """
    if not bullets:
        return []
    return [
        str(bullet)
        for bullet in bullets
        if str(bullet).strip() and not contains_prohibited(str(bullet))
    ]


class ProhibitedVocabularyError(AssertionError):
    """Raised when text that must be clean by construction is not.

    Used for AgentIQ's OWN templates — a static string this repo authored has no
    excuse for containing a guarantee, and should fail loudly at build/test time
    rather than being quietly sanitized in production.
    """


def assert_clean(text: Optional[str], where: str = "text") -> None:
    """Raise when ``text`` carries prohibited vocabulary. For our own copy."""
    violations = scan_text(text, where)
    if violations:
        raise ProhibitedVocabularyError(
            f"{where} contains prohibited projection vocabulary: "
            + "; ".join(str(v) for v in violations)
        )


__all__ = [
    "VOCABULARY_VERSION",
    "CATEGORY_GUARANTEE",
    "CATEGORY_POINT_ESTIMATE",
    "REDACTION_NOTICE",
    "ProhibitedVocabularyError",
    "VocabularyViolation",
    "assert_clean",
    "contains_prohibited",
    "sanitize_bullets",
    "sanitize_text",
    "scan_payload",
    "scan_text",
]
