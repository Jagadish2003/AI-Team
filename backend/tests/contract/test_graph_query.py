"""ENT-4 / T3-S14-A full graph contract suite.

This file covers the ENT4 graph query, graph context, telemetry, and route
contracts from ENT4_GraphStorage.docx. The tests use the contract-test SQLite
database only; no live services are required.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.graph_context_builder import (
    GRAPH_CONTEXT_MAX_ENTITIES,
    GRAPH_CONTEXT_MAX_RELATIONSHIPS,
    TRUNCATION_NOTE_TEMPLATE,
    EntityContext,
    RelationshipContext,
    build_graph_context,
    rank_entities_for_context,
    rank_relationships_for_context,
)
from app.graph_query import (
    MAX_GRAPH_TRAVERSAL_DEPTH,
    entity_neighbourhood,
    entity_path,
    opportunity_neighbourhood,
    org_graph_summary,
)
from app.main import app
from app.rbac import _ensure_members_table


DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
AUTH = {"Authorization": f"Bearer {DEV_TOKEN}"}


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _headers_for_role(role: str, org_id: str | None = None) -> Dict[str, str]:
    _ensure_members_table()
    org = org_id or f"ent4_org_{uuid4().hex[:8]}"
    con = db.connect()
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO workspace_members (org_id, user_id, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (org, DEV_TOKEN, role, _now()),
        )
        con.commit()
    finally:
        con.close()
    return {**AUTH, "X-Org-Id": org}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def _insert_entity(
    conn: sqlite3.Connection,
    org_id: str,
    display_name: str,
    entity_type: str = "process",
    status: str = "resolved",
    run_count: int = 10,
    confidence: float = 0.95,
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
        VALUES (?, ?, ?, ?, ?, 'test', ?, ?, ?, 'run-ent4',
                'run-ent4', ?, NULL, ?, ?)
        """,
        (
            entity_id,
            org_id,
            entity_type,
            display_name.lower(),
            display_name,
            f"record-{entity_id}",
            confidence,
            status,
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
    relationship_type: str = "depends_on",
    inferred: bool = False,
    confidence: float = 0.9,
) -> None:
    conn.execute(
        """
        INSERT INTO entity_relationships (
            id, org_id, from_entity_id, to_entity_id, relationship_type,
            confidence, inferred, evidence, first_seen_run_id,
            last_seen_run_id, run_count, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'run-ent4',
                'run-ent4', 1, ?)
        """,
        (
            str(uuid4()),
            org_id,
            from_entity_id,
            to_entity_id,
            relationship_type,
            confidence,
            int(inferred),
            _now(),
        ),
    )


def _seed_abc_graph(
    *,
    cycle: bool = False,
    include_inferred_leaf: bool = False,
) -> tuple[str, Dict[str, str]]:
    org_id = f"ent4_graph_{uuid4().hex[:8]}"
    with _connect() as conn:
        a = _insert_entity(conn, org_id, "A")
        b = _insert_entity(conn, org_id, "B")
        c = _insert_entity(conn, org_id, "C")
        _insert_relationship(conn, org_id, a, b)
        _insert_relationship(conn, org_id, b, c)
        if cycle:
            _insert_relationship(conn, org_id, c, a)
        ids = {"a": a, "b": b, "c": c}
        if include_inferred_leaf:
            d = _insert_entity(conn, org_id, "D")
            _insert_relationship(conn, org_id, a, d, inferred=True, confidence=0.99)
            ids["d"] = d
        conn.commit()
    return org_id, ids


def _seed_path_graph() -> tuple[str, Dict[str, str]]:
    org_id = f"ent4_path_{uuid4().hex[:8]}"
    with _connect() as conn:
        ids = {
            key: _insert_entity(conn, org_id, key.upper())
            for key in ("a", "b", "c", "d", "e", "isolated")
        }
        _insert_relationship(conn, org_id, ids["a"], ids["b"])
        _insert_relationship(conn, org_id, ids["b"], ids["d"])
        _insert_relationship(conn, org_id, ids["a"], ids["c"])
        _insert_relationship(conn, org_id, ids["c"], ids["e"])
        _insert_relationship(conn, org_id, ids["e"], ids["d"])
        conn.commit()
    return org_id, ids


def _seed_opportunity(org_id: str, opp_id: str, entity_ids: List[str]) -> str:
    run_id = f"run_ent4_{uuid4().hex[:8]}"
    db.upsert_run(
        run_id,
        {
            "id": run_id,
            "org_id": org_id,
            "status": "done",
            "startedAt": _now(),
        },
    )
    db.run_kv_set("opps", run_id, [{"id": opp_id, "title": "Graph finding"}])
    db.run_kv_set(
        "entities",
        run_id,
        [
            {
                "entity_id": entity_id,
                "entity_type": "process",
                "display_name": f"Seed {index}",
                "source_system": "test",
                "resolution_confidence": 0.95,
                "resolution_status": "resolved",
                "run_count": 10,
            }
            for index, entity_id in enumerate(entity_ids)
        ],
    )
    return run_id


def _context_entity(
    name: str,
    entity_type: str = "person",
    run_count: int = 1,
    confidence: float = 0.9,
    depth: int = 1,
    status: str = "resolved",
) -> Dict[str, Any]:
    return {
        "entity_id": f"{name.lower().replace(' ', '-')}-{uuid4().hex[:6]}",
        "display_name": name,
        "entity_type": entity_type,
        "run_count": run_count,
        "resolution_confidence": confidence,
        "depth": depth,
        "resolution_status": status,
        "source_system": "test",
    }


def _relationship(
    from_name: str,
    to_name: str,
    inferred: bool = False,
    confidence: float = 0.9,
) -> RelationshipContext:
    return RelationshipContext(
        from_entity_id=f"{from_name.lower()}-{uuid4().hex[:6]}",
        from_name=from_name,
        relationship_type="depends_on",
        to_entity_id=f"{to_name.lower()}-{uuid4().hex[:6]}",
        to_name=to_name,
        inferred=inferred,
        confidence=confidence,
    )


def test_opportunity_neighbourhood_returns_resolved_entities_within_depth():
    org_id, ids = _seed_abc_graph()

    result = opportunity_neighbourhood(org_id, [ids["a"]], max_depth=2)

    assert [node.display_name for node in result] == ["A", "B", "C"]
    assert [node.depth for node in result] == [0, 1, 2]


def test_opportunity_neighbourhood_excludes_unresolved_entities():
    org_id = f"ent4_unresolved_{uuid4().hex[:8]}"
    with _connect() as conn:
        a = _insert_entity(conn, org_id, "A")
        b = _insert_entity(conn, org_id, "B", status="unresolved")
        _insert_relationship(conn, org_id, a, b)
        conn.commit()

    result = opportunity_neighbourhood(org_id, [a], max_depth=2)

    assert [node.display_name for node in result] == ["A"]


def test_opportunity_neighbourhood_excludes_ambiguous_entities():
    org_id = f"ent4_ambiguous_{uuid4().hex[:8]}"
    with _connect() as conn:
        a = _insert_entity(conn, org_id, "A")
        b = _insert_entity(conn, org_id, "B", status="ambiguous")
        _insert_relationship(conn, org_id, a, b)
        conn.commit()

    result = opportunity_neighbourhood(org_id, [a], max_depth=2)

    assert [node.display_name for node in result] == ["A"]


def test_opportunity_neighbourhood_excludes_inferred_edges_by_default():
    org_id, ids = _seed_abc_graph(include_inferred_leaf=True)

    result = opportunity_neighbourhood(org_id, [ids["a"]], max_depth=1)

    assert [node.display_name for node in result] == ["A", "B"]


def test_opportunity_neighbourhood_can_include_inferred_edges_explicitly():
    org_id, ids = _seed_abc_graph(include_inferred_leaf=True)

    result = opportunity_neighbourhood(
        org_id,
        [ids["a"]],
        max_depth=1,
        include_inferred=True,
    )

    assert [node.display_name for node in result] == ["A", "B", "D"]


def test_cycle_a_b_c_a_terminates_without_revisiting_seed():
    org_id, ids = _seed_abc_graph(cycle=True)

    result = opportunity_neighbourhood(org_id, [ids["a"]], max_depth=5)

    names = [node.display_name for node in result]
    assert names == ["A", "B", "C"]
    assert names.count("A") == 1
    assert max(node.depth for node in result) == 2


def test_cycle_traversal_respects_configured_depth_limit():
    org_id, ids = _seed_abc_graph(cycle=True)

    result = opportunity_neighbourhood(org_id, [ids["a"]], max_depth=1)

    assert [node.display_name for node in result] == ["A", "B"]
    assert all(node.depth <= 1 for node in result)


def test_traversal_clamps_depth_to_hard_maximum():
    org_id = f"ent4_depth_{uuid4().hex[:8]}"
    with _connect() as conn:
        previous = _insert_entity(conn, org_id, "N0")
        seed = previous
        for index in range(1, 8):
            current = _insert_entity(conn, org_id, f"N{index}")
            _insert_relationship(conn, org_id, previous, current)
            previous = current
        conn.commit()

    result = opportunity_neighbourhood(org_id, [seed], max_depth=99)

    assert result[0].entity_id == seed
    assert max(node.depth for node in result) == MAX_GRAPH_TRAVERSAL_DEPTH
    assert len(result) == MAX_GRAPH_TRAVERSAL_DEPTH + 1


def test_entity_neighbourhood_returns_empty_for_missing_or_cross_org_entity():
    org_id, ids = _seed_abc_graph()
    other_org = f"ent4_other_{uuid4().hex[:8]}"

    assert entity_neighbourhood(org_id, "missing") == []
    assert entity_neighbourhood(other_org, ids["a"]) == []


def test_entity_path_returns_shortest_path():
    org_id, ids = _seed_path_graph()

    result = entity_path(org_id, ids["a"], ids["d"], max_depth=5)

    assert [node.entity_id for node in result] == [ids["a"], ids["b"], ids["d"]]
    assert [node.depth for node in result] == [0, 1, 2]


def test_entity_path_returns_empty_when_depth_too_low():
    org_id, ids = _seed_path_graph()

    result = entity_path(org_id, ids["a"], ids["d"], max_depth=1)

    assert result == []


def test_entity_path_returns_empty_when_no_path_exists():
    org_id, ids = _seed_path_graph()

    result = entity_path(org_id, ids["isolated"], ids["d"], max_depth=5)

    assert result == []


def test_entity_path_excludes_inferred_edges_by_default():
    org_id = f"ent4_path_inferred_{uuid4().hex[:8]}"
    with _connect() as conn:
        a = _insert_entity(conn, org_id, "A")
        b = _insert_entity(conn, org_id, "B")
        _insert_relationship(conn, org_id, a, b, inferred=True)
        conn.commit()

    assert entity_path(org_id, a, b, max_depth=2) == []
    assert [node.entity_id for node in entity_path(org_id, a, b, max_depth=2, include_inferred=True)] == [a, b]


def test_org_graph_summary_is_org_scoped():
    org_id, _ids = _seed_abc_graph()
    other_org, _other_ids = _seed_abc_graph()

    summary = org_graph_summary(org_id)

    assert summary["org_id"] == org_id
    assert summary["entity_counts_by_type"]["process"] == 3
    assert sum(summary["relationship_counts_by_type"].values()) == 2
    assert summary != org_graph_summary(other_org)


def test_rank_entities_caps_at_15_and_keeps_depth_zero_first():
    entities = [
        EntityContext(
            entity_id=f"direct-{i}",
            name=f"Direct {i}",
            entity_type="person",
            run_count=5,
            confidence=0.9,
            depth=0,
        )
        for i in range(3)
    ] + [
        EntityContext(
            entity_id=f"entity-{i}",
            name=f"Entity {i:02d}",
            entity_type="system",
            run_count=i,
            confidence=0.7,
            depth=1,
        )
        for i in range(30)
    ]

    ranked = rank_entities_for_context(entities)

    assert len(ranked) == GRAPH_CONTEXT_MAX_ENTITIES
    assert all(entity.depth == 0 for entity in ranked[:3])


def test_rank_entities_is_deterministic_for_same_input():
    entities = [
        EntityContext(
            entity_id=f"e-{i}",
            name=f"Name {i % 5}",
            entity_type=["system", "person", "team", "object", "process"][i % 5],
            run_count=i % 7,
            confidence=(i % 10) / 10,
            depth=i % 3,
        )
        for i in range(25)
    ]

    first = rank_entities_for_context(entities)
    second = rank_entities_for_context(list(reversed(entities)))

    assert [entity.entity_id for entity in first] == [entity.entity_id for entity in second]


def test_rank_entities_uses_type_run_confidence_and_name_tie_breaks():
    entities = [
        EntityContext("team", "A Team", "team", 100, 1.0, 1),
        EntityContext("person-low", "Z Person", "person", 2, 0.5, 1),
        EntityContext("person-high-a", "A Person", "person", 5, 0.9, 1),
        EntityContext("person-high-b", "B Person", "person", 5, 0.9, 1),
        EntityContext("object", "Important Object", "object", 100, 1.0, 1),
    ]

    ranked = rank_entities_for_context(entities, max_entities=5)

    assert [entity.entity_id for entity in ranked] == [
        "person-high-a",
        "person-high-b",
        "person-low",
        "team",
        "object",
    ]


def test_rank_relationships_caps_at_20():
    relationships = [
        _relationship(f"From {i:02d}", f"To {i:02d}", confidence=0.9)
        for i in range(35)
    ]

    ranked = rank_relationships_for_context(relationships)

    assert len(ranked) == GRAPH_CONTEXT_MAX_RELATIONSHIPS


def test_rank_relationships_puts_observed_edges_before_inferred_edges():
    inferred = _relationship("A", "B", inferred=True, confidence=1.0)
    observed = _relationship("C", "D", inferred=False, confidence=0.1)

    ranked = rank_relationships_for_context([inferred, observed], max_relationships=2)

    assert ranked[0] is observed
    assert ranked[1] is inferred


def test_rank_relationships_is_deterministic():
    relationships = [
        _relationship(f"From {i % 4}", f"To {i % 3}", inferred=bool(i % 2), confidence=0.8)
        for i in range(12)
    ]

    first = rank_relationships_for_context(relationships)
    second = rank_relationships_for_context(list(reversed(relationships)))

    assert [
        (r.from_entity_id, r.to_entity_id, r.inferred) for r in first
    ] == [
        (r.from_entity_id, r.to_entity_id, r.inferred) for r in second
    ]


def test_build_graph_context_sets_truncation_note_when_entity_cap_is_hit():
    entities = [_context_entity(f"Entity {i:02d}", run_count=i) for i in range(30)]

    context = build_graph_context("opp-1", entities, [], org_id="org")

    assert context.truncated is True
    assert context.entity_count == 30
    assert context.entity_count_shown == GRAPH_CONTEXT_MAX_ENTITIES
    assert context.observed_summary.endswith(
        TRUNCATION_NOTE_TEMPLATE.format(count=15)
    )


def test_build_graph_context_sparse_graph_is_empty_and_does_not_raise():
    entities = [_context_entity("Only One"), _context_entity("Only Two")]

    context = build_graph_context("opp-sparse", entities, [], org_id="org")

    assert context.sparse_graph is True
    assert context.observed_summary == ""
    assert context.entity_count == 2


def test_build_graph_context_excludes_ambiguous_and_unresolved_entities():
    entities = [
        _context_entity("Resolved", status="resolved"),
        _context_entity("Ambiguous", status="ambiguous"),
        _context_entity("Unresolved", status="unresolved"),
    ]

    context = build_graph_context("opp-filter", entities, [], org_id="org")

    assert context.entity_count == 1
    assert [entity.name for entity in context.entities] == ["Resolved"]


def test_build_graph_context_caps_relationships_and_marks_truncated():
    entities = [_context_entity(f"Entity {i}", run_count=i) for i in range(4)]
    relationships = [
        _relationship(f"From {i:02d}", f"To {i:02d}", confidence=0.9)
        for i in range(25)
    ]

    context = build_graph_context("opp-rels", entities, relationships, org_id="org")

    assert context.relationship_count == 25
    assert context.relationship_count_shown == GRAPH_CONTEXT_MAX_RELATIONSHIPS
    assert context.truncated is True


def test_build_graph_context_output_is_deterministic():
    entities = [_context_entity(f"Entity {i:02d}", run_count=i) for i in range(10)]
    relationships = [_relationship(f"From {i}", f"To {i}") for i in range(6)]

    first = build_graph_context("opp-d", entities, relationships, org_id="org")
    second = build_graph_context("opp-d", list(reversed(entities)), list(reversed(relationships)), org_id="org")

    assert [entity.entity_id for entity in first.entities] == [entity.entity_id for entity in second.entities]
    assert [rel.from_entity_id for rel in first.relationships] == [rel.from_entity_id for rel in second.relationships]
    assert first.observed_summary == second.observed_summary


def test_graph_context_built_telemetry_event_is_registered():
    from app.telemetry import EVENT_REGISTRY, GraphContextBuiltPayload

    assert EVENT_REGISTRY["graph.context_built"] is GraphContextBuiltPayload


def test_build_graph_context_fires_telemetry_after_every_build(monkeypatch: pytest.MonkeyPatch):
    from app import telemetry

    calls: List[tuple[str, Dict[str, Any]]] = []

    def fake_record_event(event_type: str, payload: Dict[str, Any]) -> None:
        calls.append((event_type, payload))

    monkeypatch.setattr(telemetry, "record_event", fake_record_event)

    context = build_graph_context(
        "opp-telemetry",
        [_context_entity(f"Entity {i}", run_count=i) for i in range(4)],
        [],
        org_id="org-telemetry",
    )

    assert context.sparse_graph is False
    assert calls
    event_type, payload = calls[-1]
    assert event_type == "graph.context_built"
    assert payload["entity_count"] == 4
    assert payload["entity_count_shown"] == 4
    assert payload["truncated"] is False
    assert isinstance(payload["duration_ms"], int)


GRAPH_ENDPOINTS = [
    "/api/graph/opportunity/opp-missing/neighbourhood",
    "/api/graph/entity/entity-missing/neighbourhood",
    "/api/graph/path?from_entity_id=a&to_entity_id=b",
    "/api/graph/org/summary",
]


@pytest.mark.parametrize("path", GRAPH_ENDPOINTS)
def test_graph_routes_return_401_without_auth(client: TestClient, path: str):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", GRAPH_ENDPOINTS)
def test_graph_routes_return_403_for_viewer_role(client: TestClient, path: str):
    assert client.get(path, headers=_headers_for_role("viewer")).status_code == 403


def test_graph_route_entity_neighbourhood_returns_current_org_data(client: TestClient):
    org_id, ids = _seed_abc_graph()

    response = client.get(
        f"/api/graph/entity/{ids['a']}/neighbourhood",
        headers=_headers_for_role("analyst", org_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["entity_id"] == ids["a"]
    assert body["node_count"] == 3


def test_graph_route_opportunity_neighbourhood_uses_org_visible_seed(client: TestClient):
    org_id, ids = _seed_abc_graph()
    _seed_opportunity(org_id, "opp-ent4", [ids["a"]])

    response = client.get(
        "/api/graph/opportunity/opp-ent4/neighbourhood",
        headers=_headers_for_role("analyst", org_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["opportunity_id"] == "opp-ent4"
    assert body["seed_entity_ids"] == [ids["a"]]
    assert body["node_count"] == 3


def test_graph_route_path_returns_shortest_path(client: TestClient):
    org_id, ids = _seed_path_graph()

    response = client.get(
        "/api/graph/path",
        headers=_headers_for_role("analyst", org_id),
        params={"from_entity_id": ids["a"], "to_entity_id": ids["d"]},
    )

    assert response.status_code == 200
    assert [node["entity_id"] for node in response.json()["path"]] == [
        ids["a"],
        ids["b"],
        ids["d"],
    ]


def test_graph_route_org_summary_returns_current_org_counts(client: TestClient):
    org_id, _ids = _seed_abc_graph()

    response = client.get(
        "/api/graph/org/summary",
        headers=_headers_for_role("analyst", org_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["org_id"] == org_id
    assert body["entity_counts_by_type"]["process"] == 3


def test_graph_route_cross_org_entity_returns_404(client: TestClient):
    org_a, ids = _seed_abc_graph()
    org_b = f"{org_a}_other"

    response = client.get(
        f"/api/graph/entity/{ids['a']}/neighbourhood",
        headers=_headers_for_role("analyst", org_b),
    )

    assert response.status_code == 404


def test_graph_route_cross_org_path_returns_404(client: TestClient):
    org_a, ids = _seed_path_graph()
    org_b = f"{org_a}_other"

    response = client.get(
        "/api/graph/path",
        headers=_headers_for_role("analyst", org_b),
        params={"from_entity_id": ids["a"], "to_entity_id": ids["d"]},
    )

    assert response.status_code == 404


def test_graph_route_cross_org_opportunity_returns_404(client: TestClient):
    org_a, ids = _seed_abc_graph()
    org_b = f"{org_a}_other"
    _seed_opportunity(org_a, "opp-other-org", [ids["a"]])

    response = client.get(
        "/api/graph/opportunity/opp-other-org/neighbourhood",
        headers=_headers_for_role("analyst", org_b),
    )

    assert response.status_code == 404


def test_graph_routes_do_not_trust_query_param_org_id(client: TestClient):
    org_a, ids = _seed_abc_graph()
    org_b = f"{org_a}_other"

    response = client.get(
        f"/api/graph/entity/{ids['a']}/neighbourhood",
        headers=_headers_for_role("analyst", org_b),
        params={"org_id": org_a},
    )

    assert response.status_code == 404
