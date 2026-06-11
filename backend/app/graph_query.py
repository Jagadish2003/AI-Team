"""Knowledge-graph read layer for Stage 2 (T3-S13-A).

This module is the single read path for relationship edges that flow into API
responses. It is deliberately separate from relationship_mapper.py (the write
path): mapping decides what is stored, querying decides what is surfaced.

T5 deliverables:
  get_observed_relationships(org_id, run_id) — observed edges only (inferred=0).
  get_all_relationships(org_id, run_id)      — observed + inferred edges.
  select_relationships(org_id, run_id)       — flag-aware selector used by the
                                               OppEnrichment population step.

T8 deliverable:
  get_entity_relationships(org_id, entity_id, inferred=False)
    — single-hop entity-scoped query: all edges touching a specific entity.
      inferred=False (default) returns only directly observed edges.
      inferred=True returns all edges (observed + inferred).
      The default is architecturally intentional: T3-S14-A's recursive CTE
      graph queries call this with the default so the traversal is built on
      solid observed foundations. T3-S15-A passes inferred=True only when
      INFERRED_RELATIONSHIPS_ENABLED is active. Any caller that omits the
      parameter safely gets observed edges only.

T5 queries are scoped to org_id (cross-org isolation) and to the queried run's
chronological position. A relationship remains visible for every run at or
after first_seen_run_id, even when a later upsert moves last_seen_run_id
forward.

T8 query is entity-scoped, not run-scoped: it returns edges across all runs
that involve the specified entity. This is intentional — graph traversal must
see the full history of confirmed edges, not just the current run window.

The flag check lives in select_relationships(), i.e. at population time — never
at query time. Inferred edges are always stored; the flag only decides what is
returned. get_observed_relationships()/get_all_relationships() are unconditional
building blocks so callers (and Stage 3 analysis) can always reach either set.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import deque
from typing import Dict, List, Optional

from pydantic import BaseModel

from app import db
from app.config import inferred_relationships_enabled

logger = logging.getLogger(__name__)


class RelationshipSummary(BaseModel):
    """Edge representation surfaced in OppEnrichment.relationships (T7 field).

    inferred drives the UI '[inferred]' label and is load-bearing for T3-S15-A
    LLM prompt construction — it must always reflect the stored edge's value.
    Field names match the dataclass specified in Section 5 of the story.
    """
    from_entity_name: str
    from_entity_type: str
    relationship_type: str
    to_entity_name: str
    to_entity_type: str
    inferred: bool
    confidence: float


# Edge row joined to both endpoint entities for display names/types, and to
# runs for chronological visibility and run/org ownership. INNER JOIN:
# an edge whose endpoints or first-seen run are not resolvable cannot produce a
# summary and is omitted. Ordered deterministically for stable responses.
_SELECT_EDGES = """
    SELECT
        er.relationship_type AS relationship_type,
        er.inferred          AS inferred,
        er.confidence        AS confidence,
        ef.display_name      AS from_entity_name,
        ef.entity_type       AS from_entity_type,
        et.display_name      AS to_entity_name,
        et.entity_type       AS to_entity_type
    FROM entity_relationships er
    JOIN runs queried_run ON queried_run.id = ?
    JOIN runs first_seen_run ON first_seen_run.id = er.first_seen_run_id
    JOIN entities ef ON ef.id = er.from_entity_id AND ef.org_id = er.org_id
    JOIN entities et ON et.id = er.to_entity_id   AND et.org_id = er.org_id
    WHERE er.org_id = ?
      AND COALESCE(
            json_extract(queried_run.payload, '$.org_id'),
            json_extract(queried_run.payload, '$.orgId')
          ) = er.org_id
      AND COALESCE(
            json_extract(first_seen_run.payload, '$.org_id'),
            json_extract(first_seen_run.payload, '$.orgId')
          ) = er.org_id
      AND first_seen_run.rowid <= queried_run.rowid
      {inferred_filter}
    ORDER BY er.inferred ASC, er.relationship_type ASC,
             ef.display_name ASC, et.display_name ASC
"""


def _query(org_id: str, run_id: str, observed_only: bool) -> List[RelationshipSummary]:
    """Run the edge query for a run, optionally filtering to observed edges."""
    inferred_filter = "AND er.inferred = 0" if observed_only else ""
    sql = _SELECT_EDGES.format(inferred_filter=inferred_filter)

    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (run_id, org_id)).fetchall()
    finally:
        conn.close()

    summaries: List[RelationshipSummary] = []
    for row in rows:
        summaries.append(
            RelationshipSummary(
                from_entity_name=row["from_entity_name"],
                from_entity_type=row["from_entity_type"],
                relationship_type=row["relationship_type"],
                to_entity_name=row["to_entity_name"],
                to_entity_type=row["to_entity_type"],
                inferred=bool(row["inferred"]),
                confidence=float(row["confidence"]),
            )
        )
    return summaries


def get_observed_relationships(org_id: str, run_id: str) -> List[RelationshipSummary]:
    """Return only directly observed edges (inferred=False) for a run.

    These are graph truth — owns / member_of / escalates_to derived from
    explicit source fields. Scoped to org_id and run.
    """
    return _query(org_id, run_id, observed_only=True)


def get_all_relationships(org_id: str, run_id: str) -> List[RelationshipSummary]:
    """Return all edges — observed and inferred — for a run.

    The inferred=True flag is preserved on each summary so the UI can render
    the '[inferred]' label. Scoped to org_id and run.
    """
    return _query(org_id, run_id, observed_only=False)


def select_relationships(org_id: str, run_id: str) -> List[RelationshipSummary]:
    """Flag-aware selector used by the OppEnrichment population step.

    INFERRED_RELATIONSHIPS_ENABLED=False (default) → observed edges only.
    INFERRED_RELATIONSHIPS_ENABLED=True            → observed + inferred edges.

    The flag is evaluated here, at population time. Inferred edges are always
    stored regardless of the flag — this only decides what is returned.
    """
    if inferred_relationships_enabled():
        return get_all_relationships(org_id, run_id)
    return get_observed_relationships(org_id, run_id)


# Entity-scoped SQL: returns all edges where the entity appears as either
# endpoint, across all runs. Separate template from _SELECT_EDGES because:
# (a) filter is on entity_id + org_id rather than run chronology, and
# (b) ORDER BY is the same deterministic ordering so callers get stable results.
# TODO(T3-S14): add pagination before this is exposed as a broad public API.
# Current Sprint 13 callers use internal, single-hop lookups with small
# row-count ceilings: a handful of edges per entity in demo/offline packs.
_SELECT_ENTITY_EDGES = """
    SELECT
        er.relationship_type AS relationship_type,
        er.inferred          AS inferred,
        er.confidence        AS confidence,
        ef.display_name      AS from_entity_name,
        ef.entity_type       AS from_entity_type,
        et.display_name      AS to_entity_name,
        et.entity_type       AS to_entity_type
    FROM entity_relationships er
    JOIN entities ef ON ef.id = er.from_entity_id AND ef.org_id = er.org_id
    JOIN entities et ON et.id = er.to_entity_id   AND et.org_id = er.org_id
    WHERE er.org_id = ?
      AND (er.from_entity_id = ? OR er.to_entity_id = ?)
      {inferred_filter}
    ORDER BY er.inferred ASC, er.relationship_type ASC,
             ef.display_name ASC, et.display_name ASC
"""


def get_entity_relationships(
    org_id: str,
    entity_id: str,
    inferred: bool = False,
) -> List[RelationshipSummary]:
    """Return all edges for a specific entity (single-hop lookup).

    Queries edges where from_entity_id=entity_id OR to_entity_id=entity_id
    within the given org_id, across all runs.

    inferred=False (default): returns only directly observed edges (inferred=0).
      T3-S14-A recursive CTE queries call with this default — the traversal is
      built on solid observed foundations, not co-firing hypotheses.
    inferred=True: returns all edges including inferred ones.
      T3-S15-A LLM context builder passes inferred=True only when
      INFERRED_RELATIONSHIPS_ENABLED is active.

    The inferred=False default is architecturally intentional. Any caller that
    does not explicitly pass inferred=True gets observed edges only — preventing
    inferred hypotheses from accidentally entering graph traversal or LLM prompts.

    Cross-org isolation: the entity JOIN is scoped by org_id on both sides,
    so an entity_id that happens to exist in another org cannot produce a match.
    """
    inferred_filter = "" if inferred else "AND er.inferred = 0"
    sql = _SELECT_ENTITY_EDGES.format(inferred_filter=inferred_filter)

    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (org_id, entity_id, entity_id)).fetchall()
    finally:
        conn.close()

    summaries: List[RelationshipSummary] = []
    for row in rows:
        summaries.append(
            RelationshipSummary(
                from_entity_name=row["from_entity_name"],
                from_entity_type=row["from_entity_type"],
                relationship_type=row["relationship_type"],
                to_entity_name=row["to_entity_name"],
                to_entity_type=row["to_entity_type"],
                inferred=bool(row["inferred"]),
                confidence=float(row["confidence"]),
            )
        )
    return summaries


# ===========================================================================
# ENT-4 / T3-S14-A — Graph traversal query layer
# ===========================================================================
#
# Four read-only, org-scoped traversal functions that walk the knowledge graph
# built from resolved entities and observed relationships:
#
#   opportunity_neighbourhood()  — entities connected to an opportunity's seeds
#   entity_neighbourhood()       — entities connected to a single entity
#   entity_path()                — shortest path between two entities
#   relationship_type_filter()   — flat list of edges of one relationship type
#
# Enterprise safety limits (hard, NOT per-run configurable) are built in from
# day one so traversal stays bounded on large enterprise graphs (TCU / City
# National Salesforce orgs have hundreds of entities after a few runs):
#
#   - max depth 5 hops (default 2)               -> MAX_TRAVERSAL_DEPTH / DEFAULT_TRAVERSAL_DEPTH
#   - cycle detection via a visited path/set     -> A->B->C->A terminates
#   - 500-node hard cap per query                -> MAX_NODES_PER_QUERY
#   - 10-second wall-clock timeout per query     -> QUERY_TIMEOUT_SECONDS
#
# Every query is scoped to org_id (cross-org isolation) and traverses ONLY
# resolution_status='resolved' entities — ambiguous / unresolved entities are
# excluded so the graph is trustworthy before LLM enrichment / causal chains
# consume it.
#
# IMPLEMENTATION NOTE — why Python BFS, not a PostgreSQL recursive CTE:
# The T3-S14-A reference design expresses traversal as a PostgreSQL recursive
# CTE (ARRAY visited path, ANY(:seed_ids)). AgentIQ's entities /
# entity_relationships tables live in SQLite for dev, contract tests, and the
# offline pack (see app/db.py). SQLite has recursive CTEs but no array types,
# so the reference CTE is not portable here. This module implements the SAME
# contract — directed traversal from from_entity_id -> to_entity_id, resolved
# entities only, observed edges by default, depth/cycle/node/time bounds — as a
# breadth-first traversal in Python over single-hop SQL. BFS records each entity
# at its shortest depth (matching the CTE's DISTINCT + depth ordering) and is
# portable across SQLite (dev/test) and PostgreSQL (prod).
# ---------------------------------------------------------------------------

#: Default traversal depth when the caller does not specify one.
DEFAULT_TRAVERSAL_DEPTH = 2
#: Hard maximum traversal depth. Requests above this are clamped down.
MAX_TRAVERSAL_DEPTH = 5
#: Hard cap on the number of nodes a single query may return / expand.
MAX_NODES_PER_QUERY = 500
#: Hard wall-clock timeout (seconds) for a single traversal query.
QUERY_TIMEOUT_SECONDS = 10.0

# Compatibility names used by ENT4 task branches/tests.
_NEIGHBOURHOOD_MAX_DEPTH = MAX_TRAVERSAL_DEPTH
_NEIGHBOURHOOD_NODE_CAP = MAX_NODES_PER_QUERY
_NEIGHBOURHOOD_TIMEOUT_S = QUERY_TIMEOUT_SECONDS


class GraphEntityNode(BaseModel):
    """One entity surfaced by a neighbourhood traversal.

    Mirrors the column set in the T3-S14-A reference query (Section 1a):
    entity attributes plus the edge that reached this node and its depth.
    Seed entities (depth 0) have from_entity_id / relationship_type / inferred
    set to None — nothing reached them, they are the traversal roots.
    """
    entity_id: str
    entity_type: str
    display_name: str
    resolution_confidence: float
    run_count: int
    from_entity_id: Optional[str] = None
    relationship_type: Optional[str] = None
    inferred: Optional[bool] = None
    depth: int


class GraphPathStep(BaseModel):
    """One step in a shortest path returned by entity_path().

    relationship_type / inferred describe the edge traversed to REACH this
    step from the previous one; they are None for the starting entity.
    """
    entity_id: str
    entity_type: str
    display_name: str
    relationship_type: Optional[str] = None
    inferred: Optional[bool] = None
    depth: int


NeighbourhoodNode = GraphEntityNode
GraphTraversalNode = GraphEntityNode
GraphPathNode = GraphPathStep


def _clamp_depth(max_depth: Optional[int]) -> int:
    """Clamp a requested depth into [0, MAX_TRAVERSAL_DEPTH].

    None falls back to DEFAULT_TRAVERSAL_DEPTH. Negative values clamp to 0.
    Values above the hard limit clamp down to MAX_TRAVERSAL_DEPTH — a caller can
    never request an unbounded traversal.
    """
    if max_depth is None:
        return DEFAULT_TRAVERSAL_DEPTH
    try:
        depth = int(max_depth)
    except (TypeError, ValueError):
        return DEFAULT_TRAVERSAL_DEPTH
    return max(0, min(depth, MAX_TRAVERSAL_DEPTH))


def _deadline_exceeded(start: float, timeout_s: float = QUERY_TIMEOUT_SECONDS) -> bool:
    return (time.monotonic() - start) > timeout_s


# Resolved seed entities for a neighbourhood traversal (depth 0). Scoped to
# org_id; only resolution_status='resolved' rows qualify (AC1).
_SELECT_SEED_ENTITIES = """
    SELECT id, entity_type, display_name, resolution_confidence, run_count
    FROM entities
    WHERE org_id = ?
      AND resolution_status = 'resolved'
      AND id IN ({placeholders})
"""

# A single entity by id, org-scoped and resolved only. Used to validate the
# endpoints of a path query.
_SELECT_ONE_RESOLVED_ENTITY = """
    SELECT id, entity_type, display_name, resolution_confidence, run_count
    FROM entities
    WHERE org_id = ? AND id = ? AND resolution_status = 'resolved'
"""

# Outgoing resolved neighbours of a single entity (one hop, directed
# from_entity_id -> to_entity_id), matching the reference CTE's JOIN direction.
# inferred edges are excluded unless the caller opts in.
_SELECT_NEIGHBOURS = """
    SELECT
        e.id                    AS entity_id,
        e.entity_type           AS entity_type,
        e.display_name          AS display_name,
        e.resolution_confidence AS resolution_confidence,
        e.run_count             AS run_count,
        er.from_entity_id       AS from_entity_id,
        er.relationship_type    AS relationship_type,
        er.inferred             AS inferred
    FROM entity_relationships er
    JOIN entities e
      ON e.id = er.to_entity_id
     AND e.org_id = er.org_id
     AND e.resolution_status = 'resolved'
    WHERE er.org_id = ?
      AND er.from_entity_id = ?
      {inferred_filter}
    ORDER BY e.run_count DESC, e.resolution_confidence DESC,
             e.display_name ASC, e.id ASC
"""


def _fetch_seed_nodes(
    conn: sqlite3.Connection, org_id: str, seed_entity_ids: List[str]
) -> List[GraphEntityNode]:
    """Return resolved, org-scoped seed entities as depth-0 nodes."""
    ids = [s for s in dict.fromkeys(seed_entity_ids) if s]  # de-dup, drop falsy
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = _SELECT_SEED_ENTITIES.format(placeholders=placeholders)
    rows = conn.execute(sql, (org_id, *ids)).fetchall()
    return [
        GraphEntityNode(
            entity_id=row["id"],
            entity_type=row["entity_type"],
            display_name=row["display_name"],
            resolution_confidence=float(row["resolution_confidence"]),
            run_count=int(row["run_count"]),
            from_entity_id=None,
            relationship_type=None,
            inferred=None,
            depth=0,
        )
        for row in rows
    ]


def entity_exists(org_id: str, entity_id: str) -> bool:
    """Return True when the entity belongs to org_id and is resolved."""
    conn = db.connect()
    try:
        row = conn.execute(
            _SELECT_ONE_RESOLVED_ENTITY,
            (org_id, entity_id),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _fetch_neighbours(
    conn: sqlite3.Connection,
    org_id: str,
    entity_id: str,
    include_inferred: bool,
) -> List[sqlite3.Row]:
    inferred_filter = "" if include_inferred else "AND er.inferred = 0"
    sql = _SELECT_NEIGHBOURS.format(inferred_filter=inferred_filter)
    return conn.execute(sql, (org_id, entity_id)).fetchall()


def _bfs_neighbourhood(
    org_id: str,
    seed_entity_ids: List[str],
    max_depth: int,
    include_inferred: bool,
    timeout_s: float = QUERY_TIMEOUT_SECONDS,
) -> List[GraphEntityNode]:
    """Breadth-first neighbourhood traversal shared by the two entry points.

    BFS guarantees each entity is recorded once at its shortest depth. A global
    visited set provides cycle detection: an entity already visited is never
    expanded again, so A->B->C->A terminates (AC2). Bounded by depth, the
    500-node cap, and the 10s timeout.
    """
    depth_limit = _clamp_depth(max_depth)
    start = time.monotonic()

    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row

        seeds = _fetch_seed_nodes(conn, org_id, seed_entity_ids)
        results: List[GraphEntityNode] = []
        visited: set[str] = set()
        queue: deque[GraphEntityNode] = deque()

        for node in seeds:
            if node.entity_id in visited:
                continue
            visited.add(node.entity_id)
            results.append(node)
            queue.append(node)

        while queue:
            if len(results) >= MAX_NODES_PER_QUERY:
                logger.warning(
                    "graph traversal hit %d-node cap (org=%s) — returning capped result",
                    MAX_NODES_PER_QUERY, org_id,
                )
                break
            if _deadline_exceeded(start, timeout_s):
                logger.warning(
                    "graph traversal exceeded %.0fs timeout (org=%s) — returning partial result",
                    timeout_s, org_id,
                )
                break

            current = queue.popleft()
            if current.depth >= depth_limit:
                continue

            for row in _fetch_neighbours(conn, org_id, current.entity_id, include_inferred):
                neighbour_id = row["entity_id"]
                if neighbour_id in visited:
                    continue  # cycle / already recorded at shorter depth
                visited.add(neighbour_id)
                node = GraphEntityNode(
                    entity_id=neighbour_id,
                    entity_type=row["entity_type"],
                    display_name=row["display_name"],
                    resolution_confidence=float(row["resolution_confidence"]),
                    run_count=int(row["run_count"]),
                    from_entity_id=row["from_entity_id"],
                    relationship_type=row["relationship_type"],
                    inferred=bool(row["inferred"]),
                    depth=current.depth + 1,
                )
                results.append(node)
                queue.append(node)
                if len(results) >= MAX_NODES_PER_QUERY:
                    break
    finally:
        conn.close()

    # Deterministic ordering matching the reference query:
    # depth ASC, run_count DESC, resolution_confidence DESC, display_name ASC.
    results.sort(
        key=lambda n: (n.depth, -n.run_count, -n.resolution_confidence, n.display_name, n.entity_id)
    )
    return results[:MAX_NODES_PER_QUERY]


def opportunity_neighbourhood(
    org_id: str,
    seed_entity_ids: List[str],
    max_depth: int = DEFAULT_TRAVERSAL_DEPTH,
    include_inferred: bool = False,
    timeout_s: float = QUERY_TIMEOUT_SECONDS,
) -> List[GraphEntityNode]:
    """Entities connected to an opportunity's seed entities.

    The seeds are the resolved entities attached to the opportunity
    (OppEnrichment.entities). Returns those seeds (depth 0) plus every resolved
    entity reachable within ``max_depth`` directed hops over observed edges.

    Scoped to org_id. Excludes ambiguous / unresolved entities (AC1). Cycles
    terminate via the visited set (AC2). Bounded by depth (default 2, max 5),
    the 500-node cap, and the 10s timeout. ``include_inferred`` opts inferred
    edges into the traversal (default observed-only).
    """
    return _bfs_neighbourhood(
        org_id,
        seed_entity_ids,
        max_depth,
        include_inferred,
        timeout_s=timeout_s,
    )


def entity_neighbourhood(
    org_id: str,
    entity_id: str,
    max_depth: int = DEFAULT_TRAVERSAL_DEPTH,
    include_inferred: bool = False,
    timeout_s: float = QUERY_TIMEOUT_SECONDS,
) -> List[GraphEntityNode]:
    """Entities connected to a single entity, within ``max_depth`` hops.

    Same traversal and safety bounds as opportunity_neighbourhood(), seeded from
    one entity. Used by the evidence-trace entity panel and ENT-6 causal chains.
    Returns an empty list when the entity is missing, in another org, or not
    resolved.
    """
    return _bfs_neighbourhood(
        org_id,
        [entity_id],
        max_depth,
        include_inferred,
        timeout_s=timeout_s,
    )


def entity_path(
    org_id: str,
    from_entity_id: str,
    to_entity_id: str,
    max_depth: int = MAX_TRAVERSAL_DEPTH,
    include_inferred: bool = False,
) -> List[GraphPathStep]:
    """Shortest directed path between two resolved entities.

    Breadth-first search from ``from_entity_id`` following observed edges
    (from -> to) returns the shortest path to ``to_entity_id`` as an ordered
    list of steps (start first). Returns an empty list when no path exists
    within ``max_depth`` hops, when either endpoint is missing / in another org
    / unresolved, and it always terminates on cyclic graphs (visited set).

    Scoped to org_id. Bounded by depth (max 5), the 500-node visit cap, and the
    10s timeout. ``include_inferred`` opts inferred edges into the search.
    """
    depth_limit = _clamp_depth(max_depth)
    start = time.monotonic()

    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row

        src_rows = conn.execute(
            _SELECT_ONE_RESOLVED_ENTITY, (org_id, from_entity_id)
        ).fetchall()
        dst_rows = conn.execute(
            _SELECT_ONE_RESOLVED_ENTITY, (org_id, to_entity_id)
        ).fetchall()
        if not src_rows or not dst_rows:
            return []  # an endpoint is missing, cross-org, or unresolved

        src = src_rows[0]

        # Trivial path: source is the destination.
        if from_entity_id == to_entity_id:
            return [
                GraphPathStep(
                    entity_id=src["id"],
                    entity_type=src["entity_type"],
                    display_name=src["display_name"],
                    relationship_type=None,
                    inferred=None,
                    depth=0,
                )
            ]

        # BFS tracking each node's parent + the edge used to reach it, so the
        # shortest path can be reconstructed. visited prevents revisiting nodes
        # (cycle-safe) and bounds the search to MAX_NODES_PER_QUERY visits.
        visited: set[str] = {from_entity_id}
        parent: Dict[str, tuple] = {}
        queue: deque = deque([(from_entity_id, 0)])
        found = False

        while queue and not found:
            if _deadline_exceeded(start) or len(visited) >= MAX_NODES_PER_QUERY:
                logger.warning(
                    "entity_path hit timeout/visit cap (org=%s) — returning no path",
                    org_id,
                )
                break

            current_id, depth = queue.popleft()
            if depth >= depth_limit:
                continue

            for row in _fetch_neighbours(conn, org_id, current_id, include_inferred):
                neighbour_id = row["entity_id"]
                if neighbour_id in visited:
                    continue
                visited.add(neighbour_id)
                parent[neighbour_id] = (current_id, row)
                if neighbour_id == to_entity_id:
                    found = True
                    break
                queue.append((neighbour_id, depth + 1))

        if not found:
            return []

        # Reconstruct path from to_entity_id back to from_entity_id.
        chain: List[tuple] = []
        cursor_id = to_entity_id
        while cursor_id != from_entity_id:
            prev_id, edge_row = parent[cursor_id]
            chain.append((cursor_id, edge_row))
            cursor_id = prev_id
        chain.append((from_entity_id, None))
        chain.reverse()

        steps: List[GraphPathStep] = []
        for depth, (node_id, edge_row) in enumerate(chain):
            if edge_row is None:
                steps.append(
                    GraphPathStep(
                        entity_id=src["id"],
                        entity_type=src["entity_type"],
                        display_name=src["display_name"],
                        relationship_type=None,
                        inferred=None,
                        depth=0,
                    )
                )
            else:
                steps.append(
                    GraphPathStep(
                        entity_id=node_id,
                        entity_type=edge_row["entity_type"],
                        display_name=edge_row["display_name"],
                        relationship_type=edge_row["relationship_type"],
                        inferred=bool(edge_row["inferred"]),
                        depth=depth,
                    )
                )
        return steps
    finally:
        conn.close()


# Flat edge query for one relationship type — no recursion. Both endpoints must
# be resolved and in the same org; inferred edges excluded unless opted in.
_SELECT_EDGES_BY_TYPE = """
    SELECT
        er.relationship_type AS relationship_type,
        er.inferred          AS inferred,
        er.confidence        AS confidence,
        ef.display_name      AS from_entity_name,
        ef.entity_type       AS from_entity_type,
        et.display_name      AS to_entity_name,
        et.entity_type       AS to_entity_type
    FROM entity_relationships er
    JOIN entities ef ON ef.id = er.from_entity_id AND ef.org_id = er.org_id
    JOIN entities et ON et.id = er.to_entity_id   AND et.org_id = er.org_id
    WHERE er.org_id = ?
      AND er.relationship_type = ?
      AND ef.resolution_status = 'resolved'
      AND et.resolution_status = 'resolved'
      {inferred_filter}
    ORDER BY er.confidence DESC, er.inferred ASC,
             ef.display_name ASC, et.display_name ASC
"""


def relationship_type_filter(
    org_id: str,
    relationship_type: str,
    include_inferred: bool = False,
) -> List[RelationshipSummary]:
    """Flat list of edges of one relationship type (no recursion).

    Returns every edge whose relationship_type matches, within org_id, where
    both endpoints are resolved entities. Used by ENT-3 for owns / member_of
    context. ``include_inferred`` adds inferred edges (default observed-only).
    Deterministically ordered: confidence DESC, observed-before-inferred, then
    endpoint names.
    """
    inferred_filter = "" if include_inferred else "AND er.inferred = 0"
    sql = _SELECT_EDGES_BY_TYPE.format(inferred_filter=inferred_filter)

    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (org_id, relationship_type)).fetchall()
    finally:
        conn.close()

    return [
        RelationshipSummary(
            from_entity_name=row["from_entity_name"],
            from_entity_type=row["from_entity_type"],
            relationship_type=row["relationship_type"],
            to_entity_name=row["to_entity_name"],
            to_entity_type=row["to_entity_type"],
            inferred=bool(row["inferred"]),
            confidence=float(row["confidence"]),
        )
        for row in rows
    ]


def org_graph_summary(org_id: str) -> Dict[str, object]:
    """Return org-scoped graph health counts for POC readiness checks."""
    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row
        entity_counts = conn.execute(
            """
            SELECT entity_type, COUNT(*) AS count
            FROM entities
            WHERE org_id = ?
              AND resolution_status = 'resolved'
            GROUP BY entity_type
            ORDER BY entity_type
            """,
            (org_id,),
        ).fetchall()
        relationship_counts = conn.execute(
            """
            SELECT relationship_type, COUNT(*) AS count
            FROM entity_relationships
            WHERE org_id = ?
            GROUP BY relationship_type
            ORDER BY relationship_type
            """,
            (org_id,),
        ).fetchall()
        top_entities = conn.execute(
            """
            SELECT e.id, e.display_name, e.entity_type, COUNT(er.id) AS edge_count
            FROM entities e
            LEFT JOIN entity_relationships er
              ON er.org_id = e.org_id
             AND (er.from_entity_id = e.id OR er.to_entity_id = e.id)
            WHERE e.org_id = ?
              AND e.resolution_status = 'resolved'
            GROUP BY e.id, e.display_name, e.entity_type
            ORDER BY edge_count DESC, e.display_name ASC
            LIMIT 10
            """,
            (org_id,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "org_id": org_id,
        "entity_counts_by_type": {
            row["entity_type"]: int(row["count"]) for row in entity_counts
        },
        "relationship_counts_by_type": {
            row["relationship_type"]: int(row["count"]) for row in relationship_counts
        },
        "top_entities_by_edge_count": [
            {
                "entity_id": row["id"],
                "display_name": row["display_name"],
                "entity_type": row["entity_type"],
                "edge_count": int(row["edge_count"]),
            }
            for row in top_entities
        ],
    }
