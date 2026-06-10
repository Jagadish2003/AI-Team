"""Causal inference engine for Stage 3 (T3-S16-A / ENT-6).

This module owns:
  CausalContext     — the assembled data package fed to the causal-chain prompt.
  InsufficientGraphContextError — raised when entity count < 3 (AC9).
  build_causal_context() — assembles graph neighbourhood, dependency paths,
                           and temporal support for process entities.

ENT-4 dependency note
---------------------
Section 2a specifies opportunity_neighbourhood() and entity_path() from
graph_query.py as the intended graph API.  Those functions are ENT-4
deliverables and are not present in the current branch — graph_query.py
today only exposes get_entity_relationships(org_id, entity_id, inferred=False).

Decision (option a): this module provides a minimal, clearly-scoped in-engine
implementation of both primitives on top of get_entity_relationships():
  _depth3_neighbourhood() — BFS to depth 3 with inferred=True (correct for
                            causal analysis, which validates inferred edges).
  _shortest_path()        — bidirectional BFS between two entity IDs.

When ENT-4 merges and graph_query.py exposes opportunity_neighbourhood() /
entity_path(), replace the two private helpers with imports and delete this
note.  The public signature of build_causal_context() is unchanged.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from app import db
    from app.trend_engine import calculate_anomaly, calculate_trend
    from app.temporal_enrichment import build_baseline_context
except ModuleNotFoundError:  # pragma: no cover - supports repo-root imports
    from backend.app import db  # type: ignore[no-redef]
    from backend.app.trend_engine import calculate_anomaly, calculate_trend  # type: ignore[no-redef]
    from backend.app.temporal_enrichment import build_baseline_context  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class InsufficientGraphContextError(Exception):
    """Raised when the entity neighbourhood has fewer than 3 entities.

    Callers (T3/T4) catch this, skip hypothesis generation, and log
    causal.hypothesis_rejected with reason='insufficient_graph_context'.
    """


# ---------------------------------------------------------------------------
# Graph neighbourhood data containers
# ---------------------------------------------------------------------------

@dataclass
class EntityNode:
    """Lightweight entity representation within a causal neighbourhood."""
    entity_id: str
    entity_type: str
    display_name: str
    resolution_status: str
    org_id: str


@dataclass
class EdgeNode:
    """Edge representation within a causal neighbourhood."""
    from_entity_id: str
    to_entity_id: str
    relationship_type: str
    inferred: bool
    confidence: float


@dataclass
class GraphNeighbourhood:
    """Depth-3 neighbourhood around a set of seed entities."""
    entities: list[EntityNode] = field(default_factory=list)
    edges: list[EdgeNode] = field(default_factory=list)

    @property
    def entity_count(self) -> int:
        return len(self.entities)


# ---------------------------------------------------------------------------
# CausalContext — the assembled data package T3 serialises into the prompt
# ---------------------------------------------------------------------------

@dataclass
class CausalContext:
    """All data needed to generate and validate a causal-chain hypothesis.

    Fields
    ------
    graph_context     : depth-3 entity/edge neighbourhood (inferred edges included).
    dependency_paths  : pairwise shortest paths between process entities.
    temporal_support  : signal_key → {trend, anomaly, context} for process
                        entity signals with run_count >= 5.
    """
    graph_context: GraphNeighbourhood
    dependency_paths: list[list[str]]
    temporal_support: dict[str, dict[str, Any]]


# ---------------------------------------------------------------------------
# Private graph primitives (temporary in-engine implementation — see module
# docstring for the ENT-4 migration plan)
# ---------------------------------------------------------------------------

_EDGES_WITH_IDS_SQL = """
    SELECT
        er.from_entity_id   AS from_entity_id,
        er.to_entity_id     AS to_entity_id,
        er.relationship_type AS relationship_type,
        er.inferred         AS inferred,
        er.confidence       AS confidence,
        ef.entity_type      AS from_entity_type,
        ef.display_name     AS from_display_name,
        ef.resolution_status AS from_resolution_status,
        et.entity_type      AS to_entity_type,
        et.display_name     AS to_display_name,
        et.resolution_status AS to_resolution_status
    FROM entity_relationships er
    JOIN entities ef ON ef.id = er.from_entity_id AND ef.org_id = er.org_id
    JOIN entities et ON et.id = er.to_entity_id   AND et.org_id = er.org_id
    WHERE er.org_id = ?
      AND (er.from_entity_id = ? OR er.to_entity_id = ?)
"""


def _raw_edges_for_entity(org_id: str, entity_id: str) -> list[dict[str, Any]]:
    """Return all edges (including inferred) touching entity_id."""
    conn = db.connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_EDGES_WITH_IDS_SQL, (org_id, entity_id, entity_id)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _depth3_neighbourhood(org_id: str, seed_entity_ids: list[str]) -> GraphNeighbourhood:
    """BFS to depth 3 from seed entities, including inferred edges.

    Causal analysis deliberately includes inferred edges — Stage 3 is the
    step that validates them against temporal data (spec Section 2a).

    Temporary in-engine implementation pending ENT-4's opportunity_neighbourhood().
    """
    visited_entity_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()  # (from, to, type)

    entities: dict[str, EntityNode] = {}
    edges: list[EdgeNode] = []

    queue: deque[tuple[str, int]] = deque()
    for eid in seed_entity_ids:
        if eid not in visited_entity_ids:
            visited_entity_ids.add(eid)
            queue.append((eid, 0))

    while queue:
        current_id, depth = queue.popleft()
        raw_edges = _raw_edges_for_entity(org_id, current_id)

        for row in raw_edges:
            fid = row["from_entity_id"]
            tid = row["to_entity_id"]
            rtype = row["relationship_type"]

            neighbour_id = tid if fid == current_id else fid
            neighbour_depth = depth + 1

            # Only include this edge and materialise the far-end entity if
            # the neighbour is already visited (within the boundary) or would
            # be at depth <= 3 (i.e. still inside the neighbourhood).
            if neighbour_id not in visited_entity_ids and neighbour_depth > 3:
                continue

            edge_key = (fid, tid, rtype)
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append(EdgeNode(
                    from_entity_id=fid,
                    to_entity_id=tid,
                    relationship_type=rtype,
                    inferred=bool(row["inferred"]),
                    confidence=float(row["confidence"]),
                ))

            # Materialise the current node entity if not yet seen
            if fid not in entities:
                entities[fid] = EntityNode(
                    entity_id=fid,
                    entity_type=row["from_entity_type"],
                    display_name=row["from_display_name"],
                    resolution_status=row["from_resolution_status"],
                    org_id=org_id,
                )
            if tid not in entities:
                entities[tid] = EntityNode(
                    entity_id=tid,
                    entity_type=row["to_entity_type"],
                    display_name=row["to_display_name"],
                    resolution_status=row["to_resolution_status"],
                    org_id=org_id,
                )

            # Enqueue the neighbour for expansion if within depth limit
            if neighbour_id not in visited_entity_ids:
                visited_entity_ids.add(neighbour_id)
                queue.append((neighbour_id, neighbour_depth))

    return GraphNeighbourhood(entities=list(entities.values()), edges=edges)


def _shortest_path(
    neighbourhood: GraphNeighbourhood,
    source_id: str,
    target_id: str,
) -> Optional[list[str]]:
    """Bidirectional BFS shortest path between two entity IDs in the neighbourhood.

    Returns a list of entity_ids from source to target, or None if unreachable.

    Temporary in-engine implementation pending ENT-4's entity_path().
    """
    if source_id == target_id:
        return [source_id]

    # Build adjacency from the neighbourhood's edges (undirected for path finding)
    adjacency: dict[str, set[str]] = {}
    for edge in neighbourhood.edges:
        adjacency.setdefault(edge.from_entity_id, set()).add(edge.to_entity_id)
        adjacency.setdefault(edge.to_entity_id, set()).add(edge.from_entity_id)

    # BFS from source
    visited: dict[str, Optional[str]] = {source_id: None}
    queue: deque[str] = deque([source_id])
    while queue:
        current = queue.popleft()
        if current == target_id:
            # Reconstruct path
            path: list[str] = []
            node: Optional[str] = target_id
            while node is not None:
                path.append(node)
                node = visited[node]
            path.reverse()
            return path
        for neighbour in adjacency.get(current, set()):
            if neighbour not in visited:
                visited[neighbour] = current
                queue.append(neighbour)

    return None


# ---------------------------------------------------------------------------
# Temporal support assembly
# ---------------------------------------------------------------------------

_MIN_RUNS_FOR_TEMPORAL_SUPPORT = 5  # separate from Gate 1's threshold of 10


def _build_temporal_support(
    org_id: str,
    pack_id: str,
    process_entities: list[EntityNode],
) -> dict[str, dict[str, Any]]:
    """Assemble trend/anomaly/context for process entity signals with >= 5 runs."""
    support: dict[str, dict[str, Any]] = {}

    for entity in process_entities:
        signal_key = f"{pack_id}::{entity.entity_id}::metric_value"
        try:
            trend = calculate_trend(org_id, signal_key)
        except Exception:
            logger.debug("calculate_trend failed for signal_key=%s", signal_key)
            continue

        if trend.run_count < _MIN_RUNS_FOR_TEMPORAL_SUPPORT:
            continue

        try:
            # Use a representative current value of 0.0 for anomaly context;
            # the signal history provides the statistical baseline.
            anomaly = calculate_anomaly(org_id, signal_key, current_value=0.0)
        except Exception:
            logger.debug("calculate_anomaly failed for signal_key=%s", signal_key)
            anomaly = None

        context_str: Optional[str] = None
        if anomaly is not None:
            try:
                context_str = build_baseline_context(trend, anomaly, current_value=0.0)
            except Exception:
                logger.debug("build_baseline_context failed for signal_key=%s", signal_key)

        support[signal_key] = {
            "trend": trend.trend_direction,
            "anomaly": anomaly.is_anomalous if anomaly is not None else False,
            "context": context_str,
            "run_count": trend.run_count,
        }

    return support


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_causal_context(
    org_id: str,
    opportunity_id: str,
    seed_entity_ids: list[str],
    pack_id: str,
) -> CausalContext:
    """Assemble graph neighbourhood, dependency paths, and temporal support.

    Parameters
    ----------
    org_id          : organisation scope.
    opportunity_id  : opportunity being explained (used for logging).
    seed_entity_ids : entity IDs from OppEnrichment.entities to seed the BFS.
    pack_id         : pack identifier (e.g. 'service_cloud') used to build
                      signal_keys for temporal support queries.

    Raises
    ------
    InsufficientGraphContextError
        When the assembled neighbourhood has fewer than 3 entities (AC9).
        Callers must catch this, skip hypothesis generation, and log
        causal.hypothesis_rejected with reason='insufficient_graph_context'.
    """
    neighbourhood = _depth3_neighbourhood(org_id, seed_entity_ids)

    if neighbourhood.entity_count < 3:
        logger.info(
            "Insufficient graph context for opportunity=%s org=%s entity_count=%d",
            opportunity_id,
            org_id,
            neighbourhood.entity_count,
        )
        raise InsufficientGraphContextError(
            f"opportunity {opportunity_id} has only {neighbourhood.entity_count} "
            "entities in its depth-3 neighbourhood (minimum 3 required)"
        )

    process_entities = [
        e for e in neighbourhood.entities if e.entity_type == "process"
    ]

    # Pairwise shortest paths between process entities
    dependency_paths: list[list[str]] = []
    for i, pa in enumerate(process_entities):
        for pb in process_entities[i + 1:]:
            path = _shortest_path(neighbourhood, pa.entity_id, pb.entity_id)
            if path:
                dependency_paths.append(path)

    temporal_support = _build_temporal_support(org_id, pack_id, process_entities)

    return CausalContext(
        graph_context=neighbourhood,
        dependency_paths=dependency_paths,
        temporal_support=temporal_support,
    )
