"""Contract tests for T3-S13-A — T7.

RelationshipSummary surfaced on OppEnrichment, populated observed-only by
default and including inferred edges only when INFERRED_RELATIONSHIPS_ENABLED.

Coverage:
  - OppEnrichment.relationships always present, defaults to [].
  - RelationshipSummary carries all seven required fields.
  - Population path (_load_relationship_summaries) is org-scoped and flag-gated.
  - Endpoint response: observed edges appear by default; inferred edges appear
    only when the flag is enabled (the inferred=True flag is preserved for the
    UI '[inferred]' label).
"""
import os
import sqlite3
from typing import Dict
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.graph_query import RelationshipSummary
from app.relationship_mapper import upsert_relationship
from database.models.entities import Entity
from database.models.entity_relationships import INFERRED_CONFIDENCE, OBSERVED_CONFIDENCE


REQUIRED_FIELDS = {
    "from_entity_name", "from_entity_type", "relationship_type",
    "to_entity_name", "to_entity_type", "inferred", "confidence",
}


def _auth(org_id: str = "default") -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}",
        "X-Org-Id": org_id,
    }


def _seed_workspace_member(org_id: str) -> None:
    with sqlite3.connect(os.environ.get("DB_PATH", "")) as conn:
        conn.execute(
            """
            INSERT INTO workspace_members (org_id, user_id, role, created_at)
            VALUES (%s, %s, 'owner', %s)
            ON CONFLICT (org_id, user_id) DO NOTHING
            """,
            (org_id, os.getenv("DEV_JWT", "dev-token-change-me"), "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()


def _insert_entity(
    org_id: str,
    entity_type: str,
    display_name: str,
    run_id: str,
    *,
    source_record_id: str | None = None,
    source_system: str = "test",
) -> str:
    entity = Entity(
        org_id=org_id,
        entity_type=entity_type,
        canonical_name=" ".join(display_name.split()).lower() + "-" + uuid4().hex[:6],
        display_name=display_name,
        source_system=source_system,
        source_record_id=source_record_id,
        resolution_confidence=1.0,
        resolution_status="resolved",
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=1,
    )
    row = entity.to_db_row()
    with sqlite3.connect(os.environ.get("DB_PATH", "")) as conn:
        conn.execute(
            """INSERT INTO entities (
                id, org_id, entity_type, canonical_name, display_name,
                source_system, source_record_id, resolution_confidence,
                resolution_status, first_seen_run_id, last_seen_run_id,
                run_count, metadata, created_at, updated_at
            ) VALUES (
                %(id)s, %(org_id)s, %(entity_type)s, %(canonical_name)s, %(display_name)s,
                %(source_system)s, %(source_record_id)s, %(resolution_confidence)s,
                %(resolution_status)s, %(first_seen_run_id)s, %(last_seen_run_id)s,
                %(run_count)s, %(metadata)s, %(created_at)s, %(updated_at)s
            )""",
            row,
        )
        conn.commit()
    return row["id"]


def _seed_run(org_id: str, run_id: str) -> None:
    db.run_set(run_id, {"id": run_id, "runId": run_id, "status": "complete", "org_id": org_id})


def _seed_observed(org_id: str, run_id: str) -> None:
    _seed_run(org_id, run_id)
    f = _insert_entity(org_id, "person", "Sarah Chen", run_id)
    t = _insert_entity(org_id, "object", "LOAN-001", run_id)
    upsert_relationship(
        org_id=org_id, from_entity_id=f, to_entity_id=t, relationship_type="owns",
        confidence=OBSERVED_CONFIDENCE, inferred=False, run_id=run_id,
        evidence={"field": "OwnerId"},
    )


def _seed_inferred(org_id: str, run_id: str) -> None:
    _seed_run(org_id, run_id)
    f = _insert_entity(org_id, "process", "Covenant Review", run_id)
    t = _insert_entity(org_id, "process", "Loan Origination", run_id)
    upsert_relationship(
        org_id=org_id, from_entity_id=f, to_entity_id=t, relationship_type="depends_on",
        confidence=INFERRED_CONFIDENCE, inferred=True, run_id=run_id,
        evidence={"note": "Validate with Stage 3 causal analysis before treating as truth"},
    )


# ---------------------------------------------------------------------------
# Model shape
# ---------------------------------------------------------------------------

class TestRelationshipSummaryShape:
    def test_has_all_seven_fields(self):
        rel = RelationshipSummary(
            from_entity_name="Covenant Review",
            from_entity_type="process",
            relationship_type="depends_on",
            to_entity_name="Loan Origination",
            to_entity_type="process",
            inferred=True,
            confidence=0.6,
        )
        assert set(rel.model_dump().keys()) == REQUIRED_FIELDS

    def test_oppenrichment_relationships_defaults_empty(self):
        from app.routes_sprint4_t6 import OppEnrichment
        enr = OppEnrichment(oppId="opp-1")
        assert enr.relationships == []

    def test_oppenrichment_serialises_relationships(self):
        from app.routes_sprint4_t6 import OppEnrichment
        rel = RelationshipSummary(
            from_entity_name="A", from_entity_type="process",
            relationship_type="depends_on", to_entity_name="B",
            to_entity_type="process", inferred=True, confidence=0.6,
        )
        dumped = OppEnrichment(oppId="opp-1", relationships=[rel]).model_dump()
        assert dumped["relationships"][0]["inferred"] is True
        assert set(dumped["relationships"][0].keys()) == REQUIRED_FIELDS


# ---------------------------------------------------------------------------
# Population path — _load_relationship_summaries (feeds OppEnrichment.relationships)
# ---------------------------------------------------------------------------

class TestLoadRelationshipSummaries:
    def _load_with_org(self, org_id: str, run_id: str, opportunity=None):
        from app.middleware.tenancy import _current_org_id
        from app.routes_sprint4_t6 import _load_relationship_summaries
        token = _current_org_id.set(org_id)
        try:
            return _load_relationship_summaries(run_id, opportunity)
        finally:
            _current_org_id.reset(token)

    def test_default_returns_observed_only(self, monkeypatch):
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "false")
        org = f"org-t7-def-{uuid4().hex[:8]}"
        run = f"run-t7-def-{uuid4().hex[:6]}"
        _seed_observed(org, run)
        _seed_inferred(org, run)
        result = self._load_with_org(org, run)
        assert len(result) == 1
        assert result[0].inferred is False

    def test_flag_on_includes_inferred(self, monkeypatch):
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "true")
        org = f"org-t7-on-{uuid4().hex[:8]}"
        run = f"run-t7-on-{uuid4().hex[:6]}"
        _seed_observed(org, run)
        _seed_inferred(org, run)
        result = self._load_with_org(org, run)
        assert len(result) == 2
        assert any(r.inferred for r in result)

    def test_cross_org_run_id_returns_empty(self, monkeypatch):
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "true")
        org_a = f"org-t7-owner-{uuid4().hex[:8]}"
        org_b = f"org-t7-request-{uuid4().hex[:8]}"
        run = f"run-t7-cross-{uuid4().hex[:6]}"
        _seed_observed(org_a, run)
        _seed_inferred(org_a, run)

        assert self._load_with_org(org_b, run) == []

    def test_missing_org_context_returns_empty(self):
        from app.routes_sprint4_t6 import _load_relationship_summaries
        # No tenancy context set — must degrade to [] rather than raise.
        assert _load_relationship_summaries("run-no-org") == []

    def test_relationships_show_better_saved_names_for_same_source_ids(self, monkeypatch):
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "false")
        org = f"org-t7-display-{uuid4().hex[:8]}"
        run = f"run-t7-display-{uuid4().hex[:6]}"
        owner_id = "005WG00000ZkgMfYAJ"
        case_id = "500WG00000Case483"
        _seed_run(org, run)
        old_person = _insert_entity(
            org,
            "person",
            owner_id,
            run,
            source_record_id=owner_id,
            source_system="salesforce",
        )
        old_case = _insert_entity(
            org,
            "object",
            "00003483",
            run,
            source_record_id=case_id,
            source_system="salesforce",
        )
        _insert_entity(
            org,
            "person",
            "Rita Gomez",
            run,
            source_record_id=owner_id,
            source_system="salesforce",
        )
        _insert_entity(
            org,
            "object",
            "Customer onboarding blocked",
            run,
            source_record_id=case_id,
            source_system="salesforce",
        )
        upsert_relationship(
            org_id=org,
            from_entity_id=old_person,
            to_entity_id=old_case,
            relationship_type="owns",
            confidence=OBSERVED_CONFIDENCE,
            inferred=False,
            run_id=run,
            evidence={"field": "OwnerId"},
        )

        result = self._load_with_org(org, run)

        assert len(result) == 1
        assert result[0].from_entity_name == "Rita Gomez"
        assert result[0].to_entity_name == "Customer onboarding blocked"


# ---------------------------------------------------------------------------
# Endpoint response — observed by default, inferred only when flag enabled
# ---------------------------------------------------------------------------

def test_historical_relationship_is_not_shown_on_later_run(monkeypatch):
    from app.middleware.tenancy import _current_org_id
    from app.routes_sprint4_t6 import _load_relationship_summaries

    monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "true")
    org = f"org-t7-current-{uuid4().hex[:8]}"
    old_run = f"run-t7-old-{uuid4().hex[:6]}"
    new_run = f"run-t7-new-{uuid4().hex[:6]}"
    _seed_observed(org, old_run)
    _seed_run(org, new_run)

    token = _current_org_id.set(org)
    try:
        assert _load_relationship_summaries(new_run) == []
    finally:
        _current_org_id.reset(token)


def test_inferred_relationships_are_scoped_to_detector(monkeypatch):
    from app.middleware.tenancy import _current_org_id
    from app.routes_sprint4_t6 import _load_relationship_summaries

    monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "true")
    org = f"org-t7-det-{uuid4().hex[:8]}"
    run = f"run-t7-det-{uuid4().hex[:6]}"
    _seed_run(org, run)
    loan = _insert_entity(org, "process", "LOAN_ORIGINATION_ROUTING_FRICTION", run)
    checklist = _insert_entity(org, "process", "CHECKLIST_BOTTLENECK", run)
    covenant = _insert_entity(org, "process", "COVENANT_TRACKING_GAP", run)
    upsert_relationship(
        org_id=org,
        from_entity_id=checklist,
        to_entity_id=loan,
        relationship_type="depends_on",
        confidence=INFERRED_CONFIDENCE,
        inferred=True,
        run_id=run,
    )
    upsert_relationship(
        org_id=org,
        from_entity_id=covenant,
        to_entity_id=loan,
        relationship_type="depends_on",
        confidence=INFERRED_CONFIDENCE,
        inferred=True,
        run_id=run,
    )

    token = _current_org_id.set(org)
    try:
        result = _load_relationship_summaries(
            run,
            {"detector_id": "CHECKLIST_BOTTLENECK"},
        )
    finally:
        _current_org_id.reset(token)

    assert len(result) == 1
    assert result[0].from_entity_name == "CHECKLIST_BOTTLENECK"


class TestEnrichmentEndpointRelationships:
    @pytest.fixture(scope="class")
    def client(self):
        from app.main import app
        return TestClient(app)

    def _seed_run(self, org_id: str, run_id: str, opp_id: str) -> None:
        _seed_workspace_member(org_id)
        db.run_set(run_id, {"id": run_id, "runId": run_id, "status": "complete", "orgId": org_id})
        db.run_kv_set("opps", run_id, [{"id": opp_id, "aiRationale": "rationale"}])

    def test_observed_in_default_response(self, client, monkeypatch):
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "false")
        org = f"org-t7-ep-def-{uuid4().hex[:6]}"
        run = f"run-t7-ep-def-{uuid4().hex[:6]}"
        opp = "opp-1"
        self._seed_run(org, run, opp)
        _seed_observed(org, run)
        _seed_inferred(org, run)

        r = client.get(f"/api/runs/{run}/opportunities/{opp}/enrichment", headers=_auth(org))
        assert r.status_code == 200, r.text
        rels = r.json()["relationships"]
        assert len(rels) == 1
        assert rels[0]["inferred"] is False
        assert rels[0]["relationship_type"] == "owns"

    def test_inferred_only_when_flag_enabled(self, client, monkeypatch):
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "true")
        org = f"org-t7-ep-on-{uuid4().hex[:6]}"
        run = f"run-t7-ep-on-{uuid4().hex[:6]}"
        opp = "opp-1"
        self._seed_run(org, run, opp)
        _seed_observed(org, run)
        _seed_inferred(org, run)

        r = client.get(f"/api/runs/{run}/opportunities/{opp}/enrichment", headers=_auth(org))
        assert r.status_code == 200, r.text
        rels = r.json()["relationships"]
        assert len(rels) == 2
        inferred = [x for x in rels if x["inferred"]]
        assert len(inferred) == 1
        assert inferred[0]["confidence"] == 0.6

    def test_relationships_field_always_present(self, client, monkeypatch):
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "false")
        org = f"org-t7-ep-empty-{uuid4().hex[:6]}"
        run = f"run-t7-ep-empty-{uuid4().hex[:6]}"
        opp = "opp-1"
        self._seed_run(org, run, opp)  # no edges seeded

        r = client.get(f"/api/runs/{run}/opportunities/{opp}/enrichment", headers=_auth(org))
        assert r.status_code == 200, r.text
        assert r.json()["relationships"] == []
