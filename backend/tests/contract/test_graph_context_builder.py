"""Contract tests for ENT-4 / T3-S14-A — graph context builder.

Covers app/graph_context_builder.py:
  rank_entities_for_context(), rank_relationships_for_context(),
  build_graph_context(), and the GraphContext shape.

Acceptance criteria belonging to this task:
  AC3  — rank_entities_for_context() with 30 entities returns exactly 15;
         depth-0 entities first; remaining ranked by type priority, run_count
         DESC, display_name alphabetical (deterministic tie-break).
  AC4  — rank_entities_for_context() is deterministic (same input -> same output).
  AC5  — entity_count > 15 -> GraphContext.truncated=True and observed_summary
         ends with a truncation note stating how many additional entities exist.
  AC6  — rank_relationships_for_context() returns max 20; observed edges
         (inferred=False) ranked before inferred edges.
  AC7  — build_graph_context() with entity_count < 3 returns a GraphContext with
         empty observed_summary and sparse_graph=True; does not raise.
  AC10 — graph.context_built telemetry fired after every build_graph_context()
         with entity_count, entity_count_shown, truncated, duration_ms.
"""
import os
import sqlite3
import uuid

import pytest

from app import graph_context_builder as gcb
from app.graph_context_builder import (
    GRAPH_CONTEXT_MAX_ENTITIES,
    GRAPH_CONTEXT_MAX_RELATIONSHIPS,
    SPARSE_GRAPH_THRESHOLD,
    EntityContext,
    GraphContext,
    RelationshipContext,
    build_graph_context,
    rank_entities_for_context,
    rank_relationships_for_context,
)
from database.models.entities import Entity
from database.models.entity_relationships import (
    INFERRED_CONFIDENCE,
    OBSERVED_CONFIDENCE,
    EntityRelationship,
)


# ---------------------------------------------------------------------------
# Pure-ranking fixtures (no DB)
# ---------------------------------------------------------------------------

def _ent(name, *, depth=1, entity_type="person", run_count=1, confidence=1.0, eid=None):
    return EntityContext(
        entity_id=eid or ("e-" + uuid.uuid4().hex[:8]),
        name=name,
        entity_type=entity_type,
        depth=depth,
        run_count=run_count,
        confidence=confidence,
    )


def _rel(from_name, to_name, *, relationship_type="owns", inferred=False, confidence=None):
    if confidence is None:
        confidence = INFERRED_CONFIDENCE if inferred else OBSERVED_CONFIDENCE
    return RelationshipContext(
        from_name=from_name,
        to_name=to_name,
        relationship_type=relationship_type,
        inferred=inferred,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# AC3 — rank_entities_for_context: 30 -> 15, depth-0 first, deterministic order
# ---------------------------------------------------------------------------

class TestAC3RankEntities:
    def test_thirty_entities_returns_exactly_fifteen(self):
        entities = [_ent(f"E{i:02d}", depth=1) for i in range(30)]
        ranked = rank_entities_for_context(entities)
        assert len(ranked) == GRAPH_CONTEXT_MAX_ENTITIES == 15

    def test_depth_zero_entities_first(self):
        depth0 = [_ent(f"D0-{i}", depth=0) for i in range(3)]
        depth1 = [_ent(f"D1-{i}", depth=1) for i in range(27)]
        ranked = rank_entities_for_context(depth0 + depth1)
        # First three are the depth-0 entities.
        assert all(e.depth == 0 for e in ranked[:3])
        assert {e.name for e in ranked[:3]} == {"D0-0", "D0-1", "D0-2"}
        # The rest are depth>0.
        assert all(e.depth > 0 for e in ranked[3:])

    def test_remaining_ranked_by_type_then_run_count_then_name(self):
        # All depth-1 so type priority drives order. person < team < object.
        entities = [
            _ent("Zoe", entity_type="person", run_count=5),
            _ent("Amy", entity_type="person", run_count=5),   # tie on run_count -> name
            _ent("Bob", entity_type="person", run_count=9),   # higher run_count first
            _ent("TeamX", entity_type="team", run_count=99),
            _ent("ObjY", entity_type="object", run_count=99),
        ]
        ranked = rank_entities_for_context(entities)
        names = [e.name for e in ranked]
        # person entities first (Bob run9, then Amy/Zoe run5 alphabetical), then team, then object
        assert names == ["Bob", "Amy", "Zoe", "TeamX", "ObjY"]

    def test_depth_zero_included_even_if_low_priority_type(self):
        # A depth-0 system entity must still come before a depth-1 person.
        d0 = _ent("SeedSystem", depth=0, entity_type="system", run_count=1)
        d1 = _ent("PersonA", depth=1, entity_type="person", run_count=99)
        ranked = rank_entities_for_context([d1, d0])
        assert ranked[0].name == "SeedSystem"


# ---------------------------------------------------------------------------
# AC4 — determinism
# ---------------------------------------------------------------------------

class TestAC4Determinism:
    def test_identical_input_identical_output(self):
        entities = [
            _ent(f"E{i}", depth=i % 2, entity_type=["person", "team", "object"][i % 3],
                 run_count=(i * 7) % 11, confidence=(i % 5) / 5.0, eid=f"id-{i}")
            for i in range(30)
        ]
        first = rank_entities_for_context(list(entities))
        second = rank_entities_for_context(list(entities))
        assert [e.entity_id for e in first] == [e.entity_id for e in second]

    def test_input_order_does_not_change_output(self):
        entities = [
            _ent(f"E{i}", depth=i % 2, entity_type=["person", "team", "object"][i % 3],
                 run_count=(i * 7) % 11, confidence=(i % 5) / 5.0, eid=f"id-{i}")
            for i in range(30)
        ]
        forward = rank_entities_for_context(list(entities))
        backward = rank_entities_for_context(list(reversed(entities)))
        assert [e.entity_id for e in forward] == [e.entity_id for e in backward]


# ---------------------------------------------------------------------------
# AC6 — rank_relationships_for_context: max 20, observed before inferred
# ---------------------------------------------------------------------------

class TestAC6RankRelationships:
    def test_caps_at_twenty(self):
        rels = [_rel(f"A{i}", f"B{i}") for i in range(30)]
        ranked = rank_relationships_for_context(rels)
        assert len(ranked) == GRAPH_CONTEXT_MAX_RELATIONSHIPS == 20

    def test_observed_before_inferred(self):
        rels = [
            _rel("I1", "X", inferred=True),
            _rel("O1", "Y", inferred=False),
            _rel("I2", "Z", inferred=True),
            _rel("O2", "W", inferred=False),
        ]
        ranked = rank_relationships_for_context(rels)
        inferred_flags = [r.inferred for r in ranked]
        # All observed (False) come before any inferred (True).
        assert inferred_flags == sorted(inferred_flags)  # False(0) before True(1)
        assert inferred_flags[0] is False and inferred_flags[-1] is True

    def test_higher_confidence_first(self):
        rels = [
            _rel("Low", "X", inferred=False, confidence=0.5),
            _rel("High", "Y", inferred=False, confidence=0.95),
        ]
        ranked = rank_relationships_for_context(rels)
        assert ranked[0].from_name == "High"

    def test_relationship_ranking_deterministic(self):
        rels = [_rel(f"F{i}", f"T{i}", inferred=(i % 2 == 0)) for i in range(25)]
        first = rank_relationships_for_context(list(rels))
        second = rank_relationships_for_context(list(reversed(rels)))
        assert [(r.from_name, r.to_name) for r in first] == [(r.from_name, r.to_name) for r in second]


# ---------------------------------------------------------------------------
# DB seeding helpers for build_graph_context()
# ---------------------------------------------------------------------------

def _insert_entity(org_id, display_name, *, entity_type="person",
                   resolution_status="resolved", resolution_confidence=1.0,
                   run_count=1, run_id="run_gcb"):
    entity = Entity(
        org_id=org_id, entity_type=entity_type,
        canonical_name=" ".join(display_name.split()).lower() + "-" + uuid.uuid4().hex[:8],
        display_name=display_name, source_system="test",
        resolution_confidence=resolution_confidence, resolution_status=resolution_status,
        first_seen_run_id=run_id, last_seen_run_id=run_id, run_count=run_count,
    )
    row = entity.to_db_row()
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """INSERT INTO entities (
                id, org_id, entity_type, canonical_name, display_name, source_system,
                source_record_id, resolution_confidence, resolution_status,
                first_seen_run_id, last_seen_run_id, run_count, metadata, created_at, updated_at
            ) VALUES (
                :id, :org_id, :entity_type, :canonical_name, :display_name, :source_system,
                :source_record_id, :resolution_confidence, :resolution_status,
                :first_seen_run_id, :last_seen_run_id, :run_count, :metadata, :created_at, :updated_at
            )""",
            row,
        )
        conn.commit()
    return row["id"]


def _insert_relationship(org_id, from_id, to_id, *, relationship_type="owns",
                         inferred=False, confidence=None, run_id="run_gcb"):
    if confidence is None:
        confidence = INFERRED_CONFIDENCE if inferred else OBSERVED_CONFIDENCE
    rel = EntityRelationship(
        org_id=org_id, from_entity_id=from_id, to_entity_id=to_id,
        relationship_type=relationship_type, confidence=confidence, inferred=inferred,
        first_seen_run_id=run_id, last_seen_run_id=run_id, run_count=1,
    )
    row = rel.to_db_row()
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """INSERT INTO entity_relationships (
                id, org_id, from_entity_id, to_entity_id, relationship_type,
                confidence, inferred, evidence, first_seen_run_id, last_seen_run_id,
                run_count, created_at
            ) VALUES (
                :id, :org_id, :from_entity_id, :to_entity_id, :relationship_type,
                :confidence, :inferred, :evidence, :first_seen_run_id, :last_seen_run_id,
                :run_count, :created_at
            )""",
            row,
        )
        conn.commit()
    return row["id"]


def _org():
    return "org_" + uuid.uuid4().hex[:10]


# ---------------------------------------------------------------------------
# build_graph_context — end to end
# ---------------------------------------------------------------------------

class TestBuildGraphContextEndToEnd:
    def test_chain_builds_context(self):
        org = _org()
        a = _insert_entity(org, "Alice", run_count=5)
        b = _insert_entity(org, "Bob", entity_type="object", run_count=2)
        c = _insert_entity(org, "Carol", entity_type="object", run_count=1)
        _insert_relationship(org, a, b)
        _insert_relationship(org, b, c)

        ctx = build_graph_context(org, "opp_1", [a], max_depth=2)
        assert isinstance(ctx, GraphContext)
        assert ctx.opportunity_id == "opp_1"
        assert ctx.entity_count == 3
        assert ctx.entity_count_shown == 3
        assert ctx.sparse_graph is False
        assert ctx.truncated is False
        assert ctx.max_depth_reached == 2
        # Seed (depth 0) ranked first.
        assert ctx.entities[0].entity_id == a
        # Two observed relationships derived from the traversal.
        assert ctx.relationship_count == 2
        assert {(r.from_name, r.to_name) for r in ctx.relationships} == {("Alice", "Bob"), ("Bob", "Carol")}

    def test_observed_summary_locked_format(self):
        org = _org()
        a = _insert_entity(org, "Alice", run_count=5)
        b = _insert_entity(org, "Bob", entity_type="object", run_count=2)
        c = _insert_entity(org, "Carol", entity_type="object", run_count=1)
        _insert_relationship(org, a, b, relationship_type="owns")
        _insert_relationship(org, b, c, relationship_type="owns")
        ctx = build_graph_context(org, "opp_2", [a], max_depth=2)
        assert ctx.sparse_graph is False  # 3 entities -> not sparse
        s = ctx.observed_summary
        assert "This opportunity is connected to" in s
        assert "Entities:" in s
        assert "- Alice (person, seen in 5 runs)" in s
        assert "Observed relationships:" in s
        assert "- Alice owns Bob" in s

    def test_relationship_confidence_from_category(self):
        org = _org()
        a = _insert_entity(org, "A")
        b = _insert_entity(org, "B")
        _insert_relationship(org, a, b, inferred=False)
        ctx = build_graph_context(org, "opp_c", [a], max_depth=1)
        assert ctx.relationships[0].confidence == OBSERVED_CONFIDENCE

    def test_inferred_summary_separate_when_included(self):
        org = _org()
        a = _insert_entity(org, "A")
        b = _insert_entity(org, "B")
        c = _insert_entity(org, "C")
        _insert_relationship(org, a, b, relationship_type="owns", inferred=False)
        _insert_relationship(org, a, c, relationship_type="depends_on", inferred=True)
        ctx = build_graph_context(org, "opp_i", [a], max_depth=1, include_inferred=True)
        # Observed edge in observed_summary; inferred edge in inferred_summary.
        assert "Alice" not in ctx.observed_summary  # sanity (names are A/B/C)
        assert "- A owns B" in ctx.observed_summary
        assert ctx.inferred_summary is not None
        assert "depends on" in ctx.inferred_summary

    def test_org_scoped(self):
        org_a, org_b = _org(), _org()
        a = _insert_entity(org_a, "A")
        b = _insert_entity(org_a, "B")
        _insert_relationship(org_a, a, b)
        ctx = build_graph_context(org_b, "opp_x", [a], max_depth=2)
        assert ctx.entity_count == 0
        assert ctx.sparse_graph is True


# ---------------------------------------------------------------------------
# AC5 — truncation
# ---------------------------------------------------------------------------

class TestAC5Truncation:
    def test_truncated_and_note_present(self):
        org = _org()
        hub = _insert_entity(org, "Hub", run_count=100)
        # 17 leaves at depth 1 => entity_count = 18 (> 15).
        for i in range(17):
            leaf = _insert_entity(org, f"Leaf{i:02d}", entity_type="object", run_count=i)
            _insert_relationship(org, hub, leaf)
        ctx = build_graph_context(org, "opp_trunc", [hub], max_depth=1)
        assert ctx.entity_count == 18
        assert ctx.entity_count_shown == GRAPH_CONTEXT_MAX_ENTITIES == 15
        assert ctx.truncated is True
        # Observed summary ends with a truncation note stating the extra count (3).
        assert "additional entities were identified" in ctx.observed_summary
        assert "and 3 additional entities" in ctx.observed_summary
        assert ctx.observed_summary.rstrip().endswith(
            "The most significant entities by frequency and confidence are listed above."
        )


# ---------------------------------------------------------------------------
# AC7 — sparse graph handling
# ---------------------------------------------------------------------------

class TestAC7SparseGraph:
    def test_single_entity_is_sparse(self):
        org = _org()
        a = _insert_entity(org, "Solo")
        ctx = build_graph_context(org, "opp_sparse", [a], max_depth=2)
        assert ctx.entity_count == 1
        assert ctx.sparse_graph is True
        assert ctx.observed_summary == ""

    def test_two_entities_is_sparse(self):
        org = _org()
        a = _insert_entity(org, "A")
        b = _insert_entity(org, "B")
        _insert_relationship(org, a, b)
        ctx = build_graph_context(org, "opp_sparse2", [a], max_depth=1)
        assert ctx.entity_count == 2 < SPARSE_GRAPH_THRESHOLD
        assert ctx.sparse_graph is True
        assert ctx.observed_summary == ""

    def test_three_entities_not_sparse(self):
        org = _org()
        a = _insert_entity(org, "A")
        b = _insert_entity(org, "B")
        c = _insert_entity(org, "C")
        _insert_relationship(org, a, b)
        _insert_relationship(org, b, c)
        ctx = build_graph_context(org, "opp_ok", [a], max_depth=2)
        assert ctx.entity_count == 3
        assert ctx.sparse_graph is False
        assert ctx.observed_summary != ""

    def test_no_seeds_does_not_raise(self):
        org = _org()
        ctx = build_graph_context(org, "opp_empty", [], max_depth=2)
        assert ctx.entity_count == 0
        assert ctx.sparse_graph is True
        assert ctx.observed_summary == ""

    def test_unresolved_seed_is_sparse_no_raise(self):
        org = _org()
        u = _insert_entity(org, "U", resolution_status="unresolved")
        ctx = build_graph_context(org, "opp_unres", [u], max_depth=2)
        assert ctx.entity_count == 0
        assert ctx.sparse_graph is True


# ---------------------------------------------------------------------------
# AC10 — telemetry
# ---------------------------------------------------------------------------

class TestAC10Telemetry:
    def test_event_fired_with_required_payload(self, monkeypatch):
        captured = []
        monkeypatch.setattr(
            gcb, "record_event", lambda event_type, payload=None: captured.append((event_type, payload))
        )
        org = _org()
        a = _insert_entity(org, "A")
        b = _insert_entity(org, "B")
        c = _insert_entity(org, "C")
        _insert_relationship(org, a, b)
        _insert_relationship(org, b, c)
        build_graph_context(org, "opp_tel", [a], max_depth=2)

        events = [e for e in captured if e[0] == "graph.context_built"]
        assert len(events) == 1
        payload = events[0][1]
        for key in ("entity_count", "entity_count_shown", "truncated", "duration_ms"):
            assert key in payload
        assert payload["entity_count"] == 3
        assert payload["opportunity_id"] == "opp_tel"

    def test_event_fired_even_when_sparse(self, monkeypatch):
        captured = []
        monkeypatch.setattr(
            gcb, "record_event", lambda event_type, payload=None: captured.append((event_type, payload))
        )
        org = _org()
        a = _insert_entity(org, "Solo")
        build_graph_context(org, "opp_sparse_tel", [a], max_depth=2)
        events = [e for e in captured if e[0] == "graph.context_built"]
        assert len(events) == 1
        assert events[0][1]["sparse_graph"] is True

    def test_telemetry_failure_does_not_break_build(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("telemetry down")
        monkeypatch.setattr(gcb, "record_event", boom)
        org = _org()
        a = _insert_entity(org, "A")
        b = _insert_entity(org, "B")
        c = _insert_entity(org, "C")
        _insert_relationship(org, a, b)
        _insert_relationship(org, b, c)
        ctx = build_graph_context(org, "opp_boom", [a], max_depth=2)  # must not raise
        assert ctx.entity_count == 3


# ---------------------------------------------------------------------------
# graph.context_built is registered (so record_event does not raise)
# ---------------------------------------------------------------------------

class TestTelemetryRegistration:
    def test_event_type_registered(self):
        from app.telemetry import REGISTERED_EVENT_TYPES
        assert "graph.context_built" in REGISTERED_EVENT_TYPES
