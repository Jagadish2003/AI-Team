"""Narrative discipline enforcement — 2.0-B3 T4 (AC4).

The assembled narrative may only assert what the evidence supports. Every
asserted claim in a finding's narrative must **trace back to supporting
evidence**; unsupported connective language must not survive to the reader.
This module is the checker that makes that rule enforceable rather than
aspirational, and the contract test in
``tests/contract/test_r2_0_b3_t4_narrative_discipline.py`` is what blocks the
build when a claim does not trace.

What "asserted claim" means here. The unit of assertion in an AgentIQ finding
narrative is the **why-bullet** (``aiWhyBullets``): each bullet is a discrete
claim about why the finding is real, and — by the R16-B2/ENT-4 discipline the
grounded prompt already enforces (``llm_enrichment.build_grounded_opp_prompt``)
— each carries a leading provenance tag, ``[OBSERVED]`` or
``[INFERRED: <basis>]``. That tag IS the claim's declared link to its evidence,
so it is exactly the right thing to hold to account. Deliberately out of scope:
``aiSummary`` (synthesised prose over the bullets, not a discrete claim),
``aiRisks`` and ``aiSuggestedNextSteps`` (forward-looking conditionals and
recommended actions, not assertions of current fact). Flagging those would
train authors to ignore the guard — the same reasoning A1 T5's vocabulary guard
and A3 T3's copy guard use for *not* flagging the evidence itself.

What "traces back to supporting evidence" means. A claim resolves against a
:class:`SupportIndex` — the set of things the assembly actually composed the
finding from: the selected entity / relationship / evidence items of the
:class:`~app.context_assembly.ContextPackage`, plus the finding's own
``evidenceIds`` / ``grounding_evidence_ids``. Concretely:

  * an ``[OBSERVED]`` claim asserts observed fact, so it must REFERENCE the
    support — name a selected entity/relationship, or cite an evidence id —
    and the package must actually contain observed evidence. An observed claim
    that references nothing in the support set is a claim about the world with
    no evidence behind it: unsupported. (This is the same instruction the
    grounded prompt already gives the model — "Reference only the names and
    systems listed in the context" — now enforced rather than requested.)
  * an ``[INFERRED: <basis>]`` claim is allowed to go beyond the observed set,
    but the **named basis IS its link** to what it was inferred from, so an
    inference with an empty or missing basis is unsupported connective
    language and is flagged.
  * an untagged claim, or one tagged ``[UNVERIFIED]`` (the fallback tag
    ``llm_enrichment`` stamps on a bullet whose provenance it could not
    establish — ``_UNVERIFIED_TAG``), carries no declared evidence link at
    all and is unsupported by definition.

A claim may also be supplied in the FORWARD-LOOKING structured form 2.0-B1's
trace will render — ``{"text": ..., "evidenceIds": [...]}`` — in which case it
resolves iff it cites at least one evidence id and every cited id is in the
support set. So the checker is ready for B1's structured claims and works on
today's tagged strings alike; B1's trace is the *renderer* of the same
claim→evidence linkage this module *enforces*.

Design posture, mirroring ``discovery/projection/vocabulary.py``: this module is
PURE — no DB, no clock, no LLM, no ``app`` runtime state — so the same call is
made from a contract test and (when a caller wants it) from a serve boundary,
and its verdict is reproducible. It reports COPIES / value objects and never
mutates a narrative or a stored finding.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

__all__ = [
    "NARRATIVE_CLAIM_FIELDS",
    "TAG_OBSERVED",
    "TAG_INFERRED",
    "TAG_UNVERIFIED",
    "REASON_UNTAGGED",
    "REASON_UNVERIFIED",
    "REASON_OBSERVED_NO_SUPPORT",
    "REASON_INFERRED_NO_BASIS",
    "REASON_EVIDENCE_ID_NOT_IN_SUPPORT",
    "REASON_NO_EVIDENCE_CITED",
    "NarrativeClaimViolation",
    "SupportIndex",
    "UnsupportedNarrativeClaimError",
    "split_provenance_tag",
    "is_supported",
    "scan_claim",
    "scan_narrative",
    "assert_supported",
]


# The narrative field(s) whose entries are discrete asserted claims. Kept as a
# tuple (not a bare constant) so a future field that carries claims — should one
# be added — is a one-line change and every reader sees the whole set.
NARRATIVE_CLAIM_FIELDS: Tuple[str, ...] = ("aiWhyBullets",)

TAG_OBSERVED = "OBSERVED"
TAG_INFERRED = "INFERRED"
TAG_UNVERIFIED = "UNVERIFIED"

# Why a claim failed to trace. Stable string codes (not an enum) so they read
# the same in a JSON payload, a log line, and a test assertion.
REASON_UNTAGGED = "untagged_claim"
REASON_UNVERIFIED = "unverified_claim"
REASON_OBSERVED_NO_SUPPORT = "observed_claim_without_support"
REASON_INFERRED_NO_BASIS = "inferred_claim_without_basis"
REASON_EVIDENCE_ID_NOT_IN_SUPPORT = "cited_evidence_id_not_in_support"
REASON_NO_EVIDENCE_CITED = "structured_claim_cites_no_evidence"

# Matches a leading provenance tag: [OBSERVED], [INFERRED], [INFERRED: <basis>],
# or [UNVERIFIED]. Mirrors llm_enrichment._split_observation_tag so the two
# never disagree about what a tagged bullet looks like.
_TAG_RE = re.compile(
    r"^\s*\[(OBSERVED|INFERRED(?::[^\]]*)?|UNVERIFIED)\]\s*",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _norm(text: Any) -> str:
    """Lowercase and collapse whitespace — the one normalisation used for both
    support names and claim bodies, so a match means the same thing on each side."""
    return " ".join(str(text or "").split()).lower()


def split_provenance_tag(text: str) -> Tuple[str, Optional[str], str]:
    """Split a claim string into ``(tag, basis, body)``.

    ``tag`` is one of ``OBSERVED`` / ``INFERRED`` / ``UNVERIFIED`` (upper-cased),
    or ``""`` when the claim carries no recognised tag. ``basis`` is the text
    after ``INFERRED:`` (stripped) when present, else ``None``. ``body`` is the
    claim with its tag removed. Pure and total — never raises.
    """
    m = _TAG_RE.match(text or "")
    if not m:
        return "", None, (text or "").strip()
    raw = m.group(1)
    body = (text or "")[m.end():].strip()
    upper = raw.upper()
    if upper.startswith(TAG_INFERRED):
        basis = ""
        if ":" in raw:
            basis = raw.split(":", 1)[1].strip()
        return TAG_INFERRED, basis, body
    if upper.startswith(TAG_OBSERVED):
        return TAG_OBSERVED, None, body
    return TAG_UNVERIFIED, None, body


@dataclass(frozen=True)
class NarrativeClaimViolation:
    """One asserted claim that does not trace back to supporting evidence."""

    field: str          # the narrative field the claim came from
    index: int          # 0-based position within that field's list
    claim: str          # the claim text as written
    reason: str         # one of the REASON_* codes
    detail: str         # human-readable explanation for the failure message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field,
            "index": self.index,
            "claim": self.claim,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SupportIndex:
    """The evidence a finding's claims are allowed to trace to.

    Built from the assembly's own output so the checker holds a claim to the
    exact material the finding was composed from — never to an ambient corpus.
    """

    names: FrozenSet[str] = field(default_factory=frozenset)          # normalised entity/relationship names
    evidence_ids: FrozenSet[str] = field(default_factory=frozenset)   # normalised ids a claim may cite
    has_observed: bool = False                                        # any observed item present?

    @property
    def is_empty(self) -> bool:
        return not self.names and not self.evidence_ids

    def references(self, text: str) -> bool:
        """True iff ``text`` names a support entity or cites a support id.

        Name match is word-boundary and case-insensitive (``"Payments Team"``
        matches ``"...the Payments Team reassigned..."`` but not a substring of a
        larger word); id match is a normalised token match, so an id embedded in
        prose or supplied verbatim both resolve.
        """
        norm = _norm(text)
        if not norm:
            return False
        for name in self.names:
            if name and re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", norm):
                return True
        if self.evidence_ids:
            tokens = set(_WORD_RE.findall(norm))
            for eid in self.evidence_ids:
                # An id is referenced if it appears verbatim, or as a whole
                # token (ids are frequently hyphen/colon-joined, e.g. "chunk-1").
                if eid in norm or eid in tokens:
                    return True
        return False

    @classmethod
    def from_parts(
        cls,
        names: Iterable[str] = (),
        evidence_ids: Iterable[str] = (),
        has_observed: bool = True,
    ) -> "SupportIndex":
        """Build a support set from explicit parts (the direct/unit-test path)."""
        return cls(
            names=frozenset(_norm(n) for n in names if str(n or "").strip()),
            evidence_ids=frozenset(_norm(i) for i in evidence_ids if str(i or "").strip()),
            has_observed=bool(has_observed),
        )

    @classmethod
    def from_context_package(
        cls, package: Any, opportunity: Optional[Any] = None
    ) -> "SupportIndex":
        """Build the support set from an assembled :class:`ContextPackage`.

        Names come from the selected entities' and relationships' payloads
        (``display_name`` / ``canonical_name`` / ``name`` / ``subject``);
        evidence ids from every selected item's identifier fields plus, when an
        ``opportunity`` is supplied, its ``evidenceIds`` / ``grounding_evidence_ids``.
        ``has_observed`` is true iff any selected item declares ``origin ==
        'observed'`` — an observed claim needs observed evidence to stand on.
        """
        names: set = set()
        ids: set = set()
        has_observed = False

        entities = list(getattr(package, "entities", None) or [])
        relationships = list(getattr(package, "relationships", None) or [])
        evidence = list(getattr(package, "evidence", None) or [])

        for item in entities + relationships + evidence:
            for key in ("display_name", "canonical_name", "name", "subject", "title"):
                val = _get(item, key)
                if val:
                    names.add(str(val))
            for key in (
                "entity_id",
                "relationship_id",
                "chunk_id",
                "evidence_id",
                "candidate_id",
                "id",
            ):
                val = _get(item, key)
                if val:
                    ids.add(str(val))
            if str(_get(item, "origin") or "").strip().lower() == "observed":
                has_observed = True

        if opportunity is not None:
            for key in ("evidenceIds", "grounding_evidence_ids"):
                for eid in (_get(opportunity, key) or []):
                    if str(eid or "").strip():
                        ids.add(str(eid))

        return cls.from_parts(names=names, evidence_ids=ids, has_observed=has_observed)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a dict or an attribute-bearing object."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _claim_text(claim: Any) -> str:
    """The written text of a claim, whether a plain string or a structured dict."""
    if isinstance(claim, dict):
        return str(claim.get("text") or claim.get("claim") or "")
    return str(claim or "")


def is_supported(claim: Any, support: SupportIndex) -> bool:
    """True iff ``claim`` traces back to ``support``. See module docstring."""
    return _classify(claim, support) is None


def _classify(claim: Any, support: SupportIndex) -> Optional[Tuple[str, str]]:
    """Return ``(reason, detail)`` when the claim is unsupported, else ``None``."""
    # Structured, forward-looking (2.0-B1) form: the claim cites evidence ids
    # directly. Resolve those ids against the support set.
    if isinstance(claim, dict) and "evidenceIds" in claim:
        cited = [str(e) for e in (claim.get("evidenceIds") or []) if str(e or "").strip()]
        if not cited:
            return (
                REASON_NO_EVIDENCE_CITED,
                "structured claim carries an empty evidenceIds list",
            )
        missing = [c for c in cited if _norm(c) not in support.evidence_ids]
        if missing:
            return (
                REASON_EVIDENCE_ID_NOT_IN_SUPPORT,
                f"cited evidence id(s) not in the support set: {sorted(missing)}",
            )
        return None

    text = _claim_text(claim)
    if not text.strip():
        return None  # an empty entry asserts nothing; nothing to trace.

    tag, basis, body = split_provenance_tag(text)

    if tag == "":
        return (
            REASON_UNTAGGED,
            "claim carries no provenance tag, so it declares no link to evidence",
        )
    if tag == TAG_UNVERIFIED:
        return (
            REASON_UNVERIFIED,
            "claim is tagged UNVERIFIED — its provenance could not be established",
        )
    if tag == TAG_INFERRED:
        if not (basis or "").strip():
            return (
                REASON_INFERRED_NO_BASIS,
                "inferred claim names no basis, so it is unsupported connective language",
            )
        return None
    # tag == TAG_OBSERVED
    if support.is_empty or not support.has_observed:
        return (
            REASON_OBSERVED_NO_SUPPORT,
            "claim asserts observed fact but the package holds no observed evidence",
        )
    if not support.references(body):
        return (
            REASON_OBSERVED_NO_SUPPORT,
            "observed claim references no entity, relationship, or evidence id in the support set",
        )
    return None


def scan_claim(
    claim: Any, index: int, field_name: str, support: SupportIndex
) -> Optional[NarrativeClaimViolation]:
    """Classify one claim; return a violation or ``None``."""
    verdict = _classify(claim, support)
    if verdict is None:
        return None
    reason, detail = verdict
    return NarrativeClaimViolation(
        field=field_name,
        index=index,
        claim=_claim_text(claim),
        reason=reason,
        detail=detail,
    )


def scan_narrative(
    narrative: Any,
    support: SupportIndex,
    fields: Tuple[str, ...] = NARRATIVE_CLAIM_FIELDS,
) -> List[NarrativeClaimViolation]:
    """Every unsupported claim across the narrative's asserted-claim fields.

    ``narrative`` is the per-opportunity enrichment dict (or any mapping /
    object carrying the claim fields). Empty list means every asserted claim
    traces back to supporting evidence.
    """
    violations: List[NarrativeClaimViolation] = []
    for field_name in fields:
        claims = _get(narrative, field_name) or []
        if not isinstance(claims, (list, tuple)):
            continue
        for index, claim in enumerate(claims):
            found = scan_claim(claim, index, field_name, support)
            if found is not None:
                violations.append(found)
    return violations


class UnsupportedNarrativeClaimError(AssertionError):
    """Raised by :func:`assert_supported` when a claim does not trace to evidence.

    Subclasses ``AssertionError`` so a boundary that wraps assembly in a guard
    fails loudly and a contract test reads naturally — the same choice
    ``discovery.projection.vocabulary.ProhibitedVocabularyError`` makes.
    """

    def __init__(self, where: str, violations: List[NarrativeClaimViolation]):
        self.where = where
        self.violations = violations
        lines = "\n".join(
            f"  [{v.field}#{v.index}] {v.reason}: {v.detail}\n    claim: {v.claim!r}"
            for v in violations
        )
        super().__init__(
            f"Narrative discipline violated in {where}: "
            f"{len(violations)} claim(s) do not trace back to supporting evidence:\n"
            f"{lines}"
        )


def assert_supported(
    narrative: Any,
    support: SupportIndex,
    where: str,
    fields: Tuple[str, ...] = NARRATIVE_CLAIM_FIELDS,
) -> None:
    """Raise :class:`UnsupportedNarrativeClaimError` if any claim is unsupported.

    The boundary form of :func:`scan_narrative`, for a caller that wants
    narrative discipline enforced at composition time rather than only in CI.
    """
    violations = scan_narrative(narrative, support, fields=fields)
    if violations:
        raise UnsupportedNarrativeClaimError(where, violations)
