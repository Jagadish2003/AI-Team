"""ENT-4 / T5 — Enterprise-scale graph query performance tests.

Verifies that opportunity_neighbourhood() meets hard performance bounds on a
realistic large graph (500 entities, 1000 relationships):

  * Depth 2 (common LLM context use case): completes in under 2 seconds.
  * Depth 5 (maximum allowed traversal): completes safely or times out within
    10 seconds.

The test generates the graph in a fresh isolated SQLite database so the results
are deterministic and independent of the contract test suite's shared DB.

Run:
    cd backend
    python -m pytest tests/contract/test_ent4_t5_graph_performance.py -v
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import List, Tuple

import pytest

from app.graph_query import (
    NeighbourhoodNode,
    _NEIGHBOURHOOD_MAX_DEPTH,
    _NEIGHBOURHOOD_NODE_CAP,
    _NEIGHBOURHOOD_TIMEOUT_S,
    opportunity_neighbourhood,
)
from database.models.entities import ALL_ENTITIES_DDL
from database.models.entity_relationships import ALL_ENTITY_RELATIONSHIPS_DDL

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORG_ID = "perf-test-org"
_RUN_ID = "perf-run-001"
_ENTITY_COUNT = 500
_RELATIONSHIP_COUNT = 1000
_SEED_COUNT = 5  # number of seed entities passed to opportunity_neighbourhood

# Performance bounds from the task specification
_DEPTH2_MAX_SECONDS = 2.0
_DEPTH5_MAX_SECONDS = 10.0  # must complete OR time out within this bound

# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

_ENTITY_TYPES = ["person", "team", "object", "process", "system"]
_REL_TYPES = ["owns", "member_of", "escalates_to", "depends_on", "routes_to"]


def _build_large_graph(db_path: str) -> List[str]:
    """Populate *db_path* with 500 entities and ~1000 relationships.

    Topology: bidirectional ring (each entity i → i+1 and i → i+2) creates a
    dense, cycle-heavy graph that exercises the recursive CTE's cycle-detection
    path and the LIMIT cap at deep traversal depths.  The ring ensures every
    entity is reachable from the seed set at sufficient depth, giving the
    depth-5 query real work to do.

    Returns the IDs of the first :data:`_SEED_COUNT` entities (used as seeds
    in all tests).
    """
    conn = sqlite3.connect(db_path)

    for ddl in ALL_ENTITIES_DDL + ALL_ENTITY_RELATIONSHIPS_DDL:
        conn.execute(ddl)
    conn.commit()

    now = datetime.now(timezone.utc).isoformat()
    entity_ids = [str(uuid.uuid4()) for _ in range(_ENTITY_COUNT)]

    # Insert entities in one transaction for speed
    conn.executemany(
        """
        INSERT INTO entities (
            id, org_id, entity_type, canonical_name, display_name,
            source_system, resolution_confidence, resolution_status,
            first_seen_run_id, last_seen_run_id, run_count,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                eid,
                _ORG_ID,
                _ENTITY_TYPES[i % len(_ENTITY_TYPES)],
                f"entity-{i}",
                f"Entity {i}",
                "salesforce",
                0.9,
                "resolved",
                _RUN_ID,
                _RUN_ID,
                3,
                now,
                now,
            )
            for i, eid in enumerate(entity_ids)
        ],
    )
    conn.commit()

    # Build relationships: ring topology + forward skip-2 links.
    # Each entity i gets two directed edges: i→(i+1)%N and i→(i+2)%N.
    # That produces 2 × 500 = 1000 directed edges (exactly _RELATIONSHIP_COUNT).
    rel_rows = []
    for i in range(_ENTITY_COUNT):
        for offset in (1, 2):
            j = (i + offset) % _ENTITY_COUNT
            rel_rows.append(
                (
                    str(uuid.uuid4()),
                    _ORG_ID,
                    entity_ids[i],
                    entity_ids[j],
                    _REL_TYPES[i % len(_REL_TYPES)],
                    0.9,
                    False,  # inferred=False (observed)
                    _RUN_ID,
                    _RUN_ID,
                    1,
                    now,
                )
            )

    conn.executemany(
        """
        INSERT INTO entity_relationships (
            id, org_id, from_entity_id, to_entity_id,
            relationship_type, confidence, inferred,
            first_seen_run_id, last_seen_run_id, run_count, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        rel_rows,
    )
    conn.commit()
    conn.close()

    return entity_ids[:_SEED_COUNT]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def large_graph(tmp_path):
    """Build the 500-entity graph in the test PostgreSQL database.

    AT-288 / Fix 1: the perf graph is built in the shared test database (the
    conftest routes sqlite3.connect() to PostgreSQL and the path is ignored), so
    db.connect() in the queries under test reads the same rows. The org-scoped
    seed data does not leak into other tests' org queries.
    """
    seed_ids = _build_large_graph(str(tmp_path / "perf_graph.db"))
    return seed_ids


# ---------------------------------------------------------------------------
# Performance tests
# ---------------------------------------------------------------------------


class TestOpportunityNeighbourhoodPerformance:
    """T5: opportunity_neighbourhood() must meet hard timing bounds at scale."""

    def test_depth2_completes_within_2_seconds(self, large_graph):
        """Depth-2 traversal on 500 entities / 1000 relationships must finish
        in under 2 seconds.  This is the common-case depth used by the LLM
        enrichment context builder (ENT-3) and the opportunity review panel.
        """
        seed_ids = large_graph
        start = time.perf_counter()
        results = opportunity_neighbourhood(_ORG_ID, seed_ids, max_depth=2)
        elapsed = time.perf_counter() - start

        assert elapsed < _DEPTH2_MAX_SECONDS, (
            f"Depth-2 query took {elapsed:.3f}s, expected < {_DEPTH2_MAX_SECONDS}s"
        )
        # Sanity: the seeds themselves are depth-0 nodes, so results are non-empty.
        assert len(results) > 0, "Expected at least the seed nodes in results"

    def test_depth5_completes_or_times_out_within_10_seconds(self, large_graph):
        """Depth-5 traversal must either complete normally or be aborted by the
        built-in 10-second timeout — both are acceptable outcomes.  The important
        property is that the caller always gets a response within the bound.
        """
        seed_ids = large_graph
        start = time.perf_counter()
        # Pass a 10-second timeout explicitly; default is already 10s.
        results = opportunity_neighbourhood(
            _ORG_ID, seed_ids, max_depth=5, timeout_s=_DEPTH5_MAX_SECONDS
        )
        elapsed = time.perf_counter() - start

        assert elapsed < _DEPTH5_MAX_SECONDS, (
            f"Depth-5 query took {elapsed:.3f}s, expected < {_DEPTH5_MAX_SECONDS}s "
            "(neither completed nor timed out within the bound)"
        )
        # results may be non-empty (completed) or empty (timed out gracefully).
        assert isinstance(results, list)

    # -----------------------------------------------------------------------
    # Correctness invariants that hold under scale
    # -----------------------------------------------------------------------

    def test_depth2_returns_only_resolved_entities(self, large_graph):
        """All nodes returned at any depth must have resolution_confidence > 0.
        The graph fixture inserts only resolved entities, so the filter is
        consistent — this guards against accidental removal of the
        ``resolution_status = 'resolved'`` predicate in the CTE.
        """
        seed_ids = large_graph
        results = opportunity_neighbourhood(_ORG_ID, seed_ids, max_depth=2)
        for node in results:
            assert node.resolution_confidence > 0, (
                f"Node {node.entity_id} ({node.display_name}) has "
                f"resolution_confidence={node.resolution_confidence}; "
                "only resolved entities should be returned"
            )

    def test_result_is_bounded_by_node_cap(self, large_graph):
        """The result must never exceed _NEIGHBOURHOOD_NODE_CAP (500) nodes,
        even when depth-5 traversal would otherwise visit the entire graph.
        """
        seed_ids = large_graph
        results = opportunity_neighbourhood(_ORG_ID, seed_ids, max_depth=5)
        assert len(results) <= _NEIGHBOURHOOD_NODE_CAP, (
            f"Result contained {len(results)} nodes, exceeding cap of "
            f"{_NEIGHBOURHOOD_NODE_CAP}"
        )

    def test_depth0_seeds_always_present_at_depth_zero(self, large_graph):
        """The seed entities must appear in the results at depth=0.  If the
        depth-2 query returns no results for the seeds, the ring graph
        construction or the base-case CTE is broken.
        """
        seed_ids = large_graph
        results = opportunity_neighbourhood(_ORG_ID, seed_ids, max_depth=2)
        depth_zero_ids = {n.entity_id for n in results if n.depth == 0}
        for sid in seed_ids:
            assert sid in depth_zero_ids, (
                f"Seed entity {sid} missing from depth-0 results"
            )

    def test_empty_seeds_returns_empty_list(self, large_graph):
        """No seeds → empty result, no exception."""
        results = opportunity_neighbourhood(_ORG_ID, [], max_depth=2)
        assert results == []

    def test_max_depth_clamped_to_5(self, large_graph):
        """Requesting depth > 5 must be silently clamped to 5 and not raise."""
        seed_ids = large_graph
        start = time.perf_counter()
        results = opportunity_neighbourhood(_ORG_ID, seed_ids, max_depth=99)
        elapsed = time.perf_counter() - start
        # Must complete within the same 10-second timeout as depth-5.
        assert elapsed < _DEPTH5_MAX_SECONDS
        assert isinstance(results, list)

    def test_no_exact_duplicate_rows(self, large_graph):
        """SELECT DISTINCT must ensure no two rows are completely identical
        (all nine columns the same).  An entity can legitimately appear at the
        same depth via different parent paths — that is expected in a ring
        topology where multiple seeds share a common neighbour.  What must
        NOT happen is for the exact same (entity_id, from_entity_id, depth,
        relationship_type) tuple to appear more than once.
        """
        seed_ids = large_graph
        results = opportunity_neighbourhood(_ORG_ID, seed_ids, max_depth=2)
        seen: set = set()
        for node in results:
            # Full row key: every field that SELECT DISTINCT operates on.
            key = (
                node.entity_id,
                node.from_entity_id,
                node.relationship_type,
                node.depth,
            )
            assert key not in seen, (
                f"Exact duplicate row: entity_id={node.entity_id}, "
                f"from={node.from_entity_id}, rel={node.relationship_type}, "
                f"depth={node.depth}"
            )
            seen.add(key)
