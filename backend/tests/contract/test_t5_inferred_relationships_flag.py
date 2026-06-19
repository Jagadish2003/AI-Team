"""Contract tests for T3-S13-A — T5.

INFERRED_RELATIONSHIPS_ENABLED flag, the graph_query read functions, and the
flag-aware OppEnrichment population selector.

Coverage:
  Flag (Section 4):
    - Defaults to False when the env var is absent.
    - True only for 'true' (case-insensitive); any other value is False.
    - Read at call time (toggling the env var changes the result).

  get_observed_relationships() / get_all_relationships():
    - observed returns inferred=False edges only.
    - all returns both observed and inferred edges.
    - Both scope to org_id (AC8 cross-org isolation) and to the run.

  AC5: INFERRED_RELATIONSHIPS_ENABLED=False (default) → select_relationships()
       returns only observed edges (inferred=False). Inferred edges exist in
       the DB but are not returned.
  AC6: INFERRED_RELATIONSHIPS_ENABLED=True → select_relationships() includes
       inferred edges with inferred=True and confidence=0.6.

  OppEnrichment.relationships field shape.
"""
import os
import sqlite3
from uuid import uuid4

import pytest

from app import db
from app import config
from app.graph_query import (
    RelationshipSummary,
    get_all_relationships,
    get_observed_relationships,
    select_relationships,
)
from app.relationship_mapper import upsert_relationship
from database.models.entities import Entity
from database.models.entity_relationships import INFERRED_CONFIDENCE, OBSERVED_CONFIDENCE


def _get_db_path() -> str:
    return os.environ.get("DB_PATH", "")


def _seed_run(org_id: str, run_id: str) -> None:
    db.run_set(run_id, {"id": run_id, "runId": run_id, "status": "complete", "org_id": org_id})


def _insert_entity(
    org_id: str,
    entity_type: str,
    display_name: str,
    run_id: str = "run-t5",
) -> str:
    """Insert a resolved entity and return its id string."""
    entity = Entity(
        org_id=org_id,
        entity_type=entity_type,
        canonical_name=" ".join(display_name.split()).lower() + "-" + uuid4().hex[:6],
        display_name=display_name,
        source_system="test",
        resolution_confidence=1.0,
        resolution_status="resolved",
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=1,
    )
    row = entity.to_db_row()
    with sqlite3.connect(_get_db_path()) as conn:
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


def _seed_observed_edge(org_id: str, run_id: str, rtype: str = "owns") -> None:
    """Create one observed edge (inferred=False) between two resolved entities."""
    _seed_run(org_id, run_id)
    from_id = _insert_entity(org_id, "person", "Observed Person", run_id)
    to_id = _insert_entity(org_id, "object", "Observed Object", run_id)
    upsert_relationship(
        org_id=org_id, from_entity_id=from_id, to_entity_id=to_id,
        relationship_type=rtype, confidence=OBSERVED_CONFIDENCE,
        inferred=False, run_id=run_id, evidence={"field": "OwnerId"},
    )


def _seed_inferred_edge(org_id: str, run_id: str) -> None:
    """Create one inferred edge (inferred=True, confidence=0.6)."""
    _seed_run(org_id, run_id)
    from_id = _insert_entity(org_id, "process", "Covenant Process", run_id)
    to_id = _insert_entity(org_id, "process", "Loan Process", run_id)
    upsert_relationship(
        org_id=org_id, from_entity_id=from_id, to_entity_id=to_id,
        relationship_type="depends_on", confidence=INFERRED_CONFIDENCE,
        inferred=True, run_id=run_id,
        evidence={"note": "Validate with Stage 3 causal analysis before treating as truth"},
    )


# ---------------------------------------------------------------------------
# Flag behaviour (Section 4)
# ---------------------------------------------------------------------------

class TestInferredRelationshipsFlag:
    def test_defaults_to_false_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("INFERRED_RELATIONSHIPS_ENABLED", raising=False)
        assert config.inferred_relationships_enabled() is False

    def test_true_when_env_is_true(self, monkeypatch):
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "true")
        assert config.inferred_relationships_enabled() is True

    def test_true_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "TRUE")
        assert config.inferred_relationships_enabled() is True
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "True")
        assert config.inferred_relationships_enabled() is True

    def test_other_values_are_false(self, monkeypatch):
        for val in ("false", "0", "1", "yes", "on", "", "enabled"):
            monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", val)
            assert config.inferred_relationships_enabled() is False, val


# ---------------------------------------------------------------------------
# get_observed_relationships() / get_all_relationships()
# ---------------------------------------------------------------------------

class TestQueryFunctions:
    def test_observed_returns_only_observed_edges(self):
        org = f"org-obs-{uuid4().hex[:8]}"
        run = "run-obs"
        _seed_observed_edge(org, run)
        _seed_inferred_edge(org, run)

        observed = get_observed_relationships(org, run)
        assert len(observed) == 1
        assert observed[0].inferred is False
        assert observed[0].confidence == OBSERVED_CONFIDENCE

    def test_all_returns_observed_and_inferred(self):
        org = f"org-all-{uuid4().hex[:8]}"
        run = "run-all"
        _seed_observed_edge(org, run)
        _seed_inferred_edge(org, run)

        edges = get_all_relationships(org, run)
        assert len(edges) == 2
        assert {e.inferred for e in edges} == {True, False}

    def test_returns_relationship_summary_objects(self):
        org = f"org-shape-{uuid4().hex[:8]}"
        run = "run-shape"
        _seed_observed_edge(org, run)
        edges = get_observed_relationships(org, run)
        assert isinstance(edges[0], RelationshipSummary)
        assert edges[0].from_entity_type == "person"
        assert edges[0].to_entity_type == "object"
        assert edges[0].relationship_type == "owns"

    def test_observed_query_scoped_to_org(self):
        org_a = f"org-a-{uuid4().hex[:8]}"
        org_b = f"org-b-{uuid4().hex[:8]}"
        run_a = "run-iso-a"
        run_b = "run-iso-b"
        _seed_observed_edge(org_a, run_a)
        _seed_observed_edge(org_b, run_b)

        assert len(get_observed_relationships(org_a, run_a)) == 1
        assert len(get_observed_relationships(org_b, run_b)) == 1
        assert get_observed_relationships(org_a, run_b) == []

    def test_all_query_scoped_to_org(self):
        org_a = f"org-a-all-{uuid4().hex[:8]}"
        org_b = f"org-b-all-{uuid4().hex[:8]}"
        run_a = "run-iso-all-a"
        run_b = "run-iso-all-b"
        _seed_observed_edge(org_a, run_a)
        _seed_inferred_edge(org_a, run_a)
        _seed_observed_edge(org_b, run_b)

        assert len(get_all_relationships(org_a, run_a)) == 2
        assert len(get_all_relationships(org_b, run_b)) == 1
        assert get_all_relationships(org_b, run_a) == []

    def test_scoped_to_run(self):
        org = f"org-run-{uuid4().hex[:8]}"
        run_1 = f"run-scope-1-{uuid4().hex[:6]}"
        run_2 = f"run-scope-2-{uuid4().hex[:6]}"
        _seed_observed_edge(org, run_1)
        _seed_observed_edge(org, run_2)
        assert len(get_observed_relationships(org, run_1)) == 1
        assert len(get_observed_relationships(org, run_2)) == 2

    def test_relationship_visible_in_first_run_after_later_upsert(self):
        """Regression: last_seen_run_id moving forward must not erase run-1 history."""
        org = f"org-run-history-{uuid4().hex[:8]}"
        run_1 = f"run-history-1-{uuid4().hex[:6]}"
        run_2 = f"run-history-2-{uuid4().hex[:6]}"
        _seed_run(org, run_1)
        _seed_run(org, run_2)
        from_id = _insert_entity(org, "person", "History Person", run_1)
        to_id = _insert_entity(org, "object", "History Object", run_1)

        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id=run_1, evidence={"field": "OwnerId"},
        )
        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id=run_2, evidence={"field": "OwnerId"},
        )

        assert len(get_observed_relationships(org, run_1)) == 1
        assert len(get_observed_relationships(org, run_2)) == 1

    def test_observed_query_excludes_inferred_edge_without_python_bool_filter(self):
        org = f"org-bool-{uuid4().hex[:8]}"
        run = "run-bool-filter"
        _seed_inferred_edge(org, run)

        assert get_observed_relationships(org, run) == []

    def test_empty_graph_returns_empty_list(self):
        org = f"org-empty-{uuid4().hex[:8]}"
        assert get_observed_relationships(org, "run-x") == []
        assert get_all_relationships(org, "run-x") == []


# ---------------------------------------------------------------------------
# AC5 / AC6 — flag-aware selector
# ---------------------------------------------------------------------------

class TestSelectRelationshipsAC5AC6:
    def test_ac5_flag_off_returns_observed_only(self, monkeypatch):
        """AC5: default (flag off) → only inferred=False edges returned;
        inferred edges remain in the DB but are not surfaced."""
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "false")
        org = f"org-ac5-{uuid4().hex[:8]}"
        run = "run-ac5"
        _seed_observed_edge(org, run)
        _seed_inferred_edge(org, run)

        result = select_relationships(org, run)
        assert len(result) == 1
        assert all(e.inferred is False for e in result)
        # Inferred edge is still stored — get_all sees it.
        assert len(get_all_relationships(org, run)) == 2

    def test_ac5_default_without_env_is_observed_only(self, monkeypatch):
        monkeypatch.delenv("INFERRED_RELATIONSHIPS_ENABLED", raising=False)
        org = f"org-ac5d-{uuid4().hex[:8]}"
        run = "run-ac5d"
        _seed_observed_edge(org, run)
        _seed_inferred_edge(org, run)
        result = select_relationships(org, run)
        assert [e.inferred for e in result] == [False]

    def test_ac6_flag_on_includes_inferred(self, monkeypatch):
        """AC6: flag on → inferred edges included with inferred=True and
        confidence=0.6 preserved."""
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "true")
        org = f"org-ac6-{uuid4().hex[:8]}"
        run = "run-ac6"
        _seed_observed_edge(org, run)
        _seed_inferred_edge(org, run)

        result = select_relationships(org, run)
        assert len(result) == 2
        inferred_edges = [e for e in result if e.inferred]
        assert len(inferred_edges) == 1
        assert inferred_edges[0].confidence == 0.6
        assert inferred_edges[0].relationship_type == "depends_on"

    def test_toggle_changes_result_same_process(self, monkeypatch):
        org = f"org-toggle-{uuid4().hex[:8]}"
        run = "run-toggle"
        _seed_observed_edge(org, run)
        _seed_inferred_edge(org, run)

        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "false")
        assert len(select_relationships(org, run)) == 1
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "true")
        assert len(select_relationships(org, run)) == 2

    def test_select_uses_callable_not_import_time_snapshot(self, monkeypatch):
        """Regression: env changes after import must affect select_relationships()."""
        monkeypatch.delenv("INFERRED_RELATIONSHIPS_ENABLED", raising=False)
        # Refresh the module-level snapshot; it may be stale if a previous test
        # in the session set the env var before this test cleared it.
        config.INFERRED_RELATIONSHIPS_ENABLED = config.inferred_relationships_enabled()
        assert config.INFERRED_RELATIONSHIPS_ENABLED is False
        org = f"org-calltime-{uuid4().hex[:8]}"
        run = f"run-calltime-{uuid4().hex[:6]}"
        _seed_observed_edge(org, run)
        _seed_inferred_edge(org, run)

        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "true")
        result = select_relationships(org, run)
        assert len(result) == 2
        assert any(edge.inferred for edge in result)


# ---------------------------------------------------------------------------
# OppEnrichment.relationships field
# ---------------------------------------------------------------------------

class TestOppEnrichmentRelationshipsField:
    def test_relationships_field_defaults_to_empty_list(self):
        from app.routes_sprint4_t6 import OppEnrichment
        enr = OppEnrichment(oppId="opp-1")
        assert enr.relationships == []

    def test_relationships_field_accepts_summaries(self):
        from app.routes_sprint4_t6 import OppEnrichment
        rel = RelationshipSummary(
            from_entity_name="Covenant Review",
            from_entity_type="process",
            relationship_type="depends_on",
            to_entity_name="Loan Origination",
            to_entity_type="process",
            inferred=True,
            confidence=0.6,
        )
        enr = OppEnrichment(oppId="opp-1", relationships=[rel])
        assert len(enr.relationships) == 1
        assert enr.relationships[0].inferred is True
        # Serialises with the inferred flag for the UI '[inferred]' label.
        assert enr.model_dump()["relationships"][0]["inferred"] is True
