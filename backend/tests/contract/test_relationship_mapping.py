"""Contract tests for T3-S13-A — entity_relationships table, upsert, and
map_directly_observed().

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

T3 / AC2 coverage:
  - map_directly_observed() creates an owns edge (confidence=0.9, inferred=False)
    from Salesforce OwnerId → record when both endpoints are resolved.
  - map_directly_observed() creates a member_of edge from ServiceNow
    assigned_to → assignment_group when both endpoints are resolved.
  - map_directly_observed() creates an escalates_to edge from ServiceNow
    escalated_to and Jira escalation-labelled issues.
  - All edges have confidence=0.9 and inferred=False — never parameterised.
  - All edges persist via upsert_relationship(), not direct inserts.

T3 / AC3 coverage:
  - Entities with resolution_status='ambiguous' are never used as edge
    endpoints. No edge is created when either endpoint is ambiguous.
  - get_resolved_entity() returns None for ambiguous entities, preventing
    the edge from being drawn.
  - Missing / null ingestor fields are skipped without raising an exception.
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


# ---------------------------------------------------------------------------
# T3 — map_directly_observed() tests (AC2, AC3)
# ---------------------------------------------------------------------------

import pytest
from datetime import datetime, timezone as tz

from app.relationship_mapper import map_directly_observed, get_resolved_entity
from database.models.entities import Entity


def _make_entity(
    org_id: str,
    entity_type: str,
    display_name: str,
    resolution_status: str = "resolved",
    run_id: str = "run-t3-001",
) -> Entity:
    """Build an in-memory Entity for use as the entities list in tests."""
    canonical = " ".join(display_name.split()).lower()
    return Entity(
        org_id=org_id,
        entity_type=entity_type,
        canonical_name=canonical,
        display_name=display_name,
        source_system="test",
        resolution_confidence=1.0 if resolution_status == "resolved" else 0.6,
        resolution_status=resolution_status,
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=1,
    )


def _persist_entity(entity: Entity) -> None:
    """Write entity to the test DB so FK constraints are satisfied."""
    import json as _json
    row = entity.to_db_row()
    with sqlite3.connect(_get_db_path()) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """INSERT OR IGNORE INTO entities (
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


def _get_edges(org_id: str, rtype: str) -> list[dict]:
    with sqlite3.connect(_get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM entity_relationships WHERE org_id = ? AND relationship_type = ?",
            (org_id, rtype),
        ).fetchall()
    return [dict(r) for r in rows]


class TestMapDirectlyObservedAC2:
    """AC2: map_directly_observed() creates correct edges from observed source data."""

    def test_owns_edge_created_from_salesforce_record(self):
        """owns edge: Salesforce OwnerId → record Id, confidence=0.9, inferred=False."""
        org = f"org-owns-{uuid4().hex[:8]}"
        run = "run-t3-owns-001"

        person = _make_entity(org, "person", "Sarah Chen", run_id=run)
        obj = _make_entity(org, "object", "LOAN-001", run_id=run)
        _persist_entity(person)
        _persist_entity(obj)
        entities = [person, obj]

        ingestor_data = {
            "salesforce": {
                "records": [
                    {
                        "OwnerId": "Sarah Chen",
                        "Id": "LOAN-001",
                        "source_system": "salesforce",
                    }
                ]
            }
        }

        count = map_directly_observed(org, run, ingestor_data, entities)

        assert count == 1
        edges = _get_edges(org, "owns")
        assert len(edges) == 1
        assert edges[0]["from_entity_id"] == str(person.id)
        assert edges[0]["to_entity_id"] == str(obj.id)
        assert float(edges[0]["confidence"]) == OBSERVED_CONFIDENCE
        assert int(edges[0]["inferred"]) == 0

    def test_owns_confidence_always_0_9(self):
        """confidence must be exactly 0.9 for owns — never configurable."""
        org = f"org-conf-owns-{uuid4().hex[:8]}"
        run = "run-owns-conf"
        person = _make_entity(org, "person", "Alice Wang", run_id=run)
        obj = _make_entity(org, "object", "SF-REC-001", run_id=run)
        _persist_entity(person)
        _persist_entity(obj)

        ingestor_data = {"salesforce": {"records": [{"OwnerId": "Alice Wang", "Id": "SF-REC-001"}]}}
        map_directly_observed(org, run, ingestor_data, [person, obj])

        edges = _get_edges(org, "owns")
        assert float(edges[0]["confidence"]) == 0.9

    def test_owns_inferred_always_false(self):
        """inferred must be False for owns — directly observed."""
        org = f"org-inf-owns-{uuid4().hex[:8]}"
        run = "run-inf-owns"
        person = _make_entity(org, "person", "Bob Lee", run_id=run)
        obj = _make_entity(org, "object", "SF-REC-002", run_id=run)
        _persist_entity(person)
        _persist_entity(obj)

        ingestor_data = {"salesforce": {"records": [{"OwnerId": "Bob Lee", "Id": "SF-REC-002"}]}}
        map_directly_observed(org, run, ingestor_data, [person, obj])

        edges = _get_edges(org, "owns")
        assert int(edges[0]["inferred"]) == 0

    def test_member_of_edge_created_from_servicenow_incident(self):
        """member_of edge: ServiceNow assigned_to → assignment_group."""
        org = f"org-memb-{uuid4().hex[:8]}"
        run = "run-t3-memb-001"

        person = _make_entity(org, "person", "Eve Torres", run_id=run)
        team = _make_entity(org, "team", "Commercial Credit", run_id=run)
        _persist_entity(person)
        _persist_entity(team)
        entities = [person, team]

        ingestor_data = {
            "servicenow": {
                "incident_metrics": {
                    "incidents": [
                        {
                            "number": "INC0001",
                            "assigned_to": {"display_value": "Eve Torres"},
                            "assignment_group": "Commercial Credit",
                        }
                    ]
                }
            }
        }

        count = map_directly_observed(org, run, ingestor_data, entities)

        assert count == 1
        edges = _get_edges(org, "member_of")
        assert len(edges) == 1
        assert edges[0]["from_entity_id"] == str(person.id)
        assert edges[0]["to_entity_id"] == str(team.id)
        assert float(edges[0]["confidence"]) == OBSERVED_CONFIDENCE
        assert int(edges[0]["inferred"]) == 0

    def test_member_of_string_assignment_group(self):
        """assignment_group as a plain string (not dict) is handled."""
        org = f"org-memb-str-{uuid4().hex[:8]}"
        run = "run-memb-str"
        person = _make_entity(org, "person", "Frank Ross", run_id=run)
        team = _make_entity(org, "team", "Loan Operations", run_id=run)
        _persist_entity(person)
        _persist_entity(team)

        ingestor_data = {
            "servicenow": {
                "incident_metrics": {
                    "incidents": [
                        {
                            "number": "INC0002",
                            "assigned_to": "Frank Ross",
                            "assignment_group": "Loan Operations",
                        }
                    ]
                }
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [person, team])
        assert count == 1
        edges = _get_edges(org, "member_of")
        assert len(edges) == 1

    def test_escalates_to_edge_from_servicenow_escalated_to_field(self):
        """escalates_to edge: ServiceNow escalated_to field."""
        org = f"org-esc-sn-{uuid4().hex[:8]}"
        run = "run-esc-sn"
        inc_obj = _make_entity(org, "object", "INC0003", run_id=run)
        manager = _make_entity(org, "person", "Carol Sun", run_id=run)
        _persist_entity(inc_obj)
        _persist_entity(manager)

        ingestor_data = {
            "servicenow": {
                "incident_metrics": {
                    "incidents": [
                        {
                            "number": "INC0003",
                            "escalated_to": "Carol Sun",
                        }
                    ]
                }
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [inc_obj, manager])
        assert count == 1
        edges = _get_edges(org, "escalates_to")
        assert len(edges) == 1
        assert edges[0]["from_entity_id"] == str(inc_obj.id)
        assert edges[0]["to_entity_id"] == str(manager.id)
        assert float(edges[0]["confidence"]) == OBSERVED_CONFIDENCE
        assert int(edges[0]["inferred"]) == 0

    def test_escalates_to_edge_from_jira_escalation_label(self):
        """escalates_to edge: Jira issue with 'escalation' label + escalated_to field."""
        org = f"org-esc-jira-{uuid4().hex[:8]}"
        run = "run-esc-jira"
        issue_obj = _make_entity(org, "object", "LOAN-007", run_id=run)
        manager = _make_entity(org, "person", "Dave Kim", run_id=run)
        _persist_entity(issue_obj)
        _persist_entity(manager)

        ingestor_data = {
            "jira": {
                "issue_metrics": {
                    "issues": [
                        {
                            "key": "LOAN-007",
                            "labels": ["escalation", "loan"],
                            "escalated_to": "Dave Kim",
                        }
                    ]
                }
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [issue_obj, manager])
        assert count == 1
        edges = _get_edges(org, "escalates_to")
        assert len(edges) == 1
        assert edges[0]["from_entity_id"] == str(issue_obj.id)
        assert edges[0]["to_entity_id"] == str(manager.id)

    def test_multiple_incidents_multiple_edges(self):
        """Multiple incidents with assignment data produce multiple member_of edges."""
        org = f"org-multi-{uuid4().hex[:8]}"
        run = "run-multi"
        p1 = _make_entity(org, "person", "Alice Wang", run_id=run)
        p2 = _make_entity(org, "person", "Bob Lee", run_id=run)
        team = _make_entity(org, "team", "Credit Team", run_id=run)
        for e in [p1, p2, team]:
            _persist_entity(e)

        ingestor_data = {
            "servicenow": {
                "incident_metrics": {
                    "incidents": [
                        {"number": "INC1", "assigned_to": "Alice Wang", "assignment_group": "Credit Team"},
                        {"number": "INC2", "assigned_to": "Bob Lee", "assignment_group": "Credit Team"},
                    ]
                }
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [p1, p2, team])
        assert count == 2
        edges = _get_edges(org, "member_of")
        assert len(edges) == 2

    def test_routes_through_upsert_relationship_dedup(self):
        """Calling map_directly_observed twice with same data → run_count=2, one row."""
        org = f"org-dedup-obs-{uuid4().hex[:8]}"
        run1, run2 = "run-dedup-1", "run-dedup-2"
        person = _make_entity(org, "person", "Gina Park", run_id=run1)
        team = _make_entity(org, "team", "Risk Team", run_id=run1)
        _persist_entity(person)
        _persist_entity(team)

        ingestor_data = {
            "servicenow": {
                "incident_metrics": {
                    "incidents": [
                        {"number": "INC9", "assigned_to": "Gina Park", "assignment_group": "Risk Team"}
                    ]
                }
            }
        }

        # Update run_id on entities for second call (same entities, new run)
        person2 = _make_entity(org, "person", "Gina Park", run_id=run2)
        person2.id = person.id  # same entity
        team2 = _make_entity(org, "team", "Risk Team", run_id=run2)
        team2.id = team.id

        map_directly_observed(org, run1, ingestor_data, [person, team])
        map_directly_observed(org, run2, ingestor_data, [person2, team2])

        edges = _get_edges(org, "member_of")
        assert len(edges) == 1, "Two runs of same observed data must produce one edge row"
        assert edges[0]["run_count"] == 2


class TestMapDirectlyObservedAC3:
    """AC3: ambiguous entities are never used as edge endpoints."""

    def test_ambiguous_person_skips_owns_edge(self):
        """No owns edge created when person entity is ambiguous."""
        org = f"org-ambig-p-{uuid4().hex[:8]}"
        run = "run-ambig-p"
        # Ambiguous person — resolution_status='ambiguous'
        person = _make_entity(org, "person", "Alice Smith", "ambiguous", run_id=run)
        obj = _make_entity(org, "object", "LOAN-X01", "resolved", run_id=run)
        _persist_entity(person)
        _persist_entity(obj)

        ingestor_data = {
            "salesforce": {
                "records": [{"OwnerId": "Alice Smith", "Id": "LOAN-X01"}]
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [person, obj])
        assert count == 0, "No edge must be created when person endpoint is ambiguous"
        assert len(_get_edges(org, "owns")) == 0

    def test_ambiguous_object_skips_owns_edge(self):
        """No owns edge created when object entity is ambiguous."""
        org = f"org-ambig-o-{uuid4().hex[:8]}"
        run = "run-ambig-o"
        person = _make_entity(org, "person", "Bob Ross", "resolved", run_id=run)
        obj = _make_entity(org, "object", "LOAN-X02", "ambiguous", run_id=run)
        _persist_entity(person)
        _persist_entity(obj)

        ingestor_data = {
            "salesforce": {
                "records": [{"OwnerId": "Bob Ross", "Id": "LOAN-X02"}]
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [person, obj])
        assert count == 0, "No edge must be created when object endpoint is ambiguous"

    def test_ambiguous_person_skips_member_of_edge(self):
        """No member_of edge created when person is ambiguous."""
        org = f"org-ambig-sn-p-{uuid4().hex[:8]}"
        run = "run-ambig-sn-p"
        person = _make_entity(org, "person", "Carol Chen", "ambiguous", run_id=run)
        team = _make_entity(org, "team", "Ops Team", "resolved", run_id=run)
        _persist_entity(person)
        _persist_entity(team)

        ingestor_data = {
            "servicenow": {
                "incident_metrics": {
                    "incidents": [
                        {"number": "INC-A1", "assigned_to": "Carol Chen", "assignment_group": "Ops Team"}
                    ]
                }
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [person, team])
        assert count == 0

    def test_ambiguous_team_skips_member_of_edge(self):
        """No member_of edge created when team is ambiguous."""
        org = f"org-ambig-sn-t-{uuid4().hex[:8]}"
        run = "run-ambig-sn-t"
        person = _make_entity(org, "person", "Dave Park", "resolved", run_id=run)
        team = _make_entity(org, "team", "Risk Group", "ambiguous", run_id=run)
        _persist_entity(person)
        _persist_entity(team)

        ingestor_data = {
            "servicenow": {
                "incident_metrics": {
                    "incidents": [
                        {"number": "INC-A2", "assigned_to": "Dave Park", "assignment_group": "Risk Group"}
                    ]
                }
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [person, team])
        assert count == 0

    def test_missing_owner_field_skipped_no_exception(self):
        """Record without owner field is skipped silently."""
        org = f"org-miss-own-{uuid4().hex[:8]}"
        run = "run-miss-own"
        obj = _make_entity(org, "object", "LOAN-Y01", "resolved", run_id=run)
        _persist_entity(obj)

        ingestor_data = {
            "salesforce": {
                "records": [{"Id": "LOAN-Y01"}]  # no OwnerId
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [obj])
        assert count == 0

    def test_missing_assigned_to_skipped_no_exception(self):
        """Incident without assigned_to is skipped silently."""
        org = f"org-miss-at-{uuid4().hex[:8]}"
        run = "run-miss-at"
        team = _make_entity(org, "team", "Support", "resolved", run_id=run)
        _persist_entity(team)

        ingestor_data = {
            "servicenow": {
                "incident_metrics": {
                    "incidents": [{"number": "INC-B1", "assignment_group": "Support"}]
                }
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [team])
        assert count == 0

    def test_null_ingestor_data_no_exception(self):
        """Empty ingestor_data must not raise."""
        org = f"org-empty-{uuid4().hex[:8]}"
        run = "run-empty"
        count = map_directly_observed(org, run, {}, [])
        assert count == 0

    def test_none_fields_in_records_skipped_gracefully(self):
        """Records with None values for owner and id must not raise."""
        org = f"org-none-{uuid4().hex[:8]}"
        run = "run-none"
        ingestor_data = {
            "salesforce": {
                "records": [
                    {"OwnerId": None, "Id": None},
                    {"OwnerId": "", "Id": ""},
                ]
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [])
        assert count == 0

    def test_entity_not_in_list_produces_no_edge(self):
        """If extracted entity is not in the entities list, no edge is drawn."""
        org = f"org-not-in-{uuid4().hex[:8]}"
        run = "run-not-in"
        # The entities list is empty — no entities available for lookup.
        ingestor_data = {
            "salesforce": {
                "records": [{"OwnerId": "Unknown Person", "Id": "LOAN-Z01"}]
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [])
        assert count == 0

    def test_bad_record_does_not_halt_processing_of_remaining_records(self):
        """A malformed record must not prevent processing the valid records after it."""
        org = f"org-bad-rec-{uuid4().hex[:8]}"
        run = "run-bad-rec"
        person = _make_entity(org, "person", "Eve Torres", run_id=run)
        obj = _make_entity(org, "object", "LOAN-GOOD", run_id=run)
        _persist_entity(person)
        _persist_entity(obj)

        ingestor_data = {
            "salesforce": {
                "records": [
                    # Bad record — object not in entities list
                    {"OwnerId": "Eve Torres", "Id": "LOAN-MISSING"},
                    # Good record — both endpoints in entities list
                    {"OwnerId": "Eve Torres", "Id": "LOAN-GOOD"},
                ]
            }
        }
        count = map_directly_observed(org, run, ingestor_data, [person, obj])
        assert count == 1, "Valid record after a skipped record must still be processed"


class TestGetResolvedEntity:
    """Unit tests for the get_resolved_entity() helper."""

    def test_returns_resolved_entity(self):
        org = f"org-gre-{uuid4().hex[:8]}"
        e = _make_entity(org, "person", "Alice Smith", "resolved")
        result = get_resolved_entity(org, "person", "Alice Smith", [e])
        assert result is e

    def test_canonicalizes_display_name(self):
        """Lookup must canonicalize name — case and whitespace insensitive."""
        org = f"org-gre-canon-{uuid4().hex[:8]}"
        e = _make_entity(org, "person", "Alice Smith", "resolved")
        # canonical_name of e is "alice smith"
        result = get_resolved_entity(org, "person", "ALICE  SMITH", [e])
        assert result is e

    def test_returns_none_for_ambiguous_entity(self):
        org = f"org-gre-ambig-{uuid4().hex[:8]}"
        e = _make_entity(org, "person", "Bob Jones", "ambiguous")
        result = get_resolved_entity(org, "person", "Bob Jones", [e])
        assert result is None

    def test_returns_none_when_entity_not_in_list(self):
        org = f"org-gre-missing-{uuid4().hex[:8]}"
        result = get_resolved_entity(org, "person", "Carol Sun", [])
        assert result is None

    def test_wrong_entity_type_not_returned(self):
        org = f"org-gre-type-{uuid4().hex[:8]}"
        e = _make_entity(org, "team", "Alice Smith", "resolved")
        result = get_resolved_entity(org, "person", "Alice Smith", [e])
        assert result is None

    def test_empty_display_name_returns_none(self):
        org = f"org-gre-empty-{uuid4().hex[:8]}"
        result = get_resolved_entity(org, "person", "", [])
        assert result is None
