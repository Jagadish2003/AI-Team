"""2.0-B3 T1 — the context assembler's policy as DECLARED CONFIGURATION (AC1).

R16-B2 built the assembler with the right discipline — a fixed, documented,
deterministic sequence of rules — but the *precedence* lived in code: the rank key
was a hardcoded ``(-confidence, -freshness, id)`` tuple and "observed beats
inferred" was a boolean switching between two hardcoded orderings. Two consequences
this module exists to remove:

  * changing precedence meant editing ``context_assembly.py`` and shipping a
    deploy, so a deployment could not tune what its findings are composed from;
  * there was no source-type dimension at all, so a Slack thread and a ServiceNow
    incident competed on confidence alone. "Structured records outrank
    conversational content" was a stated principle with nothing enforcing it.

The policy now lives in ``config/assembly_policy.json``. Reordering ``ranking``
there changes composition — that is AC1, and a test proves it by reordering the
declaration rather than patching code.

**Hard tiers versus soft preferences.** The declaration distinguishes them, because
collapsing the two would quietly weaken R16-B2 AC3. A dimension in
``budget_partitions`` is a HARD tier: every candidate in a better tier fills the
budget before any candidate in a worse tier is considered, so a worse-tier item can
never displace a better-tier item that fit. A dimension in ``ranking`` is a SOFT
preference applied within a tier. ``origin`` is hard by default (observed data
genuinely must not be displaced by a guess); ``source_type`` is soft, so a
highly-relevant conversation is not shut out entirely by the mere existence of a
structured record.

**Unknown values sort last, never first.** A value absent from a rank table gets a
rank worse than every declared value. An item earns precedence by declaring what it
is — the same fail-safe rule ``context_assembly`` already applies to provenance
(only an explicit ``observed`` earns observed precedence) and to freshness (undated
context scores least-fresh).

That rule is for CANDIDATES, not for the declaration. A rank-table KEY outside
:data:`KNOWN_DIMENSION_VALUES` is refused at load, and keys are normalised the same
way the lookup normalises the value it matches — so ``"Structured"`` is read as
``structured`` rather than becoming a key nothing can match, which would have sorted
structured records last, behind conversation, and inverted the very precedence the
declaration was written to set.

A missing or unparseable config RAISES rather than falling back to defaults: a
deployment that believes it configured its precedence and did not is worse off than
one told the file is broken. Loaded with an mtime cache so an edit is picked up
without a restart, and documentation-only keys (``_``-prefixed) are stripped.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = str(Path(__file__).parent / "config" / "assembly_policy.json")

# ── the declared dimension vocabulary ───────────────────────────────────────
#
# Enumerated, not free-form: a typo in the config must fail loudly at load rather
# than silently drop a precedence rule and change composition. Adding a dimension
# is a deliberate code change here plus an extractor in context_assembly.

DIMENSION_ORIGIN = "origin"
DIMENSION_SOURCE_TYPE = "source_type"
DIMENSION_CONFIDENCE = "confidence"
DIMENSION_FRESHNESS = "freshness"
DIMENSION_CANDIDATE_ID = "candidate_id"

KNOWN_DIMENSIONS: Tuple[str, ...] = (
    DIMENSION_ORIGIN,
    DIMENSION_SOURCE_TYPE,
    DIMENSION_CONFIDENCE,
    DIMENSION_FRESHNESS,
    DIMENSION_CANDIDATE_ID,
)

#: Dimensions that read a declared rank TABLE (as opposed to a value on the
#: candidate). Each one named in the declaration must have its table present.
TABLE_BACKED_DIMENSIONS: Dict[str, str] = {
    DIMENSION_ORIGIN: "origin_ranks",
    DIMENSION_SOURCE_TYPE: "source_type_ranks",
}

#: The values each rank table may be keyed on — the SAME enumerated-vocabulary rule
#: already applied to dimension NAMES, extended to the values, because the two fail
#: in opposite ways and only one of them was covered.
#:
#: A mistyped dimension name was always refused. A mistyped rank-table KEY was not:
#: the key was stored verbatim while ``_rank_key`` looks the candidate's value up
#: lower-cased, so ``"Structured"`` matched nothing, fell through to
#: :meth:`DeclaredAssemblyPolicy.unknown_rank`, and sorted structured records LAST —
#: behind conversation. That silently inverts the precedence this module exists to
#: declare ("structured records outrank conversational content"), and on ``origin``,
#: a hard budget tier, it would let inferred content displace observed.
#:
#: Keys are normalised (stripped, lower-cased) before matching, so casing in the
#: config is no longer significant; a value outside the vocabulary is refused.
#: Declaring a SUBSET is fine — an undeclared value legitimately sorts last via
#: ``unknown_rank``; that is the fail-safe for candidates, not a licence for typos.
KNOWN_DIMENSION_VALUES: Dict[str, Tuple[str, ...]] = {
    DIMENSION_ORIGIN: ("observed", "inferred"),
    DIMENSION_SOURCE_TYPE: ("structured", "prose", "code", "conversation"),
}

#: The source types the assembler understands. The three content types match the
#: retrieval substrate's chunk policies (``app/retrieval/chunking.py``); the fourth
#: is the knowledge graph and structured source records.
SOURCE_TYPE_STRUCTURED = "structured"
SOURCE_TYPE_PROSE = "prose"
SOURCE_TYPE_CODE = "code"
SOURCE_TYPE_CONVERSATION = "conversation"

#: Candidate kinds, in the vocabulary ``context_assembly`` uses. Declared here so
#: ``kind_precedence`` can be validated against a closed set — a typo there would
#: silently change which kind yields first when the total budget binds.
KIND_ENTITY = "entity"
KIND_RELATIONSHIP = "relationship"
KIND_EVIDENCE = "evidence"
KNOWN_KINDS: Tuple[str, ...] = (KIND_ENTITY, KIND_RELATIONSHIP, KIND_EVIDENCE)


class AssemblyPolicyConfigError(ValueError):
    """The declared assembly policy is missing, unreadable, or self-inconsistent."""


@dataclass(frozen=True)
class DeclaredAssemblyPolicy:
    """The policy exactly as declared, validated and ready to rank with.

    Frozen, and the rank tables are stored as plain dicts copied at load time, so a
    caller cannot mutate a shared declaration and change another caller's
    composition mid-run.
    """

    version: int
    budget_partitions: Tuple[str, ...]
    ranking: Tuple[str, ...]
    origin_ranks: Mapping[str, int]
    source_type_ranks: Mapping[str, int]
    freshness_halflife_days: float
    exclude_stale: bool
    confidence_floor: float
    max_entities: int
    max_relationships: int
    max_evidence_chunks: int
    #: 2.0-B3 T2 — the per-finding budget across ALL kinds. ``None`` disables it and
    #: leaves the per-kind caps as the only bound (the shipped default, because no
    #: calibration of prompt size against narrative quality exists yet).
    max_total_items: Optional[int] = None
    #: 2.0-B3 T2 — which kind yields first when the total budget binds. Trimmed from
    #: the LAST entry backwards, so the most substitutable kind is listed last.
    kind_precedence: Tuple[str, ...] = (KIND_ENTITY, KIND_RELATIONSHIP, KIND_EVIDENCE)
    #: 2.0-B3 T3 — which attributes two sources may be compared on and what counts as
    #: a material disagreement. Owned by ``app.context_contradictions``; declared here
    #: so a deployment has ONE assembly-policy file rather than two.
    contradictions: Optional[Any] = None
    source_path: str = ""

    @property
    def dimensions(self) -> Tuple[str, ...]:
        """Every dimension the declaration uses, hard tiers first."""
        return tuple(self.budget_partitions) + tuple(self.ranking)

    def rank_table(self, dimension: str) -> Mapping[str, int]:
        """The declared rank table for a table-backed dimension."""
        if dimension == DIMENSION_ORIGIN:
            return self.origin_ranks
        if dimension == DIMENSION_SOURCE_TYPE:
            return self.source_type_ranks
        raise AssemblyPolicyConfigError(
            f"{dimension!r} is not a table-backed dimension"
        )

    def unknown_rank(self, dimension: str) -> int:
        """The rank given to a value absent from a table — worse than all of them.

        Not a large constant: derived from the table so it stays correct if the
        table grows. Fail-safe by construction, because the alternative (an unknown
        value sorting first) would let unclassified content outrank a declared
        structured record.
        """
        table = self.rank_table(dimension)
        return (max(table.values()) + 1) if table else 0

    def to_dict(self) -> Dict[str, Any]:
        """The declaration in serialisable form — recorded on a context package so
        a stored selection_log can be read against the rules that produced it."""
        return {
            "version": self.version,
            "budget_partitions": list(self.budget_partitions),
            "ranking": list(self.ranking),
            "origin_ranks": dict(self.origin_ranks),
            "source_type_ranks": dict(self.source_type_ranks),
            "freshness_halflife_days": self.freshness_halflife_days,
            "exclude_stale": self.exclude_stale,
            "confidence_floor": self.confidence_floor,
            "caps": {
                "entities": self.max_entities,
                "relationships": self.max_relationships,
                "evidence_chunks": self.max_evidence_chunks,
                "total_items": self.max_total_items,
            },
            "kind_precedence": list(self.kind_precedence),
            "contradictions": {
                "comparable_attributes": [
                    a.name for a in getattr(self.contradictions, "comparable_attributes", ())
                ],
                "numeric_tolerance_ratio": getattr(
                    self.contradictions, "numeric_tolerance_ratio", None
                ),
                "require_observed": getattr(
                    self.contradictions, "require_observed", None
                ),
                "min_distinct_sources": getattr(
                    self.contradictions, "min_distinct_sources", None
                ),
                "max_reported": getattr(self.contradictions, "max_reported", None),
            },
        }


_CACHE: Dict[str, Tuple[float, DeclaredAssemblyPolicy]] = {}


def _strip_meta(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Drop documentation-only keys (``_``-prefixed) so they cannot be read as
    configuration."""
    return {k: v for k, v in raw.items() if not str(k).startswith("_")}


def _require_mapping(raw: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise AssemblyPolicyConfigError(f"{where} must be an object, got {type(raw).__name__}")
    return raw


def _rank_table(raw: Any, where: str, *, dimension: str) -> Dict[str, int]:
    """Validate a declared rank table: known keys, integer ranks, non-empty.

    Keys are normalised the SAME way ``context_assembly._rank_key`` normalises the
    candidate value it looks up (stripped, lower-cased), so the config and the
    lookup cannot disagree about what ``"Structured"`` means. An unrecognised key is
    refused rather than stored: it could never match a candidate, so keeping it
    would silently drop a precedence rule — exactly what refusing an unknown
    dimension name already prevents on the other half of the declaration.
    """
    table = _require_mapping(raw, where)
    if not table:
        raise AssemblyPolicyConfigError(f"{where} must declare at least one rank")
    allowed = KNOWN_DIMENSION_VALUES.get(dimension, ())
    parsed: Dict[str, int] = {}
    seen: Dict[str, str] = {}
    for key, value in table.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise AssemblyPolicyConfigError(
                f"{where}[{key!r}] must be an integer rank, got {value!r}"
            )
        normalised = str(key).strip().lower()
        if allowed and normalised not in allowed:
            raise AssemblyPolicyConfigError(
                f"{where}[{key!r}] is not a known {dimension!r} value; known values "
                f"are {list(allowed)}. A key no candidate can match would sort that "
                f"content LAST via unknown_rank rather than at the rank declared "
                f"here, silently inverting the precedence it was meant to set, so it "
                f"is refused rather than ignored."
            )
        if normalised in seen:
            raise AssemblyPolicyConfigError(
                f"{where} declares {seen[normalised]!r} and {key!r}, which are the "
                f"same value once normalised ({normalised!r}). Keeping one and "
                f"discarding the other would be an arbitrary choice about precedence."
            )
        seen[normalised] = str(key)
        parsed[normalised] = int(value)
    return parsed


def _dimension_list(raw: Any, where: str) -> Tuple[str, ...]:
    """Validate a declared dimension sequence: known names, no duplicates."""
    if not isinstance(raw, (list, tuple)):
        raise AssemblyPolicyConfigError(f"{where} must be a list, got {type(raw).__name__}")
    names = [str(x) for x in raw]
    unknown = [n for n in names if n not in KNOWN_DIMENSIONS]
    if unknown:
        raise AssemblyPolicyConfigError(
            f"{where} names unknown dimension(s) {unknown}; known dimensions are "
            f"{list(KNOWN_DIMENSIONS)}. A typo here would silently drop a precedence "
            f"rule, so it is refused rather than ignored."
        )
    if len(set(names)) != len(names):
        raise AssemblyPolicyConfigError(f"{where} repeats a dimension: {names}")
    return tuple(names)


def _kind_list(raw: Any, where: str) -> Tuple[str, ...]:
    """Validate a declared kind ordering: known kinds, no duplicates, all present.

    Every kind must appear: a partial list would leave the trim order for the
    omitted kind undefined, and "undefined" here means a silently arbitrary choice
    about what gets dropped from a prompt.
    """
    if not isinstance(raw, (list, tuple)):
        raise AssemblyPolicyConfigError(f"{where} must be a list, got {type(raw).__name__}")
    names = [str(x) for x in raw]
    unknown = [n for n in names if n not in KNOWN_KINDS]
    if unknown:
        raise AssemblyPolicyConfigError(
            f"{where} names unknown kind(s) {unknown}; known kinds are {list(KNOWN_KINDS)}"
        )
    if len(set(names)) != len(names):
        raise AssemblyPolicyConfigError(f"{where} repeats a kind: {names}")
    missing = [k for k in KNOWN_KINDS if k not in names]
    if missing:
        raise AssemblyPolicyConfigError(
            f"{where} omits {missing} — every kind must be ordered, or the trim order "
            f"for the omitted kind is undefined"
        )
    return tuple(names)


def _positive_number(raw: Any, where: str, *, allow_zero: bool = False) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise AssemblyPolicyConfigError(f"{where} must be a number, got {raw!r}")
    value = float(raw)
    if value < 0 or (value == 0 and not allow_zero):
        raise AssemblyPolicyConfigError(f"{where} must be > 0, got {value}")
    return value


def _non_negative_int(raw: Any, where: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise AssemblyPolicyConfigError(f"{where} must be an integer, got {raw!r}")
    if raw < 0:
        raise AssemblyPolicyConfigError(f"{where} must be >= 0, got {raw}")
    return int(raw)


def parse_declared_policy(
    raw: Mapping[str, Any], *, source_path: str = ""
) -> DeclaredAssemblyPolicy:
    """Validate a raw declaration into a :class:`DeclaredAssemblyPolicy`.

    Separated from file loading so a test (or a caller experimenting with a
    precedence order) can validate a declaration built in memory — which is how the
    AC1 test changes precedence without touching either code or the shipped file.
    """
    data = _strip_meta(_require_mapping(raw, "assembly policy"))

    budget_partitions = _dimension_list(
        data.get("budget_partitions", []), "budget_partitions"
    )
    ranking = _dimension_list(data.get("ranking", []), "ranking")

    if not ranking:
        raise AssemblyPolicyConfigError(
            "ranking must declare at least one dimension — with none, candidates "
            "have no defined order and the package stops being reproducible"
        )
    if ranking[-1] != DIMENSION_CANDIDATE_ID:
        raise AssemblyPolicyConfigError(
            f"ranking must end with {DIMENSION_CANDIDATE_ID!r}: it is the stable "
            f"tiebreaker that makes the package byte-identical run to run. Got "
            f"{list(ranking)}."
        )
    overlap = set(budget_partitions) & set(ranking)
    if overlap:
        raise AssemblyPolicyConfigError(
            f"dimension(s) {sorted(overlap)} appear in BOTH budget_partitions and "
            f"ranking. A dimension is either a hard tier or a soft preference; "
            f"declaring both is ambiguous about whether it may displace."
        )

    origin_ranks = _rank_table(
        data.get("origin_ranks", {}), "origin_ranks", dimension=DIMENSION_ORIGIN
    )
    source_type_ranks = _rank_table(
        data.get("source_type_ranks", {}),
        "source_type_ranks",
        dimension=DIMENSION_SOURCE_TYPE,
    )
    # Every table-backed dimension actually in use must have a usable table.
    for dimension, table_key in TABLE_BACKED_DIMENSIONS.items():
        if dimension in budget_partitions or dimension in ranking:
            if not data.get(table_key):
                raise AssemblyPolicyConfigError(
                    f"dimension {dimension!r} is declared but {table_key!r} is "
                    f"missing or empty — its precedence would be undefined"
                )

    freshness = _require_mapping(data.get("freshness", {}), "freshness")
    halflife = _positive_number(
        freshness.get("halflife_days", 30.0), "freshness.halflife_days"
    )
    exclude_stale = freshness.get("exclude_stale", True)
    if not isinstance(exclude_stale, bool):
        raise AssemblyPolicyConfigError(
            f"freshness.exclude_stale must be true/false, got {exclude_stale!r}"
        )

    floor = data.get("confidence_floor", 0.0)
    if isinstance(floor, bool) or not isinstance(floor, (int, float)):
        raise AssemblyPolicyConfigError(f"confidence_floor must be a number, got {floor!r}")
    if not 0.0 <= float(floor) <= 1.0:
        raise AssemblyPolicyConfigError(
            f"confidence_floor must be within 0.0..1.0, got {floor}"
        )

    caps = _require_mapping(data.get("caps", {}), "caps")

    # 2.0-B3 T2 — the per-finding total budget. None/absent disables it; an integer
    # must be positive, because 0 would silently compose an EMPTY context for every
    # finding, which is a configuration mistake rather than a policy choice.
    raw_total = caps.get("total_items")
    if raw_total is None:
        total_items: Optional[int] = None
    else:
        total_items = _non_negative_int(raw_total, "caps.total_items")
        if total_items == 0:
            raise AssemblyPolicyConfigError(
                "caps.total_items must be > 0 or null — 0 would compose an empty "
                "context for every finding, which is a mistake rather than a policy"
            )

    kind_precedence = _kind_list(
        data.get("kind_precedence", list(KNOWN_KINDS)), "kind_precedence"
    )

    version = data.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise AssemblyPolicyConfigError(f"version must be an integer, got {version!r}")

    # 2.0-B3 T3 — the contradiction block. Parsed by the module that owns the concept
    # so its rules live in one place, and its error is re-raised as an
    # AssemblyPolicyConfigError so a caller catching this file's error type sees every
    # way this file can be wrong. An ABSENT block yields the documented defaults (a
    # config written before T3 still detects disagreements); a PRESENT but invalid one
    # raises, because an operator who configured this and got it wrong must be told.
    try:
        from .context_contradictions import (
            ContradictionConfigError,
            parse_contradiction_policy,
        )
    except ImportError:  # pragma: no cover — flat-layout import inside backend/.
        from app.context_contradictions import (  # type: ignore[no-redef]
            ContradictionConfigError,
            parse_contradiction_policy,
        )
    try:
        contradictions = parse_contradiction_policy(data.get("contradictions"))
    except ContradictionConfigError as exc:
        raise AssemblyPolicyConfigError(str(exc)) from exc

    return DeclaredAssemblyPolicy(
        version=int(version),
        budget_partitions=budget_partitions,
        ranking=ranking,
        origin_ranks=origin_ranks,
        source_type_ranks=source_type_ranks,
        freshness_halflife_days=halflife,
        exclude_stale=exclude_stale,
        confidence_floor=float(floor),
        max_entities=_non_negative_int(caps.get("entities", 15), "caps.entities"),
        max_relationships=_non_negative_int(
            caps.get("relationships", 20), "caps.relationships"
        ),
        max_evidence_chunks=_non_negative_int(
            caps.get("evidence_chunks", 10), "caps.evidence_chunks"
        ),
        max_total_items=total_items,
        kind_precedence=kind_precedence,
        contradictions=contradictions,
        source_path=source_path,
    )


def load_declared_policy(path: Optional[str] = None) -> DeclaredAssemblyPolicy:
    """Load and validate the declared assembly policy, mtime-cached.

    Raises :class:`AssemblyPolicyConfigError` when the file is missing or invalid —
    never silently substitutes defaults, because a deployment that thinks it
    configured its precedence and did not would compose findings differently from
    what its operators believe.
    """
    cfg_path = path or DEFAULT_CONFIG_PATH

    try:
        mtime = os.path.getmtime(cfg_path)
    except OSError as exc:
        raise AssemblyPolicyConfigError(
            f"Assembly policy config not found at {cfg_path!r}: {exc}"
        ) from exc

    cached = _CACHE.get(cfg_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise AssemblyPolicyConfigError(
            f"Assembly policy config at {cfg_path!r} could not be parsed: {exc}"
        ) from exc

    policy = parse_declared_policy(raw, source_path=cfg_path)
    _CACHE[cfg_path] = (mtime, policy)
    logger.info(
        "Loaded declared assembly policy v%d from %s (tiers=%s, ranking=%s)",
        policy.version, cfg_path, list(policy.budget_partitions), list(policy.ranking),
    )
    return policy


def clear_cache() -> None:
    """Drop the mtime cache. For tests that write a config and reload it within the
    same mtime granularity."""
    _CACHE.clear()


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DIMENSION_ORIGIN",
    "DIMENSION_SOURCE_TYPE",
    "DIMENSION_CONFIDENCE",
    "DIMENSION_FRESHNESS",
    "DIMENSION_CANDIDATE_ID",
    "KNOWN_DIMENSIONS",
    "KNOWN_DIMENSION_VALUES",
    "TABLE_BACKED_DIMENSIONS",
    "SOURCE_TYPE_STRUCTURED",
    "SOURCE_TYPE_PROSE",
    "SOURCE_TYPE_CODE",
    "SOURCE_TYPE_CONVERSATION",
    "KNOWN_KINDS",
    "AssemblyPolicyConfigError",
    "DeclaredAssemblyPolicy",
    "parse_declared_policy",
    "load_declared_policy",
    "clear_cache",
]
