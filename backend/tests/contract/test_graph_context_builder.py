"""
ENT-4 / T3-S14-A T2+T3 | Graph Context Builder - Contract Tests
AgentIQ 2.0 | Track 3 - Platform Depth | Enterprise Depth Sprint

Covers the acceptance criteria owned by graph_context_builder.py:

  AC3  - rank_entities_for_context() with 30 entities returns exactly 15.
         Depth-0 entities always included first. Remaining ranked by type
         priority, run_count DESC, display_name alphabetical tie-break.
  AC4  - calling rank_entities_for_context() twice with identical input
         returns identical output. Ranking is deterministic (including for
         shuffled copies of the same graph).
  AC5  - when entity_count > 15: GraphContext.truncated=True and
         observed_summary ends with the locked truncation note stating how
         many additional entities exist.
  AC6  - rank_relationships_for_context() returns max 20. Observed edges
         (inferred=False) ranked before inferred edges.
  AC7  - build_graph_context() with entity_count < 3 returns a GraphContext
         with empty observed_summary and sparse_graph=True. Does not raise.
  AC10 - graph.context_built telemetry fired after every build_graph_context().
         Payload includes entity_count, entity_count_shown, truncated,
         duration_ms.

Run:
  cd backend
  pytest tests/contract/test_graph_context_builder.py -v
"""

from __future__ import annotations

import random
from dataclasses import fields
from typing import List, get_type_hints
from unittest.mock import patch

import pytest

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


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def make_entity(
    name: str,
    entity_type: str = "person",
    run_count: int = 1,
    confidence: float = 0.9,
    depth: int = 1,
    entity_id: str = "",
) -> EntityContext:
    return EntityContext(
        entity_id=entity_id or f"id-{name}",
        name=name,
        entity_type=entity_type,
        run_count=run_count,
        confidence=confidence,
        depth=depth,
        source_system="salesforce",
    )


def make_relationship(
    from_name: str,
    to_name: str,
    relationship_type: str = "owns",
    inferred: bool = False,
    confidence: float = 0.9,
) -> RelationshipContext:
    return RelationshipContext(
        from_entity_id=f"id-{from_name}",
        from_name=from_name,
        relationship_type=relationship_type,
        to_entity_id=f"id-{to_name}",
        to_name=to_name,
        inferred=inferred,
        confidence=confidence,
    )


def thirty_entities() -> List[EntityContext]:
    """30 mixed entities: 4 depth-0, 26 deeper across all types."""
    out = [make_entity(f"Seed {i:02d}", "object", run_count=1, depth=0) for i in range(4)]
    types = ["person", "team", "object", "process", "system"]
    for i in range(26):
        out.append(
            make_entity(
                f"Entity {i:02d}",
                types[i % len(types)],
                run_count=(i % 7) + 1,
                confidence=0.5 + (i % 5) * 0.1,
                depth=1 + (i % 3),
            )
        )
    return out


# -----------------------------------------------------------------------------
# Hard caps - Section 2a (not configurable per-run)
# -----------------------------------------------------------------------------

class TestHardCaps:
    def test_entity_cap_is_15(self):
        assert GRAPH_CONTEXT_MAX_ENTITIES == 15

    def test_relationship_cap_is_20(self):
        assert GRAPH_CONTEXT_MAX_RELATIONSHIPS == 20

    def test_sparse_threshold_is_3(self):
        assert SPARSE_GRAPH_THRESHOLD == 3


# -----------------------------------------------------------------------------
# AC3 - entity ranking: cap, depth-0 priority, type/run_count/alpha ordering
# -----------------------------------------------------------------------------

class TestEntityRankingAC3:
    def test_30_entities_returns_exactly_15(self):
        ranked = rank_entities_for_context(thirty_entities())
        assert len(ranked) == 15

    def test_depth_0_entities_always_included_first(self):
        entities = thirty_entities()
        ranked = rank_entities_for_context(entities)
        depth_0_names = {e.name for e in entities if e.depth == 0}
        assert {e.name for e in ranked[:4]} == depth_0_names
        assert all(e.depth == 0 for e in ranked[:4])
        assert all(e.depth > 0 for e in ranked[4:])

    def test_remaining_ranked_by_type_priority(self):
        entities = [
            make_entity("Sys", "system", run_count=99, depth=1),
            make_entity("Proc", "process", run_count=99, depth=1),
            make_entity("Obj", "object", run_count=99, depth=1),
            make_entity("Team", "team", run_count=99, depth=1),
            make_entity("Person", "person", run_count=1, depth=1),
        ]
        ranked = rank_entities_for_context(entities)
        assert [e.name for e in ranked] == ["Person", "Team", "Obj", "Proc", "Sys"], (
            "person > team > object > process > system regardless of run_count"
        )

    def test_within_type_run_count_desc(self):
        entities = [
            make_entity("Low", "person", run_count=2, depth=1),
            make_entity("High", "person", run_count=10, depth=1),
            make_entity("Mid", "person", run_count=5, depth=1),
        ]
        ranked = rank_entities_for_context(entities)
        assert [e.name for e in ranked] == ["High", "Mid", "Low"]

    def test_confidence_breaks_run_count_ties(self):
        entities = [
            make_entity("LowConf", "person", run_count=5, confidence=0.6, depth=1),
            make_entity("HighConf", "person", run_count=5, confidence=0.95, depth=1),
        ]
        ranked = rank_entities_for_context(entities)
        assert [e.name for e in ranked] == ["HighConf", "LowConf"]

    def test_alphabetical_tie_break(self):
        entities = [
            make_entity("Zara", "person", run_count=5, confidence=0.9, depth=1),
            make_entity("Anna", "person", run_count=5, confidence=0.9, depth=1),
            make_entity("Mike", "person", run_count=5, confidence=0.9, depth=1),
        ]
        ranked = rank_entities_for_context(entities)
        assert [e.name for e in ranked] == ["Anna", "Mike", "Zara"]

    def test_unknown_entity_type_ranks_last(self):
        entities = [
            make_entity("Proj", "project", run_count=99, depth=1),
            make_entity("Sys", "system", run_count=1, depth=1),
        ]
        ranked = rank_entities_for_context(entities)
        assert [e.name for e in ranked] == ["Sys", "Proj"]

    def test_more_than_15_depth_0_entities_capped_deterministically(self):
        entities = [make_entity(f"Seed {i:02d}", "person", depth=0) for i in range(18)]
        ranked = rank_entities_for_context(entities)
        assert len(ranked) == 15
        assert [e.name for e in ranked] == [f"Seed {i:02d}" for i in range(15)]

    def test_does_not_mutate_input(self):
        entities = thirty_entities()
        snapshot = list(entities)
        rank_entities_for_context(entities)
        assert entities == snapshot


# -----------------------------------------------------------------------------
# AC4 - entity ranking is deterministic
# -----------------------------------------------------------------------------

class TestEntityRankingDeterminismAC4:
    def test_identical_input_returns_identical_output(self):
        entities = thirty_entities()
        first = rank_entities_for_context(list(entities))
        second = rank_entities_for_context(list(entities))
        assert first == second

    def test_shuffled_input_returns_identical_output(self):
        """Same graph, any list order -> same ranked output (stable tie-breaks)."""
        entities = thirty_entities()
        baseline = rank_entities_for_context(entities)
        rng = random.Random(42)
        for _ in range(5):
            shuffled = list(entities)
            rng.shuffle(shuffled)
            assert rank_entities_for_context(shuffled) == baseline

    def test_duplicate_names_still_deterministic(self):
        a = make_entity("Same Name", "person", depth=1, entity_id="id-a")
        b = make_entity("Same Name", "person", depth=1, entity_id="id-b")
        assert rank_entities_for_context([a, b]) == rank_entities_for_context([b, a])

    def test_relationship_ranking_deterministic_under_shuffle(self):
        rels = [
            make_relationship(f"From {i:02d}", f"To {i:02d}", confidence=0.5 + (i % 4) * 0.1, inferred=bool(i % 2))
            for i in range(25)
        ]
        baseline = rank_relationships_for_context(rels)
        rng = random.Random(7)
        shuffled = list(rels)
        rng.shuffle(shuffled)
        assert rank_relationships_for_context(shuffled) == baseline


# -----------------------------------------------------------------------------
# AC5 - truncation flag + locked truncation note in observed_summary
# -----------------------------------------------------------------------------

class TestTruncationAC5:
    def _build(self, entity_total: int) -> GraphContext:
        entities = [
            make_entity(f"Entity {i:02d}", "person", run_count=i + 1, depth=0 if i < 2 else 1)
            for i in range(entity_total)
        ]
        rels = [make_relationship("Entity 00", "Entity 01")]
        with patch("app.telemetry.record_event"):
            return build_graph_context("opp-001", entities, rels, org_id="default")

    def test_truncated_true_when_entity_count_exceeds_15(self):
        ctx = self._build(38)
        assert ctx.truncated is True
        assert ctx.entity_count == 38
        assert ctx.entity_count_shown == 15

    def test_observed_summary_ends_with_locked_truncation_note(self):
        ctx = self._build(38)
        assert ctx.observed_summary.endswith(
            "and 23 additional entities were identified but are not shown here. "
            "The most significant entities by frequency and confidence are listed above."
        )

    def test_truncation_note_states_additional_entity_count(self):
        ctx = self._build(20)
        assert "and 5 additional entities" in ctx.observed_summary

    def test_no_truncation_note_when_under_cap(self):
        ctx = self._build(10)
        assert ctx.truncated is False
        assert ctx.entity_count_shown == 10
        assert "additional entities" not in ctx.observed_summary

    def test_truncated_true_when_relationships_exceed_cap(self):
        entities = [make_entity(f"Entity {i}", depth=0) for i in range(5)]
        rels = [make_relationship(f"From {i:02d}", f"To {i:02d}") for i in range(25)]
        with patch("app.telemetry.record_event"):
            ctx = build_graph_context("opp-002", entities, rels)
        assert ctx.truncated is True
        assert ctx.relationship_count == 25
        assert ctx.relationship_count_shown == 20


# -----------------------------------------------------------------------------
# AC6 - relationship ranking: cap, observed before inferred
# -----------------------------------------------------------------------------

class TestRelationshipRankingAC6:
    def test_returns_max_20(self):
        rels = [make_relationship(f"From {i:02d}", f"To {i:02d}") for i in range(30)]
        assert len(rank_relationships_for_context(rels)) == 20

    def test_observed_ranked_before_inferred(self):
        rels = [
            make_relationship("A", "B", inferred=True, confidence=0.99),
            make_relationship("C", "D", inferred=False, confidence=0.5),
            make_relationship("E", "F", inferred=True, confidence=0.8),
            make_relationship("G", "H", inferred=False, confidence=0.9),
        ]
        ranked = rank_relationships_for_context(rels)
        assert [r.inferred for r in ranked] == [False, False, True, True], (
            "every observed edge must rank before every inferred edge, "
            "even when the inferred edge has higher confidence"
        )

    def test_confidence_desc_within_observed(self):
        rels = [
            make_relationship("A", "B", confidence=0.6),
            make_relationship("C", "D", confidence=0.9),
            make_relationship("E", "F", confidence=0.75),
        ]
        ranked = rank_relationships_for_context(rels)
        assert [r.confidence for r in ranked] == [0.9, 0.75, 0.6]

    def test_alphabetical_tie_break_on_from_name(self):
        rels = [
            make_relationship("Zeta", "X", confidence=0.9),
            make_relationship("Alpha", "X", confidence=0.9),
        ]
        ranked = rank_relationships_for_context(rels)
        assert [r.from_name for r in ranked] == ["Alpha", "Zeta"]

    def test_high_confidence_inferred_cannot_displace_observed_within_cap(self):
        observed = [make_relationship(f"Obs {i:02d}", "X", confidence=0.5) for i in range(20)]
        inferred = [make_relationship(f"Inf {i:02d}", "X", inferred=True, confidence=0.99) for i in range(5)]
        ranked = rank_relationships_for_context(inferred + observed)
        assert len(ranked) == 20
        assert all(not r.inferred for r in ranked), (
            "20 observed edges fill the cap - no inferred edge may displace one"
        )

    def test_does_not_mutate_input(self):
        rels = [make_relationship(f"From {i}", "X", confidence=0.1 * i) for i in range(5)]
        snapshot = list(rels)
        rank_relationships_for_context(rels)
        assert rels == snapshot


# -----------------------------------------------------------------------------
# AC7 - sparse graph handling (never raises)
# -----------------------------------------------------------------------------

class TestSparseGraphAC7:
    @pytest.mark.parametrize("entity_total", [0, 1, 2])
    def test_sparse_graph_flag_and_empty_summary(self, entity_total):
        entities = [make_entity(f"Entity {i}", depth=0) for i in range(entity_total)]
        with patch("app.telemetry.record_event"):
            ctx = build_graph_context("opp-sparse", entities, [])
        assert ctx.sparse_graph is True
        assert ctx.observed_summary == ""
        assert ctx.inferred_summary is None
        assert ctx.entity_count == entity_total

    def test_three_entities_is_not_sparse(self):
        entities = [make_entity(f"Entity {i}", depth=0) for i in range(3)]
        with patch("app.telemetry.record_event"):
            ctx = build_graph_context("opp-003", entities, [])
        assert ctx.sparse_graph is False
        assert ctx.observed_summary != ""

    def test_does_not_raise_on_empty_input(self):
        with patch("app.telemetry.record_event"):
            ctx = build_graph_context("opp-empty", [], [])
        assert isinstance(ctx, GraphContext)
        assert ctx.opportunity_id == "opp-empty"

    def test_does_not_raise_on_malformed_rows(self):
        with patch("app.telemetry.record_event"):
            ctx = build_graph_context(
                "opp-bad",
                [None, 42, {"display_name": "Ok Entity", "entity_type": "person", "depth": 0}],
                [None, "junk", {"from_name": "Ok Entity", "to_name": "Other", "relationship_type": "owns"}],
            )
        assert isinstance(ctx, GraphContext)
        assert ctx.entity_count == 1
        assert ctx.relationship_count == 1


# -----------------------------------------------------------------------------
# build_graph_context - shape, raw-row input, summaries
# -----------------------------------------------------------------------------

class TestBuildGraphContextShape:
    def test_graphcontext_has_all_contract_fields(self):
        names = {f.name for f in fields(GraphContext)}
        assert names >= {
            "opportunity_id",
            "entities",
            "relationships",
            "observed_summary",
            "inferred_summary",
            "entity_count",
            "entity_count_shown",
            "relationship_count",
            "relationship_count_shown",
            "truncated",
            "max_depth_reached",
            "sparse_graph",
        }

    def test_accepts_raw_query_rows(self):
        """Raw opportunity_neighbourhood()-style rows are coerced cleanly."""
        rows = [
            {
                "entity_id": f"e-{i}",
                "entity_type": "person",
                "display_name": f"Person {i}",
                "resolution_confidence": 0.9,
                "run_count": i + 1,
                "depth": 0 if i == 0 else 1,
            }
            for i in range(4)
        ]
        edges = [
            {
                "from_entity_id": "e-0",
                "from_entity_name": "Person 0",
                "relationship_type": "member_of",
                "to_entity_id": "e-1",
                "to_entity_name": "Person 1",
                "inferred": 0,
                "confidence": 0.9,
            }
        ]
        with patch("app.telemetry.record_event"):
            ctx = build_graph_context("opp-raw", rows, edges, org_id="default")
        assert ctx.entity_count == 4
        assert ctx.relationship_count == 1
        assert ctx.entities[0].name == "Person 0"  # depth-0 first
        assert "Person 0 member of Person 1" in ctx.observed_summary

    def test_max_depth_reached_derived_from_entities(self):
        entities = [
            make_entity("A", depth=0),
            make_entity("B", depth=1),
            make_entity("C", depth=3),
        ]
        with patch("app.telemetry.record_event"):
            ctx = build_graph_context("opp-depth", entities, [])
        assert ctx.max_depth_reached == 3

    def test_max_depth_reached_explicit_override(self):
        entities = [make_entity(f"E{i}", depth=0) for i in range(3)]
        with patch("app.telemetry.record_event"):
            ctx = build_graph_context("opp-depth2", entities, [], max_depth_reached=2)
        assert ctx.max_depth_reached == 2

    def test_inferred_summary_none_when_no_inferred_edges(self):
        entities = [make_entity(f"E{i}", depth=0) for i in range(3)]
        rels = [make_relationship("E0", "E1")]
        with patch("app.telemetry.record_event"):
            ctx = build_graph_context("opp-obs", entities, rels)
        assert ctx.inferred_summary is None

    def test_inferred_summary_populated_and_separate_from_observed(self):
        entities = [make_entity(f"E{i}", depth=0) for i in range(3)]
        rels = [
            make_relationship("E0", "E1", relationship_type="owns"),
            make_relationship("E1", "E2", relationship_type="escalates_to", inferred=True, confidence=0.6),
        ]
        with patch("app.telemetry.record_event"):
            ctx = build_graph_context("opp-inf", entities, rels)
        assert ctx.inferred_summary is not None
        assert "E1 escalates to E2" in ctx.inferred_summary
        assert "escalates to" not in ctx.observed_summary
        assert "E0 owns E1" in ctx.observed_summary

    def test_build_is_deterministic_end_to_end(self):
        entities = thirty_entities()
        rels = [
            make_relationship(f"From {i:02d}", f"To {i:02d}", confidence=0.5 + (i % 4) * 0.1)
            for i in range(25)
        ]
        rng = random.Random(99)
        with patch("app.telemetry.record_event"):
            first = build_graph_context("opp-det", entities, rels)
            shuffled_e, shuffled_r = list(entities), list(rels)
            rng.shuffle(shuffled_e)
            rng.shuffle(shuffled_r)
            second = build_graph_context("opp-det", shuffled_e, shuffled_r)
        assert first.observed_summary == second.observed_summary
        assert first.entities == second.entities
        assert first.relationships == second.relationships


# -----------------------------------------------------------------------------
# AC10 - graph.context_built telemetry
# -----------------------------------------------------------------------------

class TestTelemetryAC10:
    def test_event_type_registered(self):
        from app.telemetry import REGISTERED_EVENT_TYPES

        assert "graph.context_built" in REGISTERED_EVENT_TYPES

    def test_registered_with_graph_context_built_payload(self):
        from app.telemetry import EVENT_REGISTRY, GraphContextBuiltPayload

        assert EVENT_REGISTRY["graph.context_built"] is GraphContextBuiltPayload

    @pytest.mark.parametrize(
        "field_name", ["entity_count", "entity_count_shown", "truncated", "duration_ms"]
    )
    def test_payload_schema_has_required_fields(self, field_name):
        from app.telemetry import GraphContextBuiltPayload

        assert field_name in get_type_hints(GraphContextBuiltPayload)

    def test_event_fired_after_every_build(self):
        entities = [make_entity(f"E{i}", depth=0) for i in range(20)]
        with patch("app.telemetry.record_event") as mock_record:
            build_graph_context("opp-tel", entities, [], org_id="default")

        mock_record.assert_called_once()
        event_type, payload = mock_record.call_args[0]
        assert event_type == "graph.context_built"
        assert payload["entity_count"] == 20
        assert payload["entity_count_shown"] == 15
        assert payload["truncated"] is True
        assert isinstance(payload["duration_ms"], int)
        assert payload["duration_ms"] >= 0

    def test_event_fired_on_sparse_path(self):
        with patch("app.telemetry.record_event") as mock_record:
            build_graph_context("opp-tel-sparse", [], [], org_id="default")

        mock_record.assert_called_once()
        event_type, payload = mock_record.call_args[0]
        assert event_type == "graph.context_built"
        assert payload["sparse_graph"] is True
        assert payload["entity_count"] == 0

    def test_build_survives_telemetry_failure(self):
        entities = [make_entity(f"E{i}", depth=0) for i in range(3)]
        with patch("app.telemetry.record_event", side_effect=Exception("telemetry down")):
            ctx = build_graph_context("opp-tel-fail", entities, [])
        assert isinstance(ctx, GraphContext)
        assert ctx.entity_count == 3

    def test_record_event_accepts_real_payload(self):
        """The registered event type accepts a real emission without raising."""
        from app.telemetry import record_event

        record_event(
            "graph.context_built",
            {
                "opportunity_id": "opp-real",
                "org_id": "default",
                "source": "graph_context_builder",
                "entity_count": 5,
                "entity_count_shown": 5,
                "relationship_count": 2,
                "relationship_count_shown": 2,
                "truncated": False,
                "sparse_graph": False,
                "duration_ms": 1,
            },
        )


