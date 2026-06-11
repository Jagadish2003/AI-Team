"""ENT-4 / T3-S14-A - Graph context builder with enterprise hard caps.

Turns raw graph query results (the rows produced by the T3-S14-A traversal
queries, e.g. ``opportunity_neighbourhood()``) into a clean, prompt-ready
:class:`GraphContext` for ENT-3 LLM enrichment and the ``/api/graph`` routes.

Two enterprise constraints are built in from day one and are NOT configurable
per-run:

  * :data:`GRAPH_CONTEXT_MAX_ENTITIES` (15) - hard cap on entities in LLM
    prompt context.
  * :data:`GRAPH_CONTEXT_MAX_RELATIONSHIPS` (20) - hard cap on relationship
    edges in LLM prompt context.

Rationale: enterprise Salesforce orgs (TCU, City National) accumulate hundreds
of graph entities after a few runs. Sending all of them into the prompt
produces token bloat, degraded output quality, and unpredictable
summarisation. 15 entities is sufficient context for a meaningful grounded
summary.

Ranking is deterministic - same graph input, same ranked output, every time,
regardless of input list order. Non-deterministic ranking produces different
enrichment text on different runs even when the underlying data has not
changed, which erodes trust and cannot be debugged.

Entity ranking (AC3): depth-0 entities (directly linked to the opportunity)
always come first; remaining entities are ranked by type priority
(person > team > object > process > system), run_count DESC,
confidence DESC, then alphabetical display-name tie-break.

Relationship ranking (AC6): observed edges (``inferred=False``) strictly
before inferred edges, then confidence DESC, then stable alphabetical
tie-breaks. Observed-first deliberately takes precedence over confidence -
AC6 requires that no inferred edge ever outranks an observed one, however
confident the inference.

When the graph exceeds the cap, ``observed_summary`` ends with the locked
truncation note (Section 2c) so the LLM knows it is seeing a partial view:
it must not assume the listed entities are exhaustive.

``build_graph_context()`` never raises (AC7): a sparse graph (< 3 entities),
malformed rows, or telemetry failure all degrade gracefully. A
``graph.context_built`` telemetry event is fired after every build (AC10).

This module is distinct from ``app.graph_context`` (the ENT-3 / T3-S15-A
run-KV enrichment bridge). This builder consumes traversal rows that carry
``depth`` / ``run_count`` ranking signals; do not conflate the two.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enterprise hard caps - not configurable per-run (Section 2a)
# ---------------------------------------------------------------------------

GRAPH_CONTEXT_MAX_ENTITIES = 15       # hard cap for LLM prompt context
GRAPH_CONTEXT_MAX_RELATIONSHIPS = 20  # hard cap for LLM prompt context

# Below this many entities the graph is too thin to summarise - the context
# is flagged sparse_graph=True and observed_summary stays empty (AC7).
SPARSE_GRAPH_THRESHOLD = 3

# Entity type ranking priority (Section 2b). Lower sorts first. Types absent
# from this map (e.g. 'project') rank after all known types.
TYPE_PRIORITY: Dict[str, int] = {
    "person": 1,
    "team": 2,
    "object": 3,
    "process": 4,
    "system": 5,
}
_UNKNOWN_TYPE_PRIORITY = 9

# Locked truncation-note format (Section 2c). observed_summary ends with this
# sentence when the entity cap is hit, so the LLM knows: (1) the graph is
# larger than what it sees, (2) the entities shown are the most significant,
# (3) it should not assume the listed entities are exhaustive.
TRUNCATION_NOTE_TEMPLATE = (
    "and {count} additional entities were identified but are not shown here. "
    "The most significant entities by frequency and confidence are listed above."
)


# ---------------------------------------------------------------------------
# Context structures (Section 3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EntityContext:
    """One graph entity as seen by the LLM context, with ranking signals."""

    entity_id: str
    name: str                  # display_name
    entity_type: str           # person / team / object / process / system
    run_count: int             # how many runs this entity was seen in
    confidence: float          # resolution_confidence
    depth: int                 # hops from the opportunity seed; 0 = direct
    source_system: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "EntityContext":
        """Build from a raw traversal row (accepts query or context key names)."""
        return cls(
            entity_id=str(row.get("entity_id") or row.get("id") or ""),
            name=str(row.get("display_name") or row.get("name") or ""),
            entity_type=str(row.get("entity_type") or ""),
            run_count=int(row.get("run_count") or 0),
            confidence=float(
                row.get("resolution_confidence", row.get("confidence", 0.0)) or 0.0
            ),
            depth=int(row.get("depth") or 0),
            source_system=str(row.get("source_system") or ""),
        )


@dataclass(frozen=True)
class RelationshipContext:
    """One graph edge as seen by the LLM context, with ranking signals."""

    from_entity_id: str
    from_name: str
    relationship_type: str
    to_entity_id: str
    to_name: str
    inferred: bool
    confidence: float

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "RelationshipContext":
        """Build from a raw edge row (accepts query or context key names)."""
        return cls(
            from_entity_id=str(row.get("from_entity_id") or ""),
            from_name=str(row.get("from_name") or row.get("from_entity_name") or ""),
            relationship_type=str(row.get("relationship_type") or ""),
            to_entity_id=str(row.get("to_entity_id") or ""),
            to_name=str(row.get("to_name") or row.get("to_entity_name") or ""),
            inferred=bool(row.get("inferred") or False),
            confidence=float(row.get("confidence") or 0.0),
        )


@dataclass
class GraphContext:
    """Ranked, capped, prompt-ready view of an opportunity's graph
    neighbourhood (Section 3, plus the AC7 ``sparse_graph`` flag)."""

    opportunity_id: str
    entities: List[EntityContext] = field(default_factory=list)        # ranked, capped at 15
    relationships: List[RelationshipContext] = field(default_factory=list)  # ranked, capped at 20
    observed_summary: str = ""          # locked format; ends with truncation note when capped
    inferred_summary: Optional[str] = None
    entity_count: int = 0               # total in graph (before cap)
    entity_count_shown: int = 0         # after cap (max 15)
    relationship_count: int = 0         # total (before cap)
    relationship_count_shown: int = 0   # after cap (max 20)
    truncated: bool = False             # True when graph exceeded a cap
    max_depth_reached: int = 0
    sparse_graph: bool = False          # True when entity_count < SPARSE_GRAPH_THRESHOLD


# ---------------------------------------------------------------------------
# Deterministic ranking (Section 2b)
# ---------------------------------------------------------------------------

def _entity_sort_key(e: EntityContext) -> tuple:
    """Deterministic ordering for entities of equal depth class.

    Type priority, then run_count DESC, then confidence DESC, then
    alphabetical display name, then entity_id as the final total-order
    tie-break so duplicate names cannot make the ranking input-order
    dependent.
    """
    return (
        TYPE_PRIORITY.get(e.entity_type, _UNKNOWN_TYPE_PRIORITY),
        -e.run_count,
        -e.confidence,
        e.name,
        e.entity_id,
    )


def rank_entities_for_context(
    entities: Sequence[EntityContext],
    max_entities: int = GRAPH_CONTEXT_MAX_ENTITIES,
) -> List[EntityContext]:
    """Deterministic ranking for LLM context selection (AC3, AC4).

    Priority order:
      1. Depth 0 (directly linked to the opportunity) - always included first
      2. Person entities with highest run_count - most frequently seen people
      3. Team entities with highest run_count - most active teams
      4. Object entities with highest resolution confidence
      5. Process entities - included if space permits
      6. System entities - lowest priority in context
    Tie-breaking: alphabetical by display name, then entity_id (deterministic).

    Does not mutate the input. Same graph input - in any list order - always
    produces the same ranked output.
    """
    depth_0 = sorted((e for e in entities if e.depth == 0), key=_entity_sort_key)
    remaining = sorted((e for e in entities if e.depth != 0), key=_entity_sort_key)
    return (depth_0 + remaining)[:max_entities]


def rank_relationships_for_context(
    relationships: Sequence[RelationshipContext],
    max_relationships: int = GRAPH_CONTEXT_MAX_RELATIONSHIPS,
) -> List[RelationshipContext]:
    """Deterministic relationship ranking (AC6).

    Observed edges (inferred=False) strictly before inferred edges, then
    confidence DESC, then alphabetical tie-breaks (from_name,
    relationship_type, to_name, entity ids). Observed-first takes precedence
    over confidence so an inferred hypothesis can never outrank graph truth.

    Does not mutate the input.
    """
    ranked = sorted(
        relationships,
        key=lambda r: (
            r.inferred,        # False=0 sorts before True=1 - observed first
            -r.confidence,
            r.from_name,
            r.relationship_type,
            r.to_name,
            r.from_entity_id,
            r.to_entity_id,
        ),
    )
    return ranked[:max_relationships]


# ---------------------------------------------------------------------------
# Summary rendering (Sections 2c / 3)
# ---------------------------------------------------------------------------

def _entity_line(e: EntityContext) -> str:
    source = f", {e.source_system}" if e.source_system else ""
    return (
        f"- {e.name} ({e.entity_type}{source}; seen in {e.run_count} runs, "
        f"confidence {e.confidence:.2f})"
    )


def _relationship_line(r: RelationshipContext) -> str:
    rel = r.relationship_type.replace("_", " ")
    return f"- {r.from_name} {rel} {r.to_name} (confidence {r.confidence:.2f})"


def _render_observed_summary(
    entities: Sequence[EntityContext],
    observed: Sequence[RelationshipContext],
    entity_count: int,
    truncated_entities: int,
) -> str:
    """Render the observed-context block deterministically.

    Lists the capped entity set, then the observed edges among the capped
    relationship set. When the entity cap was hit, the summary ends with the
    locked truncation note (AC5).
    """
    lines: List[str] = [f"Entities ({len(entities)} of {entity_count} shown):"]
    lines.extend(_entity_line(e) for e in entities)

    if observed:
        lines.append("Observed relationships:")
        lines.extend(_relationship_line(r) for r in observed)

    summary = "\n".join(lines)
    if truncated_entities > 0:
        summary += "\n" + TRUNCATION_NOTE_TEMPLATE.format(count=truncated_entities)
    return summary


def _render_inferred_summary(
    inferred: Sequence[RelationshipContext],
) -> Optional[str]:
    """Render the inferred-edges block, or None when there are none shown."""
    if not inferred:
        return None
    lines = ["Inferred relationships (hypotheses, not observed facts):"]
    lines.extend(_relationship_line(r) for r in inferred)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Builder (T3) + telemetry (AC10)
# ---------------------------------------------------------------------------

def _coerce_entities(raw: Sequence[Any]) -> List[EntityContext]:
    """Accept EntityContext instances or raw mapping rows; skip bad rows."""
    out: List[EntityContext] = []
    for item in raw or []:
        try:
            if isinstance(item, EntityContext):
                out.append(item)
            elif isinstance(item, Mapping):
                out.append(EntityContext.from_row(item))
            else:
                logger.debug("graph_context_builder: skipping entity row %r", item)
        except Exception as exc:
            logger.debug("graph_context_builder: bad entity row %r: %s", item, exc)
    return out


def _coerce_relationships(raw: Sequence[Any]) -> List[RelationshipContext]:
    """Accept RelationshipContext instances or raw mapping rows; skip bad rows."""
    out: List[RelationshipContext] = []
    for item in raw or []:
        try:
            if isinstance(item, RelationshipContext):
                out.append(item)
            elif isinstance(item, Mapping):
                out.append(RelationshipContext.from_row(item))
            else:
                logger.debug("graph_context_builder: skipping edge row %r", item)
        except Exception as exc:
            logger.debug("graph_context_builder: bad edge row %r: %s", item, exc)
    return out


def _record_context_built(context: GraphContext, org_id: Optional[str], duration_ms: int) -> None:
    """Fire graph.context_built (AC10). Best-effort - never raises."""
    try:
        from app import telemetry

        telemetry.record_event(
            "graph.context_built",
            {
                "opportunity_id": context.opportunity_id,
                "org_id": org_id or "unknown",
                "source": "graph_context_builder",
                "entity_count": context.entity_count,
                "entity_count_shown": context.entity_count_shown,
                "relationship_count": context.relationship_count,
                "relationship_count_shown": context.relationship_count_shown,
                "truncated": context.truncated,
                "sparse_graph": context.sparse_graph,
                "duration_ms": duration_ms,
            },
        )
    except Exception as exc:
        logger.warning("graph_context_builder: telemetry failed: %s", exc)


def build_graph_context(
    opportunity_id: str,
    entities: Sequence[Any],
    relationships: Sequence[Any],
    org_id: Optional[str] = None,
    max_depth_reached: Optional[int] = None,
) -> GraphContext:
    """Turn raw graph query results into a ranked, capped :class:`GraphContext`.

    ``entities`` / ``relationships`` are the rows returned by the T3-S14-A
    traversal queries (``opportunity_neighbourhood()``) - either already-typed
    context objects or raw mapping rows. ``max_depth_reached`` may be supplied
    by the query layer; when omitted it is derived from the entity depths.

    Behaviour:
      * Entity set capped at 15, relationships at 20 - ranked deterministically
        (AC3/AC4/AC6) so the same graph always yields the same context.
      * When the graph exceeds the cap, ``truncated=True`` and
        ``observed_summary`` ends with the locked truncation note (AC5).
      * A sparse graph (< 3 entities) returns ``sparse_graph=True`` with an
        empty ``observed_summary`` - and does not raise (AC7).
      * Fires a ``graph.context_built`` telemetry event on every call (AC10).
    """
    started = time.perf_counter()
    try:
        safe_entities = _coerce_entities(entities)
        safe_relationships = _coerce_relationships(relationships)

        entity_count = len(safe_entities)
        relationship_count = len(safe_relationships)

        ranked_entities = rank_entities_for_context(safe_entities)
        ranked_relationships = rank_relationships_for_context(safe_relationships)

        truncated = (
            entity_count > GRAPH_CONTEXT_MAX_ENTITIES
            or relationship_count > GRAPH_CONTEXT_MAX_RELATIONSHIPS
        )
        sparse = entity_count < SPARSE_GRAPH_THRESHOLD

        if max_depth_reached is None:
            max_depth_reached = max((e.depth for e in safe_entities), default=0)

        observed_shown = [r for r in ranked_relationships if not r.inferred]
        inferred_shown = [r for r in ranked_relationships if r.inferred]

        if sparse:
            # Too thin to ground a summary against - the enrichment caller
            # falls back to its non-graph prompt (AC7).
            observed_summary = ""
            inferred_summary = None
        else:
            observed_summary = _render_observed_summary(
                ranked_entities,
                observed_shown,
                entity_count,
                truncated_entities=max(0, entity_count - len(ranked_entities)),
            )
            inferred_summary = _render_inferred_summary(inferred_shown)

        context = GraphContext(
            opportunity_id=opportunity_id,
            entities=ranked_entities,
            relationships=ranked_relationships,
            observed_summary=observed_summary,
            inferred_summary=inferred_summary,
            entity_count=entity_count,
            entity_count_shown=len(ranked_entities),
            relationship_count=relationship_count,
            relationship_count_shown=len(ranked_relationships),
            truncated=truncated,
            max_depth_reached=int(max_depth_reached),
            sparse_graph=sparse,
        )
    except Exception as exc:
        # Never break the caller (enrichment / routes): degrade to an empty,
        # sparse context, mirroring the non-blocking Stage 2 philosophy.
        logger.warning(
            "graph_context_builder: build failed for opportunity %s: %s",
            opportunity_id,
            exc,
        )
        context = GraphContext(opportunity_id=opportunity_id, sparse_graph=True)

    duration_ms = int((time.perf_counter() - started) * 1000)
    _record_context_built(context, org_id, duration_ms)
    return context


