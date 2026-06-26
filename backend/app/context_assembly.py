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
except ModuleNotFoundError:  # Runtime inside backend/ where app is top-level.
    from app.graph_constants import (
        GRAPH_CONTEXT_MAX_ENTITIES,
        GRAPH_CONTEXT_MAX_RELATIONSHIPS,
    )
    from app.provenance import INFERRED, OBSERVED

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


@dataclass
class ContextPackage:
    """The single, bounded, ordered context an opportunity is built from."""

    entities: List[Any] = field(default_factory=list)        # selected, ordered, <= cap
    relationships: List[Any] = field(default_factory=list)   # selected, ordered, <= cap
    evidence: List[Any] = field(default_factory=list)        # empty until retrieval (1.8)
    policy_used: AssemblyPolicy = field(default_factory=AssemblyPolicy)
    selection_log: List[dict] = field(default_factory=list)  # why each item was in/out


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
    """
    return (
        -_confidence(candidate),
        -_freshness_score(candidate, reference, halflife_days),
        candidate.candidate_id,
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

    # Rule 1 — exclude anything below the confidence floor. Done first so weak
    # context can never occupy budget ahead of stronger context (AC2).
    below_floor: List[Candidate] = []
    eligible: List[Candidate] = []
    for candidate in candidates:
        if _confidence(candidate) < policy.confidence_floor:
            below_floor.append(candidate)
        else:
            eligible.append(candidate)

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
    # of input order (AC1): below-floor entries sorted by id, then included items
    # by rank position, then ranked/budget exclusions in ranked order.
    log: List[dict] = []
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

    return ContextPackage(
        entities=[_unwrap(c) for c in selected_entities],
        relationships=[_unwrap(c) for c in selected_relationships],
        evidence=[_unwrap(c) for c in selected_evidence],
        policy_used=policy,
        selection_log=log_entities + log_relationships + log_evidence,
    )
