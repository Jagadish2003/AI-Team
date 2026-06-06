"""Contract tests for T3-S12-A — Entity Extraction from Ingestor Runs.

T1 coverage (this file):
  AC1 — entities table exists with all 15 columns including
         resolution_confidence and resolution_status.

Remaining ACs (AC2–AC12) are covered by T2–T8 stories.
"""
import sqlite3
import os
import pytest


def _get_db_path() -> str:
    return os.environ["DB_PATH"]


# ---------------------------------------------------------------------------
# AC1 — entities table schema
# ---------------------------------------------------------------------------

class TestEntitiesTableSchema:
    """AC1: entities table created with all 15 columns including
    resolution_confidence and resolution_status."""

    def _columns(self) -> dict[str, dict]:
        """Return {column_name: pragma_row} for the entities table."""
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("PRAGMA table_info(entities)").fetchall()
        return {r["name"]: dict(r) for r in rows}

    def _indexes(self) -> list[dict]:
        """Return index pragma rows for the entities table."""
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("PRAGMA index_list(entities)").fetchall()]

    def test_table_exists(self):
        cols = self._columns()
        assert cols, "entities table does not exist or has no columns"

    def test_all_15_columns_present(self):
        expected = {
            "id", "org_id", "entity_type", "canonical_name", "display_name",
            "source_system", "source_record_id", "resolution_confidence",
            "resolution_status", "first_seen_run_id", "last_seen_run_id",
            "run_count", "metadata", "created_at", "updated_at",
        }
        actual = set(self._columns().keys())
        missing = expected - actual
        assert not missing, f"Missing columns: {missing}"
        assert len(actual) == 15, f"Expected 15 columns, got {len(actual)}: {actual}"

    def test_resolution_confidence_column_present_and_not_nullable(self):
        cols = self._columns()
        assert "resolution_confidence" in cols, "resolution_confidence column missing"
        assert cols["resolution_confidence"]["notnull"] == 1, (
            "resolution_confidence must be NOT NULL"
        )

    def test_resolution_status_column_present_and_not_nullable(self):
        cols = self._columns()
        assert "resolution_status" in cols, "resolution_status column missing"
        assert cols["resolution_status"]["notnull"] == 1, (
            "resolution_status must be NOT NULL"
        )

    def test_id_is_primary_key(self):
        cols = self._columns()
        assert cols["id"]["pk"] == 1, "id must be the primary key"

    def test_mandatory_not_null_columns(self):
        not_null_expected = {
            "id", "org_id", "entity_type", "canonical_name", "display_name",
            "source_system", "resolution_confidence", "resolution_status",
            "first_seen_run_id", "last_seen_run_id", "run_count",
            "created_at", "updated_at",
        }
        cols = self._columns()
        for col in not_null_expected:
            assert cols[col]["notnull"] == 1, f"{col} must be NOT NULL"

    def test_nullable_columns(self):
        # source_record_id and metadata are nullable — derived entities have no record ID
        cols = self._columns()
        assert cols["source_record_id"]["notnull"] == 0, "source_record_id must be nullable"
        assert cols["metadata"]["notnull"] == 0, "metadata must be nullable"

    def test_three_indexes_exist(self):
        indexes = self._indexes()
        # Exclude the implicit primary key index
        non_pk = [i for i in indexes if i["origin"] != "pk"]
        assert len(non_pk) == 3, (
            f"Expected 3 indexes on entities, got {len(non_pk)}: "
            f"{[i['name'] for i in non_pk]}"
        )

    def test_canonical_name_index_exists(self):
        indexes = self._indexes()
        names = {i["name"] for i in indexes}
        assert "idx_entities_org_canonical" in names, (
            "idx_entities_org_canonical index missing — required for resolution lookups"
        )

    def test_org_type_index_exists(self):
        indexes = self._indexes()
        names = {i["name"] for i in indexes}
        assert "idx_entities_org_type" in names, (
            "idx_entities_org_type index missing — required for scoped entity listing"
        )

    def test_org_run_index_exists(self):
        indexes = self._indexes()
        names = {i["name"] for i in indexes}
        assert "idx_entities_org_run" in names, (
            "idx_entities_org_run index missing — required for run-scoped entity queries"
        )

    def test_insert_and_read_round_trip(self):
        """Smoke: a valid entity row can be inserted and retrieved."""
        from database.models.entities import Entity
        entity = Entity(
            org_id="test-org",
            entity_type="person",
            canonical_name="sarah chen",
            display_name="Sarah Chen",
            source_system="jira",
            resolution_confidence=0.8,
            resolution_status="resolved",
            first_seen_run_id="run-001",
            last_seen_run_id="run-001",
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
                    :id, :org_id, :entity_type, :canonical_name, :display_name,
                    :source_system, :source_record_id, :resolution_confidence,
                    :resolution_status, :first_seen_run_id, :last_seen_run_id,
                    :run_count, :metadata, :created_at, :updated_at
                )""",
                row,
            )
            conn.commit()
            conn.row_factory = sqlite3.Row
            fetched = dict(conn.execute(
                "SELECT * FROM entities WHERE id = ?", (row["id"],)
            ).fetchone())

        assert fetched["org_id"] == "test-org"
        assert fetched["resolution_confidence"] == 0.8
        assert fetched["resolution_status"] == "resolved"
        assert fetched["canonical_name"] == "sarah chen"
        assert fetched["run_count"] == 1
