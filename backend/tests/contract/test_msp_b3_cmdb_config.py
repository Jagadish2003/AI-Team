"""MSP-B3 T1 contract: ServiceNow CMDB class scope is configurable per org."""
from __future__ import annotations

from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer dev-token-change-me"}


def _headers(org_id: str) -> dict:
    return {**AUTH, "X-Org-Id": org_id}


def _connect_for_config(org_id: str) -> None:
    from app import db
    from app.rbac import seed_owner

    seed_owner(org_id, "dev-token-change-me")
    db.org_connector_set(
        org_id,
        "servicenow",
        {"id": "servicenow", "name": "ServiceNow", "status": "connected"},
    )


def test_two_orgs_configure_and_resolve_independent_cmdb_scopes(client: TestClient):
    from discovery.ingest import clear_live_connectors, set_ingest_org
    from discovery.ingest.servicenow import resolve_cmdb_class_scope

    org_a = "msp_b3_cmdb_a"
    org_b = "msp_b3_cmdb_b"
    _connect_for_config(org_a)
    _connect_for_config(org_b)

    response_a = client.post(
        "/api/connectors/servicenow/configure",
        headers=_headers(org_a),
        json={"cmdb_class_scope": ["cmdb_ci_server", "cmdb_ci_container"]},
    )
    response_b = client.post(
        "/api/connectors/servicenow/configure",
        headers=_headers(org_b),
        json={"cmdb_class_scope": ["cmdb_ci_lb"]},
    )
    assert response_a.status_code == 200
    assert response_b.status_code == 200

    try:
        set_ingest_org(org_a)
        assert resolve_cmdb_class_scope() == (
            "cmdb_ci_container",
            "cmdb_ci_server",
        )
        set_ingest_org(org_b)
        assert resolve_cmdb_class_scope() == ("cmdb_ci_lb",)
    finally:
        clear_live_connectors()


def test_config_route_rejects_encoded_query_injection(client: TestClient):
    org_id = "msp_b3_cmdb_invalid"
    _connect_for_config(org_id)

    response = client.post(
        "/api/connectors/servicenow/configure",
        headers=_headers(org_id),
        json={"cmdb_class_scope": ["cmdb_ci_server^ORnameLIKEsecret"]},
    )

    assert response.status_code == 422
    assert "expected cmdb_ci_*" in response.json()["detail"]


def test_existing_bodyless_configure_call_remains_compatible(client: TestClient):
    org_id = "msp_b3_cmdb_legacy_configure"
    _connect_for_config(org_id)

    response = client.post(
        "/api/connectors/servicenow/configure",
        headers=_headers(org_id),
    )

    assert response.status_code == 200
    assert response.json()["configured"] is True
