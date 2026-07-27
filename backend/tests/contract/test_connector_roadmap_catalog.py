"""R191-R1 T5 (AT-726) — Integration Hub catalog roadmap-labelling.

SAP and Dynamics 365 catalog tiles (auth/config exists, ingestion does not) — and
any other tile without a shipped ingestor — are roadmap-labelled and
non-connectable ("Coming — 2.0.1"). Satisfies AC2: they render as roadmap
(target 2.0.1) in the Hub, are not connectable, and no run can select them.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app import connector_roadmap, db

AUTH = {"Authorization": "Bearer dev-token-change-me"}
DEV_USER = "dev-token-change-me"


# --------------------------------------------------------------------------- #
# Pure-function unit coverage (the single source of truth)
# --------------------------------------------------------------------------- #
class TestRoadmapRule:
    def test_sap_and_d365_are_roadmap_targeting_2_0_1(self):
        for cid in ("sap", "dynamics365"):
            assert connector_roadmap.is_roadmap(cid) is True
            assert connector_roadmap.is_shipped(cid) is False
            assert connector_roadmap.roadmap_target(cid) == "2.0.1"

    def test_shipped_connectors_are_not_roadmap(self):
        for cid in ("salesforce", "servicenow", "jira", "github", "slack",
                    "teams", "confluence", "sharepoint", "postgresql",
                    "sql_server", "oracle_db"):
            assert connector_roadmap.is_shipped(cid) is True
            assert connector_roadmap.is_roadmap(cid) is False

    def test_other_unshipped_tile_is_roadmap_unscheduled(self):
        # A tile with neither auth-config nor ingestor still gets the roadmap
        # treatment, but with no committed release.
        assert connector_roadmap.is_roadmap("snowflake") is True
        assert connector_roadmap.roadmap_target("snowflake") == "unscheduled"

    def test_annotate_adds_flags_without_touching_status(self):
        sap = connector_roadmap.annotate_connector(
            {"id": "sap", "name": "SAP", "status": "disconnected", "tier": "standard"}
        )
        assert sap["roadmap"] is True
        assert sap["roadmapTarget"] == "2.0.1"
        # additive only — never rewrites status/tier
        assert sap["status"] == "disconnected"
        assert sap["tier"] == "standard"

        sfdc = connector_roadmap.annotate_connector(
            {"id": "salesforce", "name": "Salesforce", "status": "connected"}
        )
        assert sfdc["roadmap"] is False
        assert sfdc["roadmapTarget"] is None
        assert sfdc["status"] == "connected"

    def test_block_message_names_connector_and_target(self):
        msg = connector_roadmap.roadmap_block_message("sap", "SAP")
        assert "SAP" in msg and "2.0.1" in msg
        unscheduled = connector_roadmap.roadmap_block_message("notion", "Notion")
        assert "Notion" in unscheduled and "2.0.1" not in unscheduled

    def test_unknown_id_is_neither_shipped_nor_roadmap(self):
        # A synthetic/uncatalogued id (e.g. a license-limit test fixture) must
        # never be treated as roadmap, or it would be wrongly blocked from
        # connecting. It is simply "not a roadmap tile".
        assert connector_roadmap.is_roadmap("sys1") is False
        assert connector_roadmap.is_shipped("sys1") is False

    def test_catalog_is_fully_classified(self):
        # Anchor-on-shipped honesty: every real catalog tile is EITHER shipped
        # (connectable-eligible) OR roadmap (labelled + non-connectable) — never
        # neither. This is the static seed-level guard; the dynamically-discovered
        # ingestor cross-check is the separate R191-R1 AC1 task.
        import json
        from pathlib import Path

        seed = Path(__file__).resolve().parents[2] / "database" / "seed" / "connectors.json"
        catalog = json.loads(seed.read_text())
        unclassified = [
            c["id"]
            for c in catalog
            if not (
                connector_roadmap.is_shipped(c["id"])
                ^ connector_roadmap.is_roadmap(c["id"])
            )
        ]
        assert unclassified == [], (
            "Every catalog tile must be exactly one of shipped/roadmap. "
            f"Unclassified (add to SHIPPED_CONNECTOR_IDS or ROADMAP_TARGETS): {unclassified}"
        )


# --------------------------------------------------------------------------- #
# API surface: GET /api/connectors carries the roadmap flags
# --------------------------------------------------------------------------- #
def _seed_catalog(ids) -> None:
    for cid in ids:
        db.upsert(
            "connectors",
            cid,
            {"id": cid, "name": cid.replace("_", " ").title(), "status": "disconnected"},
        )


def _by_id(rows, cid):
    return next((r for r in rows if r.get("id") == cid), None)


class TestCatalogApi:
    def test_sap_and_d365_render_as_roadmap_2_0_1(self, client: TestClient):
        _seed_catalog(["sap", "dynamics365", "salesforce", "snowflake"])
        rows = client.get("/api/connectors", headers=AUTH).json()

        sap = _by_id(rows, "sap")
        d365 = _by_id(rows, "dynamics365")
        assert sap["roadmap"] is True and sap["roadmapTarget"] == "2.0.1"
        assert d365["roadmap"] is True and d365["roadmapTarget"] == "2.0.1"

    def test_shipped_tile_is_not_roadmap(self, client: TestClient):
        _seed_catalog(["salesforce"])
        rows = client.get("/api/connectors", headers=AUTH).json()
        sfdc = _by_id(rows, "salesforce")
        assert sfdc["roadmap"] is False
        assert sfdc["roadmapTarget"] is None

    def test_other_unshipped_tile_is_roadmap(self, client: TestClient):
        _seed_catalog(["snowflake"])
        rows = client.get("/api/connectors", headers=AUTH).json()
        snow = _by_id(rows, "snowflake")
        assert snow["roadmap"] is True
        assert snow["roadmapTarget"] == "unscheduled"


# --------------------------------------------------------------------------- #
# Non-connectable: connecting a roadmap connector is refused (AC2)
# --------------------------------------------------------------------------- #
class TestRoadmapNotConnectable:
    def _owner_org(self) -> str:
        from app.rbac import seed_owner

        org = f"org_t5_{uuid.uuid4().hex[:10]}"
        seed_owner(org, DEV_USER)
        return org

    def _connect(self, client, org, cid, status="connected"):
        return client.post(
            f"/api/connectors/{cid}/connect",
            json={} if status == "connected" else {"status": status},
            headers={**AUTH, "X-Org-Id": org},
        )

    def test_connecting_sap_is_refused_with_named_reason(self, client: TestClient):
        org = self._owner_org()
        _seed_catalog(["sap"])
        resp = self._connect(client, org, "sap")
        assert resp.status_code == 409, resp.text
        detail = resp.json().get("detail", "")
        # Names the target release + why it is refused (the exact SAP/D365 display
        # name is asserted name-for-name in the block-message unit test above).
        assert "2.0.1" in detail and "roadmap" in detail.lower()

    def test_connecting_dynamics365_is_refused(self, client: TestClient):
        org = self._owner_org()
        _seed_catalog(["dynamics365"])
        resp = self._connect(client, org, "dynamics365")
        assert resp.status_code == 409, resp.text

    def test_connecting_roadmap_connector_absent_from_catalog_still_409(
        self, client: TestClient
    ):
        """Regression (review finding): a roadmap tile that is NOT pre-seeded in the
        org's connector catalog must STILL be refused with 409 — the roadmap guard
        runs before the catalog lookup, so a missing catalog row can never fall
        through to a 404 and silently bypass the non-connectable block."""
        org = self._owner_org()
        _seed_catalog(["github"])  # 'sap' intentionally absent from the catalog
        resp = self._connect(client, org, "sap")
        assert resp.status_code == 409, resp.text
        assert "roadmap" in resp.json().get("detail", "").lower()

    def test_shipped_connector_still_connects(self, client: TestClient):
        org = self._owner_org()
        _seed_catalog(["github"])
        resp = self._connect(client, org, "github")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "connected"
