"""2.0-B4 T2 — the concepts API (AC5's "visible to pack authors"), DB-free.

These routes serve the platform's own mapping contracts: the same answer for every
tenant, containing no customer data. So unlike every other read route in this app they
touch no database, which is why this is a unit test rather than a contract test — it
needs no seeded org and no test database to be meaningful, and running it here means
the API surface is covered even when the shared DB is unavailable.

Auth is exercised by OVERRIDING the router's own dependency objects rather than by
skipping them, so the test proves the routes are actually mounted behind
``require_auth`` + ``require_role`` rather than assuming it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import routes_concepts  # noqa: E402
from discovery.concepts import conformance as conf  # noqa: E402
from discovery.concepts import mappers as mp  # noqa: E402
from discovery.concepts import model as M  # noqa: E402


@pytest.fixture(scope="module")
def client():
    """A minimal app carrying only the concept routes.

    Deliberately not ``app.main``: importing it starts background jobs and seeds an
    owner, both of which need a database that these routes do not.
    """
    app = FastAPI()
    routes_concepts.register_concept_routes(app)
    # The router declares its auth dependencies at import; override the exact callables
    # it holds, which is only possible if they really are attached.
    assert routes_concepts.router.dependencies, "routes must be mounted behind auth"
    for dependency in routes_concepts.router.dependencies:
        app.dependency_overrides[dependency.dependency] = lambda: None
    return TestClient(app)


def test_registration_is_idempotent():
    """Double registration would duplicate every path; main.py's startup path calls it
    once but a reload must not double-mount."""
    app = FastAPI()
    routes_concepts.register_concept_routes(app)
    before = len(app.routes)
    routes_concepts.register_concept_routes(app)
    assert len(app.routes) == before


def test_contracts_endpoint_serves_the_versioned_concept_set(client):
    body = client.get("/api/concepts/contracts").json()
    assert body["concept_set_version"] >= 1
    assert set(body["concepts"]) == set(M.CONCEPT_SET)
    assert set(body["contract_versions"]) == set(M.CONCEPT_SET)
    assert body["breaking_change_rules"], "the bump rules are part of the contract"


def test_conformance_endpoint_names_the_mapper_behind_every_claim(client):
    body = client.get("/api/concepts/conformance").json()
    assert set(body["connectors"]) == set(conf.CONFORMANCE)
    assert body["mappers"]["mapper_count"] == len(mp.MAPPERS)
    for connector_id, view in body["connectors"].items():
        for position in view["concepts"]:
            if position["conforms"]:
                assert position["mapper"], f"{connector_id}/{position['concept']}"


def test_gaps_endpoint_serves_both_orientations(client):
    """A pack author usually needs both: which sources can carry a concept, and what
    one source still owes."""
    body = client.get("/api/concepts/gaps").json()
    assert set(body["concepts"]) == set(M.CONCEPT_SET)
    assert set(body["connectors"]) == set(conf.CONFORMANCE)
    assert body["outstanding_count"] >= 1
    assert body["field_gap_count"] >= 1
    assert body["registry_behind_code"] == []


def test_gaps_endpoint_states_what_will_be_missing(client):
    """The AC5 payload: a field gap reaches the API, with its reason."""
    body = client.get("/api/concepts/gaps").json()
    servicenow = next(
        e for e in body["concepts"][M.CONCEPT_STATE_TRANSITION]["usable"]
        if e["connector_id"] == "servicenow"
    )
    assert "actor_group" in servicenow["fields_never_populated"]
    gap = next(g for g in servicenow["field_gaps"] if g["field"] == "actor_group")
    assert gap["kind"] == "absent"
    assert gap["reason"].strip()


def test_by_concept_endpoint_returns_the_inverted_view(client):
    body = client.get("/api/concepts/by-concept").json()
    assert body[M.CONCEPT_APPROVAL]["usable_connector_ids"] == ["salesforce"]


def test_connector_endpoint_serves_one_connector(client):
    body = client.get("/api/concepts/connectors/jira").json()
    assert body["connector_id"] == "jira"
    assert {e["concept"] for e in body["supported"]} == {
        M.CONCEPT_WORK_ITEM, M.CONCEPT_ARTIFACT, M.CONCEPT_ENTITY_REFERENCE,
    }
    assert body["outstanding"], "jira still owes state_transition and assignment"


def test_unknown_connector_404s_and_names_the_declared_set(client):
    """404 rather than an empty document: an empty response would read as 'this
    connector supports nothing', which is a different and wrong answer."""
    response = client.get("/api/concepts/connectors/sap")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "no conformance declaration" in detail
    assert "servicenow" in detail


def test_every_endpoint_is_read_only(client):
    """The registry is code. A write route would imply it can be edited at runtime."""
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/api/concepts/contracts")
        assert response.status_code == 405, f"{method.upper()} should not be allowed"
