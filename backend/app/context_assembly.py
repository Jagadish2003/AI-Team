"""R16-B2 — Context Assembly Foundation: deterministic context selection.

This module is the one place that decides, *deterministically and reproducibly*,
what context each opportunity is built from. Given an opportunity it produces a
single, bounded, ordered :class:`ContextPackage`: the exact set of entities,
relationships, and evidence chunks that downstream enrichment is allowed to see.

The core idea is **deterministic policy over probabilistic inputs**. The inputs
(a variable knowledge graph today; similarity-ranked retrieval in 1.8) are not
deterministic. The assembler's *rules* are. Same opportunity + same graph + same
:class:`AssemblyPolicy` always yields a byte-identical package, run to run. That
determinism is what keeps an LLM-and-vector-store system explainable.

The policy is applied as a FIXED SEQUENCE of rules (document Section 2). The
order itself is part of the contract — changing it changes results — so it is
explicit and tested here, not incidental:

  0. Exclude STALE candidates (a source artifact changed and its chunks are not
     yet refreshed) unless the policy opts in via ``include_stale``. Stale is
     the strongest exclusion — outdated evidence must never be served as current,
     regardless of its confidence — so it runs before the floor. Every stale
     exclusion is logged as ``excluded: stale`` (R18-B2 T4).              [AC6]
  1. Exclude anything below ``confidence_floor`` (weak context must never
     displace stronger context, even when budget remains).               [AC2]
  2. Partition each kind into ``observed`` and ``inferred`` candidates.
  3. Observed fills the budget first; inferred only fills the space left
     over — an inferred item can never displace an observed item that fit. [AC3]
  4. Within each partition, rank deterministically by ``confidence``, then
     ``freshness`` (half-life weighted), then a stable tiebreaker (the
     candidate id). Equal confidence + equal freshness => the id decides.  [AC5]
  5. Apply the hard caps (``max_entities`` / ``max_relationships`` /
     ``max_evidence_chunks``) so downstream LLM input stays bounded.       [AC4]
  6. Record every include/exclude decision in ``selection_log`` so the
     choice is recorded, not reconstructed.                               [AC6]

Rule 3 is the load-bearing one: it is the assembly-layer enforcement of
"observed beats inferred" — the same principle the Evidence & Identity Spine
(R16-B1) enforces at capture time, enforced again here at selection time.

Determinism note: freshness is measured against a reference derived from the
*inputs* (the newest candidate, or an explicit precomputed ``freshness_days``),
never the wall clock, so two calls at different times still produce a
byte-identical package (AC1). The selection_log is likewise fully ordered
independently of input order — included items by rank position, exclusions by a
stable key — so the audit trail itself is reproducible.

Forward-compatibility (Section 4): :func:`assemble_context` accepts an
``evidence_source`` that is ``None`` in 1.6 (no retrieval yet). When the
retrieval substrate lands in 1.8 it is passed here and the SAME rules (floor,
ranking, budget, logging) apply to retrieved evidence chunks — no caller change.
The hook is deliberately permissive (callable, retrieval-style object, or plain
iterable) and advisory (a failing source yields no evidence, never an error),
so a stub source verifies it today and the real substrate plugs in unchanged.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Tuple

try:  # Repo-root import style (tests add both roots to sys.path).
    from backend.app.graph_constants import (
        GRAPH_CONTEXT_MAX_ENTITIES,
        GRAPH_CONTEXT_MAX_RELATIONSHIPS,
    )
    from backend.app.provenance import INFERRED, OBSERVED
    from backend.app.assembly_policy_config import SOURCE_TYPE_STRUCTURED
except ModuleNotFoundError:  # Runtime inside backend/ where app is top-level.
    from app.graph_constants import (
        GRAPH_CONTEXT_MAX_ENTITIES,
        GRAPH_CONTEXT_MAX_RELATIONSHIPS,
    )
    from app.provenance import INFERRED, OBSERVED
    from app.assembly_policy_config import SOURCE_TYPE_STRUCTURED

logger = logging.getLogger(__name__)

# Budget for retrieved evidence chunks. The entity/relationship caps are the
# enterprise-safety constants owned by ``app.graph_constants`` (imported above,
# never redefined here); the evidence-chunk budget is new in R16-B2 and lives
# here until retrieval (1.8) gives it a permanent home.
DEFAULT_MAX_EVIDENCE_CHUNKS = 10

# Candidate kinds.
KIND_ENTITY = "entity"
KIND_RELATIONSHIP = "relationship"
KIND_EVIDENCE = "evidence"

# selection_log decisions and reasons (document Section 3).
DECISION_INCLUDED = "included"
DECISION_EXCLUDED = "excluded"
REASON_BELOW_FLOOR = "below_confidence_floor"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_RANKED_OUT = "ranked_out"
# R18-B2 T4: a candidate excluded because its source artifact changed and its
# chunks are stale (not yet re-embedded). Surfaced on the selection log as
# 'excluded: stale' so freshness exclusions are visible, never silent (AC6).
REASON_STALE = "stale"

# 2.0-B3 T1 — declared-dimension names, mirrored from app.assembly_policy_config so
# this module can build a rank key without importing the loader on every call (the
# loader imports nothing from here, and keeping the direction one-way avoids a
# cycle). The loader's KNOWN_DIMENSIONS is the authority; a contract test pins the
# two lists together so they cannot drift.
_ORIGIN_DIM = "origin"
_SOURCE_TYPE_DIM = "source_type"
_CONFIDENCE_DIM = "confidence"
_FRESHNESS_DIM = "freshness"
_CANDIDATE_ID_DIM = "candidate_id"


def _reason_included(position: int) -> str:
    """The Section-3 inclusion reason, carrying the 1-based rank position."""
    return f"included@position_{position}"


__all__ = [
    "AssemblyPolicy",
    "Candidate",
    "ContextPackage",
    "assemble_context",
    "select_candidates",
    "DEFAULT_MAX_EVIDENCE_CHUNKS",
    "KIND_ENTITY",
    "KIND_RELATIONSHIP",
    "KIND_EVIDENCE",
    "REASON_BELOW_FLOOR",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_RANKED_OUT",
    "REASON_STALE",
    "REASON_TOTAL_BUDGET",
    "SOURCE_TYPE_STRUCTURED",
    "KindBudget",
    "AssemblyBudgetReport",
]


# ---------------------------------------------------------------------------
# Policy + data shapes (Section 1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AssemblyPolicy:
    """Deterministic policy. Same policy + same inputs => same output.

    Frozen so a policy cannot be mutated mid-assembly (that would break the
    "same policy => same output" guarantee). The entity/relationship caps
    default to the shared ``graph_constants`` values rather than re-declaring
    the magic numbers locally.
    """

    max_entities: int = GRAPH_CONTEXT_MAX_ENTITIES            # hard cap (15)
    max_relationships: int = GRAPH_CONTEXT_MAX_RELATIONSHIPS  # hard cap (20)
    max_evidence_chunks: int = DEFAULT_MAX_EVIDENCE_CHUNKS    # budget (1.8+)
    confidence_floor: float = 0.0          # exclude context below this confidence
    observed_first: bool = True            # observed strictly before inferred
    freshness_halflife_days: float = 30.0  # older context weighed down
    include_stale: bool = False            # R18-B2 T4: admit stale candidates?
    # 2.0-B3 T2: the per-finding budget across ALL kinds. None leaves the per-kind
    # caps as the only bound, which is the shipped default — the value is
    # uncalibrated, and a guessed number here would trim every finding silently.
    max_total_items: Optional[int] = None

    # ── 2.0-B3 T1: precedence as DECLARED configuration (AC1) ──────────────
    #
    # ``declaration`` is the loaded ``config/assembly_policy.json``. When present
    # it OWNS precedence: which dimensions form hard budget tiers, the soft
    # ranking order within a tier, and the rank tables for origin and source type.
    # Reordering ``ranking`` in that file changes composition with no code change.
    #
    # None keeps the R16-B2 behaviour exactly — the fields above, ranked
    # confidence -> freshness -> id with observed as a hard tier — so every
    # existing caller and test is unaffected and this stays additive. Prefer
    # :meth:`declared` over constructing with a declaration by hand.
    declaration: Optional[Any] = None

    @classmethod
    def declared(
        cls, declaration: Optional[Any] = None, **overrides: Any
    ) -> "AssemblyPolicy":
        """Build a policy from the declared configuration (2.0-B3 T1 / AC1).

        Loads ``config/assembly_policy.json`` unless a parsed declaration is
        supplied. The caps, floor and freshness half-life come from the declaration
        too, so there is ONE place a deployment states them; ``overrides`` remains
        available for a caller with a legitimate per-call bound (a narrower budget
        for a small prompt, say) without editing the shared file.

        Raises ``AssemblyPolicyConfigError`` when the declaration is missing or
        invalid — never silently falls back to the in-code defaults, because a
        deployment that believes it configured precedence and did not would compose
        findings differently from what its operators think.
        """
        if declaration is None:
            from .assembly_policy_config import load_declared_policy

            declaration = load_declared_policy()
        base = dict(
            max_entities=declaration.max_entities,
            max_relationships=declaration.max_relationships,
            max_evidence_chunks=declaration.max_evidence_chunks,
            confidence_floor=declaration.confidence_floor,
            freshness_halflife_days=declaration.freshness_halflife_days,
            include_stale=not declaration.exclude_stale,
            max_total_items=declaration.max_total_items,
            observed_first=_ORIGIN_DIM in declaration.budget_partitions,
            declaration=declaration,
        )
        base.update(overrides)
        return cls(**base)


@dataclass(frozen=True)
class Candidate:
    """One item competing for a place in the context package.

    A candidate normalises the provenance the rules need (the same fields the
    Evidence & Identity Spine records): its ``origin`` (observed vs inferred),
    ``confidence``, and freshness — either a precomputed ``freshness_days`` or a
    ``source_timestamp`` it is derived from. ``candidate_id`` is the stable
    tiebreaker (an entity id, relationship id, or evidence/chunk id). ``payload``
    is the underlying object returned in the package when selected.
    """

    candidate_id: str
    kind: str                               # entity | relationship | evidence
    origin: str                             # observed | inferred
    confidence: float = 0.0
    source_timestamp: Optional[str] = None  # ISO-8601; None => unknown freshness
    payload: Any = None
    freshness_days: Optional[float] = None  # precomputed age in days, if known
    is_stale: bool = False                  # R18-B2 T4: source changed, not refreshed
    # 2.0-B3 T1: what KIND OF SOURCE this came from — structured | prose | code |
    # conversation. The dimension that makes "structured records outrank
    # conversational content" enforceable rather than merely stated; before this
    # there was nothing to rank a Slack thread against a ServiceNow incident by
    # except confidence. Empty means undeclared, which sorts LAST among declared
    # source types (an item earns precedence by declaring what it is).
    source_type: str = ""


@dataclass
class ContextPackage:
    """The single, bounded, ordered context an opportunity is built from."""

    entities: List[Any] = field(default_factory=list)        # selected, ordered, <= cap
    relationships: List[Any] = field(default_factory=list)   # selected, ordered, <= cap
    evidence: List[Any] = field(default_factory=list)        # empty until retrieval (1.8)
    policy_used: AssemblyPolicy = field(default_factory=AssemblyPolicy)
    selection_log: List[dict] = field(default_factory=list)  # why each item was in/out
    # 2.0-B3 T1: the DECLARATION that produced this package, serialised. A
    # selection_log read six months later has to be interpretable against the
    # precedence in force when it was written — and since that precedence is now
    # editable configuration, the log alone is no longer self-explaining. None when
    # no declaration was used (the R16-B2 in-code defaults).
    policy_declaration: Optional[dict] = None
    # 2.0-B3 T2 (AC2): what the per-finding budgets cost this package, in one
    # serialisable object. The selection_log always held the per-candidate decisions,
    # but answering "did I lose context, and to which budget?" meant parsing every
    # entry — so in practice nobody asked. Shaped for 2.0-B1's trace to render.
    budget_report: Optional[dict] = None


# ---------------------------------------------------------------------------
# 2.0-B3 T2 — budgeted composition: the drop record (AC2)
# ---------------------------------------------------------------------------

#: A candidate trimmed to satisfy the per-finding TOTAL budget, after it had
#: already won its per-kind competition. Distinct from ``budget_exhausted``
#: (crowded out within its own kind) because the remedy differs: this one says the
#: finding as a whole was too big, not that this kind was oversubscribed.
REASON_TOTAL_BUDGET = "total_budget"


@dataclass(frozen=True)
class KindBudget:
    """One kind's budget outcome — budget, what fit, what did not, and why.

    Mirrors MSP-B7's ``BudgetReport`` deliberately: that module established the
    repo's loud-degradation shape (budget / processed / deferred / breached /
    reason), and a reader who has seen one should recognise the other.
    """

    kind: str
    budget: int
    considered: int          # candidates that reached the ranking (post floor/stale)
    selected: int
    dropped_by_budget: int   # ranked below the cut, or crowded out by a better tier
    dropped_below_floor: int
    dropped_stale: int
    dropped_by_total_budget: int = 0  # trimmed afterwards by the per-finding total

    @property
    def offered(self) -> int:
        """Every candidate this kind was handed, including those excluded pre-ranking."""
        return self.considered + self.dropped_below_floor + self.dropped_stale

    @property
    def dropped(self) -> int:
        return (
            self.dropped_by_budget
            + self.dropped_below_floor
            + self.dropped_stale
            + self.dropped_by_total_budget
        )

    @property
    def breached(self) -> bool:
        """True iff the BUDGET is what cost this kind context.

        Deliberately not "anything was dropped": a below-floor or stale exclusion is
        a quality decision that would have happened with unlimited budget, and
        reporting those as a budget breach would send a reader to widen a budget that
        was never the constraint.
        """
        return (self.dropped_by_budget + self.dropped_by_total_budget) > 0

    @property
    def reason(self) -> Optional[str]:
        if not self.breached:
            return None
        parts = []
        if self.dropped_by_budget:
            parts.append(
                f"{self.dropped_by_budget} dropped against the {self.kind} budget of "
                f"{self.budget}"
            )
        if self.dropped_by_total_budget:
            parts.append(
                f"{self.dropped_by_total_budget} trimmed by the per-finding total budget"
            )
        return "; ".join(parts)

    def _replace_trimmed(self, trimmed: int) -> "KindBudget":
        """This kind's outcome with the total-budget trim recorded.

        No arithmetic on ``dropped_by_budget``: the trim RE-LABELS its log entries to
        ``total_budget`` before the report is derived, so they have already left that
        count. Subtracting here as well double-corrected it — a per-kind budget that
        genuinely dropped 5 reported 2, which made ``offered`` stop reconciling with
        ``selected + dropped``. A report that does not add up is worse than none.
        """
        if not trimmed:
            return self
        return KindBudget(
            kind=self.kind,
            budget=self.budget,
            considered=self.considered,
            selected=self.selected,
            dropped_by_budget=self.dropped_by_budget,
            dropped_below_floor=self.dropped_below_floor,
            dropped_stale=self.dropped_stale,
            dropped_by_total_budget=trimmed,
        )

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "budget": self.budget,
            "offered": self.offered,
            "considered": self.considered,
            "selected": self.selected,
            "dropped": self.dropped,
            "dropped_by_budget": self.dropped_by_budget,
            "dropped_by_total_budget": self.dropped_by_total_budget,
            "dropped_below_floor": self.dropped_below_floor,
            "dropped_stale": self.dropped_stale,
            "breached": self.breached,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AssemblyBudgetReport:
    """What the per-finding budgets cost this package (2.0-B3 T2 / AC2).

    The point is that a truncated context is never SILENT. The selection_log has
    always carried per-candidate decisions, but reading it required parsing every
    entry to answer "did I lose context, and to which budget?" — so in practice
    nobody asked. This is that answer, in one object, JSON-serialisable for the
    run record and shaped for 2.0-B1's trace to render.
    """

    per_kind: Tuple[KindBudget, ...] = ()
    total_budget: Optional[int] = None
    total_selected: int = 0

    @property
    def total_dropped(self) -> int:
        return sum(k.dropped for k in self.per_kind)

    @property
    def breached(self) -> bool:
        """True iff a budget — per-kind or total — cost this finding context."""
        return any(k.breached for k in self.per_kind)

    @property
    def reason(self) -> Optional[str]:
        if not self.breached:
            return None
        return "; ".join(k.reason for k in self.per_kind if k.reason)

    def to_dict(self) -> dict:
        return {
            "total_budget": self.total_budget,
            "total_selected": self.total_selected,
            "total_dropped": self.total_dropped,
            "breached": self.breached,
            "reason": self.reason,
            "per_kind": [k.to_dict() for k in self.per_kind],
        }


def _relabel_dropped(log: List[dict], candidate_id: str) -> None:
    """Mark an already-included log entry as dropped by the total budget.

    Mutates the entry in place rather than appending a second one: two entries for
    one candidate would make the log self-contradictory ("included" AND "excluded"),
    and every reader would then need to know which wins.
    """
    for entry in log:
        if entry.get("candidate_id") == candidate_id:
            entry["decision"] = DECISION_EXCLUDED
            entry["reason"] = REASON_TOTAL_BUDGET
            return


def _kind_budget(kind: str, budget: int, log: List[dict], selected: int) -> KindBudget:
    """Derive one kind's budget outcome from its own selection log.

    Derived from the log rather than counted alongside it, so the report and the
    log can never disagree about what happened — a report that drifted from the
    log would be worse than no report.
    """
    below = sum(1 for e in log if e.get("reason") == REASON_BELOW_FLOOR)
    stale = sum(1 for e in log if e.get("reason") == REASON_STALE)
    by_budget = sum(
        1 for e in log
        if e.get("reason") in (REASON_BUDGET_EXHAUSTED, REASON_RANKED_OUT)
    )
    return KindBudget(
        kind=kind,
        budget=budget,
        considered=len(log) - below - stale,
        selected=selected,
        dropped_by_budget=by_budget,
        dropped_below_floor=below,
        dropped_stale=stale,
    )


# ---------------------------------------------------------------------------
# Freshness — deterministic, derived from the inputs (never the wall clock)
# ---------------------------------------------------------------------------

def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to an aware datetime, or None.

    Naive timestamps are assumed UTC so aware/naive values are always
    comparable. Unparseable input degrades to None (treated as least-fresh)
    rather than raising — assembly must never break on a malformed timestamp.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _reference_timestamp(candidates: List[Candidate]) -> Optional[datetime]:
    """The freshness reference: the newest candidate timestamp in the input.

    Deriving the reference from the inputs (not ``datetime.now()``) is what
    makes the whole package deterministic across calls (AC1): the newest item
    has age 0, everything else decays relative to it.
    """
    stamps = [ts for ts in (_parse_ts(c.source_timestamp) for c in candidates) if ts]
    return max(stamps) if stamps else None


def _age_days(candidate: Candidate, reference: Optional[datetime]) -> Optional[float]:
    """Age of a candidate in days, or None when its freshness is unknown.

    A precomputed ``freshness_days`` wins when present (a producer may carry the
    age directly); otherwise the age is derived from ``source_timestamp``
    relative to the shared reference. None means undated (least fresh).
    """
    if candidate.freshness_days is not None:
        try:
            return max(0.0, float(candidate.freshness_days))
        except (TypeError, ValueError):
            return None
    ts = _parse_ts(candidate.source_timestamp)
    if ts is None or reference is None:
        return None
    return max(0.0, (reference - ts).total_seconds() / 86400.0)


def _freshness_score(
    candidate: Candidate, reference: Optional[datetime], halflife_days: float
) -> float:
    """Half-life weighted freshness in (0, 1]; 1.0 == as fresh as the reference.

    Older context decays geometrically by ``halflife_days``. Undated context
    scores 0.0 (least fresh), so timestamped/dated context outranks undated
    context at equal confidence.
    """
    age = _age_days(candidate, reference)
    if age is None:
        return 0.0
    if age <= 0:
        return 1.0
    if halflife_days <= 0:
        return 0.0
    return 0.5 ** (age / halflife_days)


def _freshness_days(candidate: Candidate, reference: Optional[datetime]):
    """The freshness recorded in the selection_log: the precomputed value when
    supplied, else whole-day age relative to the reference, else None."""
    if candidate.freshness_days is not None:
        return candidate.freshness_days
    ts = _parse_ts(candidate.source_timestamp)
    if ts is None or reference is None:
        return None
    return max(0, int((reference - ts).total_seconds() // 86400))


def _confidence(candidate: Candidate) -> float:
    """Confidence as a float, treating a missing value as 0.0 (weakest)."""
    return float(candidate.confidence) if candidate.confidence is not None else 0.0


def _is_observed(candidate: Candidate) -> bool:
    """Observed iff origin is exactly OBSERVED; anything else is inferred.

    Defaulting unknown/empty origins to inferred is fail-safe: an item can only
    earn observed precedence by explicitly declaring observed provenance.
    """
    return candidate.origin == OBSERVED


# ---------------------------------------------------------------------------
# The deterministic ordering rules (Section 2)
# ---------------------------------------------------------------------------

def _rank_key(candidate: Candidate, reference: Optional[datetime], halflife_days: float):
    """Total-order ranking key (Rule 4): confidence, then freshness, then id.

    Sorted ASCENDING this yields confidence DESC, freshness DESC, then
    ``candidate_id`` ASC. The id is the stable tiebreaker (AC5): two candidates
    with equal confidence and equal freshness always order identically.

    This is the R16-B2 key, retained verbatim as the behaviour used when no policy
    declaration is present. When one IS present, :func:`_declared_rank_key` builds
    the key from the declared ``ranking`` order instead (2.0-B3 T1 / AC1).
    """
    return (
        -_confidence(candidate),
        -_freshness_score(candidate, reference, halflife_days),
        candidate.candidate_id,
    )


def _dimension_value(
    candidate: Candidate,
    dimension: str,
    declaration: Any,
    reference: Optional[datetime],
    halflife_days: float,
):
    """One dimension's sort value for a candidate — ascending means "better first".

    The single place a declared dimension name becomes a comparable value. Adding a
    dimension means adding a branch here and a name to ``KNOWN_DIMENSIONS``; the
    loader refuses an unknown name, so a config typo fails at load rather than
    silently dropping a precedence rule.
    """
    if dimension == _CONFIDENCE_DIM:
        return -_confidence(candidate)
    if dimension == _FRESHNESS_DIM:
        return -_freshness_score(candidate, reference, halflife_days)
    if dimension == _CANDIDATE_ID_DIM:
        return candidate.candidate_id
    if dimension == _ORIGIN_DIM:
        value = OBSERVED if _is_observed(candidate) else INFERRED
        table = declaration.rank_table(_ORIGIN_DIM)
        return table.get(value, declaration.unknown_rank(_ORIGIN_DIM))
    if dimension == _SOURCE_TYPE_DIM:
        table = declaration.rank_table(_SOURCE_TYPE_DIM)
        return table.get(
            (candidate.source_type or "").strip().lower(),
            declaration.unknown_rank(_SOURCE_TYPE_DIM),
        )
    # Unreachable via the loader (it validates names), so this is a guard against a
    # declaration constructed in code that bypassed validation.
    raise ValueError(f"unknown assembly-policy dimension {dimension!r}")


def _declared_rank_key(
    candidate: Candidate,
    declaration: Any,
    reference: Optional[datetime],
    halflife_days: float,
):
    """Ranking key built from the DECLARED ``ranking`` order (2.0-B3 T1 / AC1).

    Lexicographic over the declared dimensions, so the first entry dominates and
    reordering the declaration reorders composition — with no code change. The
    loader guarantees the sequence ends in ``candidate_id``, which is what keeps the
    key a total order and the package byte-identical run to run.
    """
    return tuple(
        _dimension_value(candidate, dim, declaration, reference, halflife_days)
        for dim in declaration.ranking
    )


def _partition_key(
    candidate: Candidate, declaration: Any, reference: Optional[datetime], halflife: float
):
    """The HARD tier a candidate belongs to, best tier first.

    Everything in a better tier fills the budget before anything in a worse tier is
    considered, so a worse-tier candidate can never displace a better-tier one that
    fit. This is R16-B2 AC3 ("an inferred item can never displace an observed item")
    generalised: which dimensions are hard is now declared rather than implied by a
    single ``observed_first`` boolean.
    """
    return tuple(
        _dimension_value(candidate, dim, declaration, reference, halflife)
        for dim in declaration.budget_partitions
    )


def _log_entry(
    candidate: Candidate,
    decision: str,
    reason: str,
    reference: Optional[datetime],
) -> dict:
    """One Section-3 selection_log entry for a candidate."""
    return {
        "candidate_id": candidate.candidate_id,
        "kind": candidate.kind,
        "origin": OBSERVED if _is_observed(candidate) else INFERRED,
        # 2.0-B3 T1: recorded because source type is now a precedence dimension.
        # A log that showed confidence and freshness but not the source type could
        # not explain why a high-confidence conversation ranked below a weaker
        # structured record — the decision would look arbitrary.
        "source_type": candidate.source_type or "",
        "decision": decision,
        "reason": reason,
        "confidence": _confidence(candidate),
        "freshness_days": _freshness_days(candidate, reference),
    }


def select_candidates(
    candidates: List[Candidate],
    cap: int,
    policy: Optional[AssemblyPolicy] = None,
    reference: Optional[datetime] = None,
) -> Tuple[List[Candidate], List[dict]]:
    """Apply the six ordered rules to one kind of candidate.

    Returns the selected candidates (ordered, within ``cap``) and the
    selection_log for every input candidate. This is the engine the whole
    service is built on; :func:`assemble_context` runs it once per kind.

    The reference timestamp may be supplied so several kinds share one freshness
    frame; when omitted it is derived from ``candidates`` alone.
    """
    policy = policy or AssemblyPolicy()
    if reference is None:
        reference = _reference_timestamp(candidates)
    halflife = policy.freshness_halflife_days

    # Rule 0 — exclude STALE candidates unless the policy opts in. A stale chunk's
    # source artifact changed and its content is not yet refreshed; serving it as
    # current is the failure mode the freshness contract exists to prevent, so it
    # is excluded ahead of the confidence floor and regardless of confidence
    # (R18-B2 T4 / AC1). The exclusion is recorded as 'excluded: stale' below so
    # the decision is visible on the selection log, not a silent filter (AC6).
    stale: List[Candidate] = []
    fresh: List[Candidate] = []
    for candidate in candidates:
        if candidate.is_stale and not policy.include_stale:
            stale.append(candidate)
        else:
            fresh.append(candidate)

    # Rule 1 — exclude anything below the confidence floor. Done next so weak
    # context can never occupy budget ahead of stronger context (AC2).
    below_floor: List[Candidate] = []
    eligible: List[Candidate] = []
    for candidate in fresh:
        if _confidence(candidate) < policy.confidence_floor:
            below_floor.append(candidate)
        else:
            eligible.append(candidate)

    # Rules 2–4 — tier, then rank within tier.
    #
    # 2.0-B3 T1: when a policy DECLARATION is present it owns both steps — which
    # dimensions form hard budget tiers (``budget_partitions``) and the soft order
    # within a tier (``ranking``). Sorting by (tier, rank) as one lexicographic key
    # is exactly equivalent to grouping into tiers and concatenating them best-first,
    # so a worse-tier candidate still cannot displace a better-tier one that fit
    # (R16-B2 AC3, generalised). With no declaration the original R16-B2 path runs
    # unchanged.
    declaration = getattr(policy, "declaration", None)
    if declaration is not None:
        keyf = lambda c: (  # noqa: E731
            _partition_key(c, declaration, reference, halflife)
            + _declared_rank_key(c, declaration, reference, halflife)
        )
        ordered = sorted(eligible, key=keyf)
        # The best tier, used only to describe WHY a candidate past the cap missed
        # out (budget_exhausted vs ranked_out) on the selection log.
        observed = (
            [c for c in eligible if _is_observed(c)]
            if _ORIGIN_DIM in declaration.budget_partitions
            else []
        )
    else:
        # Rule 2 — partition into observed and inferred.
        observed = [c for c in eligible if _is_observed(c)]
        inferred = [c for c in eligible if not _is_observed(c)]

        # Rule 4 — rank deterministically within each partition.
        keyf = lambda c: _rank_key(c, reference, halflife)  # noqa: E731
        observed.sort(key=keyf)
        inferred.sort(key=keyf)

        # Rule 3 — observed fills the budget first; inferred only fills what's left.
        # Concatenating observed ahead of inferred makes "observed beats inferred"
        # structural: an inferred item can never displace an observed item that fit
        # (AC3). With observed_first off, the two partitions compete on rank alone.
        if policy.observed_first:
            ordered = observed + inferred
        else:
            ordered = sorted(eligible, key=keyf)

    # Rule 5 — apply the hard cap.
    cap = max(0, cap)
    selected = ordered[:cap]
    excluded = ordered[cap:]

    # Rule 6 — record every decision. The log is fully deterministic regardless
    # of input order (AC1): stale exclusions sorted by id, then below-floor entries
    # sorted by id, then included items by rank position, then ranked/budget
    # exclusions in ranked order.
    log: List[dict] = []
    for candidate in sorted(stale, key=lambda c: c.candidate_id):
        log.append(_log_entry(candidate, DECISION_EXCLUDED, REASON_STALE, reference))
    for candidate in sorted(below_floor, key=lambda c: c.candidate_id):
        log.append(_log_entry(candidate, DECISION_EXCLUDED, REASON_BELOW_FLOOR, reference))
    for position, candidate in enumerate(selected, start=1):
        log.append(
            _log_entry(candidate, DECISION_INCLUDED, _reason_included(position), reference)
        )
    observed_filled_budget = policy.observed_first and len(observed) >= cap
    for candidate in excluded:
        # An inferred candidate crowded out because observed alone consumed the
        # whole budget is "budget_exhausted" (Rule 3 in action). Anything else
        # simply ranked below the cut.
        if observed_filled_budget and not _is_observed(candidate):
            reason = REASON_BUDGET_EXHAUSTED
        else:
            reason = REASON_RANKED_OUT
        log.append(_log_entry(candidate, DECISION_EXCLUDED, reason, reference))

    return selected, log


# ---------------------------------------------------------------------------
# Input adapters — turn a graph / evidence source into candidates
# ---------------------------------------------------------------------------

def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from an object attribute or a mapping key, else default."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _get_first(obj: Any, names: Tuple[str, ...], default: Any = None) -> Any:
    """First present (non-None) value among ``names``, else default.

    Lets the adapters accept the several real field spellings already in the
    codebase (e.g. an entity's confidence is ``resolution_confidence`` in the
    entities table but ``confidence`` on an ``EntityContext``) without coupling
    the assembler to any one producer.
    """
    for name in names:
        val = _get(obj, name, None)
        if val is not None:
            return val
    return default


def _coerce_float(value: Any) -> Optional[float]:
    """Best-effort float, or None when the value is absent/unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _entities_to_candidates(graph: Any) -> List[Candidate]:
    """Adapt a graph's entities into entity candidates.

    Entities are resolved from source-system records, so they default to
    ``observed``. Tolerant of GraphContext-style objects (``EntityContext`` with
    ``entity_id`` / ``confidence``), entity-table rows (``resolution_confidence``)
    and plain dicts; never raises on a missing field.
    """
    entities = _get(graph, "entities", []) or []
    candidates: List[Candidate] = []
    for ent in entities:
        eid = _get_first(ent, ("entity_id", "id", "candidate_id"))
        candidates.append(
            Candidate(
                candidate_id=str(eid),
                kind=KIND_ENTITY,
                origin=_get_first(ent, ("origin",), OBSERVED),
                confidence=float(
                    _get_first(ent, ("confidence", "resolution_confidence"), 0.0) or 0.0
                ),
                source_timestamp=_get(ent, "source_timestamp"),
                freshness_days=_coerce_float(_get(ent, "freshness_days")),
                # 2.0-B3 T1: the graph IS the structured record — it is resolved
                # from source-system records, never from prose or chat. An explicit
                # source_type on the item still wins, so a producer that knows
                # better can say so.
                source_type=_get_first(ent, ("source_type",), SOURCE_TYPE_STRUCTURED),
                payload=ent,
            )
        )
    return candidates


def _relationship_id(rel: Any) -> str:
    """A stable id for a relationship candidate.

    Prefers an explicit id; otherwise derives a deterministic composite key from
    the edge endpoints + type (``from->to:type``) so the tiebreaker is stable
    across runs even when the edge object carries no id of its own. Endpoint
    fields are read by both their id and their name spellings.
    """
    rid = _get_first(rel, ("relationship_id", "id", "candidate_id"))
    if rid:
        return str(rid)
    frm = _get_first(rel, ("from_id", "from_entity_id", "from_name", "from_entity_name"), "?")
    to = _get_first(rel, ("to_id", "to_entity_id", "to_name", "to_entity_name"), "?")
    rtype = _get_first(rel, ("relationship_type", "type"), "?")
    return f"{frm}->{to}:{rtype}"


def _relationships_to_candidates(graph: Any) -> List[Candidate]:
    """Adapt a graph's relationships into candidates, observed vs inferred.

    An edge's ``inferred`` flag maps onto the observed/inferred origin the rules
    partition on (an explicit ``origin`` wins when present).
    """
    relationships = _get(graph, "relationships", []) or []
    candidates: List[Candidate] = []
    for rel in relationships:
        origin = _get(rel, "origin")
        if origin is None:
            origin = INFERRED if _get(rel, "inferred", False) else OBSERVED
        candidates.append(
            Candidate(
                candidate_id=_relationship_id(rel),
                kind=KIND_RELATIONSHIP,
                origin=origin,
                confidence=float(_get(rel, "confidence", 0.0) or 0.0),
                source_timestamp=_get(rel, "source_timestamp"),
                freshness_days=_coerce_float(_get(rel, "freshness_days")),
                source_type=_get_first(rel, ("source_type",), SOURCE_TYPE_STRUCTURED),
                payload=rel,
            )
        )
    return candidates


def _call_source(fn: Callable, opportunity: Any, policy: AssemblyPolicy) -> Any:
    """Call an evidence source supporting both ``(opportunity, policy)`` and
    ``(opportunity)`` signatures, so a 1.6 stub and a 1.8 retrieval source both
    plug in unchanged."""
    try:
        return fn(opportunity, policy)
    except TypeError:
        return fn(opportunity)


def _resolve_evidence(evidence_source: Any, opportunity: Any, policy: AssemblyPolicy) -> List[Any]:
    """Pull raw evidence chunks from whatever an evidence source is (Section 4).

    In 1.6 ``evidence_source`` is None and this returns ``[]``. The interface is
    deliberately loose so the 1.8 retrieval substrate can plug in unchanged: a
    source may be a callable, an object exposing
    ``retrieve``/``fetch``/``search``/``fetch_evidence``/``get_evidence``, or a
    plain iterable of chunks. Advisory: any failure yields no evidence rather
    than breaking assembly.
    """
    if evidence_source is None:
        return []
    try:
        if callable(evidence_source):
            result = _call_source(evidence_source, opportunity, policy)
        else:
            result = None
            for method in ("retrieve", "fetch", "search", "fetch_evidence", "get_evidence"):
                fn = getattr(evidence_source, method, None)
                if callable(fn):
                    result = _call_source(fn, opportunity, policy)
                    break
            if result is None:
                result = list(evidence_source)  # plain iterable of chunks
        return list(result) if result is not None else []
    except Exception as exc:  # noqa: BLE001 — evidence is advisory in 1.6.
        logger.warning("context_assembly: evidence_source failed (advisory): %s", exc)
        return []


def _evidence_to_candidates(
    evidence_source: Any, opportunity: Any, policy: AssemblyPolicy
) -> List[Candidate]:
    """Adapt retrieved evidence chunks into candidates (empty until 1.8)."""
    candidates: List[Candidate] = []
    for idx, chunk in enumerate(_resolve_evidence(evidence_source, opportunity, policy)):
        cid = (
            _get(chunk, "chunk_id")
            or _get(chunk, "evidence_id")
            or _get(chunk, "id")
            or _get(chunk, "candidate_id")
            or f"evidence-{idx}"
        )
        candidates.append(
            Candidate(
                candidate_id=str(cid),
                kind=KIND_EVIDENCE,
                origin=_get(chunk, "origin", OBSERVED),
                confidence=float(_get(chunk, "confidence", 0.0) or 0.0),
                source_timestamp=_get(chunk, "source_timestamp"),
                freshness_days=_coerce_float(_get(chunk, "freshness_days")),
                is_stale=bool(_get(chunk, "is_stale", False)),
                # 2.0-B3 T1: the retrieval substrate already labels every chunk
                # prose / code / conversation — the same vocabulary the declared
                # source_type_ranks table is keyed on, so the precedence rule reads
                # the producer's own classification rather than guessing from the
                # source system. Left empty when the chunk does not say, which sorts
                # last among declared types.
                source_type=str(
                    _get_first(chunk, ("source_type", "content_type"), "") or ""
                ),
                payload=chunk,
            )
        )
    return candidates


def _unwrap(candidate: Candidate) -> Any:
    """Return the underlying payload when present, else the candidate itself."""
    return candidate.payload if candidate.payload is not None else candidate


# ---------------------------------------------------------------------------
# assemble_context — the public entry point (Section 1)
# ---------------------------------------------------------------------------

def assemble_context(
    opportunity: Any,
    graph: Any,
    policy: Optional[AssemblyPolicy] = None,
    evidence_source: Optional[Callable] = None,
) -> ContextPackage:
    """Build the deterministic, bounded :class:`ContextPackage` for an opportunity.

    Deterministic: identical ``opportunity``, ``graph`` and ``policy`` always
    produce a byte-identical package, run to run (AC1). ``evidence_source`` is
    None in 1.6 (no retrieval yet); the parameter exists now so the 1.8
    retrieval substrate plugs in without changing any caller (AC7).

    Each kind (entities, relationships, evidence) is run through the same six
    ordered rules with its own cap. All three kinds share one freshness frame so
    "fresher" means the same thing across the package.
    """
    policy = policy or AssemblyPolicy()

    entity_candidates = _entities_to_candidates(graph)
    relationship_candidates = _relationships_to_candidates(graph)
    evidence_candidates = _evidence_to_candidates(evidence_source, opportunity, policy)

    # One freshness reference across every kind so decay is comparable.
    reference = _reference_timestamp(
        entity_candidates + relationship_candidates + evidence_candidates
    )

    selected_entities, log_entities = select_candidates(
        entity_candidates, policy.max_entities, policy, reference
    )
    selected_relationships, log_relationships = select_candidates(
        relationship_candidates, policy.max_relationships, policy, reference
    )
    selected_evidence, log_evidence = select_candidates(
        evidence_candidates, policy.max_evidence_chunks, policy, reference
    )

    # 2.0-B3 T2 (AC2) — the per-finding TOTAL budget, applied after each kind has
    # won its own competition. The per-kind caps sum to more than a prompt should
    # carry, so this is the bound that reflects what a finding can actually afford.
    #
    # Deterministic by construction: kinds yield in the DECLARED reverse
    # ``kind_precedence`` order, and within a kind the already-ranked tail is trimmed
    # first — so the item dropped is always the lowest-ranked item of the most
    # substitutable kind. No arbitrary choice anywhere.
    by_kind = {
        KIND_ENTITY: list(selected_entities),
        KIND_RELATIONSHIP: list(selected_relationships),
        KIND_EVIDENCE: list(selected_evidence),
    }
    logs_by_kind = {
        KIND_ENTITY: log_entities,
        KIND_RELATIONSHIP: log_relationships,
        KIND_EVIDENCE: log_evidence,
    }
    trimmed_by_kind = {k: 0 for k in by_kind}
    total_budget = getattr(policy, "max_total_items", None)
    if total_budget is not None:
        precedence = (
            policy.declaration.kind_precedence
            if policy.declaration is not None
            else (KIND_ENTITY, KIND_RELATIONSHIP, KIND_EVIDENCE)
        )
        overflow = sum(len(v) for v in by_kind.values()) - int(total_budget)
        for kind in reversed(precedence):
            if overflow <= 0:
                break
            items = by_kind.get(kind) or []
            give = min(overflow, len(items))
            if not give:
                continue
            # Re-log the trimmed tail so the drop is recorded, never silent (AC2).
            for candidate in items[len(items) - give:]:
                _relabel_dropped(logs_by_kind[kind], candidate.candidate_id)
            by_kind[kind] = items[: len(items) - give]
            trimmed_by_kind[kind] += give
            overflow -= give
        if overflow > 0:
            # Every kind emptied and still over: only reachable with a budget of 0,
            # which the loader refuses. Logged rather than ignored.
            logger.warning(
                "context_assembly: total budget %s could not be satisfied — %d item(s) "
                "still over after trimming every kind", total_budget, overflow,
            )

    selected_entities = by_kind[KIND_ENTITY]
    selected_relationships = by_kind[KIND_RELATIONSHIP]
    selected_evidence = by_kind[KIND_EVIDENCE]

    budget_report = AssemblyBudgetReport(
        per_kind=tuple(
            _kind_budget(kind, budget, logs_by_kind[kind], len(by_kind[kind]))._replace_trimmed(
                trimmed_by_kind[kind]
            )
            for kind, budget in (
                (KIND_ENTITY, policy.max_entities),
                (KIND_RELATIONSHIP, policy.max_relationships),
                (KIND_EVIDENCE, policy.max_evidence_chunks),
            )
        ),
        total_budget=total_budget,
        total_selected=sum(len(v) for v in by_kind.values()),
    )
    if budget_report.breached:
        # Loud, at info: a truncated context is a fact an operator may need when a
        # narrative looks thin, and it must not require log-level archaeology.
        logger.info(
            "context_assembly: budget shaped this package — %s", budget_report.reason
        )

    return ContextPackage(
        entities=[_unwrap(c) for c in selected_entities],
        relationships=[_unwrap(c) for c in selected_relationships],
        evidence=[_unwrap(c) for c in selected_evidence],
        policy_used=policy,
        selection_log=log_entities + log_relationships + log_evidence,
        policy_declaration=(
            policy.declaration.to_dict() if policy.declaration is not None else None
        ),
        budget_report=budget_report.to_dict(),
    )
