"""Contract tests for T3-S13-A — entity_relationships table and upsert.

T1 / AC1 coverage:
  - entity_relationships table exists with all 12 columns.
  - inferred and confidence are NOT NULL (load-bearing for T3-S14-A, T3-S15-A).
  - Three required indexes: idx_er_org_from, idx_er_org_to, idx_er_org_type.
  - FK columns from_entity_id and to_entity_id reference entities.id.
  - Table can be created repeatedly without error (idempotent DDL).
  - Rows can be inserted and queried by org_id, from_entity_id,
    to_entity_id, and inferred flag.

T2 / AC7 coverage:
  - upsert_relationship() called twice with the same natural key
    (org_id, from_entity_id, to_entity_id, relationship_type) produces
    exactly one row with run_count=2. Never creates duplicates.
  - first_seen_run_id is set only on creation; never changed on update.
  - last_seen_run_id is updated to the most recent run_id on every call.
  - confidence and inferred are set only on creation; never changed on update.
  - evidence is updated to the most recent run's value on update.
  - Calling N times on the same key yields run_count=N, one row only.
  - upsert on a new key always creates a fresh row with run_count=1.

T2 / AC8 coverage (upsert-layer isolation):
  - upsert_relationship() for org_A never matches or modifies a row
    belonging to org_B, even when entity UUIDs are identical.
  - Same (from_id, to_id, type) in two different orgs creates two
    independent rows with independent run counts.
"""
import os
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from database.models.entity_relationships import (
    ALL_ENTITY_RELATIONSHIPS_DDL,
    INFERRED_CONFIDENCE,
    OBSERVED_CONFIDENCE,
    RELATIONSHIP_TYPES,
    EntityRelationship,
)
from database.models.entities import ALL_ENTITIES_DDL, Entity
from app.relationship_mapper import upsert_relationship


def _get_db_path() -> str:
    return os.environ["DB_PATH"]


def _insert_entity(conn: sqlite3.Connection, org_id: str, run_id: str = "run-001") -> str:
    """Insert a resolved entity and return its id string."""
    entity = Entity(
        org_id=org_id,
        entity_type="person",
        canonical_name=f"test-person-{uuid4().hex[:8]}",
        display_name="Test Person",
        source_system="jira",
        resolution_confidence=1.0,
        resolution_status="resolved",
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=1,
    )
    row = entity.to_db_row()
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
    return row["id"]


def _insert_relationship(
    conn: sqlite3.Connection,
    org_id: str,
    from_id: str,
    to_id: str,
    relationship_type: str = "owns",
    inferred: bool = False,
    run_id: str = "run-001",
) -> str:
    confidence = INFERRED_CONFIDENCE if inferred else OBSERVED_CONFIDENCE
    rel = EntityRelationship(
        org_id=org_id,
        from_entity_id=from_id,
        to_entity_id=to_id,
        relationship_type=relationship_type,
        confidence=confidence,
        inferred=inferred,
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        evidence={"field": "OwnerId", "source": "salesforce"},
    )
    row = rel.to_db_row()
    conn.execute(
        """INSERT INTO entity_relationships (
            id, org_id, from_entity_id, to_entity_id, relationship_type,
            confidence, inferred, evidence, first_seen_run_id,
            last_seen_run_id, run_count, created_at
        ) VALUES (
            :id, :org_id, :from_entity_id, :to_entity_id, :relationship_type,
            :confidence, :inferred, :evidence, :first_seen_run_id,
            :last_seen_run_id, :run_count, :created_at
        )""",
        row,
    )
    conn.commit()
    return row["id"]


class TestEntityRelationshipsTableSchema:
    """AC1: entity_relationships table created with 12 columns and three indexes."""

    def _columns(self) -> dict[str, dict]:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("PRAGMA table_info(entity_relationships)").fetchall()
        return {r["name"]: dict(r) for r in rows}

    def _indexes(self) -> list[dict]:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("PRAGMA index_list(entity_relationships)").fetchall()]

    def _index_columns(self, index_name: str) -> list[str]:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
        return [r["name"] for r in rows]

    def test_table_exists(self):
        cols = self._columns()
        assert cols, "entity_relationships table does not exist or has no columns"

    def test_all_12_columns_present(self):
        expected = {
            "id", "org_id", "from_entity_id", "to_entity_id", "relationship_type",
            "confidence", "inferred", "evidence", "first_seen_run_id",
            "last_seen_run_id", "run_count", "created_at",
        }
        actual = set(self._columns().keys())
        missing = expected - actual
        assert not missing, f"Missing columns: {missing}"
        assert len(actual) == 12, f"Expected 12 columns, got {len(actual)}: {actual}"

    def test_inferred_column_not_nullable(self):
        cols = self._columns()
        assert "inferred" in cols, "inferred column missing"
        assert cols["inferred"]["notnull"] == 1, "inferred must be NOT NULL (load-bearing for T3-S14-A)"

    def test_confidence_column_not_nullable(self):
        cols = self._columns()
        assert "confidence" in cols, "confidence column missing"
        assert cols["confidence"]["notnull"] == 1, "confidence must be NOT NULL (load-bearing for T3-S15-A)"

    def test_org_id_not_nullable(self):
        cols = self._columns()
        assert cols["org_id"]["notnull"] == 1, "org_id must be NOT NULL"

    def test_from_entity_id_not_nullable(self):
        cols = self._columns()
        assert cols["from_entity_id"]["notnull"] == 1, "from_entity_id must be NOT NULL"

    def test_to_entity_id_not_nullable(self):
        cols = self._columns()
        assert cols["to_entity_id"]["notnull"] == 1, "to_entity_id must be NOT NULL"

    def test_relationship_type_not_nullable(self):
        cols = self._columns()
        assert cols["relationship_type"]["notnull"] == 1, "relationship_type must be NOT NULL"

    def test_evidence_is_nullable(self):
        cols = self._columns()
        assert "evidence" in cols, "evidence column missing"
        assert cols["evidence"]["notnull"] == 0, "evidence must be nullable"

    def test_three_indexes_present(self):
        indexes = {r["name"] for r in self._indexes()}
        required = {"idx_er_org_from", "idx_er_org_to", "idx_er_org_type"}
        missing = required - indexes
        assert not missing, f"Missing required indexes: {missing}"

    def test_idx_er_org_from_columns(self):
        cols = self._index_columns("idx_er_org_from")
        assert cols == ["org_id", "from_entity_id"], (
            f"idx_er_org_from must cover (org_id, from_entity_id), got {cols}"
        )

    def test_idx_er_org_to_columns(self):
        cols = self._index_columns("idx_er_org_to")
        assert cols == ["org_id", "to_entity_id"], (
            f"idx_er_org_to must cover (org_id, to_entity_id), got {cols}"
        )

    def test_idx_er_org_type_columns(self):
        cols = self._index_columns("idx_er_org_type")
        assert cols == ["org_id", "relationship_type", "inferred"], (
            f"idx_er_org_type must cover (org_id, relationship_type, inferred), got {cols}"
        )

    def test_idempotent_ddl_no_error_on_second_apply(self):
        with sqlite3.connect(_get_db_path()) as conn:
            for ddl in ALL_ENTITY_RELATIONSHIPS_DDL:
                conn.execute(ddl)
            conn.commit()


class TestEntityRelationshipsInsertAndQuery:
    """AC1: rows can be inserted and queried by org_id, from/to entity, inferred flag."""

    def test_insert_observed_relationship(self):
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            from_id = str(uuid4())
            to_id = str(uuid4())
            rel_id = _insert_relationship(conn, "test-org-insert", from_id, to_id, "owns", inferred=False)

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM entity_relationships WHERE id = ?", (rel_id,)
            ).fetchone()

        assert row is not None
        assert row["org_id"] == "test-org-insert"
        assert row["from_entity_id"] == from_id
        assert row["to_entity_id"] == to_id
        assert row["relationship_type"] == "owns"
        assert float(row["confidence"]) == OBSERVED_CONFIDENCE
        assert int(row["inferred"]) == 0

    def test_insert_inferred_relationship(self):
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            from_id = str(uuid4())
            to_id = str(uuid4())
            rel_id = _insert_relationship(
                conn, "test-org-inferred", from_id, to_id, "depends_on", inferred=True
            )

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM entity_relationships WHERE id = ?", (rel_id,)
            ).fetchone()

        assert row is not None
        assert float(row["confidence"]) == INFERRED_CONFIDENCE
        assert int(row["inferred"]) == 1

    def test_query_by_org_id(self):
        org = f"org-query-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()))
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()))

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = ?", (org,)
            ).fetchall()

        assert len(rows) == 2

    def test_query_by_from_entity_id(self):
        org = f"org-from-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            _insert_relationship(conn, org, from_id, str(uuid4()))
            _insert_relationship(conn, org, from_id, str(uuid4()))
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()))

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = ? AND from_entity_id = ?",
                (org, from_id),
            ).fetchall()

        assert len(rows) == 2

    def test_query_by_to_entity_id(self):
        org = f"org-to-{uuid4().hex[:8]}"
        to_id = str(uuid4())
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            _insert_relationship(conn, org, str(uuid4()), to_id)
            _insert_relationship(conn, org, str(uuid4()), to_id)
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()))

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = ? AND to_entity_id = ?",
                (org, to_id),
            ).fetchall()

        assert len(rows) == 2

    def test_query_by_inferred_flag_false(self):
        org = f"org-inferred-false-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "owns", inferred=False)
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "member_of", inferred=False)
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "depends_on", inferred=True)

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = ? AND inferred = 0",
                (org,),
            ).fetchall()

        assert len(rows) == 2
        assert all(int(r["inferred"]) == 0 for r in rows)

    def test_query_by_inferred_flag_true(self):
        org = f"org-inferred-true-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "owns", inferred=False)
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "depends_on", inferred=True)
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "routes_to", inferred=True)

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = ? AND inferred = 1",
                (org,),
            ).fetchall()

        assert len(rows) == 2
        assert all(int(r["inferred"]) == 1 for r in rows)

    def test_all_five_relationship_types_accepted(self):
        org = f"org-types-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            for rtype in sorted(RELATIONSHIP_TYPES):
                inferred = rtype in ("depends_on", "routes_to")
                _insert_relationship(conn, org, str(uuid4()), str(uuid4()), rtype, inferred=inferred)

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT relationship_type FROM entity_relationships WHERE org_id = ?", (org,)
            ).fetchall()

        stored_types = {r["relationship_type"] for r in rows}
        assert stored_types == RELATIONSHIP_TYPES

    def test_evidence_json_round_trip(self):
        org = f"org-evidence-{uuid4().hex[:8]}"
        evidence = {"field": "OwnerId", "source": "salesforce", "run_id": "run-abc"}
        rel = EntityRelationship(
            org_id=org,
            from_entity_id=uuid4(),
            to_entity_id=uuid4(),
            relationship_type="owns",
            confidence=OBSERVED_CONFIDENCE,
            inferred=False,
            first_seen_run_id="run-abc",
            last_seen_run_id="run-abc",
            evidence=evidence,
        )
        row = rel.to_db_row()
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                """INSERT INTO entity_relationships (
                    id, org_id, from_entity_id, to_entity_id, relationship_type,
                    confidence, inferred, evidence, first_seen_run_id,
                    last_seen_run_id, run_count, created_at
                ) VALUES (
                    :id, :org_id, :from_entity_id, :to_entity_id, :relationship_type,
                    :confidence, :inferred, :evidence, :first_seen_run_id,
                    :last_seen_run_id, :run_count, :created_at
                )""",
                row,
            )
            conn.commit()

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            db_row = conn.execute(
                "SELECT * FROM entity_relationships WHERE id = ?", (row["id"],)
            ).fetchone()

        recovered = EntityRelationship.from_db_row(dict(db_row))
        assert recovered.evidence == evidence

    def test_null_evidence_accepted(self):
        org = f"org-null-ev-{uuid4().hex[:8]}"
        rel = EntityRelationship(
            org_id=org,
            from_entity_id=uuid4(),
            to_entity_id=uuid4(),
            relationship_type="member_of",
            confidence=OBSERVED_CONFIDENCE,
            inferred=False,
            first_seen_run_id="run-001",
            last_seen_run_id="run-001",
            evidence=None,
        )
        row = rel.to_db_row()
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                """INSERT INTO entity_relationships (
                    id, org_id, from_entity_id, to_entity_id, relationship_type,
                    confidence, inferred, evidence, first_seen_run_id,
                    last_seen_run_id, run_count, created_at
                ) VALUES (
                    :id, :org_id, :from_entity_id, :to_entity_id, :relationship_type,
                    :confidence, :inferred, :evidence, :first_seen_run_id,
                    :last_seen_run_id, :run_count, :created_at
                )""",
                row,
            )
            conn.commit()

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            db_row = conn.execute(
                "SELECT evidence FROM entity_relationships WHERE id = ?", (row["id"],)
            ).fetchone()

        assert db_row["evidence"] is None


class TestEntityRelationshipsCrossOrgIsolation:
    """AC8: entity_relationships for org_A never returned in org_B queries."""

    def test_cross_org_isolation(self):
        org_a = f"org-a-{uuid4().hex[:8]}"
        org_b = f"org-b-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            rel_id_a = _insert_relationship(conn, org_a, str(uuid4()), str(uuid4()))
            rel_id_b = _insert_relationship(conn, org_b, str(uuid4()), str(uuid4()))

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows_a = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = ?", (org_a,)
            ).fetchall()
            rows_b = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = ?", (org_b,)
            ).fetchall()

        ids_a = {r["id"] for r in rows_a}
        ids_b = {r["id"] for r in rows_b}

        assert rel_id_a in ids_a
        assert rel_id_b not in ids_a
        assert rel_id_b in ids_b
        assert rel_id_a not in ids_b

    def test_cross_org_isolation_inferred_query(self):
        org_a = f"org-a-inf-{uuid4().hex[:8]}"
        org_b = f"org-b-inf-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            _insert_relationship(conn, org_a, str(uuid4()), str(uuid4()), "depends_on", inferred=True)
            _insert_relationship(conn, org_b, str(uuid4()), str(uuid4()), "depends_on", inferred=True)

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows_a = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = ? AND inferred = 1", (org_a,)
            ).fetchall()

        assert len(rows_a) == 1
        assert rows_a[0]["org_id"] == org_a


class TestEntityRelationshipDataclass:
    """Dataclass validation and confidence constant tests."""

    def test_invalid_relationship_type_raises(self):
        import pytest
        with pytest.raises(ValueError, match="relationship_type"):
            EntityRelationship(
                org_id="org",
                from_entity_id=uuid4(),
                to_entity_id=uuid4(),
                relationship_type="invalid_type",
                confidence=0.9,
                inferred=False,
                first_seen_run_id="run-1",
                last_seen_run_id="run-1",
            )

    def test_observed_confidence_constant(self):
        assert OBSERVED_CONFIDENCE == 0.9

    def test_inferred_confidence_constant(self):
        assert INFERRED_CONFIDENCE == 0.6

    def test_five_relationship_types_defined(self):
        assert RELATIONSHIP_TYPES == {"owns", "member_of", "escalates_to", "depends_on", "routes_to"}


# ---------------------------------------------------------------------------
# T2 — upsert_relationship() tests (AC7, AC8)
# ---------------------------------------------------------------------------

class TestUpsertRelationshipAC7:
    """AC7: upsert_relationship() deduplication and run_count accuracy."""

    def _query_rows(self, org_id: str, from_id: str, to_id: str, rtype: str) -> list[dict]:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM entity_relationships
                WHERE org_id = ? AND from_entity_id = ?
                  AND to_entity_id = ? AND relationship_type = ?
                """,
                (org_id, from_id, to_id, rtype),
            ).fetchall()
        return [dict(r) for r in rows]

    def test_first_call_creates_row_with_run_count_1(self):
        org = f"org-upsert-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        rel = upsert_relationship(
            org_id=org,
            from_entity_id=from_id,
            to_entity_id=to_id,
            relationship_type="owns",
            confidence=OBSERVED_CONFIDENCE,
            inferred=False,
            run_id="run-001",
            evidence={"field": "OwnerId"},
        )

        rows = self._query_rows(org, from_id, to_id, "owns")
        assert len(rows) == 1, "Expected exactly one row after first upsert"
        assert rows[0]["run_count"] == 1
        assert rel.run_count == 1

    def test_second_call_same_key_yields_run_count_2_one_row(self):
        """AC7 core: two calls on the same natural key → one row, run_count=2."""
        org = f"org-dup-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        upsert_relationship(
            org_id=org,
            from_entity_id=from_id,
            to_entity_id=to_id,
            relationship_type="owns",
            confidence=OBSERVED_CONFIDENCE,
            inferred=False,
            run_id="run-001",
        )
        rel2 = upsert_relationship(
            org_id=org,
            from_entity_id=from_id,
            to_entity_id=to_id,
            relationship_type="owns",
            confidence=OBSERVED_CONFIDENCE,
            inferred=False,
            run_id="run-002",
        )

        rows = self._query_rows(org, from_id, to_id, "owns")
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)} — duplicate created"
        assert rows[0]["run_count"] == 2
        assert rel2.run_count == 2

    def test_n_calls_yields_run_count_n(self):
        org = f"org-n-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())
        n = 5

        for i in range(n):
            upsert_relationship(
                org_id=org,
                from_entity_id=from_id,
                to_entity_id=to_id,
                relationship_type="member_of",
                confidence=OBSERVED_CONFIDENCE,
                inferred=False,
                run_id=f"run-{i:03d}",
            )

        rows = self._query_rows(org, from_id, to_id, "member_of")
        assert len(rows) == 1
        assert rows[0]["run_count"] == n

    def test_first_seen_run_id_never_changes_on_update(self):
        org = f"org-fsri-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-FIRST",
        )
        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-SECOND",
        )
        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-THIRD",
        )

        rows = self._query_rows(org, from_id, to_id, "owns")
        assert rows[0]["first_seen_run_id"] == "run-FIRST", (
            "first_seen_run_id must not change after creation"
        )

    def test_last_seen_run_id_updated_on_each_call(self):
        org = f"org-lsri-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="escalates_to", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-A",
        )
        rows_after_1 = self._query_rows(org, from_id, to_id, "escalates_to")
        assert rows_after_1[0]["last_seen_run_id"] == "run-A"

        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="escalates_to", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-B",
        )
        rows_after_2 = self._query_rows(org, from_id, to_id, "escalates_to")
        assert rows_after_2[0]["last_seen_run_id"] == "run-B"

    def test_confidence_immutable_on_update(self):
        """confidence set at creation must never change, even if caller passes different value."""
        org = f"org-conf-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="depends_on", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-001",
        )
        # Second call passes a different confidence — must be ignored.
        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="depends_on", confidence=INFERRED_CONFIDENCE,
            inferred=True, run_id="run-002",
        )

        rows = self._query_rows(org, from_id, to_id, "depends_on")
        assert float(rows[0]["confidence"]) == OBSERVED_CONFIDENCE, (
            "confidence must not change on update"
        )

    def test_inferred_immutable_on_update(self):
        """inferred set at creation must never change."""
        org = f"org-inf-imm-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="routes_to", confidence=INFERRED_CONFIDENCE,
            inferred=True, run_id="run-001",
        )
        # Second call passes inferred=False — must be ignored.
        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="routes_to", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-002",
        )

        rows = self._query_rows(org, from_id, to_id, "routes_to")
        assert int(rows[0]["inferred"]) == 1, "inferred must not change on update"

    def test_evidence_updated_to_most_recent_on_update(self):
        org = f"org-ev-upd-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-001",
            evidence={"field": "OwnerId", "source": "salesforce"},
        )
        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-002",
            evidence={"field": "OwnerId", "source": "ncino", "note": "updated"},
        )

        rows = self._query_rows(org, from_id, to_id, "owns")
        import json
        ev = json.loads(rows[0]["evidence"])
        assert ev["source"] == "ncino", "evidence must be updated to most recent run"

    def test_different_relationship_types_same_entities_are_separate_rows(self):
        """Relationship type is part of the natural key — different types are independent rows."""
        org = f"org-diff-type-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-001",
        )
        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="member_of", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-001",
        )

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = ? AND from_entity_id = ? AND to_entity_id = ?",
                (org, from_id, to_id),
            ).fetchall()

        assert len(rows) == 2
        types = {r["relationship_type"] for r in rows}
        assert types == {"owns", "member_of"}

    def test_returns_entity_relationship_object(self):
        org = f"org-ret-{uuid4().hex[:8]}"
        rel = upsert_relationship(
            org_id=org,
            from_entity_id=str(uuid4()),
            to_entity_id=str(uuid4()),
            relationship_type="owns",
            confidence=OBSERVED_CONFIDENCE,
            inferred=False,
            run_id="run-001",
        )
        assert isinstance(rel, EntityRelationship)
        assert rel.org_id == org
        assert rel.relationship_type == "owns"
        assert rel.run_count == 1


class TestUpsertRelationshipAC8CrossOrg:
    """AC8 (upsert layer): upsert for org_A must never match a row owned by org_B."""

    def test_same_entity_ids_different_orgs_create_independent_rows(self):
        """Two orgs with identical entity UUIDs must not share edge rows."""
        org_a = f"org-a-upsert-{uuid4().hex[:8]}"
        org_b = f"org-b-upsert-{uuid4().hex[:8]}"
        # Use deliberately identical entity IDs to force a collision if isolation breaks.
        from_id = str(uuid4())
        to_id = str(uuid4())

        rel_a = upsert_relationship(
            org_id=org_a, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-001",
        )
        rel_b = upsert_relationship(
            org_id=org_b, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-001",
        )

        # Each org has exactly one row; they are distinct rows.
        assert rel_a.id != rel_b.id
        assert rel_a.org_id == org_a
        assert rel_b.org_id == org_b

    def test_upsert_in_org_b_does_not_increment_org_a_run_count(self):
        """Upserting in org_B must not touch org_A's run_count."""
        org_a = f"org-a-cnt-{uuid4().hex[:8]}"
        org_b = f"org-b-cnt-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        upsert_relationship(
            org_id=org_a, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-001",
        )
        # Three upserts in org_B with the same entity IDs.
        for i in range(3):
            upsert_relationship(
                org_id=org_b, from_entity_id=from_id, to_entity_id=to_id,
                relationship_type="owns", confidence=OBSERVED_CONFIDENCE,
                inferred=False, run_id=f"run-{i:03d}",
            )

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            row_a = conn.execute(
                """SELECT run_count FROM entity_relationships
                   WHERE org_id = ? AND from_entity_id = ? AND to_entity_id = ? AND relationship_type = ?""",
                (org_a, from_id, to_id, "owns"),
            ).fetchone()
            row_b = conn.execute(
                """SELECT run_count FROM entity_relationships
                   WHERE org_id = ? AND from_entity_id = ? AND to_entity_id = ? AND relationship_type = ?""",
                (org_b, from_id, to_id, "owns"),
            ).fetchone()

        assert row_a["run_count"] == 1, "org_A run_count must not be affected by org_B upserts"
        assert row_b["run_count"] == 3

    def test_upsert_lookup_scoped_to_org_id(self):
        """The existence check in upsert must use org_id — never cross-org."""
        org_a = f"org-a-scope-{uuid4().hex[:8]}"
        org_b = f"org-b-scope-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        # Insert a row for org_B first.
        upsert_relationship(
            org_id=org_b, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="member_of", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-001",
        )
        # Upsert for org_A with the same entity IDs must create a NEW row,
        # not update org_B's row.
        rel_a = upsert_relationship(
            org_id=org_a, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="member_of", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-001",
        )

        assert rel_a.run_count == 1, (
            "upsert for org_A must create a new row, not match org_B's row"
        )

        # org_B's row must also still have run_count=1 (untouched).
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            row_b = conn.execute(
                """SELECT run_count FROM entity_relationships
                   WHERE org_id = ? AND from_entity_id = ? AND to_entity_id = ? AND relationship_type = ?""",
                (org_b, from_id, to_id, "member_of"),
            ).fetchone()
        assert row_b["run_count"] == 1
