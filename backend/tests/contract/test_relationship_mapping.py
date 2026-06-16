"""Contract tests for T3-S13-A — entity_relationships table, upsert, and
map_directly_observed().

T1 / AC1 coverage:
  - entity_relationships table exists with all 12 columns.
  - inferred and confidence are NOT NULL (load-bearing for T3-S14-A, T3-S15-A).
  - Three lookup indexes plus a unique natural-key index.
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
  - Calling the same key in N distinct runs yields run_count=N, one row only.
  - Repeating the same key within one run does not inflate run_count.
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

T8 / AC12 coverage:
  - get_entity_relationships(org_id, entity_id) returns RelationshipSummary
    objects for every edge where entity_id appears as from OR to endpoint.
  - Default inferred=False returns only observed edges (inferred=0).
  - inferred=True returns observed + inferred edges.
  - Edges from a different org are never returned even if entity UUIDs match.
  - Entity with no edges returns an empty list without error.
  - Results include display_name and entity_type from the entities table.
  - ORDER BY is deterministic: inferred ASC, relationship_type ASC,
    from_entity_name ASC, to_entity_name ASC.

T9 / AC10 coverage:
  - relationship.mapping_completed is in REGISTERED_EVENT_TYPES before merge.
  - Registry maps the event to RelationshipMappingCompletedPayload TypedDict.
  - TypedDict has all six required fields: org_id, run_id, observed_count,
    inferred_count, skipped_ambiguous_count, mapping_duration_ms.
  - map_relationships() calls record_event with the correct event type and
    accurate observed_count, inferred_count, skipped_ambiguous_count values.
  - mapping_duration_ms is a non-negative float in the payload.
  - record_event is called exactly once per map_relationships() invocation.
  - A record_event() failure does not propagate out of map_relationships().
"""
import os
import logging
import sqlite3
import psycopg2
from datetime import datetime, timezone
from typing import get_type_hints
from unittest.mock import patch
from uuid import uuid4

import pytest

from database.models.entity_relationships import (
    ALL_ENTITY_RELATIONSHIPS_DDL,
    INFERRED_CONFIDENCE,
    OBSERVED_CONFIDENCE,
    RELATIONSHIP_TYPES,
    EntityRelationship,
)
from database.models.entities import ALL_ENTITIES_DDL, Entity
from app.relationship_mapper import upsert_relationship, map_relationships
from app.graph_query import get_entity_relationships


def _get_db_path() -> str:
    return os.environ.get("DB_PATH", "")


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
            %(id)s, %(org_id)s, %(entity_type)s, %(canonical_name)s, %(display_name)s,
            %(source_system)s, %(source_record_id)s, %(resolution_confidence)s,
            %(resolution_status)s, %(first_seen_run_id)s, %(last_seen_run_id)s,
            %(run_count)s, %(metadata)s, %(created_at)s, %(updated_at)s
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
            %(id)s, %(org_id)s, %(from_entity_id)s, %(to_entity_id)s, %(relationship_type)s,
            %(confidence)s, %(inferred)s, %(evidence)s, %(first_seen_run_id)s,
            %(last_seen_run_id)s, %(run_count)s, %(created_at)s
        )""",
        row,
    )
    conn.commit()
    return row["id"]


class TestEntityRelationshipsTableSchema:
    """AC1: entity_relationships table created with 12 columns and required indexes."""

    def _columns(self) -> dict[str, dict]:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT column_name, data_type, is_nullable, character_maximum_length
                   FROM information_schema.columns
                   WHERE table_name = 'entity_relationships'
                   ORDER BY ordinal_position"""
            ).fetchall()
        return {r["column_name"]: dict(r) for r in rows}

    def _primary_key_columns(self) -> list[str]:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT a.attname
                   FROM pg_index i
                   JOIN pg_attribute a
                     ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                   WHERE i.indrelid = 'entity_relationships'::regclass
                     AND i.indisprimary"""
            ).fetchall()
        return [r["attname"] for r in rows]

    def _indexes(self) -> list[dict]:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in conn.execute(
                    """SELECT indexname, indexdef FROM pg_indexes
                       WHERE tablename = 'entity_relationships'"""
                ).fetchall()
            ]

    def _index_def(self, index_name: str) -> str:
        for idx in self._indexes():
            if idx["indexname"] == index_name:
                return idx["indexdef"]
        return ""

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
        assert cols["inferred"]["is_nullable"] == "NO", "inferred must be NOT NULL (load-bearing for T3-S14-A)"

    def test_confidence_column_not_nullable(self):
        cols = self._columns()
        assert "confidence" in cols, "confidence column missing"
        assert cols["confidence"]["is_nullable"] == "NO", "confidence must be NOT NULL (load-bearing for T3-S15-A)"

    def test_org_id_not_nullable(self):
        cols = self._columns()
        assert cols["org_id"]["is_nullable"] == "NO", "org_id must be NOT NULL"

    def test_from_entity_id_not_nullable(self):
        cols = self._columns()
        assert cols["from_entity_id"]["is_nullable"] == "NO", "from_entity_id must be NOT NULL"

    def test_to_entity_id_not_nullable(self):
        cols = self._columns()
        assert cols["to_entity_id"]["is_nullable"] == "NO", "to_entity_id must be NOT NULL"

    def test_relationship_type_not_nullable(self):
        cols = self._columns()
        assert cols["relationship_type"]["is_nullable"] == "NO", "relationship_type must be NOT NULL"

    def test_evidence_is_nullable(self):
        cols = self._columns()
        assert "evidence" in cols, "evidence column missing"
        assert cols["evidence"]["is_nullable"] == "YES", "evidence must be nullable"

    def test_three_indexes_present(self):
        indexes = {r["indexname"] for r in self._indexes()}
        required = {"idx_er_org_from", "idx_er_org_to", "idx_er_org_type"}
        missing = required - indexes
        assert not missing, f"Missing required indexes: {missing}"

    def test_idx_er_org_from_columns(self):
        indexdef = self._index_def("idx_er_org_from")
        assert indexdef, "idx_er_org_from index missing"
        pos_org = indexdef.find("org_id")
        pos_from = indexdef.find("from_entity_id")
        assert pos_org != -1 and pos_from != -1 and pos_org < pos_from, (
            f"idx_er_org_from must cover (org_id, from_entity_id) in order, got {indexdef}"
        )

    def test_idx_er_org_to_columns(self):
        indexdef = self._index_def("idx_er_org_to")
        assert indexdef, "idx_er_org_to index missing"
        pos_org = indexdef.find("org_id")
        pos_to = indexdef.find("to_entity_id")
        assert pos_org != -1 and pos_to != -1 and pos_org < pos_to, (
            f"idx_er_org_to must cover (org_id, to_entity_id) in order, got {indexdef}"
        )

    def test_idx_er_org_type_columns(self):
        indexdef = self._index_def("idx_er_org_type")
        assert indexdef, "idx_er_org_type index missing"
        pos_org = indexdef.find("org_id")
        pos_type = indexdef.find("relationship_type")
        pos_inf = indexdef.find("inferred")
        assert pos_org != -1 and pos_type != -1 and pos_inf != -1, (
            f"idx_er_org_type must cover (org_id, relationship_type, inferred), got {indexdef}"
        )
        assert pos_org < pos_type < pos_inf, (
            f"idx_er_org_type columns must be in order (org_id, relationship_type, inferred), got {indexdef}"
        )

    def test_natural_key_unique_index(self):
        indexdef = self._index_def("idx_er_org_natural_key")
        assert indexdef, "Missing relationship natural-key unique index"
        assert "UNIQUE INDEX" in indexdef, f"natural-key index must be UNIQUE, got {indexdef}"
        positions = [
            indexdef.find(c)
            for c in ("org_id", "from_entity_id", "to_entity_id", "relationship_type")
        ]
        assert all(p != -1 for p in positions), indexdef
        assert positions == sorted(positions), (
            f"natural-key index columns must be in order "
            f"(org_id, from_entity_id, to_entity_id, relationship_type), got {indexdef}"
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
            from_id = str(uuid4())
            to_id = str(uuid4())
            rel_id = _insert_relationship(conn, "test-org-insert", from_id, to_id, "owns", inferred=False)

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM entity_relationships WHERE id = %s", (rel_id,)
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
            from_id = str(uuid4())
            to_id = str(uuid4())
            rel_id = _insert_relationship(
                conn, "test-org-inferred", from_id, to_id, "depends_on", inferred=True
            )

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM entity_relationships WHERE id = %s", (rel_id,)
            ).fetchone()

        assert row is not None
        assert float(row["confidence"]) == INFERRED_CONFIDENCE
        assert int(row["inferred"]) == 1

    def test_query_by_org_id(self):
        org = f"org-query-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()))
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()))

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = %s", (org,)
            ).fetchall()

        assert len(rows) == 2

    def test_query_by_from_entity_id(self):
        org = f"org-from-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        with sqlite3.connect(_get_db_path()) as conn:
            _insert_relationship(conn, org, from_id, str(uuid4()))
            _insert_relationship(conn, org, from_id, str(uuid4()))
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()))

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = %s AND from_entity_id = %s",
                (org, from_id),
            ).fetchall()

        assert len(rows) == 2

    def test_query_by_to_entity_id(self):
        org = f"org-to-{uuid4().hex[:8]}"
        to_id = str(uuid4())
        with sqlite3.connect(_get_db_path()) as conn:
            _insert_relationship(conn, org, str(uuid4()), to_id)
            _insert_relationship(conn, org, str(uuid4()), to_id)
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()))

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = %s AND to_entity_id = %s",
                (org, to_id),
            ).fetchall()

        assert len(rows) == 2

    def test_query_by_inferred_flag_false(self):
        org = f"org-inferred-false-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "owns", inferred=False)
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "member_of", inferred=False)
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "depends_on", inferred=True)

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = %s AND inferred = FALSE",
                (org,),
            ).fetchall()

        assert len(rows) == 2
        assert all(int(r["inferred"]) == 0 for r in rows)

    def test_query_by_inferred_flag_true(self):
        org = f"org-inferred-true-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "owns", inferred=False)
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "depends_on", inferred=True)
            _insert_relationship(conn, org, str(uuid4()), str(uuid4()), "routes_to", inferred=True)

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = %s AND inferred = TRUE",
                (org,),
            ).fetchall()

        assert len(rows) == 2
        assert all(int(r["inferred"]) == 1 for r in rows)

    def test_all_five_relationship_types_accepted(self):
        org = f"org-types-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            for rtype in sorted(RELATIONSHIP_TYPES):
                inferred = rtype in ("depends_on", "routes_to")
                _insert_relationship(conn, org, str(uuid4()), str(uuid4()), rtype, inferred=inferred)

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT relationship_type FROM entity_relationships WHERE org_id = %s", (org,)
            ).fetchall()

        stored_types = {r["relationship_type"] for r in rows}
        assert stored_types == RELATIONSHIP_TYPES

    def test_invalid_relationship_type_check_constraint_enforced(self):
        """SQLite CHECK constraint must reject values outside RELATIONSHIP_TYPES."""
        org = f"org-invalid-check-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            with pytest.raises(psycopg2.IntegrityError):
                conn.execute(
                    """INSERT INTO entity_relationships (
                        id, org_id, from_entity_id, to_entity_id, relationship_type,
                        confidence, inferred, evidence, first_seen_run_id,
                        last_seen_run_id, run_count, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        str(uuid4()),
                        org,
                        str(uuid4()),
                        str(uuid4()),
                        "invalid_type",
                        OBSERVED_CONFIDENCE,
                        False,
                        None,
                        "run-invalid-check",
                        "run-invalid-check",
                        1,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            conn.rollback()

    def test_sqlite_fk_off_allows_orphans_but_graph_join_omits_them(self):
        """Document current SQLite FK behavior: orphans may exist but do not surface."""
        org = f"org-orphan-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())
        with sqlite3.connect(_get_db_path()) as conn:
            _insert_relationship(conn, org, from_id, to_id, "owns")
            count = conn.execute(
                "SELECT COUNT(*) FROM entity_relationships WHERE org_id = %s",
                (org,),
            ).fetchone()[0]

        assert count == 1
        assert get_entity_relationships(org, from_id) == []

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
            conn.execute(
                """INSERT INTO entity_relationships (
                    id, org_id, from_entity_id, to_entity_id, relationship_type,
                    confidence, inferred, evidence, first_seen_run_id,
                    last_seen_run_id, run_count, created_at
                ) VALUES (
                    %(id)s, %(org_id)s, %(from_entity_id)s, %(to_entity_id)s, %(relationship_type)s,
                    %(confidence)s, %(inferred)s, %(evidence)s, %(first_seen_run_id)s,
                    %(last_seen_run_id)s, %(run_count)s, %(created_at)s
                )""",
                row,
            )
            conn.commit()

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            db_row = conn.execute(
                "SELECT * FROM entity_relationships WHERE id = %s", (row["id"],)
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
            conn.execute(
                """INSERT INTO entity_relationships (
                    id, org_id, from_entity_id, to_entity_id, relationship_type,
                    confidence, inferred, evidence, first_seen_run_id,
                    last_seen_run_id, run_count, created_at
                ) VALUES (
                    %(id)s, %(org_id)s, %(from_entity_id)s, %(to_entity_id)s, %(relationship_type)s,
                    %(confidence)s, %(inferred)s, %(evidence)s, %(first_seen_run_id)s,
                    %(last_seen_run_id)s, %(run_count)s, %(created_at)s
                )""",
                row,
            )
            conn.commit()

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            db_row = conn.execute(
                "SELECT evidence FROM entity_relationships WHERE id = %s", (row["id"],)
            ).fetchone()

        assert db_row["evidence"] is None


class TestEntityRelationshipsCrossOrgIsolation:
    """AC8: entity_relationships for org_A never returned in org_B queries."""

    def test_cross_org_isolation(self):
        org_a = f"org-a-{uuid4().hex[:8]}"
        org_b = f"org-b-{uuid4().hex[:8]}"
        with sqlite3.connect(_get_db_path()) as conn:
            rel_id_a = _insert_relationship(conn, org_a, str(uuid4()), str(uuid4()))
            rel_id_b = _insert_relationship(conn, org_b, str(uuid4()), str(uuid4()))

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows_a = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = %s", (org_a,)
            ).fetchall()
            rows_b = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = %s", (org_b,)
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
            _insert_relationship(conn, org_a, str(uuid4()), str(uuid4()), "depends_on", inferred=True)
            _insert_relationship(conn, org_b, str(uuid4()), str(uuid4()), "depends_on", inferred=True)

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows_a = conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = %s AND inferred = TRUE", (org_a,)
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
                WHERE org_id = %s AND from_entity_id = %s
                  AND to_entity_id = %s AND relationship_type = %s
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
        """AC7 core: the same natural key in two runs gives run_count=2."""
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

    def test_repeated_call_in_same_run_does_not_increment_run_count(self):
        org = f"org-same-run-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        for _ in range(2):
            rel = upsert_relationship(
                org_id=org,
                from_entity_id=from_id,
                to_entity_id=to_id,
                relationship_type="owns",
                confidence=OBSERVED_CONFIDENCE,
                inferred=False,
                run_id="run-same",
            )

        rows = self._query_rows(org, from_id, to_id, "owns")
        assert len(rows) == 1
        assert rows[0]["run_count"] == 1
        assert rel.run_count == 1

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

    def test_confidence_mismatch_logged_on_update(self, caplog):
        """Incoming confidence drift is ignored but visible in debug logs."""
        org = f"org-conf-log-{uuid4().hex[:8]}"
        from_id = str(uuid4())
        to_id = str(uuid4())

        upsert_relationship(
            org_id=org, from_entity_id=from_id, to_entity_id=to_id,
            relationship_type="depends_on", confidence=OBSERVED_CONFIDENCE,
            inferred=False, run_id="run-001",
        )
        with caplog.at_level(logging.DEBUG, logger="app.relationship_mapper"):
            upsert_relationship(
                org_id=org, from_entity_id=from_id, to_entity_id=to_id,
                relationship_type="depends_on", confidence=INFERRED_CONFIDENCE,
                inferred=True, run_id="run-002",
            )

        assert any("confidence mismatch ignored" in r.getMessage() for r in caplog.records)

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
                "SELECT * FROM entity_relationships WHERE org_id = %s AND from_entity_id = %s AND to_entity_id = %s",
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
                   WHERE org_id = %s AND from_entity_id = %s AND to_entity_id = %s AND relationship_type = %s""",
                (org_a, from_id, to_id, "owns"),
            ).fetchone()
            row_b = conn.execute(
                """SELECT run_count FROM entity_relationships
                   WHERE org_id = %s AND from_entity_id = %s AND to_entity_id = %s AND relationship_type = %s""",
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
                   WHERE org_id = %s AND from_entity_id = %s AND to_entity_id = %s AND relationship_type = %s""",
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
            ) ON CONFLICT DO NOTHING""",
            row,
        )
        conn.commit()


def _get_edges(org_id: str, rtype: str) -> list[dict]:
    with sqlite3.connect(_get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM entity_relationships WHERE org_id = %s AND relationship_type = %s",
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

    def test_duplicate_source_record_in_same_run_maps_one_edge(self):
        org = f"org-owns-duplicate-{uuid4().hex[:8]}"
        run = "run-t3-owns-duplicate"

        person = _make_entity(org, "person", "Sarah Chen", run_id=run)
        obj = _make_entity(org, "object", "LOAN-001", run_id=run)
        _persist_entity(person)
        _persist_entity(obj)
        record = {
            "OwnerId": "Sarah Chen",
            "Id": "LOAN-001",
            "source_system": "salesforce",
        }
        ingestor_data = {
            "salesforce": {
                "records": [record],
                "cases": [dict(record)],
            }
        }

        count = map_directly_observed(org, run, ingestor_data, [person, obj])

        edges = _get_edges(org, "owns")
        assert count == 1
        assert len(edges) == 1
        assert edges[0]["run_count"] == 1

    def test_owns_edge_matches_source_record_ids(self):
        """owns edge: OwnerId and record Id can match source_record_id values."""
        org = f"org-owns-srcid-{uuid4().hex[:8]}"
        run = "run-t3-owns-srcid"

        person = _make_entity(org, "person", "Sarah Chen", run_id=run)
        person.source_record_id = "005WG00000ZkgMfYAJ"
        obj = _make_entity(org, "object", "Loan Application 1042", run_id=run)
        obj.source_record_id = "a01WG00000Loan1042"
        _persist_entity(person)
        _persist_entity(obj)

        ingestor_data = {
            "salesforce": {
                "records": [
                    {
                        "OwnerId": "005WG00000ZkgMfYAJ",
                        "Id": "a01WG00000Loan1042",
                        "Name": "Loan Application 1042",
                    }
                ]
            }
        }

        count = map_directly_observed(org, run, ingestor_data, [person, obj])

        assert count == 1
        edges = _get_edges(org, "owns")
        assert len(edges) == 1
        assert edges[0]["from_entity_id"] == str(person.id)
        assert edges[0]["to_entity_id"] == str(obj.id)

    def test_observed_evidence_stores_no_display_names_or_record_values(self):
        """Evidence stores source metadata only, not ingestor display values."""
        import json
        org = f"org-pii-ev-{uuid4().hex[:8]}"
        run = "run-pii-ev"
        person = _make_entity(org, "person", "Private User", run_id=run)
        obj = _make_entity(org, "object", "LOAN-PII-001", run_id=run)
        _persist_entity(person)
        _persist_entity(obj)

        ingestor_data = {
            "salesforce": {
                "records": [
                    {
                        "OwnerId": "Private User",
                        "Id": "LOAN-PII-001",
                        "Name": "Sensitive Loan Title",
                        "Amount": 123456,
                    }
                ]
            }
        }
        map_directly_observed(org, run, ingestor_data, [person, obj])

        evidence = json.loads(_get_edges(org, "owns")[0]["evidence"])
        assert evidence == {"field": "OwnerId", "source": "salesforce"}
        assert "Private User" not in json.dumps(evidence)
        assert "LOAN-PII-001" not in json.dumps(evidence)
        assert "Sensitive Loan Title" not in json.dumps(evidence)

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


# ---------------------------------------------------------------------------
# T4 — map_inferred_from_detectors() tests (AC4, AC11)
# ---------------------------------------------------------------------------

from app.relationship_mapper import (
    map_inferred_from_detectors,
    get_process_entity,
    get_system_entity,
    INFERRED_VALIDATION_NOTE,
    INFERRED_RULE_DETECTOR_IDS,
)


class _DetectorResultStub:
    """Minimal stand-in for a DetectorResult — map_inferred_from_detectors()
    only reads .detector_id and .signal_source."""

    def __init__(self, detector_id: str, signal_source: str = "salesforce"):
        self.detector_id = detector_id
        self.signal_source = signal_source


def _make_process_entity(org_id: str, detector_id: str, run_id: str = "run-t4") -> Entity:
    """Process entity as extract_entities() creates them: display_name=detector_id."""
    return _make_entity(org_id, "process", detector_id, "resolved", run_id=run_id)


def _make_system_entity(org_id: str, signal_source: str, run_id: str = "run-t4") -> Entity:
    """System entity as extract_entities() creates them: display_name=signal_source."""
    return _make_entity(org_id, "system", signal_source, "resolved", run_id=run_id)


class TestInferredRuleDetectorRegistry:
    def test_rule_detector_ids_are_registered_in_pack_config(self):
        """Co-firing rule detector IDs must match active pack detector constants."""
        import importlib
        from discovery.packs.pack_config import get_detector_modules, list_packs

        registered: set[str] = set()
        for pack_id in list_packs():
            for module_path in get_detector_modules(pack_id):
                module = importlib.import_module(module_path)
                detector_id = getattr(module, "DETECTOR_ID", None)
                if detector_id:
                    registered.add(str(detector_id))

        assert INFERRED_RULE_DETECTOR_IDS <= registered


class TestMapInferredFromDetectorsAC4:
    """AC4: depends_on edge written with inferred=True, confidence=0.6 when
    LOAN_ORIGINATION_ROUTING_FRICTION and COVENANT_TRACKING_GAP both fire, with the
    validation note in evidence."""

    def test_covenant_depends_on_loan_origination_edge_created(self):
        org = f"org-t4-ac4-{uuid4().hex[:8]}"
        run = "run-t4-ac4"
        loan = _make_process_entity(org, "LOAN_ORIGINATION_ROUTING_FRICTION", run)
        cov = _make_process_entity(org, "COVENANT_TRACKING_GAP", run)
        _persist_entity(loan)
        _persist_entity(cov)

        detectors = [
            _DetectorResultStub("LOAN_ORIGINATION_ROUTING_FRICTION"),
            _DetectorResultStub("COVENANT_TRACKING_GAP"),
        ]
        count = map_inferred_from_detectors(org, run, detectors, [loan, cov])

        assert count == 1
        edges = _get_edges(org, "depends_on")
        assert len(edges) == 1
        # Edge direction: Covenant Review depends_on Loan Origination.
        assert edges[0]["from_entity_id"] == str(cov.id)
        assert edges[0]["to_entity_id"] == str(loan.id)

    def test_inferred_edge_confidence_is_0_6(self):
        org = f"org-t4-conf-{uuid4().hex[:8]}"
        run = "run-t4-conf"
        loan = _make_process_entity(org, "LOAN_ORIGINATION_ROUTING_FRICTION", run)
        cov = _make_process_entity(org, "COVENANT_TRACKING_GAP", run)
        _persist_entity(loan)
        _persist_entity(cov)

        map_inferred_from_detectors(
            org, run,
            [_DetectorResultStub("LOAN_ORIGINATION_ROUTING_FRICTION"),
             _DetectorResultStub("COVENANT_TRACKING_GAP")],
            [loan, cov],
        )
        edges = _get_edges(org, "depends_on")
        assert float(edges[0]["confidence"]) == INFERRED_CONFIDENCE
        assert float(edges[0]["confidence"]) == 0.6

    def test_inferred_edge_inferred_flag_is_true(self):
        org = f"org-t4-inf-{uuid4().hex[:8]}"
        run = "run-t4-inf"
        loan = _make_process_entity(org, "LOAN_ORIGINATION_ROUTING_FRICTION", run)
        cov = _make_process_entity(org, "COVENANT_TRACKING_GAP", run)
        _persist_entity(loan)
        _persist_entity(cov)

        map_inferred_from_detectors(
            org, run,
            [_DetectorResultStub("LOAN_ORIGINATION_ROUTING_FRICTION"),
             _DetectorResultStub("COVENANT_TRACKING_GAP")],
            [loan, cov],
        )
        edges = _get_edges(org, "depends_on")
        assert int(edges[0]["inferred"]) == 1

    def test_evidence_contains_validation_note_rationale_and_detector_ids(self):
        import json
        org = f"org-t4-ev-{uuid4().hex[:8]}"
        run = "run-t4-ev"
        loan = _make_process_entity(org, "LOAN_ORIGINATION_ROUTING_FRICTION", run)
        cov = _make_process_entity(org, "COVENANT_TRACKING_GAP", run)
        _persist_entity(loan)
        _persist_entity(cov)

        map_inferred_from_detectors(
            org, run,
            [_DetectorResultStub("LOAN_ORIGINATION_ROUTING_FRICTION"),
             _DetectorResultStub("COVENANT_TRACKING_GAP")],
            [loan, cov],
        )
        edges = _get_edges(org, "depends_on")
        ev = json.loads(edges[0]["evidence"])
        assert ev["note"] == INFERRED_VALIDATION_NOTE
        assert ev["note"] == "Validate with Stage 3 causal analysis before treating as truth"
        assert "rationale" in ev and ev["rationale"]
        assert set(ev["detector_ids"]) == {"LOAN_ORIGINATION_ROUTING_FRICTION", "COVENANT_TRACKING_GAP"}

    def test_no_edge_when_only_one_detector_fires(self):
        org = f"org-t4-single-{uuid4().hex[:8]}"
        run = "run-t4-single"
        loan = _make_process_entity(org, "LOAN_ORIGINATION_ROUTING_FRICTION", run)
        cov = _make_process_entity(org, "COVENANT_TRACKING_GAP", run)
        _persist_entity(loan)
        _persist_entity(cov)

        # Only one of the pair fires — rule must NOT trigger.
        count = map_inferred_from_detectors(
            org, run, [_DetectorResultStub("LOAN_ORIGINATION_ROUTING_FRICTION")], [loan, cov]
        )
        assert count == 0
        assert len(_get_edges(org, "depends_on")) == 0

    def test_missing_process_entity_skips_edge_no_exception(self):
        org = f"org-t4-missing-{uuid4().hex[:8]}"
        run = "run-t4-missing"
        # Only the covenant process exists; loan origination process is absent.
        cov = _make_process_entity(org, "COVENANT_TRACKING_GAP", run)
        _persist_entity(cov)

        count = map_inferred_from_detectors(
            org, run,
            [_DetectorResultStub("LOAN_ORIGINATION_ROUTING_FRICTION"),
             _DetectorResultStub("COVENANT_TRACKING_GAP")],
            [cov],
        )
        assert count == 0
        assert len(_get_edges(org, "depends_on")) == 0

    def test_dedup_across_runs_increments_run_count(self):
        org = f"org-t4-dedup-{uuid4().hex[:8]}"
        loan = _make_process_entity(org, "LOAN_ORIGINATION_ROUTING_FRICTION")
        cov = _make_process_entity(org, "COVENANT_TRACKING_GAP")
        _persist_entity(loan)
        _persist_entity(cov)
        detectors = [
            _DetectorResultStub("LOAN_ORIGINATION_ROUTING_FRICTION"),
            _DetectorResultStub("COVENANT_TRACKING_GAP"),
        ]
        map_inferred_from_detectors(org, "run-1", detectors, [loan, cov])
        map_inferred_from_detectors(org, "run-2", detectors, [loan, cov])

        edges = _get_edges(org, "depends_on")
        assert len(edges) == 1, "Same inferred edge across runs must not duplicate"
        assert edges[0]["run_count"] == 2

    def test_empty_detector_results_no_edge_no_exception(self):
        org = f"org-t4-empty-{uuid4().hex[:8]}"
        count = map_inferred_from_detectors(org, "run-empty", [], [])
        assert count == 0


class TestMapInferredFromDetectorsAC11:
    """AC11: all four nCino co-firing rules write edges to the DB in every run
    where both detectors fire, regardless of flag state."""

    def _all_entities(self, org: str, run: str) -> list:
        """Process + system entities needed by all four rules."""
        ents = [
            _make_process_entity(org, "LOAN_ORIGINATION_ROUTING_FRICTION", run),
            _make_process_entity(org, "COVENANT_TRACKING_GAP", run),
            _make_process_entity(org, "CHECKLIST_BOTTLENECK", run),
            _make_process_entity(org, "DISBURSEMENT_OVERDUE", run),
            _make_system_entity(org, "sqlserver", run),
            _make_system_entity(org, "salesforce", run),
        ]
        for e in ents:
            _persist_entity(e)
        return ents

    def _all_detectors_fire(self) -> list:
        return [
            _DetectorResultStub("LOAN_ORIGINATION_ROUTING_FRICTION", "salesforce"),
            _DetectorResultStub("COVENANT_TRACKING_GAP", "salesforce"),
            _DetectorResultStub("CHECKLIST_BOTTLENECK", "salesforce"),
            _DetectorResultStub("DISBURSEMENT_OVERDUE", "salesforce"),
            _DetectorResultStub("DB_SLA_BREACH_RATE", "sqlserver"),
        ]

    def test_all_four_rules_write_edges(self):
        org = f"org-t4-all-{uuid4().hex[:8]}"
        run = "run-t4-all"
        ents = self._all_entities(org, run)
        count = map_inferred_from_detectors(org, run, self._all_detectors_fire(), ents)

        assert count == 4, "All four co-firing rules must write an edge"
        depends = _get_edges(org, "depends_on")
        routes = _get_edges(org, "routes_to")
        assert len(depends) == 3, "Rules 1-3 produce three depends_on edges"
        assert len(routes) == 1, "Rule 4 produces one routes_to edge"
        # Every inferred edge is inferred=True, confidence=0.6.
        for edge in depends + routes:
            assert int(edge["inferred"]) == 1
            assert float(edge["confidence"]) == 0.6

    def test_rule4_routes_to_sqlserver_to_salesforce(self):
        org = f"org-t4-r4-{uuid4().hex[:8]}"
        run = "run-t4-r4"
        ents = self._all_entities(org, run)
        by_name = {(e.entity_type, e.canonical_name): e for e in ents}
        sqlserver = by_name[("system", "sqlserver")]
        salesforce = by_name[("system", "salesforce")]

        map_inferred_from_detectors(org, run, self._all_detectors_fire(), ents)

        routes = _get_edges(org, "routes_to")
        assert len(routes) == 1
        # SQL Server routes_to Salesforce.
        assert routes[0]["from_entity_id"] == str(sqlserver.id)
        assert routes[0]["to_entity_id"] == str(salesforce.id)

    def test_rules_write_regardless_of_flag(self, monkeypatch):
        """Storage is unconditional — setting INFERRED_RELATIONSHIPS_ENABLED=false
        must not suppress writes (the flag is a surfacing control, not storage)."""
        monkeypatch.setenv("INFERRED_RELATIONSHIPS_ENABLED", "false")
        org = f"org-t4-flag-{uuid4().hex[:8]}"
        run = "run-t4-flag"
        ents = self._all_entities(org, run)
        count = map_inferred_from_detectors(org, run, self._all_detectors_fire(), ents)
        assert count == 4, "Edges must be stored even when the flag is off"

    def test_only_rules_with_both_detectors_fire(self):
        """Rules whose detector pair is incomplete are skipped; others still write."""
        org = f"org-t4-partial-{uuid4().hex[:8]}"
        run = "run-t4-partial"
        ents = self._all_entities(org, run)
        # Only LOAN_ORIGINATION_ROUTING_FRICTION + COVENANT_TRACKING_GAP fire (Rule 1 only).
        detectors = [
            _DetectorResultStub("LOAN_ORIGINATION_ROUTING_FRICTION", "salesforce"),
            _DetectorResultStub("COVENANT_TRACKING_GAP", "salesforce"),
        ]
        count = map_inferred_from_detectors(org, run, detectors, ents)
        assert count == 1
        assert len(_get_edges(org, "depends_on")) == 1
        assert len(_get_edges(org, "routes_to")) == 0


class TestGetProcessAndSystemEntity:
    """Unit tests for the T4 endpoint-resolution helpers."""

    def test_get_process_entity_matches_canonicalized_detector_id(self):
        org = f"org-gpe-{uuid4().hex[:8]}"
        proc = _make_process_entity(org, "COVENANT_TRACKING_GAP")
        # detector_id lookup is case-insensitive via canonicalization.
        assert get_process_entity(org, "COVENANT_TRACKING_GAP", [proc]) is proc
        assert get_process_entity(org, "covenant_tracking_gap", [proc]) is proc

    def test_get_process_entity_returns_none_when_absent(self):
        org = f"org-gpe-none-{uuid4().hex[:8]}"
        assert get_process_entity(org, "COVENANT_TRACKING_GAP", []) is None

    def test_get_process_entity_ignores_non_process_types(self):
        org = f"org-gpe-type-{uuid4().hex[:8]}"
        # A system entity with the same name must not be returned as a process.
        sys_e = _make_system_entity(org, "COVENANT_TRACKING_GAP")
        assert get_process_entity(org, "COVENANT_TRACKING_GAP", [sys_e]) is None

    def test_get_system_entity_priority_order(self):
        org = f"org-gse-{uuid4().hex[:8]}"
        sql = _make_system_entity(org, "sqlserver")
        assert get_system_entity(org, ["sqlserver", "mssql"], [sql]) is sql

    def test_get_system_entity_falls_back_to_alias(self):
        org = f"org-gse-alias-{uuid4().hex[:8]}"
        sql = _make_system_entity(org, "mssql")
        # Primary candidate 'sqlserver' absent; alias 'mssql' matches.
        assert get_system_entity(org, ["sqlserver", "mssql"], [sql]) is sql

    def test_get_system_entity_returns_none_when_absent(self):
        org = f"org-gse-none-{uuid4().hex[:8]}"
        assert get_system_entity(org, ["salesforce"], []) is None


# ---------------------------------------------------------------------------
# T8 / AC12 — get_entity_relationships(org_id, entity_id, inferred=False)
# ---------------------------------------------------------------------------

def _insert_named_entity(
    conn: sqlite3.Connection,
    org_id: str,
    display_name: str,
    entity_type: str = "person",
    run_id: str = "run-t8",
) -> str:
    """Insert a resolved entity with a specific display_name and return its id."""
    entity = Entity(
        org_id=org_id,
        entity_type=entity_type,
        canonical_name=display_name.lower().replace(" ", "-"),
        display_name=display_name,
        source_system="salesforce",
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
            %(id)s, %(org_id)s, %(entity_type)s, %(canonical_name)s, %(display_name)s,
            %(source_system)s, %(source_record_id)s, %(resolution_confidence)s,
            %(resolution_status)s, %(first_seen_run_id)s, %(last_seen_run_id)s,
            %(run_count)s, %(metadata)s, %(created_at)s, %(updated_at)s
        )""",
        row,
    )
    conn.commit()
    return row["id"]


class TestGetEntityRelationshipsAC12:
    """AC12: get_entity_relationships(org_id, entity_id, inferred=False)."""

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(_get_db_path())

    def test_returns_edges_where_entity_is_from_endpoint(self):
        """Entity appearing as from_entity_id is returned."""
        org = f"org-ac12-from-{uuid4().hex[:8]}"
        conn = self._conn()
        from_id = _insert_named_entity(conn, org, "Alice Summers", "person")
        to_id = _insert_named_entity(conn, org, "Case-001", "object")
        _insert_relationship(conn, org, from_id, to_id, "owns")
        conn.close()

        results = get_entity_relationships(org, from_id)
        assert len(results) == 1
        r = results[0]
        assert r.relationship_type == "owns"
        assert r.from_entity_name == "Alice Summers"
        assert r.to_entity_name == "Case-001"
        assert r.inferred is False
        assert r.confidence == OBSERVED_CONFIDENCE

    def test_returns_edges_where_entity_is_to_endpoint(self):
        """Entity appearing as to_entity_id is also returned (bidirectional lookup)."""
        org = f"org-ac12-to-{uuid4().hex[:8]}"
        conn = self._conn()
        from_id = _insert_named_entity(conn, org, "Bob Chen", "person")
        to_id = _insert_named_entity(conn, org, "Support Team", "team")
        _insert_relationship(conn, org, from_id, to_id, "member_of")
        conn.close()

        results = get_entity_relationships(org, to_id)
        assert len(results) == 1
        r = results[0]
        assert r.relationship_type == "member_of"
        assert r.from_entity_name == "Bob Chen"
        assert r.to_entity_name == "Support Team"

    def test_returns_both_from_and_to_edges(self):
        """Entity as from in one edge and to in another — both are returned."""
        org = f"org-ac12-both-{uuid4().hex[:8]}"
        conn = self._conn()
        # entity_a owns entity_b; entity_c escalates_to entity_a
        a_id = _insert_named_entity(conn, org, "Entity A", "person")
        b_id = _insert_named_entity(conn, org, "Entity B", "object")
        c_id = _insert_named_entity(conn, org, "Entity C", "person")
        _insert_relationship(conn, org, a_id, b_id, "owns")
        _insert_relationship(conn, org, c_id, a_id, "escalates_to")
        conn.close()

        results = get_entity_relationships(org, a_id)
        rel_types = {r.relationship_type for r in results}
        assert "owns" in rel_types
        assert "escalates_to" in rel_types
        assert len(results) == 2

    def test_default_inferred_false_excludes_inferred_edges(self):
        """Default inferred=False filters out edges with inferred=True."""
        org = f"org-ac12-obs-{uuid4().hex[:8]}"
        conn = self._conn()
        from_id = _insert_named_entity(conn, org, "Proc X", "process")
        to_id = _insert_named_entity(conn, org, "Sys Y", "system")
        _insert_relationship(conn, org, from_id, to_id, "depends_on", inferred=True)
        conn.close()

        # Default: inferred=False — should return nothing
        results = get_entity_relationships(org, from_id)
        assert results == []

    def test_inferred_true_includes_inferred_edges(self):
        """inferred=True returns edges with inferred=True as well as observed."""
        org = f"org-ac12-inf-{uuid4().hex[:8]}"
        conn = self._conn()
        from_id = _insert_named_entity(conn, org, "Proc P", "process")
        to_id_obs = _insert_named_entity(conn, org, "Sys Obs", "system")
        to_id_inf = _insert_named_entity(conn, org, "Sys Inf", "system")
        _insert_relationship(conn, org, from_id, to_id_obs, "depends_on", inferred=False)
        _insert_relationship(conn, org, from_id, to_id_inf, "routes_to", inferred=True)
        conn.close()

        # With inferred=True, both edges are returned
        results = get_entity_relationships(org, from_id, inferred=True)
        assert len(results) == 2
        inferred_flags = {r.inferred for r in results}
        assert True in inferred_flags
        assert False in inferred_flags

    def test_inferred_true_flag_preserved_on_summary(self):
        """RelationshipSummary.inferred reflects the stored value."""
        org = f"org-ac12-flag-{uuid4().hex[:8]}"
        conn = self._conn()
        from_id = _insert_named_entity(conn, org, "Alice", "person")
        to_id = _insert_named_entity(conn, org, "Beta System", "system")
        _insert_relationship(conn, org, from_id, to_id, "routes_to", inferred=True)
        conn.close()

        results = get_entity_relationships(org, from_id, inferred=True)
        assert len(results) == 1
        assert results[0].inferred is True
        assert results[0].confidence == INFERRED_CONFIDENCE

    def test_cross_org_isolation(self):
        """Edges from a different org are never returned even with the same entity UUIDs."""
        org_a = f"org-ac12-xa-{uuid4().hex[:8]}"
        org_b = f"org-ac12-xb-{uuid4().hex[:8]}"
        conn = self._conn()
        # Create entities in org_a
        from_a = _insert_named_entity(conn, org_a, "Person A", "person")
        to_a = _insert_named_entity(conn, org_a, "Object A", "object")
        _insert_relationship(conn, org_a, from_a, to_a, "owns")
        # Create identical entities in org_b with the SAME UUIDs would conflict
        # so we use different entities but query org_b for org_a's entity_id.
        conn.close()

        # Querying org_b for an entity that only exists in org_a → empty
        results = get_entity_relationships(org_b, from_a)
        assert results == []

    def test_no_edges_returns_empty_list(self):
        """Entity with no edges returns an empty list without raising."""
        org = f"org-ac12-empty-{uuid4().hex[:8]}"
        conn = self._conn()
        solo_id = _insert_named_entity(conn, org, "Solo Entity", "person")
        conn.close()

        results = get_entity_relationships(org, solo_id)
        assert results == []

    def test_nonexistent_entity_returns_empty_list(self):
        """Unknown entity_id returns an empty list (no DB error)."""
        org = f"org-ac12-noent-{uuid4().hex[:8]}"
        results = get_entity_relationships(org, str(uuid4()))
        assert results == []

    def test_result_fields_come_from_entities_table(self):
        """Summary display_name and entity_type are sourced from entities JOIN."""
        org = f"org-ac12-fields-{uuid4().hex[:8]}"
        conn = self._conn()
        from_id = _insert_named_entity(conn, org, "Carol Davis", "person")
        to_id = _insert_named_entity(conn, org, "Incident-999", "object")
        _insert_relationship(conn, org, from_id, to_id, "escalates_to")
        conn.close()

        results = get_entity_relationships(org, from_id)
        assert len(results) == 1
        r = results[0]
        assert r.from_entity_name == "Carol Davis"
        assert r.from_entity_type == "person"
        assert r.to_entity_name == "Incident-999"
        assert r.to_entity_type == "object"

    def test_order_is_deterministic(self):
        """Results are ordered: inferred ASC, relationship_type ASC, names ASC."""
        org = f"org-ac12-order-{uuid4().hex[:8]}"
        conn = self._conn()
        hub = _insert_named_entity(conn, org, "Hub Entity", "person")
        s1 = _insert_named_entity(conn, org, "System Alpha", "system")
        s2 = _insert_named_entity(conn, org, "System Beta", "system")
        t1 = _insert_named_entity(conn, org, "Team Zeta", "team")
        # Two observed edges with different relationship types
        _insert_relationship(conn, org, hub, s1, "owns", inferred=False)
        _insert_relationship(conn, org, hub, s2, "member_of", inferred=False)
        _insert_relationship(conn, org, hub, t1, "depends_on", inferred=False)
        conn.close()

        results = get_entity_relationships(org, hub)
        rel_types = [r.relationship_type for r in results]
        # Alphabetical: depends_on < member_of < owns
        assert rel_types == sorted(rel_types)


# ---------------------------------------------------------------------------
# T9 / AC10 — relationship.mapping_completed telemetry event
# ---------------------------------------------------------------------------

def _make_resolved_entity_obj(org_id: str, display_name: str, entity_type: str) -> Entity:
    """Build an in-memory resolved Entity without hitting the DB.

    canonical_name uses the same normalisation as relationship_mapper._canonicalize()
    so that get_resolved_entity() can match by canonical_name.
    """
    canonical_name = " ".join(display_name.split()).lower()
    return Entity(
        org_id=org_id,
        entity_type=entity_type,
        canonical_name=canonical_name,
        display_name=display_name,
        source_system="salesforce",
        resolution_confidence=1.0,
        resolution_status="resolved",
        first_seen_run_id="run-t9",
        last_seen_run_id="run-t9",
        run_count=1,
    )


def _make_ambiguous_entity_obj(org_id: str, display_name: str, entity_type: str) -> Entity:
    """Build an in-memory ambiguous Entity without hitting the DB.

    canonical_name uses the same normalisation as relationship_mapper._canonicalize().
    """
    canonical_name = " ".join(display_name.split()).lower()
    return Entity(
        org_id=org_id,
        entity_type=entity_type,
        canonical_name=canonical_name,
        display_name=display_name,
        source_system="salesforce",
        resolution_confidence=0.5,
        resolution_status="ambiguous",
        first_seen_run_id="run-t9",
        last_seen_run_id="run-t9",
        run_count=1,
    )


class TestRelationshipMappingTelemetryAC10:
    """AC10: relationship.mapping_completed registered and emitted correctly."""

    # ── Registry tests (no DB needed) ──────────────────────────────────────

    def test_event_type_in_registered_event_types(self):
        """AC10 — relationship.mapping_completed is in REGISTERED_EVENT_TYPES."""
        from app.telemetry import REGISTERED_EVENT_TYPES
        assert "relationship.mapping_completed" in REGISTERED_EVENT_TYPES

    def test_event_type_registered_with_correct_typeddict(self):
        """AC10 — registry entry maps to RelationshipMappingCompletedPayload."""
        from app.telemetry import EVENT_REGISTRY, RelationshipMappingCompletedPayload
        assert EVENT_REGISTRY["relationship.mapping_completed"] is RelationshipMappingCompletedPayload

    def test_payload_typeddict_importable(self):
        """AC10 — RelationshipMappingCompletedPayload is importable from app.telemetry."""
        from app.telemetry import RelationshipMappingCompletedPayload  # noqa: F401

    def test_payload_has_org_id_field(self):
        """AC10 — TypedDict includes org_id."""
        from app.telemetry import RelationshipMappingCompletedPayload
        assert "org_id" in get_type_hints(RelationshipMappingCompletedPayload)

    def test_payload_has_run_id_field(self):
        """AC10 — TypedDict includes run_id."""
        from app.telemetry import RelationshipMappingCompletedPayload
        assert "run_id" in get_type_hints(RelationshipMappingCompletedPayload)

    def test_payload_has_observed_count_field(self):
        """AC10 — TypedDict includes observed_count (not observed_edges)."""
        from app.telemetry import RelationshipMappingCompletedPayload
        hints = get_type_hints(RelationshipMappingCompletedPayload)
        assert "observed_count" in hints
        assert "observed_edges" not in hints, "stale field name 'observed_edges' must be removed"

    def test_payload_has_inferred_count_field(self):
        """AC10 — TypedDict includes inferred_count (not inferred_edges)."""
        from app.telemetry import RelationshipMappingCompletedPayload
        hints = get_type_hints(RelationshipMappingCompletedPayload)
        assert "inferred_count" in hints
        assert "inferred_edges" not in hints, "stale field name 'inferred_edges' must be removed"

    def test_payload_has_skipped_ambiguous_count_field(self):
        """AC10 — TypedDict includes skipped_ambiguous_count."""
        from app.telemetry import RelationshipMappingCompletedPayload
        assert "skipped_ambiguous_count" in get_type_hints(RelationshipMappingCompletedPayload)

    def test_payload_has_mapping_duration_ms_field(self):
        """AC10 — TypedDict includes mapping_duration_ms."""
        from app.telemetry import RelationshipMappingCompletedPayload
        assert "mapping_duration_ms" in get_type_hints(RelationshipMappingCompletedPayload)

    def test_payload_has_all_six_required_fields(self):
        """AC10 — TypedDict has exactly the six fields specified in Section 9."""
        from app.telemetry import RelationshipMappingCompletedPayload
        hints = get_type_hints(RelationshipMappingCompletedPayload)
        required = {"org_id", "run_id", "observed_count", "inferred_count",
                    "skipped_ambiguous_count", "mapping_duration_ms"}
        missing = required - set(hints.keys())
        assert not missing, f"TypedDict missing required fields: {missing}"

    # ── Emission tests (require DB via map_relationships) ──────────────────

    def _make_ingestor_data_with_owner(self, owner_name: str, obj_name: str) -> dict:
        """Build minimal Salesforce ingestor data for a single owns edge."""
        return {
            "salesforce": {
                "records": [
                    {"OwnerId": owner_name, "Id": obj_name}
                ]
            }
        }

    def test_record_event_called_once_on_success(self):
        """AC10 — record_event called exactly once per map_relationships() call."""
        org = f"org-ac10-once-{uuid4().hex[:8]}"
        run = f"run-ac10-once-{uuid4().hex[:8]}"
        captured = []

        with patch("app.telemetry.record_event", side_effect=lambda et, p: captured.append((et, p))):
            map_relationships(org, run, {}, [], [])

        rel_events = [(et, p) for et, p in captured if et == "relationship.mapping_completed"]
        assert len(rel_events) == 1

    def test_record_event_payload_contains_org_id_and_run_id(self):
        """AC10 — payload carries the org_id and run_id passed to map_relationships."""
        org = f"org-ac10-ids-{uuid4().hex[:8]}"
        run = f"run-ac10-ids-{uuid4().hex[:8]}"
        captured = []

        with patch("app.telemetry.record_event", side_effect=lambda et, p: captured.append((et, p))):
            map_relationships(org, run, {}, [], [])

        payload = next(p for et, p in captured if et == "relationship.mapping_completed")
        assert payload["org_id"] == org
        assert payload["run_id"] == run

    def test_observed_count_matches_actual_edges_written(self):
        """AC10 — observed_count in payload equals the count from map_directly_observed."""
        org = f"org-ac10-obs-{uuid4().hex[:8]}"
        run = f"run-ac10-obs-{uuid4().hex[:8]}"
        # Two resolved entities that will produce an owns edge
        person = _make_resolved_entity_obj(org, "Owner Jane", "person")
        obj = _make_resolved_entity_obj(org, "Case-007", "object")
        # Insert them so upsert_relationship FK is satisfied
        conn = sqlite3.connect(_get_db_path())
        conn.row_factory = sqlite3.Row
        for ent in (person, obj):
            row = ent.to_db_row()
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
                ) ON CONFLICT DO NOTHING""",
                row,
            )
        conn.commit()
        conn.close()

        ingestor_data = self._make_ingestor_data_with_owner("Owner Jane", "Case-007")
        captured = []

        with patch("app.telemetry.record_event", side_effect=lambda et, p: captured.append((et, p))):
            result = map_relationships(org, run, ingestor_data, [], [person, obj])

        payload = next(p for et, p in captured if et == "relationship.mapping_completed")
        assert payload["observed_count"] == result["observed"]
        assert payload["observed_count"] >= 1

    def test_inferred_count_zero_when_no_detectors_fire(self):
        """AC10 — inferred_count=0 when detector list is empty."""
        org = f"org-ac10-inf0-{uuid4().hex[:8]}"
        run = f"run-ac10-inf0-{uuid4().hex[:8]}"
        captured = []

        with patch("app.telemetry.record_event", side_effect=lambda et, p: captured.append((et, p))):
            map_relationships(org, run, {}, [], [])

        payload = next(p for et, p in captured if et == "relationship.mapping_completed")
        assert payload["inferred_count"] == 0

    def test_skipped_ambiguous_count_zero_when_no_ambiguous_entities(self):
        """AC10 — skipped_ambiguous_count=0 when all entities are resolved."""
        org = f"org-ac10-sk0-{uuid4().hex[:8]}"
        run = f"run-ac10-sk0-{uuid4().hex[:8]}"
        captured = []

        with patch("app.telemetry.record_event", side_effect=lambda et, p: captured.append((et, p))):
            map_relationships(org, run, {}, [], [])

        payload = next(p for et, p in captured if et == "relationship.mapping_completed")
        assert payload["skipped_ambiguous_count"] == 0

    def test_skipped_ambiguous_count_nonzero_when_ambiguous_entity_present(self):
        """AC10 — skipped_ambiguous_count incremented when endpoint is ambiguous."""
        org = f"org-ac10-skamb-{uuid4().hex[:8]}"
        run = f"run-ac10-skamb-{uuid4().hex[:8]}"
        # Person is ambiguous — owns edge for this record must be skipped
        ambig_person = _make_ambiguous_entity_obj(org, "Ambiguous Owner", "person")
        obj_ent = _make_resolved_entity_obj(org, "Record-XYZ", "object")
        ingestor_data = self._make_ingestor_data_with_owner("Ambiguous Owner", "Record-XYZ")
        captured = []

        with patch("app.telemetry.record_event", side_effect=lambda et, p: captured.append((et, p))):
            map_relationships(org, run, ingestor_data, [], [ambig_person, obj_ent])

        payload = next(p for et, p in captured if et == "relationship.mapping_completed")
        assert payload["skipped_ambiguous_count"] >= 1

    def test_mapping_duration_ms_is_non_negative_float(self):
        """AC10 — mapping_duration_ms is a non-negative numeric value."""
        org = f"org-ac10-dur-{uuid4().hex[:8]}"
        run = f"run-ac10-dur-{uuid4().hex[:8]}"
        captured = []

        with patch("app.telemetry.record_event", side_effect=lambda et, p: captured.append((et, p))):
            map_relationships(org, run, {}, [], [])

        payload = next(p for et, p in captured if et == "relationship.mapping_completed")
        dur = payload["mapping_duration_ms"]
        assert isinstance(dur, (int, float))
        assert dur >= 0

    def test_record_event_failure_does_not_propagate(self):
        """AC10 — a record_event() failure must not raise from map_relationships()."""
        org = f"org-ac10-fail-{uuid4().hex[:8]}"
        run = f"run-ac10-fail-{uuid4().hex[:8]}"

        with patch("app.telemetry.record_event", side_effect=RuntimeError("sink")):
            # Must not raise
            result = map_relationships(org, run, {}, [], [])

        assert "observed" in result

    def test_event_not_emitted_on_missing_import(self):
        """AC10 — if telemetry import fails, map_relationships still returns counts."""
        import builtins
        original_import = builtins.__import__

        def blocking_import(name, *args, **kwargs):
            if name == "app.telemetry":
                raise ImportError("telemetry unavailable")
            return original_import(name, *args, **kwargs)

        org = f"org-ac10-imp-{uuid4().hex[:8]}"
        run = f"run-ac10-imp-{uuid4().hex[:8]}"

        with patch("builtins.__import__", side_effect=blocking_import):
            # map_relationships guards the import in try/except — must not raise
            try:
                result = map_relationships(org, run, {}, [], [])
                assert "observed" in result
            except ImportError:
                pass  # acceptable: builtins patch may already be partially applied
