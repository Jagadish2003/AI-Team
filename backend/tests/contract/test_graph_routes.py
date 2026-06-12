"""ENT-4 / T3-S14-A T6 - graph API route contract tests."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.rbac import _ensure_members_table


DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
AUTH = {"Authorization": f"Bearer {DEV_TOKEN}"}


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _headers_for_role(role: str, org_id: str | None = None) -> Dict[str, str]:
    _ensure_members_table()
    org = org_id or f"graph_org_{uuid4().hex[:8]}"
    con = db.connect()
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO workspace_members (org_id, user_id, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (org, DEV_TOKEN, role, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    return {**AUTH, "X-Org-Id": org}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_entity(
    conn: sqlite3.Connection,
    org_id: str,
    name: str,
    entity_type: str = "process",
    status: str = "resolved",
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
        VALUES (?, ?, ?, ?, ?, 'test', ?, 0.95, ?, 'run-graph',
                'run-graph', 10, NULL, ?, ?)
        """,
        (
            entity_id,
            org_id,
            entity_type,
            name.lower(),
            name,
            f"record-{entity_id}",
            status,
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
    relationship_type: str = "owns",
) -> None:
    conn.execute(
        """
        INSERT INTO entity_relationships (
            id, org_id, from_entity_id, to_entity_id, relationship_type,
            confidence, inferred, evidence, first_seen_run_id,
            last_seen_run_id, run_count, created_at
        )
        VALUES (?, ?, ?, ?, ?, 0.9, 0, NULL, 'run-graph',
                'run-graph', 1, ?)
        """,
        (str(uuid4()), org_id, from_entity_id, to_entity_id, relationship_type, _now()),
    )


def _seed_graph(org_id: str) -> dict[str, str]:
    with sqlite3.connect(os.environ["DB_PATH"]) as conn:
        a = _insert_entity(conn, org_id, "Loan Intake")
        b = _insert_entity(conn, org_id, "Credit Review")
        c = _insert_entity(conn, org_id, "Approval Desk")
        _insert_relationship(conn, org_id, a, b, "depends_on")
        _insert_relationship(conn, org_id, b, c, "routes_to")
        conn.commit()
    return {"a": a, "b": b, "c": c}


def _seed_opportunity(org_id: str, opp_id: str, entity_ids: list[str]) -> str:
    run_id = f"run_graph_{uuid4().hex[:8]}"
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


GRAPH_ENDPOINTS = [
    "/api/graph/opportunity/opp-missing/neighbourhood",
    "/api/graph/entity/entity-missing/neighbourhood",
    "/api/graph/path?from_entity_id=a&to_entity_id=b",
    "/api/graph/org/summary",
]


def test_graph_routes_registered_once():
    paths = [getattr(route, "path", None) for route in app.routes]
    assert paths.count("/api/graph/opportunity/{opp_id}/neighbourhood") == 1
    assert paths.count("/api/graph/entity/{entity_id}/neighbourhood") == 1
    assert paths.count("/api/graph/path") == 1
    assert paths.count("/api/graph/org/summary") == 1


@pytest.mark.parametrize("path", GRAPH_ENDPOINTS)
def test_graph_routes_require_authentication(client: TestClient, path: str):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", GRAPH_ENDPOINTS)
def test_graph_routes_require_analyst_role(client: TestClient, path: str):
    assert client.get(path, headers=_headers_for_role("viewer")).status_code == 403


def test_entity_neighbourhood_returns_org_scoped_graph(client: TestClient):
    org_id = f"graph_route_{uuid4().hex[:8]}"
    ids = _seed_graph(org_id)
    response = client.get(
        f"/api/graph/entity/{ids['a']}/neighbourhood",
        headers=_headers_for_role("analyst", org_id),
        params={"max_depth": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["entity_id"] == ids["a"]
    assert body["node_count"] == 3
    assert [node["display_name"] for node in body["nodes"]] == [
        "Loan Intake",
        "Credit Review",
        "Approval Desk",
    ]


def test_entity_neighbourhood_limit_caps_returned_nodes(client: TestClient):
    # ENT-4 review #4: an optional `limit` query param lets display clients ask
    # for a smaller payload; the full result is still returned when omitted.
    org_id = f"graph_limit_{uuid4().hex[:8]}"
    ids = _seed_graph(org_id)
    headers = _headers_for_role("analyst", org_id)

    full = client.get(
        f"/api/graph/entity/{ids['a']}/neighbourhood",
        headers=headers,
        params={"max_depth": 2},
    )
    assert full.status_code == 200
    assert full.json()["node_count"] == 3

    limited = client.get(
        f"/api/graph/entity/{ids['a']}/neighbourhood",
        headers=headers,
        params={"max_depth": 2, "limit": 1},
    )
    assert limited.status_code == 200
    body = limited.json()
    assert body["node_count"] == 1
    assert len(body["nodes"]) == 1
    # Deterministic order is preserved — the most relevant (seed) node is kept.
    assert body["nodes"][0]["display_name"] == "Loan Intake"


def test_neighbourhood_limit_rejects_out_of_range(client: TestClient):
    org_id = f"graph_limit_bad_{uuid4().hex[:8]}"
    ids = _seed_graph(org_id)
    headers = _headers_for_role("analyst", org_id)
    assert client.get(
        f"/api/graph/entity/{ids['a']}/neighbourhood",
        headers=headers,
        params={"limit": 0},
    ).status_code == 422


def test_graph_path_returns_shortest_path(client: TestClient):
    org_id = f"graph_path_{uuid4().hex[:8]}"
    ids = _seed_graph(org_id)
    response = client.get(
        "/api/graph/path",
        headers=_headers_for_role("analyst", org_id),
        params={"from_entity_id": ids["a"], "to_entity_id": ids["c"], "max_depth": 5},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["path_found"] is True
    assert [node["entity_id"] for node in body["path"]] == [ids["a"], ids["b"], ids["c"]]


def test_org_summary_returns_counts_for_current_org_only(client: TestClient):
    org_id = f"graph_summary_{uuid4().hex[:8]}"
    other_org = f"graph_other_{uuid4().hex[:8]}"
    _seed_graph(org_id)
    _seed_graph(other_org)

    response = client.get(
        "/api/graph/org/summary",
        headers=_headers_for_role("analyst", org_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["org_id"] == org_id
    assert body["entity_counts_by_type"]["process"] == 3
    assert sum(body["relationship_counts_by_type"].values()) == 2


def test_opportunity_neighbourhood_uses_org_visible_opportunity_seeds(client: TestClient):
    org_id = f"graph_opp_{uuid4().hex[:8]}"
    ids = _seed_graph(org_id)
    _seed_opportunity(org_id, "opp-graph", [ids["a"]])

    response = client.get(
        "/api/graph/opportunity/opp-graph/neighbourhood",
        headers=_headers_for_role("analyst", org_id),
        params={"max_depth": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["opportunity_id"] == "opp-graph"
    assert body["seed_entity_ids"] == [ids["a"]]
    assert body["node_count"] == 3


def test_cross_org_entity_access_returns_404(client: TestClient):
    org_a = f"graph_org_a_{uuid4().hex[:8]}"
    org_b = f"graph_org_b_{uuid4().hex[:8]}"
    ids = _seed_graph(org_a)

    response = client.get(
        f"/api/graph/entity/{ids['a']}/neighbourhood",
        headers=_headers_for_role("analyst", org_b),
    )

    assert response.status_code == 404


def test_cross_org_path_access_returns_404(client: TestClient):
    org_a = f"graph_path_a_{uuid4().hex[:8]}"
    org_b = f"graph_path_b_{uuid4().hex[:8]}"
    ids = _seed_graph(org_a)

    response = client.get(
        "/api/graph/path",
        headers=_headers_for_role("analyst", org_b),
        params={"from_entity_id": ids["a"], "to_entity_id": ids["c"]},
    )

    assert response.status_code == 404


def test_cross_org_opportunity_access_returns_404(client: TestClient):
    org_a = f"graph_opp_a_{uuid4().hex[:8]}"
    org_b = f"graph_opp_b_{uuid4().hex[:8]}"
    ids = _seed_graph(org_a)
    _seed_opportunity(org_a, "opp-other-org", [ids["a"]])

    response = client.get(
        "/api/graph/opportunity/opp-other-org/neighbourhood",
        headers=_headers_for_role("analyst", org_b),
    )

    assert response.status_code == 404
