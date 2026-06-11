"""ENT-4 / T3-S14-A T4 - graph traversal cycle detection contract tests.

The graph layer must be safe when enterprise data contains cycles. These tests
build A -> B -> C -> A in the real contract-test database and verify
opportunity_neighbourhood() terminates, does not revisit A, and respects the
requested depth limit.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from app.graph_query import opportunity_neighbourhood


def _db_path() -> str:
    return os.environ["DB_PATH"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_entity(
    conn: sqlite3.Connection,
    org_id: str,
    display_name: str,
    run_count: int = 10,
) -> str:
    entity_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO entities (
            id, org_id, entity_type, canonical_name, display_name,
            source_system, source_record_id, resolution_confidence,
            resolution_status, first_seen_run_id, last_seen_run_id,
            run_count, metadata, created_at, updated_at
        )
        VALUES (?, ?, 'process', ?, ?, 'test', ?, 0.95, 'resolved',
                'run-cycle', 'run-cycle', ?, NULL, ?, ?)
        """,
        (
            entity_id,
            org_id,
            display_name.lower(),
            display_name,
            f"record-{entity_id}",
            run_count,
            _now(),
            _now(),
        ),
    )
    return entity_id


def _insert_relationship(
    conn: sqlite3.Connection,
    org_id: str,
    from_entity_id: str,
    to_entity_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO entity_relationships (
            id, org_id, from_entity_id, to_entity_id, relationship_type,
            confidence, inferred, evidence, first_seen_run_id,
            last_seen_run_id, run_count, created_at
        )
        VALUES (?, ?, ?, ?, 'owns', 0.9, 0, NULL, 'run-cycle',
                'run-cycle', 1, ?)
        """,
        (
            str(uuid4()),
            org_id,
            from_entity_id,
            to_entity_id,
            _now(),
        ),
    )


def _seed_abc_cycle() -> tuple[str, str, str, str]:
    org_id = f"org-cycle-{uuid4().hex[:8]}"
    with sqlite3.connect(_db_path()) as conn:
        a_id = _insert_entity(conn, org_id, "A")
        b_id = _insert_entity(conn, org_id, "B")
        c_id = _insert_entity(conn, org_id, "C")

        _insert_relationship(conn, org_id, a_id, b_id)
        _insert_relationship(conn, org_id, b_id, c_id)
        _insert_relationship(conn, org_id, c_id, a_id)
        conn.commit()

    return org_id, a_id, b_id, c_id


def test_cycle_a_b_c_a_terminates_without_revisiting_seed():
    org_id, a_id, _b_id, _c_id = _seed_abc_cycle()

    result = opportunity_neighbourhood(org_id, [a_id], max_depth=5)

    names = [node.display_name for node in result]
    assert names == ["A", "B", "C"]
    assert names.count("A") == 1
    assert max(node.depth for node in result) == 2


def test_cycle_traversal_respects_configured_depth_limit():
    org_id, a_id, _b_id, _c_id = _seed_abc_cycle()

    result = opportunity_neighbourhood(org_id, [a_id], max_depth=1)

    names = [node.display_name for node in result]
    assert names == ["A", "B"]
    assert "C" not in names
    assert all(node.depth <= 1 for node in result)


def test_cycle_traversal_depth_two_reaches_c_but_not_back_to_a():
    org_id, a_id, _b_id, _c_id = _seed_abc_cycle()

    result = opportunity_neighbourhood(org_id, [a_id], max_depth=2)

    by_name = {node.display_name: node.depth for node in result}
    assert by_name == {"A": 0, "B": 1, "C": 2}
