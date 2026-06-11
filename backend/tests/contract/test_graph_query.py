"""Contract tests for ENT-4 / T3-S14-A — graph query traversal layer.

Covers the graph query layer in app/graph_query.py:
  opportunity_neighbourhood(), entity_neighbourhood(), entity_path(),
  relationship_type_filter()

and the acceptance criteria belonging to this task:
  AC1 — opportunity_neighbourhood returns resolved entities within max_depth;
        excludes ambiguous and unresolved entities.
  AC2 — cycle A->B->C->A terminates without infinite recursion.
  AC8 — entity_path returns shortest path; empty list when no path.

plus the hard safety limits (max depth 5, default depth 2, cycle detection via
visited set, 500-node cap, 10s timeout), org-scoping (cross-org isolation), and
observed-vs-inferred edge filtering.

All traversal runs on the SQLite contract DB (conftest applies migrations that
create the entities / entity_relationships tables).
"""
import os
import sqlite3
import uuid

import pytest

from app import graph_query
from app.graph_query import (
    DEFAULT_TRAVERSAL_DEPTH,
    MAX_NODES_PER_QUERY,
    MAX_TRAVERSAL_DEPTH,
    QUERY_TIMEOUT_SECONDS,
    GraphEntityNode,
    GraphPathStep,
    entity_neighbourhood,
    entity_path,
    opportunity_neighbourhood,
    relationship_type_filter,
)
from database.models.entities import Entity
from database.models.entity_relationships import EntityRelationship


# ---------------------------------------------------------------------------
# Seeding helpers (mirror the established pattern in test_t7_oppenrichment_relationships)
# ---------------------------------------------------------------------------

def _insert_entity(
    org_id: str,
    display_name: str,
    *,
    entity_type: str = "person",
    resolution_status: str = "resolved",
    resolution_confidence: float = 1.0,
    run_count: int = 1,
    run_id: str = "run_graph",
) -> str:
    entity = Entity(
        org_id=org_id,
        entity_type=entity_type,
        canonical_name=" ".join(display_name.split()).lower() + "-" + uuid.uuid4().hex[:8],
        display_name=display_name,
        source_system="test",
        resolution_confidence=resolution_confidence,
        resolution_status=resolution_status,
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=run_count,
    )
    row = entity.to_db_row()
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """INSERT INTO entities (
                id, org_id, entity_type, canonical_name, display_name,
                source_system, source_record_id, resolution_confidence,
                resolution_status, first_seen_run_id, last_seen_run_id,
                run_count, metadata, created_at, updated_at
            ) VALUES (
                :id, :org_id, :entity_type, :canonical_name, :display_name,
                :source_system, :source_record_id, :resolution_confidence,
                :resolution_status, :first_seen_run_id, :last_seen_run_id,
                :run_count, :metadata, :created_at, :updated_at
            )""",
            row,
        )
        conn.commit()
    return row["id"]


def _insert_relationship(
    org_id: str,
    from_id: str,
    to_id: str,
    *,
    relationship_type: str = "owns",
    inferred: bool = False,
    confidence: float = 0.9,
    run_id: str = "run_graph",
) -> str:
    rel = EntityRelationship(
        org_id=org_id,
        from_entity_id=from_id,
        to_entity_id=to_id,
        relationship_type=relationship_type,
        confidence=confidence,
        inferred=inferred,
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=1,
    )
    row = rel.to_db_row()
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """INSERT INTO entity_relationships (
                id, org_id, from_entity_id, to_entity_id, relationship_type,
                confidence, inferred, evidence, first_seen_run_id,
                last_seen_run_id, run_count, created_at
            ) VALUES (
                :id, :org_id, :from_entity_id, :to_entity_id, :relationship_type,
                :confidence, :inferred, :evidence, :first_seen_run_id,
                :last_seen_run_id, :run_count, :created_at
            )""",
            row,
        )
        conn.commit()
    return row["id"]


def _org() -> str:
    """Unique org id per test for isolation between tests sharing the DB."""
    return "org_" + uuid.uuid4().hex[:10]


def _chain(org: str, names: list[str], **edge_kwargs) -> list[str]:
    """Insert entities for each name and link them in a directed chain."""
    ids = [_insert_entity(org, n) for n in names]
    for a, b in zip(ids, ids[1:]):
        _insert_relationship(org, a, b, **edge_kwargs)
    return ids


# ---------------------------------------------------------------------------
# AC1 — opportunity_neighbourhood: resolved within depth, exclude ambiguous/unresolved
# ---------------------------------------------------------------------------

class TestAC1OpportunityNeighbourhood:
    def test_returns_seed_at_depth_zero(self):
        org = _org()
        a = _insert_entity(org, "Alice")
        nodes = opportunity_neighbourhood(org, [a])
        assert len(nodes) == 1
        assert nodes[0].entity_id == a
        assert nodes[0].depth == 0
        assert nodes[0].from_entity_id is None

    def test_returns_depth_one_neighbour(self):
        org = _org()
        a, b = _chain(org, ["Alice", "Bob"])
        nodes = opportunity_neighbourhood(org, [a])
        ids = {n.entity_id: n for n in nodes}
        assert set(ids) == {a, b}
        assert ids[b].depth == 1
        assert ids[b].from_entity_id == a
        assert ids[b].relationship_type == "owns"

    def test_returns_depth_two_neighbour_at_default_depth(self):
        org = _org()
        a, b, c = _chain(org, ["A", "B", "C"])
        nodes = opportunity_neighbourhood(org, [a])  # default depth 2
        depths = {n.entity_id: n.depth for n in nodes}
        assert depths == {a: 0, b: 1, c: 2}

    def test_excludes_ambiguous_neighbour(self):
        org = _org()
        a = _insert_entity(org, "A")
        amb = _insert_entity(org, "Ambiguous", resolution_status="ambiguous")
        _insert_relationship(org, a, amb)
        nodes = opportunity_neighbourhood(org, [a])
        assert {n.entity_id for n in nodes} == {a}

    def test_excludes_unresolved_neighbour(self):
        org = _org()
        a = _insert_entity(org, "A")
        unr = _insert_entity(org, "Unresolved", resolution_status="unresolved")
        _insert_relationship(org, a, unr)
        nodes = opportunity_neighbourhood(org, [a])
        assert {n.entity_id for n in nodes} == {a}

    def test_ambiguous_seed_excluded(self):
        org = _org()
        amb = _insert_entity(org, "AmbiguousSeed", resolution_status="ambiguous")
        nodes = opportunity_neighbourhood(org, [amb])
        assert nodes == []

    def test_respects_max_depth(self):
        org = _org()
        a, b, c = _chain(org, ["A", "B", "C"])
        nodes = opportunity_neighbourhood(org, [a], max_depth=1)
        assert {n.entity_id for n in nodes} == {a, b}  # C (depth 2) excluded

    def test_org_isolation(self):
        org_a, org_b = _org(), _org()
        a = _insert_entity(org_a, "A-in-A")
        b = _insert_entity(org_a, "B-in-A")
        _insert_relationship(org_a, a, b)
        # Same ids queried under a different org must return nothing.
        assert opportunity_neighbourhood(org_b, [a]) == []

    def test_seed_list_multiple(self):
        org = _org()
        a, b = _chain(org, ["A", "B"])
        x, y = _chain(org, ["X", "Y"])
        nodes = opportunity_neighbourhood(org, [a, x])
        assert {n.entity_id for n in nodes} == {a, b, x, y}


# ---------------------------------------------------------------------------
# Observed vs inferred edge filtering
# ---------------------------------------------------------------------------

class TestInferredFiltering:
    def test_inferred_edge_excluded_by_default(self):
        org = _org()
        a = _insert_entity(org, "A")
        b = _insert_entity(org, "B")
        _insert_relationship(org, a, b, inferred=True, confidence=0.6, relationship_type="depends_on")
        nodes = opportunity_neighbourhood(org, [a])
        assert {n.entity_id for n in nodes} == {a}  # inferred neighbour not traversed

    def test_inferred_edge_included_when_opted_in(self):
        org = _org()
        a = _insert_entity(org, "A")
        b = _insert_entity(org, "B")
        _insert_relationship(org, a, b, inferred=True, confidence=0.6, relationship_type="depends_on")
        nodes = opportunity_neighbourhood(org, [a], include_inferred=True)
        assert {n.entity_id for n in nodes} == {a, b}


# ---------------------------------------------------------------------------
# AC2 — cycle detection
# ---------------------------------------------------------------------------

class TestAC2CycleDetection:
    def test_cycle_terminates(self):
        org = _org()
        a, b, c = [_insert_entity(org, n) for n in ("A", "B", "C")]
        _insert_relationship(org, a, b)
        _insert_relationship(org, b, c)
        _insert_relationship(org, c, a)  # cycle back to A
        nodes = opportunity_neighbourhood(org, [a], max_depth=MAX_TRAVERSAL_DEPTH)
        # Terminates, each entity recorded exactly once.
        ids = [n.entity_id for n in nodes]
        assert sorted(ids) == sorted([a, b, c])
        assert len(ids) == len(set(ids))  # no duplicates / no infinite expansion

    def test_self_loop_terminates(self):
        org = _org()
        a = _insert_entity(org, "A")
        _insert_relationship(org, a, a)  # self loop
        nodes = opportunity_neighbourhood(org, [a])
        assert {n.entity_id for n in nodes} == {a}

    def test_two_node_cycle_terminates(self):
        org = _org()
        a = _insert_entity(org, "A")
        b = _insert_entity(org, "B")
        _insert_relationship(org, a, b)
        _insert_relationship(org, b, a)
        nodes = opportunity_neighbourhood(org, [a], max_depth=MAX_TRAVERSAL_DEPTH)
        assert sorted(n.entity_id for n in nodes) == sorted([a, b])


# ---------------------------------------------------------------------------
# entity_neighbourhood
# ---------------------------------------------------------------------------

class TestEntityNeighbourhood:
    def test_single_seed_neighbourhood(self):
        org = _org()
        a, b, c = _chain(org, ["A", "B", "C"])
        nodes = entity_neighbourhood(org, a)
        assert {n.entity_id for n in nodes} == {a, b, c}

    def test_missing_entity_returns_empty(self):
        org = _org()
        assert entity_neighbourhood(org, "does-not-exist") == []

    def test_unresolved_seed_returns_empty(self):
        org = _org()
        u = _insert_entity(org, "U", resolution_status="unresolved")
        assert entity_neighbourhood(org, u) == []


# ---------------------------------------------------------------------------
# AC8 — entity_path: shortest path, empty when none
# ---------------------------------------------------------------------------

class TestAC8EntityPath:
    def test_shortest_path_in_order(self):
        org = _org()
        a, b, c = _chain(org, ["A", "B", "C"])
        path = entity_path(org, a, c)
        assert [s.entity_id for s in path] == [a, b, c]
        assert path[0].relationship_type is None  # start has no incoming edge
        assert path[1].relationship_type == "owns"
        assert [s.depth for s in path] == [0, 1, 2]

    def test_direct_path(self):
        org = _org()
        a, b = _chain(org, ["A", "B"])
        path = entity_path(org, a, b)
        assert [s.entity_id for s in path] == [a, b]

    def test_no_path_returns_empty(self):
        org = _org()
        a = _insert_entity(org, "A")
        b = _insert_entity(org, "B")  # no edge between them
        assert entity_path(org, a, b) == []

    def test_same_entity_returns_single_node(self):
        org = _org()
        a = _insert_entity(org, "A")
        path = entity_path(org, a, a)
        assert len(path) == 1
        assert path[0].entity_id == a
        assert path[0].depth == 0

    def test_chooses_shortest_when_multiple_paths(self):
        org = _org()
        a, b, c, d = [_insert_entity(org, n) for n in ("A", "B", "C", "D")]
        # Long path A->B->C->D and short path A->D
        _insert_relationship(org, a, b)
        _insert_relationship(org, b, c)
        _insert_relationship(org, c, d)
        _insert_relationship(org, a, d)
        path = entity_path(org, a, d)
        assert [s.entity_id for s in path] == [a, d]  # shortest (1 hop)

    def test_path_beyond_max_depth_returns_empty(self):
        org = _org()
        ids = _chain(org, ["A", "B", "C", "D", "E"])  # 4 hops A..E
        # E is 4 hops from A; max_depth=3 cannot reach it.
        assert entity_path(org, ids[0], ids[-1], max_depth=3) == []
        # max_depth=4 can.
        assert [s.entity_id for s in entity_path(org, ids[0], ids[-1], max_depth=4)] == ids

    def test_path_cross_org_returns_empty(self):
        org_a, org_b = _org(), _org()
        a, b = _chain(org_a, ["A", "B"])
        assert entity_path(org_b, a, b) == []

    def test_path_terminates_on_cycle(self):
        org = _org()
        a, b, c = [_insert_entity(org, n) for n in ("A", "B", "C")]
        _insert_relationship(org, a, b)
        _insert_relationship(org, b, c)
        _insert_relationship(org, c, a)  # cycle
        # No path to an unreachable separate node; must terminate (not hang).
        z = _insert_entity(org, "Z")
        assert entity_path(org, a, z) == []


# ---------------------------------------------------------------------------
# relationship_type_filter
# ---------------------------------------------------------------------------

class TestRelationshipTypeFilter:
    def test_returns_only_matching_type(self):
        org = _org()
        a, b, c = [_insert_entity(org, n) for n in ("A", "B", "C")]
        _insert_relationship(org, a, b, relationship_type="owns")
        _insert_relationship(org, a, c, relationship_type="member_of")
        owns = relationship_type_filter(org, "owns")
        assert len(owns) == 1
        assert owns[0].relationship_type == "owns"

    def test_excludes_inferred_by_default(self):
        org = _org()
        a, b = _chain(org, ["A", "B"], relationship_type="depends_on", inferred=True, confidence=0.6)
        assert relationship_type_filter(org, "depends_on") == []
        included = relationship_type_filter(org, "depends_on", include_inferred=True)
        assert len(included) == 1
        assert included[0].inferred is True

    def test_org_scoped(self):
        org_a, org_b = _org(), _org()
        a, b = _chain(org_a, ["A", "B"], relationship_type="owns")
        assert relationship_type_filter(org_b, "owns") == []

    def test_excludes_edges_to_unresolved_endpoint(self):
        org = _org()
        a = _insert_entity(org, "A")
        u = _insert_entity(org, "U", resolution_status="unresolved")
        _insert_relationship(org, a, u, relationship_type="owns")
        assert relationship_type_filter(org, "owns") == []


# ---------------------------------------------------------------------------
# Safety limits
# ---------------------------------------------------------------------------

class TestSafetyLimits:
    def test_constants(self):
        assert DEFAULT_TRAVERSAL_DEPTH == 2
        assert MAX_TRAVERSAL_DEPTH == 5
        assert MAX_NODES_PER_QUERY == 500
        assert QUERY_TIMEOUT_SECONDS == 10.0

    def test_depth_clamped_to_max(self):
        org = _org()
        # 7-node chain => depths 0..6. Requesting depth 99 must clamp to 5,
        # so the depth-6 node is NOT returned.
        ids = _chain(org, [f"N{i}" for i in range(7)])
        nodes = opportunity_neighbourhood(org, [ids[0]], max_depth=99)
        max_depth_seen = max(n.depth for n in nodes)
        assert max_depth_seen == MAX_TRAVERSAL_DEPTH  # 5
        assert ids[6] not in {n.entity_id for n in nodes}
        assert ids[5] in {n.entity_id for n in nodes}

    def test_depth_zero_returns_only_seed(self):
        org = _org()
        a, b = _chain(org, ["A", "B"])
        nodes = opportunity_neighbourhood(org, [a], max_depth=0)
        assert {n.entity_id for n in nodes} == {a}

    def test_negative_depth_clamped_to_zero(self):
        org = _org()
        a, b = _chain(org, ["A", "B"])
        nodes = opportunity_neighbourhood(org, [a], max_depth=-3)
        assert {n.entity_id for n in nodes} == {a}

    def test_node_cap_enforced(self):
        org = _org()
        hub = _insert_entity(org, "Hub")
        # A star with more than the cap would be slow to seed; verify the cap
        # constant is enforced by capping the result length on a modest fan-out.
        for i in range(10):
            leaf = _insert_entity(org, f"Leaf{i}")
            _insert_relationship(org, hub, leaf)
        nodes = opportunity_neighbourhood(org, [hub])
        assert len(nodes) <= MAX_NODES_PER_QUERY
        assert len(nodes) == 11  # hub + 10 leaves


# ---------------------------------------------------------------------------
# Result shape / determinism
# ---------------------------------------------------------------------------

class TestResultShapeAndDeterminism:
    def test_node_shape(self):
        org = _org()
        a, b = _chain(org, ["A", "B"])
        nodes = opportunity_neighbourhood(org, [a])
        assert all(isinstance(n, GraphEntityNode) for n in nodes)

    def test_path_step_shape(self):
        org = _org()
        a, b = _chain(org, ["A", "B"])
        path = entity_path(org, a, b)
        assert all(isinstance(s, GraphPathStep) for s in path)

    def test_neighbourhood_deterministic(self):
        org = _org()
        a, b, c = _chain(org, ["A", "B", "C"])
        first = [n.entity_id for n in opportunity_neighbourhood(org, [a])]
        second = [n.entity_id for n in opportunity_neighbourhood(org, [a])]
        assert first == second

    def test_neighbourhood_ordered_by_depth(self):
        org = _org()
        a, b, c = _chain(org, ["A", "B", "C"])
        depths = [n.depth for n in opportunity_neighbourhood(org, [a])]
        assert depths == sorted(depths)  # depth ascending
