"""
test_salesforce_products.py — ENG-IH-3 Sprint 9
Contract tests for Salesforce product declaration endpoints

Tests:
  PATCH /api/connectors/salesforce/products:
    - 200 on valid products list
    - Returns validated product IDs and human-readable labels
    - Unknown product IDs filtered silently
    - Empty list accepted (clears declaration)
    - 400 when Salesforce not connected
    - 401 without auth

  GET /api/connectors/salesforce/products:
    - 200 returns current declaration
    - Returns empty list when no declaration made
    - Returns previously saved declaration

  Workspace catalog integration:
    - After PATCH, GET /api/integration-hub/workspace-catalog
      shows salesforce.products populated
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import db

client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def seed_dev_owner():
    """Seed dev-token as owner of 'default' org so RBAC checks pass."""
    from app.rbac import seed_owner, _ensure_members_table
    _ensure_members_table()
    seed_owner("default", "dev-token-change-me")


def auth():
    return {"Authorization": "Bearer dev-token-change-me"}

def _set_sf_status(status: str):
    """Helper: set Salesforce connector status for test."""
    connector = db.get_one("connectors", "salesforce")
    if not connector:
        connector = {
            "id": "salesforce", "name": "Salesforce",
            "status": status, "configured": False,
            "products": [], "tier": "recommended",
            "metrics": [], "lastSynced": "—",
            "reads": [], "signalStrength": 0, "category": "CRM",
        }
    else:
        connector["status"] = status
    db.upsert("connectors", "salesforce", connector)


class TestPatchSalesforceProducts:

    def test_patch_valid_products_200(self):
        _set_sf_status("connected")
        resp = client.patch(
            "/api/connectors/salesforce/products",
            headers=auth(),
            json={"products": ["salesforce_pss", "salesforce_sc"]},
        )
        assert resp.status_code == 200

    def test_patch_returns_products_and_labels(self):
        _set_sf_status("connected")
        resp = client.patch(
            "/api/connectors/salesforce/products",
            headers=auth(),
            json={"products": ["salesforce_pss", "salesforce_ncino"]},
        )
        data = resp.json()
        assert "products" in data
        assert "labels" in data
        assert "salesforce_pss"   in data["products"]
        assert "salesforce_ncino" in data["products"]
        assert len(data["labels"]) == 2
        assert "Public Sector Solutions" in data["labels"][0]

    def test_patch_filters_unknown_ids(self):
        _set_sf_status("connected")
        resp = client.patch(
            "/api/connectors/salesforce/products",
            headers=auth(),
            json={"products": ["salesforce_pss", "unknown_product_xyz"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "salesforce_pss"       in data["products"]
        assert "unknown_product_xyz"  not in data["products"]

    def test_patch_empty_list_accepted(self):
        _set_sf_status("connected")
        resp = client.patch(
            "/api/connectors/salesforce/products",
            headers=auth(),
            json={"products": []},
        )
        assert resp.status_code == 200
        assert resp.json()["products"] == []

    def test_patch_400_when_not_connected(self):
        _set_sf_status("disconnected")
        resp = client.patch(
            "/api/connectors/salesforce/products",
            headers=auth(),
            json={"products": ["salesforce_pss"]},
        )
        assert resp.status_code == 400

    def test_patch_401_without_auth(self):
        resp = client.patch(
            "/api/connectors/salesforce/products",
            json={"products": ["salesforce_pss"]},
        )
        assert resp.status_code == 401

    def test_patch_all_six_products(self):
        _set_sf_status("connected")
        all_products = [
            "salesforce_pss", "salesforce_sc", "salesforce_ncino",
            "salesforce_fsc", "salesforce_rc", "salesforce_hc",
        ]
        resp = client.patch(
            "/api/connectors/salesforce/products",
            headers=auth(),
            json={"products": all_products},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["products"]) == 6
        assert set(data["products"]) == set(all_products)

    def test_patch_idempotent(self):
        _set_sf_status("connected")
        payload = {"products": ["salesforce_pss"]}
        r1 = client.patch("/api/connectors/salesforce/products",
            headers=auth(), json=payload)
        r2 = client.patch("/api/connectors/salesforce/products",
            headers=auth(), json=payload)
        assert r1.json()["products"] == r2.json()["products"]


class TestGetSalesforceProducts:

    def test_get_returns_200(self):
        resp = client.get("/api/connectors/salesforce/products", headers=auth())
        assert resp.status_code == 200

    def test_get_returns_empty_when_no_declaration(self):
        _set_sf_status("connected")
        # Clear products
        client.patch("/api/connectors/salesforce/products",
            headers=auth(), json={"products": []})
        resp = client.get("/api/connectors/salesforce/products", headers=auth())
        assert resp.json()["products"] == []

    def test_get_returns_previously_saved(self):
        _set_sf_status("connected")
        client.patch("/api/connectors/salesforce/products",
            headers=auth(),
            json={"products": ["salesforce_pss", "salesforce_sc"]})
        resp = client.get("/api/connectors/salesforce/products", headers=auth())
        data = resp.json()
        assert "salesforce_pss" in data["products"]
        assert "salesforce_sc"  in data["products"]

    def test_get_401_without_auth(self):
        resp = client.get("/api/connectors/salesforce/products")
        assert resp.status_code == 401


class TestWorkspaceCatalogIntegration:

    def test_products_appear_in_workspace_catalog(self):
        """
        ENG-IH-3 AC2: After PATCH, workspace catalog shows
        salesforce.products populated.
        """
        _set_sf_status("connected")
        client.patch(
            "/api/connectors/salesforce/products",
            headers=auth(),
            json={"products": ["salesforce_pss", "salesforce_sc"]},
        )
        catalog = client.get(
            "/api/integration-hub/workspace-catalog",
            headers=auth(),
        ).json()
        sf = next(
            (s for s in catalog["primary_platforms"]
             if s["system_id"] == "salesforce"),
            None,
        )
        assert sf is not None
        assert "salesforce_pss" in sf["products"]
        assert "salesforce_sc"  in sf["products"]

    def test_cleared_products_show_empty_in_catalog(self):
        _set_sf_status("connected")
        client.patch("/api/connectors/salesforce/products",
            headers=auth(), json={"products": []})
        catalog = client.get(
            "/api/integration-hub/workspace-catalog",
            headers=auth(),
        ).json()
        sf = next(
            (s for s in catalog["primary_platforms"]
             if s["system_id"] == "salesforce"),
            None,
        )
        assert sf is not None
        assert sf["products"] == []
