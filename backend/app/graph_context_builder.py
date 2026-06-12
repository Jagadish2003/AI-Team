"""ENT-4 / T3-S14-A — Graph context builder.

``build_graph_context()`` is the bridge between the low-level graph query layer
(``app.graph_query``) and the higher-level features that reason over the graph:
ENT-3 LLM enrichment and ENT-6 causal chains. It collects the neighbourhood
around an opportunity, ranks it deterministically, caps it to an
enterprise-safe size, and renders a stable prompt-ready summary.

Pipeline:
  1. opportunity_neighbourhood() collects resolved entities (and the observed
     edges that connect them) around the opportunity's seed entities.
  2. rank_entities_for_context() / rank_relationships_for_context() apply the
     deterministic ranking and the hard caps (15 entities, 20 relationships).
  3. The result is packaged into a :class:`GraphContext` with a LOCKED
     ``observed_summary`` text block that ENT-3's prompt inserts verbatim.

Enterprise constraints (hard, NOT per-run configurable) — Section 2a:
  - GRAPH_CONTEXT_MAX_ENTITIES = 15        cap on entities placed in the prompt
  - GRAPH_CONTEXT_MAX_RELATIONSHIPS = 20   cap on relationships placed in the prompt
TCU / City National Salesforce orgs have hundreds of entities after a few runs;
without the caps and deterministic ranking the prompt context becomes token-heavy
and the LLM output degrades and drifts run-to-run.

Determinism: rank_*_for_context() are pure and total-ordered (alphabetical
tie-break plus entity_id as a final tie-break), so the same graph always yields
the same 15 entities, the same summary, and therefore byte-stable prompts.

Sparse graphs: when fewer than SPARSE_GRAPH_THRESHOLD (3) entities are present
build_graph_context() does NOT fail — it returns a valid GraphContext with
sparse_graph=True and an empty observed_summary so the enrichment pipeline can
fall back to its pre-graph prompt.

build_graph_context() never raises: a missing graph, missing org context, or any
query error degrades to a valid empty/sparse context.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

try:
    from backend.app.graph_query import opportunity_neighbourhood
    from backend.app.telemetry import record_event
    from backend.app.graph_constants import (
        GRAPH_CONTEXT_MAX_ENTITIES,
        GRAPH_CONTEXT_MAX_RELATIONSHIPS,
        SPARSE_GRAPH_THRESHOLD,
    )
    from backend.database.models.entity_relationships import (
        INFERRED_CONFIDENCE,
        OBSERVED_CONFIDENCE,
    )
except ModuleNotFoundError:  # Runtime inside backend/ where app is top-level.
    from app.graph_query import opportunity_neighbourhood
    from app.telemetry import record_event
    from app.graph_constants import (
        GRAPH_CONTEXT_MAX_ENTITIES,
        GRAPH_CONTEXT_MAX_RELATIONSHIPS,
        SPARSE_GRAPH_THRESHOLD,
    )
    from database.models.entity_relationships import (
        INFERRED_CONFIDENCE,
        OBSERVED_CONFIDENCE,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hard caps (Section 2a) — not configurable per run.
#
# GRAPH_CONTEXT_MAX_ENTITIES (15), GRAPH_CONTEXT_MAX_RELATIONSHIPS (20) and
# SPARSE_GRAPH_THRESHOLD (3) are defined once in app.graph_constants and imported
# above (ENT-4 review #9). They are re-exported here so existing
# ``from app.graph_context_builder import GRAPH_CONTEXT_MAX_ENTITIES`` callers and
# tests keep working — but the values live in a single place.
# ---------------------------------------------------------------------------

# Deterministic entity-type ranking priority (Section 2b). Lower = higher
# priority in the LLM context. 'project' is in the entity model's type set but
# not the spec's priority list, so it sorts after the named types.
TYPE_PRIORITY = {
    "person": 1,
    "team": 2,
    "object": 3,
    "process": 4,
    "system": 5,
    "project": 6,
}
_UNKNOWN_TYPE_PRIORITY = 9


# ---------------------------------------------------------------------------
# Context dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EntityContext:
    """A single entity considered for the LLM context, with ranking inputs."""
    entity_id: str
    name: str                 # display_name
    entity_type: str
    depth: int
    run_count: int
    confidence: float         # resolution_confidence


@dataclass
class RelationshipContext:
    """A single relationship edge considered for the LLM context."""
    from_name: str
    to_name: str
    relationship_type: str
    inferred: bool
    confidence: float


@dataclass
class GraphContext:
    """Assembled, prompt-ready view of an opportunity's graph neighbourhood.

    Field set per Section 3. ``observed_summary`` is the locked text block ENT-3
    inserts verbatim into the DIRECTLY OBSERVED prompt section; ``inferred_summary``
    holds inferred edges separately (None when there are none). The count fields
    surface on OppEnrichment (graph_entity_count / _shown / graph_truncated).
    ``sparse_graph`` signals the pre-graph fallback path.
    """
    opportunity_id: str
    entities: List[EntityContext] = field(default_factory=list)        # ranked, <= 15
    relationships: List[RelationshipContext] = field(default_factory=list)  # ranked, <= 20
    observed_summary: str = ""
    inferred_summary: Optional[str] = None
    entity_count: int = 0              # total in neighbourhood (before cap)
    entity_count_shown: int = 0        # after cap (<= 15)
    relationship_count: int = 0        # total (before cap)
    relationship_count_shown: int = 0  # after cap (<= 20)
    truncated: bool = False            # True when a cap was exceeded
    max_depth_reached: int = 0
    sparse_graph: bool = False         # True when entity_count < SPARSE_GRAPH_THRESHOLD


# ---------------------------------------------------------------------------
# Deterministic ranking (Section 2b)
# ---------------------------------------------------------------------------

def rank_entities_for_context(
    entities: List[EntityContext],
    max_entities: int = GRAPH_CONTEXT_MAX_ENTITIES,
) -> List[EntityContext]:
    """Deterministic ranking + cap for LLM entity context (Section 2b).

    Priority order:
      1. Depth-0 entities (directly linked to the opportunity) — always first.
      2. Remaining entities by type priority (person, team, object, process,
         system), then run_count DESC, then resolution_confidence DESC.
    Tie-break: display_name alphabetical, then entity_id — a total order, so the
    same input always yields the same output (AC4).
    """
    depth_0 = [e for e in entities if e.depth == 0]
    remaining = [e for e in entities if e.depth != 0]

    # Depth-0 ordered deterministically among themselves (still ahead of all
    # depth>0 entities regardless of type).
    depth_0.sort(key=lambda e: (-e.run_count, -e.confidence, e.name, e.entity_id))
    remaining.sort(
        key=lambda e: (
            TYPE_PRIORITY.get(e.entity_type, _UNKNOWN_TYPE_PRIORITY),
            -e.run_count,
            -e.confidence,
            e.name,
            e.entity_id,
        )
    )

    return (depth_0 + remaining)[:max_entities]


def rank_relationships_for_context(
    relationships: List[RelationshipContext],
    max_relationships: int = GRAPH_CONTEXT_MAX_RELATIONSHIPS,
) -> List[RelationshipContext]:
    """Deterministic ranking + cap for LLM relationship context (Section 2b).

    Order: confidence DESC, then inferred ASC (observed edges before inferred —
    False sorts before True), then from_name / to_name / relationship_type
    alphabetical as a total-order tie-break.
    """
    ordered = sorted(
        relationships,
        key=lambda r: (
            r.inferred,  # False (0) before True (1) — observed first
            -r.confidence,
            r.from_name,
            r.to_name,
            r.relationship_type,
        ),
    )
    return ordered[:max_relationships]


# ---------------------------------------------------------------------------
# Summary rendering — LOCKED format (ENT-3's prompt depends on this)
# ---------------------------------------------------------------------------

def _humanise(relationship_type: str) -> str:
    return str(relationship_type).replace("_", " ")


def _truncation_note(entity_count: int, entity_count_shown: int) -> str:
    """Section 2c truncation note. States how many additional entities exist."""
    extra = entity_count - entity_count_shown
    return (
        f"...and {extra} additional entities were identified but are not shown "
        f"here. The most significant entities by frequency and confidence are "
        f"listed above."
    )


def _render_observed_summary(
    opportunity_id: str,
    entities: List[EntityContext],
    observed_rels: List[RelationshipContext],
    entity_count: int,
    entity_count_shown: int,
    max_depth_reached: int,
    truncated: bool,
) -> str:
    """Render the LOCKED observed_summary block.

    Plain-English, deterministic, prompt-ready. Lists the ranked (capped)
    entities, then observed relationships, then the truncation note when the
    graph exceeded a cap. The format is locked — ENT-3's first-pass prompt
    inserts this verbatim and depends on it being stable and readable.
    """
    lines: List[str] = []
    lines.append(
        f"This opportunity is connected to {entity_count_shown} "
        f"{'entity' if entity_count_shown == 1 else 'entities'} within "
        f"{max_depth_reached} {'hop' if max_depth_reached == 1 else 'hops'} of "
        f"the knowledge graph."
    )

    lines.append("")
    lines.append("Entities:")
    for e in entities:
        runs = f"{e.run_count} {'run' if e.run_count == 1 else 'runs'}"
        lines.append(f"- {e.name} ({e.entity_type}, seen in {runs})")

    if observed_rels:
        lines.append("")
        lines.append("Observed relationships:")
        for r in observed_rels:
            lines.append(f"- {r.from_name} {_humanise(r.relationship_type)} {r.to_name}")

    if truncated:
        lines.append("")
        lines.append(_truncation_note(entity_count, entity_count_shown))

    return "\n".join(lines)


def _render_inferred_summary(inferred_rels: List[RelationshipContext]) -> Optional[str]:
    """Render the inferred-edges block, or None when there are none.

    Inferred edges are kept separate from observed facts so the prompt can tag
    them [INFERRED: ...] rather than presenting them as observed truth.
    """
    if not inferred_rels:
        return None
    lines = ["Inferred relationships (not directly observed):"]
    for r in inferred_rels:
        lines.append(f"- {r.from_name} {_humanise(r.relationship_type)} {r.to_name}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# build_graph_context — the public entry point (T3)
# ---------------------------------------------------------------------------

def _nodes_to_entity_contexts(nodes) -> List[EntityContext]:
    return [
        EntityContext(
            entity_id=n.entity_id,
            name=n.display_name,
            entity_type=n.entity_type,
            depth=n.depth,
            run_count=n.run_count,
            confidence=n.resolution_confidence,
        )
        for n in nodes
    ]


def _nodes_to_relationship_contexts(nodes) -> List[RelationshipContext]:
    """Derive relationship edges from the neighbourhood nodes.

    Each non-seed node carries the edge that reached it
    (from_entity_id -> entity_id, relationship_type, inferred). Edge confidence
    is the model-defined value for its category (observed 0.9 / inferred 0.6) —
    these are the only permitted edge-confidence values in the data model.
    """
    id_to_name = {n.entity_id: n.display_name for n in nodes}
    rels: List[RelationshipContext] = []
    for n in nodes:
        if not n.from_entity_id or not n.relationship_type:
            continue  # seed node (depth 0) — no incoming edge
        from_name = id_to_name.get(n.from_entity_id)
        if from_name is None:
            continue  # endpoint not in the surfaced set — skip defensively
        inferred = bool(n.inferred)
        rels.append(
            RelationshipContext(
                from_name=from_name,
                to_name=n.display_name,
                relationship_type=n.relationship_type,
                inferred=inferred,
                confidence=INFERRED_CONFIDENCE if inferred else OBSERVED_CONFIDENCE,
            )
        )
    return rels


def build_graph_context(
    org_id: str,
    opportunity_id: str,
    seed_entity_ids: List[str],
    max_depth: int = 2,
    include_inferred: bool = False,
) -> GraphContext:
    """Assemble the prompt-ready :class:`GraphContext` for an opportunity.

    Collects the graph around ``seed_entity_ids`` (the opportunity's resolved
    entities, e.g. OppEnrichment.entities) via opportunity_neighbourhood(),
    ranks and caps the entities (15) and relationships (20) deterministically,
    and renders the locked observed_summary.

    Sparse graphs (entity_count < 3) return a valid GraphContext with
    sparse_graph=True and an empty observed_summary — the function never raises
    (AC7). A graph.context_built telemetry event is emitted on every call
    (AC10), fire-and-forget.
    """
    start = time.monotonic()

    try:
        nodes = opportunity_neighbourhood(
            org_id, seed_entity_ids, max_depth=max_depth, include_inferred=include_inferred
        )
    except Exception as exc:  # never raise — degrade to an empty/sparse context.
        logger.warning(
            "build_graph_context: neighbourhood query failed for opp=%s (org=%s): %s",
            opportunity_id, org_id, exc,
        )
        nodes = []

    entity_contexts = _nodes_to_entity_contexts(nodes)
    relationship_contexts = _nodes_to_relationship_contexts(nodes)

    entity_count = len(entity_contexts)
    relationship_count = len(relationship_contexts)
    max_depth_reached = max((n.depth for n in nodes), default=0)
    sparse = entity_count < SPARSE_GRAPH_THRESHOLD

    ranked_entities = rank_entities_for_context(entity_contexts)
    ranked_relationships = rank_relationships_for_context(relationship_contexts)
    entity_count_shown = len(ranked_entities)
    relationship_count_shown = len(ranked_relationships)

    truncated = (
        entity_count > GRAPH_CONTEXT_MAX_ENTITIES
        or relationship_count > GRAPH_CONTEXT_MAX_RELATIONSHIPS
    )

    if sparse:
        # Valid, non-raising context; empty summary signals the fallback path.
        observed_summary = ""
        inferred_summary = None
    else:
        observed_rels = [r for r in ranked_relationships if not r.inferred]
        inferred_rels = [r for r in ranked_relationships if r.inferred]
        observed_summary = _render_observed_summary(
            opportunity_id=opportunity_id,
            entities=ranked_entities,
            observed_rels=observed_rels,
            entity_count=entity_count,
            entity_count_shown=entity_count_shown,
            max_depth_reached=max_depth_reached,
            truncated=truncated,
        )
        inferred_summary = _render_inferred_summary(inferred_rels)

    context = GraphContext(
        opportunity_id=opportunity_id,
        entities=ranked_entities,
        relationships=ranked_relationships,
        observed_summary=observed_summary,
        inferred_summary=inferred_summary,
        entity_count=entity_count,
        entity_count_shown=entity_count_shown,
        relationship_count=relationship_count,
        relationship_count_shown=relationship_count_shown,
        truncated=truncated,
        max_depth_reached=max_depth_reached,
        sparse_graph=sparse,
    )

    duration_ms = max(0, int((time.monotonic() - start) * 1000))
    try:
        record_event(
            "graph.context_built",
            {
                "org_id": org_id,
                "opportunity_id": opportunity_id,
                "entity_count": entity_count,
                "entity_count_shown": entity_count_shown,
                "relationship_count": relationship_count,
                "relationship_count_shown": relationship_count_shown,
                "truncated": truncated,
                "sparse_graph": sparse,
                "duration_ms": duration_ms,
                "source": "graph_context_builder",
            },
        )
    except Exception as exc:  # fire-and-forget — telemetry never breaks the build.
        logger.debug("graph.context_built telemetry failed: %s", exc)

    return context
