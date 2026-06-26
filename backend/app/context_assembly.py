"""Context Assembly Foundation (R16-B2, Part One / T1–T4, T6).

The one backend service that decides, *deterministically and reproducibly*, what
context each opportunity is built from. Given an opportunity and its candidate
context (graph entities, graph relationships, and — from 1.8 — retrieved
evidence), it produces a single bounded, ordered :class:`ContextPackage` that
downstream enrichment and reasoning are allowed to consume.

The core idea (doc §intro): **deterministic policy over probabilistic inputs.**
The inputs (similarity-ranked retrieval, a variable graph, inferred proposals)
are not deterministic; the assembler's RULES are. Same policy + same inputs =>
the same package every run. That is what keeps an LLM-and-vector-store system
explainable.

The six ordered rules (doc §2) are applied as an explicit, tested sequence — the
order is part of the contract:

  1. Exclude anything below ``confidence_floor``.
  2. Partition candidates into observed and inferred.
  3. Observed fills the budget first; inferred only fills the remaining space
     (the load-bearing rule — inferred can never crowd out observed).
  4. Within each partition, rank deterministically: confidence, then freshness
     (half-life weighted), then a stable tiebreaker (candidate id).
  5. Apply the hard caps (max_entities / max_relationships / max_evidence_chunks).
  6. Record every include/exclude decision in ``selection_log`` (auditability).

Forward-compatibility (doc §4): :func:`assemble_context` accepts an
``evidence_source`` that is ``None`` in 1.6 (no retrieval yet). When retrieval
lands in 1.8 it is passed here and flows through the exact same rules — no caller
change. This is why assembly is built before retrieval, not after.

This module owns context selection. T5 (wiring existing enrichment/graph paths to
call through here) is a separate, later step and is intentionally NOT done in this
foundation subtask.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

# Hard caps are a single source of truth (CLAUDE.md: do not redefine the caps
# locally). The entity/relationship caps are carried from the current graph
# design; the evidence budget is new in this story (retrieval lands in 1.8).
try:  # package layout when imported as ``backend.app`` (e.g. some test paths)
    from backend.app.graph_constants import (
        GRAPH_CONTEXT_MAX_ENTITIES,
        GRAPH_CONTEXT_MAX_RELATIONSHIPS,
    )
except ModuleNotFoundError:  # runtime inside backend/ where ``app`` is top-level
    from app.graph_constants import (
        GRAPH_CONTEXT_MAX_ENTITIES,
        GRAPH_CONTEXT_MAX_RELATIONSHIPS,
    )

logger = logging.getLogger(__name__)

# ---- origin values (the load-bearing observed-vs-inferred distinction) ----
OBSERVED = "observed"
INFERRED = "inferred"

# Budget for retrieved evidence chunks. Local to this module: it is a new concept
# (the evidence source arrives with retrieval in 1.8), not one of the graph caps.
DEFAULT_MAX_EVIDENCE_CHUNKS = 10

# selection_log reason codes (doc §3).
REASON_BELOW_FLOOR = "below_confidence_floor"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_RANKED_OUT = "ranked_out"


@dataclass(frozen=True)
class AssemblyPolicy:
    """Deterministic policy. Same policy + same inputs => same output.

    Frozen so a policy is a stable, hashable value object — it is recorded
    verbatim on every :class:`ContextPackage` (``policy_used``) for auditability.
    """

    max_entities: int = GRAPH_CONTEXT_MAX_ENTITIES        # hard cap (15)
    max_relationships: int = GRAPH_CONTEXT_MAX_RELATIONSHIPS  # hard cap (20)
    max_evidence_chunks: int = DEFAULT_MAX_EVIDENCE_CHUNKS   # budget for evidence (1.8+)
    confidence_floor: float = 0.0    # exclude context below this confidence
    observed_first: bool = True      # observed strictly before inferred
    freshness_halflife_days: float = 30.0  # older evidence weighed down


@dataclass
class ContextPackage:
    """The final selected context downstream logic is allowed to consume.

    ``entities`` / ``relationships`` / ``evidence`` are the selected payloads, in
    final (observed-first, ranked) order and within budget. ``policy_used`` is the
    exact policy that produced this package. ``selection_log`` records why each
    candidate was included or excluded (doc §3) — the full evidence trace (1.9)
    reads it.
    """

    entities: List[Any] = field(default_factory=list)
    relationships: List[Any] = field(default_factory=list)
    evidence: List[Any] = field(default_factory=list)        # empty until retrieval (1.8)
    policy_used: AssemblyPolicy = field(default_factory=AssemblyPolicy)
    selection_log: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalised candidate — a uniform view over entities / relationships / evidence
# so the six rules apply identically regardless of the candidate's concrete type
# (dataclass like EntityContext/RelationshipContext, or a plain dict).
# ---------------------------------------------------------------------------

@dataclass
class _Candidate:
    candidate_id: str
    kind: str          # 'entity' | 'relationship' | 'evidence'
    origin: str        # 'observed' | 'inferred'
    confidence: float
    freshness_days: float
    freshness_score: float
    payload: Any


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict OR an attribute from an object, defensively."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse a datetime / ISO-8601 string to a naive-UTC datetime, or None."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _candidate_id(obj: Any, kind: str) -> str:
    """A stable, deterministic id for a candidate — the tiebreaker (Rule 4)."""
    if kind == "relationship":
        rid = _get(obj, "id") or _get(obj, "edge_id") or _get(obj, "relationship_id")
        if rid:
            return str(rid)
        frm = _get(obj, "from_id") or _get(obj, "from_entity_id") or _get(obj, "from_name") or "?"
        to = _get(obj, "to_id") or _get(obj, "to_entity_id") or _get(obj, "to_name") or "?"
        rtype = _get(obj, "relationship_type") or _get(obj, "type") or "?"
        return f"{frm}->{to}:{rtype}"
    if kind == "evidence":
        return str(
            _get(obj, "chunk_id") or _get(obj, "id")
            or _get(obj, "retrieval_result_id") or _get(obj, "source_artifact") or "?"
        )
    # entity
    return str(
        _get(obj, "entity_id") or _get(obj, "id")
        or _get(obj, "name") or _get(obj, "display_name") or "?"
    )


def _origin_of(obj: Any) -> str:
    """Resolve a candidate's origin. Explicit ``origin`` wins; else an ``inferred``
    flag; else default observed (graph entities are resolved facts)."""
    origin = _get(obj, "origin")
    if isinstance(origin, str) and origin.strip().lower() in (OBSERVED, INFERRED):
        return origin.strip().lower()
    inferred = _get(obj, "inferred")
    if inferred is not None:
        return INFERRED if bool(inferred) else OBSERVED
    return OBSERVED


def _confidence_of(obj: Any) -> float:
    val = _get(obj, "confidence")
    if val is None:
        val = _get(obj, "resolution_confidence")  # EntityContext / entity rows
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _freshness_days_of(obj: Any, reference_time: Optional[datetime]) -> float:
    """Age of a candidate in days — deterministic, never wall-clock.

    Prefers a precomputed ``freshness_days``; else derives it from the candidate's
    timestamp relative to the opportunity's reference time (so two runs over the
    same data compute the same age). Returns 0.0 when neither is available — graph
    entities/relationships carry no timestamp in 1.6, so freshness is neutral for
    them and ranking falls to confidence then the tiebreaker.
    """
    fd = _get(obj, "freshness_days")
    if fd is not None:
        try:
            return max(0.0, float(fd))
        except (TypeError, ValueError):
            pass
    ts = (
        _get(obj, "source_timestamp") or _get(obj, "timestamp")
        or _get(obj, "last_seen") or _get(obj, "created_at")
    )
    parsed = _parse_dt(ts)
    if parsed is not None and reference_time is not None:
        return max(0.0, (reference_time - parsed).total_seconds() / 86400.0)
    return 0.0


def _freshness_score(freshness_days: float, halflife_days: float) -> float:
    """Half-life freshness weight in (0, 1]: 1.0 when fresh, decaying with age."""
    if halflife_days is None or halflife_days <= 0:
        return 1.0
    return 0.5 ** (max(0.0, freshness_days) / halflife_days)


def _reference_time(opportunity: Any) -> Optional[datetime]:
    """A deterministic 'now' for freshness, taken from the opportunity (never the
    wall clock, so the package is reproducible run-to-run — AC1)."""
    for key in (
        "assembled_at", "completedAt", "completed_at", "source_timestamp",
        "observed_at", "startedAt", "started_at",
    ):
        parsed = _parse_dt(_get(opportunity, key))
        if parsed is not None:
            return parsed
    return None


def _normalize(obj: Any, kind: str, reference_time: Optional[datetime],
               halflife: float) -> _Candidate:
    fd = _freshness_days_of(obj, reference_time)
    return _Candidate(
        candidate_id=_candidate_id(obj, kind),
        kind=kind,
        origin=_origin_of(obj),
        confidence=_confidence_of(obj),
        freshness_days=fd,
        freshness_score=_freshness_score(fd, halflife),
        payload=obj,
    )


def _rank_key(c: _Candidate):
    """Deterministic ranking key (Rule 4): confidence DESC, freshness DESC, then
    the stable candidate-id tiebreaker (ASC) — a total order, so equal
    confidence+freshness always sort identically across runs (AC5)."""
    return (-c.confidence, -c.freshness_score, c.candidate_id)


def _log_entry(c: _Candidate, decision: str, reason: str) -> dict:
    return {
        "candidate_id": c.candidate_id,
        "kind": c.kind,
        "origin": c.origin,
        "decision": decision,
        "reason": reason,
        "confidence": c.confidence,
        "freshness_days": c.freshness_days,
    }


def _select_kind(candidates: List[_Candidate], cap: int, policy: AssemblyPolicy):
    """Apply the six ordered rules to one kind's candidates.

    Returns ``(selected_payloads, log_entries)``. ``selected_payloads`` are the
    original objects in final order (observed first, then inferred, each ranked),
    within ``cap`` (AC4). Every candidate gets a log entry (AC6); the log is
    ordered deterministically (included by position, then excluded by id) so the
    package is byte-identical across runs (AC1).
    """
    excluded: List[tuple] = []  # (candidate, reason)

    # Rule 1 — confidence floor.
    passed: List[_Candidate] = []
    for c in candidates:
        if c.confidence < policy.confidence_floor:
            excluded.append((c, REASON_BELOW_FLOOR))
        else:
            passed.append(c)

    if cap <= 0:
        # No budget for this kind at all — everything that passed the floor is
        # crowded out by the (zero) budget.
        selected: List[_Candidate] = []
        for c in passed:
            excluded.append((c, REASON_BUDGET_EXHAUSTED))
    elif policy.observed_first:
        # Rules 2–4 — partition, then rank each partition deterministically.
        observed = sorted([c for c in passed if c.origin == OBSERVED], key=_rank_key)
        inferred = sorted([c for c in passed if c.origin == INFERRED], key=_rank_key)
        # Rule 5 + Rule 3 — observed fills the cap first; inferred takes only what
        # remains, so an inferred item can never displace an observed item that fit.
        obs_taken, obs_rest = observed[:cap], observed[cap:]
        remaining = cap - len(obs_taken)
        if remaining > 0:
            inf_taken, inf_ranked_out = inferred[:remaining], inferred[remaining:]
            inf_budget_out: List[_Candidate] = []
        else:
            inf_taken, inf_ranked_out = [], []
            inf_budget_out = inferred  # observed consumed the whole budget (Rule 3)
        selected = obs_taken + inf_taken
        for c in obs_rest:
            excluded.append((c, REASON_RANKED_OUT))
        for c in inf_ranked_out:
            excluded.append((c, REASON_RANKED_OUT))
        for c in inf_budget_out:
            excluded.append((c, REASON_BUDGET_EXHAUSTED))
    else:
        # observed_first disabled — single deterministic ranking across all.
        ranked = sorted(passed, key=_rank_key)
        selected = ranked[:cap]
        for c in ranked[cap:]:
            excluded.append((c, REASON_RANKED_OUT))

    # Rule 6 — selection_log. Included in final position order; excluded sorted by
    # candidate id so the log is deterministic regardless of input order.
    log: List[dict] = []
    for pos, c in enumerate(selected):
        log.append(_log_entry(c, "included", f"included@position_{pos}"))
    for c, reason in sorted(excluded, key=lambda t: (t[0].candidate_id, t[0].kind)):
        log.append(_log_entry(c, "excluded", reason))

    return [c.payload for c in selected], log


def _extract(graph: Any, attr: str) -> List[Any]:
    """Pull a list (``entities`` / ``relationships``) off a GraphContext-like
    object or a dict; tolerate None / missing."""
    value = _get(graph, attr, None)
    if value is None:
        return []
    return list(value)


def _collect_evidence(evidence_source: Any, opportunity: Any,
                      policy: AssemblyPolicy) -> List[Any]:
    """Resolve raw evidence candidates from ``evidence_source`` (doc §4).

    ``None`` in 1.6 -> no evidence. From 1.8 the retrieval substrate is passed and
    its chunks flow through the same rules. Accepts a callable
    ``(opportunity, policy)`` (or ``(opportunity)``), an object exposing
    ``fetch_evidence`` / ``fetch`` / ``get_evidence``, or a plain iterable — so a
    stub source verifies the hook today (AC7). Advisory: a failing source yields
    no evidence rather than breaking assembly.
    """
    if evidence_source is None:
        return []
    try:
        if callable(evidence_source):
            try:
                result = evidence_source(opportunity, policy)
            except TypeError:
                result = evidence_source(opportunity)
        else:
            result = None
            for meth in ("fetch_evidence", "fetch", "get_evidence"):
                fn = getattr(evidence_source, meth, None)
                if callable(fn):
                    result = fn(opportunity, policy)
                    break
            if result is None:
                result = evidence_source  # assume a plain iterable of chunks
        return list(result) if result is not None else []
    except Exception as exc:  # noqa: BLE001 — evidence is advisory.
        logger.warning("context_assembly: evidence_source failed (advisory): %s", exc)
        return []


def assemble_context(
    opportunity: Any,
    graph: Any,
    policy: AssemblyPolicy,
    evidence_source: Optional[Callable] = None,
) -> ContextPackage:
    """Assemble the deterministic, bounded :class:`ContextPackage` for one opportunity.

    Args:
        opportunity: The opportunity the context is being assembled for. May carry
            a reference timestamp (``completedAt`` / ``source_timestamp`` / …) used
            for deterministic freshness weighting.
        graph: A graph context (e.g. ``graph_context_builder.GraphContext``) — any
            object/dict exposing ``entities`` and ``relationships``.
        policy: The :class:`AssemblyPolicy` (limits + rules) to apply.
        evidence_source: ``None`` in 1.6 (no retrieval). The 1.8 retrieval source
            plugs in here with no caller change (doc §4).

    Returns:
        A :class:`ContextPackage` — selected entities, relationships, evidence (in
        observed-first, ranked, capped order), the policy used, and the full
        selection log. Deterministic: identical inputs always yield an identical
        package (AC1).
    """
    policy = policy or AssemblyPolicy()
    reference_time = _reference_time(opportunity)
    halflife = policy.freshness_halflife_days

    entity_candidates = [
        _normalize(o, "entity", reference_time, halflife) for o in _extract(graph, "entities")
    ]
    relationship_candidates = [
        _normalize(o, "relationship", reference_time, halflife)
        for o in _extract(graph, "relationships")
    ]
    evidence_candidates = [
        _normalize(o, "evidence", reference_time, halflife)
        for o in _collect_evidence(evidence_source, opportunity, policy)
    ]

    entities, entity_log = _select_kind(entity_candidates, policy.max_entities, policy)
    relationships, relationship_log = _select_kind(
        relationship_candidates, policy.max_relationships, policy
    )
    evidence, evidence_log = _select_kind(
        evidence_candidates, policy.max_evidence_chunks, policy
    )

    return ContextPackage(
        entities=entities,
        relationships=relationships,
        evidence=evidence,
        policy_used=policy,
        selection_log=entity_log + relationship_log + evidence_log,
    )
