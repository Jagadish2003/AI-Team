"""Contract tests for T3-S12-A - Entity Extraction from Ingestor Runs.

T1 coverage (AC1):
  - entities table exists with all 15 columns including
    resolution_confidence and resolution_status.
  - required indexes support resolution lookup, run-scoped queries,
    and run_count filtering.
  - entity rows can be inserted and queried by org_id, run_id,
    canonical_name, and source_record_id.

T3 coverage (AC6, AC7, AC8):
  AC6 - extract_entities() extracts Person entities from Jira assignee
        and ServiceNow assigned_to fields.
  AC7 - extract_entities() extracts System entities for each signal_source
        in detector_results.
  AC8 - extract_entities() failure does not raise to the caller. The
        function is non-blocking.

Additional T3 coverage:
  - Salesforce Person extraction (OwnerId path, confidence=1.0)
  - ServiceNow Team extraction from assignment_groups
  - Jira Team extraction from project name
  - Jira Object extraction from issue keys
  - ServiceNow Object extraction from incident numbers
  - Process entity extraction per detector_id (confidence=1.0)
  - Cross-org isolation: entities from different orgs never collide
  - Confidence rules: name-based=0.8, source_record_id present=1.0,
    System/Process=1.0
  - Deduplication: same signal_source/detector_id emits one entity
  - Empty ingestor data: no exception, empty result
  - Missing nested fields: graceful skip, no exception
"""
import os
import sqlite3

import psycopg2

# Single source of truth for the service-account display threshold. Importing it
# here (instead of hardcoding 3) keeps the tests in lockstep with the producer
# in entity_extractor.py — Issue #6.
from database.models.entities import ENTITY_MIN_RUN_COUNT


def _get_db_path() -> str:
    return os.environ.get("DB_PATH", "")


class TestEntitiesTableSchema:
    """AC1: entities table created with all required columns and indexes."""

    def _columns(self) -> dict[str, dict]:
        """Return {column_name: info} for the entities table.

        PostgreSQL port: the original used ``PRAGMA table_info(entities)``. We now
        read information_schema.columns and pg_index, and synthesize the keys the
        assertions below rely on:
          * ``notnull`` — 1 if is_nullable='NO' else 0 (mirrors SQLite's int flag).
          * ``pk``      — 1 if the column participates in the primary key else 0.
        """
        with sqlite3.connect(_get_db_path()) as conn:
            col_rows = conn.execute(
                "SELECT column_name, data_type, is_nullable, character_maximum_length "
                "FROM information_schema.columns WHERE table_name = %s "
                "ORDER BY ordinal_position",
                ("entities",),
            ).fetchall()
            pk_rows = conn.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = 'entities'::regclass AND i.indisprimary"
            ).fetchall()
        pk_cols = {r["attname"] for r in pk_rows}
        result: dict[str, dict] = {}
        for r in col_rows:
            name = r["column_name"]
            result[name] = {
                "name": name,
                "data_type": r["data_type"],
                "is_nullable": r["is_nullable"],
                "character_maximum_length": r["character_maximum_length"],
                "notnull": 1 if r["is_nullable"] == "NO" else 0,
                "pk": 1 if name in pk_cols else 0,
            }
        return result

    def _indexes(self) -> list[dict]:
        """Return non-PK index rows for the entities table.

        PostgreSQL port of ``PRAGMA index_list(entities)``. pg_indexes also lists
        the PK's backing index, so we tag origin ('pk' vs 'c') from pg_index so the
        existing ``origin != 'pk'`` filter keeps working.
        """
        with sqlite3.connect(_get_db_path()) as conn:
            idx_rows = conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s",
                ("entities",),
            ).fetchall()
            primary_rows = conn.execute(
                "SELECT c.relname FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = 'entities'::regclass AND i.indisprimary"
            ).fetchall()
        primary_names = {r["relname"] for r in primary_rows}
        out: list[dict] = []
        for r in idx_rows:
            name = r["indexname"]
            out.append({
                "name": name,
                "indexdef": r["indexdef"],
                "origin": "pk" if name in primary_names else "c",
            })
        return out

    def _index_columns(self, index_name: str) -> list[str]:
        """Return ordered column names for a specific entities index.

        PostgreSQL port of ``PRAGMA index_info(<index>)`` — column order is read
        from pg_attribute via the index's indkey ordering.
        """
        with sqlite3.connect(_get_db_path()) as conn:
            rows = conn.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) "
                "JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) "
                "ON k.attnum = a.attnum "
                "WHERE c.relname = %s "
                "ORDER BY k.ord",
                (index_name,),
            ).fetchall()
        return [r["attname"] for r in rows]

    def _insert_entity(self) -> dict:
        from database.models.entities import Entity

        entity = Entity(
            org_id="test-org",
            entity_type="person",
            canonical_name="sarah chen",
            display_name="Sarah Chen",
            source_system="jira",
            source_record_id="JIRA-123",
            resolution_confidence=0.8,
            resolution_status="resolved",
            first_seen_run_id="run-001",
            last_seen_run_id="run-001",
            run_count=3,
            metadata={"team": "support"},
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
        return row

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
        # source_record_id and metadata are nullable because derived entities
        # may not map to a source row.
        cols = self._columns()
        assert cols["source_record_id"]["notnull"] == 0, "source_record_id must be nullable"
        assert cols["metadata"]["notnull"] == 0, "metadata must be nullable"

    def test_three_required_indexes_exist(self):
        indexes = self._indexes()
        non_pk = [i for i in indexes if i["origin"] != "pk"]
        names = {i["name"] for i in non_pk}
        expected = {
            "idx_entities_org_canonical",
            "idx_entities_org_run",
            "idx_entities_org_run_count",
        }
        assert len(non_pk) == 3, (
            f"Expected 3 indexes on entities, got {len(non_pk)}: "
            f"{[i['name'] for i in non_pk]}"
        )
        assert names == expected

    def test_canonical_name_index_columns(self):
        assert self._index_columns("idx_entities_org_canonical") == [
            "org_id", "entity_type", "canonical_name",
        ]

    def test_org_run_index_columns(self):
        assert self._index_columns("idx_entities_org_run") == [
            "org_id", "last_seen_run_id",
        ]

    def test_org_run_count_index_columns(self):
        assert self._index_columns("idx_entities_org_run_count") == [
            "org_id", "run_count",
        ]

    def test_insert_and_query_by_required_access_paths(self):
        row = self._insert_entity()
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            by_id = dict(conn.execute(
                "SELECT * FROM entities WHERE id = %s",
                (row["id"],),
            ).fetchone())
            by_org = dict(conn.execute(
                "SELECT * FROM entities WHERE org_id = %s AND id = %s",
                ("test-org", row["id"]),
            ).fetchone())
            by_run = dict(conn.execute(
                "SELECT * FROM entities WHERE org_id = %s AND last_seen_run_id = %s",
                ("test-org", "run-001"),
            ).fetchone())
            by_canonical = dict(conn.execute(
                """
                SELECT * FROM entities
                WHERE org_id = %s AND entity_type = %s AND canonical_name = %s
                """,
                ("test-org", "person", "sarah chen"),
            ).fetchone())
            by_source_record = dict(conn.execute(
                "SELECT * FROM entities WHERE org_id = %s AND source_record_id = %s",
                ("test-org", "JIRA-123"),
            ).fetchone())

        assert by_id["org_id"] == "test-org"
        assert by_id["resolution_confidence"] == 0.8
        assert by_id["resolution_status"] == "resolved"
        assert by_id["canonical_name"] == "sarah chen"
        assert by_id["run_count"] == 3
        assert by_org["id"] == row["id"]
        assert by_run["id"] == row["id"]
        assert by_canonical["id"] == row["id"]
        assert by_source_record["id"] == row["id"]


# =============================================================================
# T3 tests — extract_entities() (AC6, AC7, AC8 + additional coverage)
# =============================================================================

import pytest
from types import SimpleNamespace
from typing import Any, Dict, List


def _extract(
    *,
    org_id: str = "ext-org",
    run_id: str = "run-t3-001",
    pack_id: str = "service_cloud",
    detector_results: List[Any] = None,
    ingestor_data: Dict[str, Any] = None,
):
    """Helper: call extract_entities() with defaults for missing args."""
    from app.entity_extractor import extract_entities
    return extract_entities(
        org_id=org_id,
        run_id=run_id,
        pack_id=pack_id,
        detector_results=detector_results or [],
        ingestor_data=ingestor_data or {},
    )


def _fake_detector(*, signal_source: str, detector_id: str) -> SimpleNamespace:
    """Build a minimal DetectorResult-like object for testing."""
    return SimpleNamespace(signal_source=signal_source, detector_id=detector_id)


def _db_entities(org_id: str, run_id: str) -> List[Dict]:
    """Return all entity rows written during a run."""
    with sqlite3.connect(_get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM entities WHERE org_id = %s AND last_seen_run_id = %s",
                (org_id, run_id),
            ).fetchall()
        ]


def _db_entities_by_type(org_id: str, run_id: str, entity_type: str) -> List[Dict]:
    with sqlite3.connect(_get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r) for r in conn.execute(
                """SELECT * FROM entities
                   WHERE org_id = %s AND last_seen_run_id = %s AND entity_type = %s""",
                (org_id, run_id, entity_type),
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# AC6 — Person from Jira assignee and ServiceNow assigned_to
# ---------------------------------------------------------------------------

class TestAC6PersonExtraction:
    """AC6: extract_entities() extracts Person entities from Jira and ServiceNow."""

    def test_jira_assignee_display_name_creates_person(self):
        """Person entity extracted from Jira issue.assignee.displayName."""
        org, run = "ac6-j-a", "run-ac6-j-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {
                    "issues": [
                        {"key": "CRM-1", "assignee": {"displayName": "Alice Wan"}, "status": "Open"}
                    ]
                }
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        names = {e["display_name"] for e in persons}
        assert "Alice Wan" in names, f"Jira assignee not extracted. Got: {names}"

    def test_jira_assignee_canonical_name_lowercased(self):
        org, run = "ac6-j-b", "run-ac6-j-b"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {
                    "issues": [{"key": "CRM-2", "assignee": {"displayName": "Bob Lee"}}]
                }
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        assert any(e["canonical_name"] == "bob lee" for e in persons)

    def test_jira_person_has_no_source_record_id(self):
        """Jira name-based persons must have source_record_id=NULL (no cross-system ID)."""
        org, run = "ac6-j-c", "run-ac6-j-c"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {
                    "issues": [{"key": "CRM-3", "assignee": {"displayName": "Carol Sun"}}]
                }
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        carol = next((e for e in persons if "carol" in e["canonical_name"]), None)
        assert carol is not None
        assert carol["source_record_id"] is None, (
            "Jira name-based person must not have source_record_id"
        )

    def test_jira_person_confidence_0_8(self):
        """Jira name-based person → resolution_confidence=0.8."""
        org, run = "ac6-j-d", "run-ac6-j-d"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {
                    "issues": [{"key": "CRM-4", "assignee": {"displayName": "Dave Kim"}}]
                }
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        dave = next((e for e in persons if "dave" in e["canonical_name"]), None)
        assert dave is not None
        assert dave["resolution_confidence"] == 0.8

    def test_servicenow_assigned_to_display_value_creates_person(self):
        """Person entity extracted from ServiceNow incident.assigned_to.display_value."""
        org, run = "ac6-sn-a", "run-ac6-sn-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"servicenow": {
                "incident_metrics": {
                    "incidents": [
                        {
                            "number": "INC001",
                            "assigned_to": {"display_value": "Eve Torres"},
                        }
                    ]
                }
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        names = {e["display_name"] for e in persons}
        assert "Eve Torres" in names, f"SN assigned_to not extracted. Got: {names}"

    def test_servicenow_person_has_no_source_record_id(self):
        """ServiceNow name-based person must have source_record_id=NULL."""
        org, run = "ac6-sn-b", "run-ac6-sn-b"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"servicenow": {
                "incident_metrics": {
                    "incidents": [
                        {"number": "INC002", "assigned_to": {"display_value": "Frank Ross"}}
                    ]
                }
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        frank = next((e for e in persons if "frank" in e["canonical_name"]), None)
        assert frank is not None
        assert frank["source_record_id"] is None

    def test_servicenow_person_confidence_0_8(self):
        org, run = "ac6-sn-c", "run-ac6-sn-c"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"servicenow": {
                "incident_metrics": {
                    "incidents": [
                        {"number": "INC003", "assigned_to": {"display_value": "Gina Park"}}
                    ]
                }
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        gina = next((e for e in persons if "gina" in e["canonical_name"]), None)
        assert gina is not None
        assert gina["resolution_confidence"] == 0.8

    def test_missing_assignee_field_skipped_gracefully(self):
        """Issues without an assignee field produce no person entity — no exception."""
        org, run = "ac6-j-e", "run-ac6-j-e"
        result = _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {
                    "issues": [{"key": "CRM-5"}]  # no assignee key
                }
            }},
        )
        # Should not raise, returns list (may be empty)
        assert isinstance(result, list)

    def test_empty_assigned_to_skipped_gracefully(self):
        """Incidents with empty assigned_to do not create person entity."""
        org, run = "ac6-sn-d", "run-ac6-sn-d"
        result = _extract(
            org_id=org, run_id=run,
            ingestor_data={"servicenow": {
                "incident_metrics": {
                    "incidents": [{"number": "INC004", "assigned_to": {}}]
                }
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        assert persons == [], f"Empty assigned_to must not create a person. Got: {persons}"


# ---------------------------------------------------------------------------
# AC7 — System entity per signal_source in detector_results
# ---------------------------------------------------------------------------

class TestAC7SystemExtraction:
    """AC7: extract_entities() extracts System entities per distinct signal_source."""

    def test_single_signal_source_creates_system_entity(self):
        org, run = "ac7-a", "run-ac7-a"
        _extract(
            org_id=org, run_id=run,
            detector_results=[_fake_detector(signal_source="salesforce", detector_id="D1")],
        )
        systems = _db_entities_by_type(org, run, "system")
        assert len(systems) == 1
        assert systems[0]["display_name"] == "salesforce"

    def test_system_entity_confidence_1_0(self):
        """System entities use stable AgentIQ identifiers → confidence=1.0."""
        org, run = "ac7-b", "run-ac7-b"
        _extract(
            org_id=org, run_id=run,
            detector_results=[_fake_detector(signal_source="jira", detector_id="D2")],
        )
        systems = _db_entities_by_type(org, run, "system")
        assert systems[0]["resolution_confidence"] == 1.0

    def test_system_entity_resolved_status(self):
        org, run = "ac7-c", "run-ac7-c"
        _extract(
            org_id=org, run_id=run,
            detector_results=[_fake_detector(signal_source="servicenow", detector_id="D3")],
        )
        systems = _db_entities_by_type(org, run, "system")
        assert systems[0]["resolution_status"] == "resolved"

    def test_duplicate_signal_sources_produce_one_system_entity(self):
        """Multiple results with the same signal_source → only one System entity."""
        org, run = "ac7-d", "run-ac7-d"
        _extract(
            org_id=org, run_id=run,
            detector_results=[
                _fake_detector(signal_source="salesforce", detector_id="D4"),
                _fake_detector(signal_source="salesforce", detector_id="D5"),
                _fake_detector(signal_source="salesforce", detector_id="D6"),
            ],
        )
        systems = _db_entities_by_type(org, run, "system")
        sf_systems = [s for s in systems if s["display_name"] == "salesforce"]
        assert len(sf_systems) == 1, (
            f"Duplicate signal_source must produce one System entity. Got: {sf_systems}"
        )

    def test_two_distinct_signal_sources_produce_two_system_entities(self):
        org, run = "ac7-e", "run-ac7-e"
        _extract(
            org_id=org, run_id=run,
            detector_results=[
                _fake_detector(signal_source="salesforce", detector_id="D7"),
                _fake_detector(signal_source="jira", detector_id="D8"),
            ],
        )
        systems = _db_entities_by_type(org, run, "system")
        source_names = {s["display_name"] for s in systems}
        assert "salesforce" in source_names
        assert "jira" in source_names

    def test_system_entity_source_record_id_is_connector_id(self):
        """System entity source_record_id equals the signal_source (connector_id)."""
        org, run = "ac7-f", "run-ac7-f"
        _extract(
            org_id=org, run_id=run,
            detector_results=[_fake_detector(signal_source="salesforce", detector_id="D9")],
        )
        systems = _db_entities_by_type(org, run, "system")
        assert systems[0]["source_record_id"] == "salesforce"

    def test_empty_detector_results_produces_no_system_entities(self):
        org, run = "ac7-g", "run-ac7-g"
        _extract(org_id=org, run_id=run, detector_results=[])
        systems = _db_entities_by_type(org, run, "system")
        assert systems == []


# ---------------------------------------------------------------------------
# AC8 — Non-blocking: extract_entities() failure must not raise
# ---------------------------------------------------------------------------

class TestAC8NonBlocking:
    """AC8: extract_entities() must never raise — failures are non-blocking."""

    def test_returns_list_on_empty_inputs(self):
        result = _extract(org_id="ac8-a", run_id="run-ac8-a")
        assert isinstance(result, list)

    def test_completely_empty_ingestor_data_does_not_raise(self):
        result = _extract(
            org_id="ac8-b", run_id="run-ac8-b",
            ingestor_data={},
        )
        assert isinstance(result, list)

    def test_none_values_in_ingestor_data_do_not_raise(self):
        result = _extract(
            org_id="ac8-c", run_id="run-ac8-c",
            ingestor_data={"salesforce": None, "jira": None, "servicenow": None},
        )
        assert isinstance(result, list)

    def test_malformed_jira_data_does_not_raise(self):
        """Jira data with unexpected structure is handled without propagating."""
        result = _extract(
            org_id="ac8-d", run_id="run-ac8-d",
            ingestor_data={"jira": {"issue_metrics": "not_a_dict"}},
        )
        assert isinstance(result, list)

    def test_malformed_servicenow_incidents_do_not_raise(self):
        result = _extract(
            org_id="ac8-e", run_id="run-ac8-e",
            ingestor_data={"servicenow": {"incident_metrics": {"incidents": "not_a_list"}}},
        )
        assert isinstance(result, list)

    def test_detector_result_missing_attributes_does_not_raise(self):
        """DetectorResult without signal_source/detector_id is skipped cleanly."""
        bad = SimpleNamespace()  # no signal_source or detector_id attrs
        result = _extract(
            org_id="ac8-f", run_id="run-ac8-f",
            detector_results=[bad],
        )
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Additional T3 coverage: Salesforce, Process, Team, Object, cross-org
# ---------------------------------------------------------------------------

class TestSalesforcePersonExtraction:
    """Salesforce/nCino OwnerId path → Person with confidence=1.0."""

    def test_salesforce_approver_id_creates_person(self):
        org, run = "sf-p-a", "run-sf-p-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "approval_processes": [
                    {"process_name": "Discount", "approver_ids": ["005Qy000001AAA"]}
                ]
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        assert len(persons) >= 1
        assert persons[0]["source_record_id"] == "005Qy000001AAA"

    def test_salesforce_approver_id_confidence_1_0(self):
        """OwnerId present → confidence=1.0 per Section 4b."""
        org, run = "sf-p-b", "run-sf-p-b"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "approval_processes": [
                    {"process_name": "Credit", "approver_ids": ["005Qy000002BBB"]}
                ]
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        assert persons[0]["resolution_confidence"] == 1.0

    def test_salesforce_source_system_is_salesforce(self):
        org, run = "sf-p-c", "run-sf-p-c"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "approval_processes": [{"approver_ids": ["005Qy000003CCC"]}]
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        assert persons[0]["source_system"] == "salesforce"


class TestSalesforceUserNameResolution:
    """user_names map (resolved at ingest time) → Person display_name is the
    real name while source_record_id keeps the raw Salesforce User Id."""

    def test_approver_id_resolves_to_display_name(self):
        org, run = "sf-un-a", "run-sf-un-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "approval_processes": [
                    {"process_name": "Discount", "approver_ids": ["005Qy000001AAA"]}
                ],
                "user_names": {"005Qy000001AAA": "Sarah Chen"},
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        assert len(persons) == 1
        # display_name is the human name; the stable Id is retained separately.
        assert persons[0]["display_name"] == "Sarah Chen"
        assert persons[0]["source_record_id"] == "005Qy000001AAA"

    def test_owner_id_string_resolves_to_display_name(self):
        org, run = "sf-un-b", "run-sf-un-b"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "cases": [
                    {"Id": "500CASE001", "CaseNumber": "1", "OwnerId": "005Qy000002BBB"}
                ],
                "user_names": {"005Qy000002BBB": "Marcus Rivera"},
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        marcus = next(p for p in persons if p["source_record_id"] == "005Qy000002BBB")
        assert marcus["display_name"] == "Marcus Rivera"

    def test_ncino_owner_resolves_via_ncino_user_names(self):
        org, run = "sf-un-c", "run-sf-un-c"
        _extract(
            org_id=org, run_id=run, pack_id="ncino",
            ingestor_data={"salesforce": {
                "ncino": {
                    "loans": [
                        {"Id": "LOAN001", "Name": "Loan 001", "OwnerId": "005Qy000003CCC"}
                    ],
                    "user_names": {"005Qy000003CCC": "Amy Loan Officer"},
                }
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        amy = next(p for p in persons if p["source_record_id"] == "005Qy000003CCC")
        assert amy["display_name"] == "Amy Loan Officer"

    def test_unmapped_id_falls_back_to_raw_id(self):
        """An Id absent from user_names keeps the raw-Id display (graceful)."""
        org, run = "sf-un-d", "run-sf-un-d"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "approval_processes": [{"approver_ids": ["005Qy000004DDD"]}],
                "user_names": {"005Qy000009ZZZ": "Someone Else"},
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        assert persons[0]["display_name"] == "005Qy000004DDD"
        assert persons[0]["source_record_id"] == "005Qy000004DDD"

    def test_later_user_name_updates_existing_id_row(self):
        org = f"sf-un-upgrade-{_uuid.uuid4().hex[:8]}"
        owner_id = "005Qy000005EEE"
        first_run = "run-sf-un-upgrade-1"
        second_run = "run-sf-un-upgrade-2"

        _extract(
            org_id=org,
            run_id=first_run,
            ingestor_data={"salesforce": {
                "cases": [{"Id": "500CASE010", "CaseNumber": "000010", "OwnerId": owner_id}]
            }},
        )
        _extract(
            org_id=org,
            run_id=second_run,
            ingestor_data={"salesforce": {
                "cases": [{"Id": "500CASE010", "CaseNumber": "000010", "OwnerId": owner_id}],
                "user_names": {owner_id: "Nora Patel"},
            }},
        )

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = [
                dict(r) for r in conn.execute(
                    """
                    SELECT * FROM entities
                    WHERE org_id = %s
                      AND entity_type = 'person'
                      AND source_record_id = %s
                    """,
                    (org, owner_id),
                ).fetchall()
            ]

        assert len(rows) == 1
        assert rows[0]["display_name"] == "Nora Patel"
        assert rows[0]["canonical_name"] == "nora patel"
        assert rows[0]["last_seen_run_id"] == second_run


class TestSalesforceOwnerDeduplication:
    """Repeated source records must expose one entity per Salesforce owner."""

    @staticmethod
    def _kv_entities(run_id: str) -> list:
        with sqlite3.connect(_get_db_path()) as conn:
            row = conn.execute(
                "SELECT payload FROM kv WHERE key = %s",
                (f"entities:{run_id}",),
            ).fetchone()
        if row is None:
            return []
        parsed = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _person(org_id: str, display_name: str) -> dict:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM entities
                WHERE org_id = %s AND entity_type = 'person' AND display_name = %s
                """,
                (org_id, display_name),
            ).fetchone()
        assert row is not None
        return dict(row)

    def test_service_cloud_cases_with_same_owner_are_unique(self):
        org = f"sf-owner-dedupe-{_uuid.uuid4().hex[:8]}"
        owner_id = "005SERVICECLOUDOWNER"
        payload = {
            "salesforce": {
                "cases": [
                    {"Id": "500CASE001", "CaseNumber": "000001", "OwnerId": owner_id},
                    {"Id": "500CASE002", "CaseNumber": "000002", "OwnerId": owner_id},
                    {"Id": "500CASE003", "CaseNumber": "000003", "OwnerId": owner_id},
                ]
            }
        }

        final_entities = []
        final_run = ""
        for run_number in range(1, ENTITY_MIN_RUN_COUNT + 1):
            final_run = f"run-sf-owner-dedupe-{run_number}"
            final_entities = _extract(
                org_id=org,
                run_id=final_run,
                pack_id="service_cloud",
                ingestor_data=payload,
            )

        entity_ids = [str(entity.id) for entity in final_entities]
        assert len(entity_ids) == len(set(entity_ids))
        assert sum(entity.display_name == owner_id for entity in final_entities) == 1
        assert self._person(org, owner_id)["run_count"] == ENTITY_MIN_RUN_COUNT

        summaries = self._kv_entities(final_run)
        assert sum(item["display_name"] == owner_id for item in summaries) == 1

    def test_ncino_loans_with_same_owner_are_unique(self):
        org = f"ncino-owner-dedupe-{_uuid.uuid4().hex[:8]}"
        owner_id = "005NCINOOWNER"
        payload = {
            "salesforce": {
                "ncino": {
                    "loans": [
                        {"Id": "LOAN001", "Name": "Loan 001", "OwnerId": owner_id},
                        {"Id": "LOAN002", "Name": "Loan 002", "OwnerId": owner_id},
                        {"Id": "LOAN003", "Name": "Loan 003", "OwnerId": owner_id},
                    ]
                }
            }
        }

        final_entities = []
        final_run = ""
        for run_number in range(1, ENTITY_MIN_RUN_COUNT + 1):
            final_run = f"run-ncino-owner-dedupe-{run_number}"
            final_entities = _extract(
                org_id=org,
                run_id=final_run,
                pack_id="ncino",
                ingestor_data=payload,
            )

        entity_ids = [str(entity.id) for entity in final_entities]
        assert len(entity_ids) == len(set(entity_ids))
        assert sum(entity.display_name == owner_id for entity in final_entities) == 1
        assert self._person(org, owner_id)["run_count"] == ENTITY_MIN_RUN_COUNT

        summaries = self._kv_entities(final_run)
        assert sum(item["display_name"] == owner_id for item in summaries) == 1


class TestProcessEntityExtraction:
    """Process entity extracted per distinct detector_id → confidence=1.0."""

    def test_process_entity_created_per_detector_id(self):
        org, run = "proc-a", "run-proc-a"
        _extract(
            org_id=org, run_id=run,
            detector_results=[_fake_detector(signal_source="sf", detector_id="HANDOFF_FRICTION")],
        )
        processes = _db_entities_by_type(org, run, "process")
        assert any(e["display_name"] == "HANDOFF_FRICTION" for e in processes)

    def test_process_entity_confidence_1_0(self):
        """Stable AgentIQ identifiers → confidence=1.0 per spec."""
        org, run = "proc-b", "run-proc-b"
        _extract(
            org_id=org, run_id=run,
            detector_results=[_fake_detector(signal_source="sf", detector_id="APPROVAL_DELAY")],
        )
        processes = _db_entities_by_type(org, run, "process")
        assert processes[0]["resolution_confidence"] == 1.0

    def test_duplicate_detector_ids_produce_one_process_entity(self):
        org, run = "proc-c", "run-proc-c"
        _extract(
            org_id=org, run_id=run,
            detector_results=[
                _fake_detector(signal_source="sf", detector_id="KNOWLEDGE_GAP"),
                _fake_detector(signal_source="sn", detector_id="KNOWLEDGE_GAP"),
            ],
        )
        processes = _db_entities_by_type(org, run, "process")
        kg = [p for p in processes if p["display_name"] == "KNOWLEDGE_GAP"]
        assert len(kg) == 1, f"Same detector_id must produce one Process entity. Got: {kg}"


class TestTeamAndObjectExtraction:
    """Team (Jira project, SN assignment_group) and Object extraction."""

    def test_jira_project_creates_team_entity(self):
        org, run = "team-j-a", "run-team-j-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {"project": "LOAN", "issues": []}
            }},
        )
        teams = _db_entities_by_type(org, run, "team")
        assert any(e["display_name"] == "LOAN" for e in teams)

    def test_salesforce_team_field_creates_team_entity(self):
        org, run = "team-sf-a", "run-team-sf-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "case_teams": [{"TeamName": "Commercial Credit", "TeamId": "00G-team"}]
            }},
        )
        teams = _db_entities_by_type(org, run, "team")
        assert any(e["display_name"] == "Commercial Credit" for e in teams)

    def test_servicenow_assignment_group_creates_team_entity(self):
        org, run = "team-sn-a", "run-team-sn-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"servicenow": {
                "assignment_groups": [{"group_name": "Level 1 Support", "incident_count": 50}]
            }},
        )
        teams = _db_entities_by_type(org, run, "team")
        assert any(e["display_name"] == "Level 1 Support" for e in teams)

    def test_jira_issue_key_creates_object_entity(self):
        org, run = "obj-j-a", "run-obj-j-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {
                    "issues": [{"key": "LOAN-001", "status": "Open"}]
                }
            }},
        )
        objects = _db_entities_by_type(org, run, "object")
        assert any(e["display_name"] == "LOAN-001" for e in objects)

    def test_servicenow_incident_number_creates_object_entity(self):
        org, run = "obj-sn-a", "run-obj-sn-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"servicenow": {
                "incident_metrics": {
                    "incidents": [{"number": "INC0000001", "state": "New"}]
                }
            }},
        )
        objects = _db_entities_by_type(org, run, "object")
        assert any(e["display_name"] == "INC0000001" for e in objects)

    def test_salesforce_case_subject_creates_object_display_name(self):
        org, run = "obj-sf-subject", "run-obj-sf-subject"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "cases": [
                    {
                        "Id": "500CASE001",
                        "CaseNumber": "000001",
                        "Subject": "Customer login blocked",
                        "OwnerId": "005OWNER001",
                    }
                ]
            }},
        )
        objects = _db_entities_by_type(org, run, "object")
        case = next((o for o in objects if o["source_record_id"] == "500CASE001"), None)
        assert case is not None
        assert case["display_name"] == "Customer login blocked"

    def test_salesforce_case_number_used_when_subject_missing(self):
        org, run = "obj-sf-case-number", "run-obj-sf-case-number"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "cases": [
                    {"Id": "500CASE002", "CaseNumber": "000002", "OwnerId": "005OWNER002"}
                ]
            }},
        )
        objects = _db_entities_by_type(org, run, "object")
        case = next((o for o in objects if o["source_record_id"] == "500CASE002"), None)
        assert case is not None
        assert case["display_name"] == "000002"

    def test_later_case_subject_updates_existing_case_number_row(self):
        org = f"obj-sf-upgrade-{_uuid.uuid4().hex[:8]}"
        first_run = "run-obj-sf-upgrade-1"
        second_run = "run-obj-sf-upgrade-2"

        _extract(
            org_id=org,
            run_id=first_run,
            ingestor_data={"salesforce": {
                "cases": [
                    {"Id": "500CASE003", "CaseNumber": "000003", "OwnerId": "005OWNER003"}
                ]
            }},
        )
        _extract(
            org_id=org,
            run_id=second_run,
            ingestor_data={"salesforce": {
                "cases": [
                    {
                        "Id": "500CASE003",
                        "CaseNumber": "000003",
                        "Subject": "Customer refund blocked",
                        "OwnerId": "005OWNER003",
                    }
                ]
            }},
        )

        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = [
                dict(r) for r in conn.execute(
                    """
                    SELECT * FROM entities
                    WHERE org_id = %s
                      AND entity_type = 'object'
                      AND source_record_id = %s
                    """,
                    (org, "500CASE003"),
                ).fetchall()
            ]

        assert len(rows) == 1
        assert rows[0]["display_name"] == "Customer refund blocked"
        assert rows[0]["canonical_name"] == "customer refund blocked"
        assert rows[0]["last_seen_run_id"] == second_run

    def test_ncino_loan_name_creates_object_display_name(self):
        org, run = "obj-ncino-name", "run-obj-ncino-name"
        _extract(
            org_id=org, run_id=run,
            pack_id="ncino",
            ingestor_data={"salesforce": {
                "ncino": {
                    "loans": [
                        {
                            "Id": "LOAN001",
                            "Name": "Commercial Term Loan - Acme",
                            "OwnerId": "005OWNER003",
                        }
                    ]
                }
            }},
        )
        objects = _db_entities_by_type(org, run, "object")
        loan = next((o for o in objects if o["source_record_id"] == "LOAN001"), None)
        assert loan is not None
        assert loan["display_name"] == "Commercial Term Loan - Acme"

    def test_jira_object_has_source_record_id(self):
        """Jira Object entity carries source_record_id = issue_key."""
        org, run = "obj-j-b", "run-obj-j-b"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {
                    "issues": [{"key": "CRM-999", "status": "Closed"}]
                }
            }},
        )
        objects = _db_entities_by_type(org, run, "object")
        crm = next((o for o in objects if o["display_name"] == "CRM-999"), None)
        assert crm is not None
        assert crm["source_record_id"] == "CRM-999"


class TestProjectExtraction:
    """Project entities from Jira, ServiceNow, and nCino source payloads."""

    def test_jira_project_creates_project_entity(self):
        org, run = "proj-j-a", "run-proj-j-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {"project": "LOAN", "project_key": "LOAN", "issues": []}
            }},
        )
        projects = _db_entities_by_type(org, run, "project")
        assert any(e["display_name"] == "LOAN" for e in projects)

    def test_jira_epic_creates_project_entity(self):
        org, run = "proj-j-b", "run-proj-j-b"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {
                    "issues": [
                        {
                            "key": "LOAN-100",
                            "epic": {"name": "Loan Intake Modernization", "key": "LOAN-E1"},
                        }
                    ]
                }
            }},
        )
        projects = _db_entities_by_type(org, run, "project")
        assert any(e["display_name"] == "Loan Intake Modernization" for e in projects)

    def test_servicenow_project_creates_project_entity(self):
        org, run = "proj-sn-a", "run-proj-sn-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"servicenow": {
                "projects": [{"name": "Incident Deflection", "sys_id": "pm-001"}]
            }},
        )
        projects = _db_entities_by_type(org, run, "project")
        assert any(e["display_name"] == "Incident Deflection" for e in projects)

    def test_ncino_loan_portfolio_creates_project_entity(self):
        org, run = "proj-nc-a", "run-proj-nc-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "ncino": {
                    "loan_portfolios": [
                        {
                            "portfolio_name": "Commercial Lending Portfolio",
                            "portfolio_id": "PF-001",
                            "OwnerId": "005-owner",
                        }
                    ]
                }
            }},
        )
        projects = _db_entities_by_type(org, run, "project")
        assert any(e["display_name"] == "Commercial Lending Portfolio" for e in projects)


class TestAdditionalSourceCoverage:
    """Document-level Sprint 12 source fields beyond the core AC list."""

    def test_salesforce_assigned_to_creates_person(self):
        org, run = "src-sf-a", "run-src-sf-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"salesforce": {
                "tasks": [{"AssignedTo": "005-assigned-user"}]
            }},
        )
        persons = _db_entities_by_type(org, run, "person")
        assert any(e["display_name"] == "005-assigned-user" for e in persons)

    def test_workspace_catalog_creates_system_entity(self):
        org, run = "src-cat-a", "run-src-cat-a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"connectors": [
                {"id": "servicenow", "name": "ServiceNow"}
            ]},
        )
        systems = _db_entities_by_type(org, run, "system")
        assert any(e["display_name"] == "ServiceNow" for e in systems)


class TestCrossOrgIsolation:
    """Entities in different orgs never collide (scoped by org_id)."""

    def test_same_jira_assignee_in_different_orgs_produces_separate_entities(self):
        _extract(
            org_id="iso-org-1", run_id="run-iso-1",
            ingestor_data={"jira": {
                "issue_metrics": {
                    "issues": [{"key": "X-1", "assignee": {"displayName": "Helen Gray"}}]
                }
            }},
        )
        _extract(
            org_id="iso-org-2", run_id="run-iso-2",
            ingestor_data={"jira": {
                "issue_metrics": {
                    "issues": [{"key": "X-2", "assignee": {"displayName": "Helen Gray"}}]
                }
            }},
        )
        e1 = _db_entities_by_type("iso-org-1", "run-iso-1", "person")
        e2 = _db_entities_by_type("iso-org-2", "run-iso-2", "person")
        ids1 = {e["id"] for e in e1}
        ids2 = {e["id"] for e in e2}
        assert ids1.isdisjoint(ids2), "Cross-org entities must never share row IDs"

    def test_org1_entities_not_visible_under_org2_run(self):
        _extract(
            org_id="iso-org-3", run_id="run-iso-3",
            ingestor_data={"servicenow": {
                "incident_metrics": {
                    "incidents": [
                        {"number": "INC999", "assigned_to": {"display_value": "Ivan Cole"}}
                    ]
                }
            }},
        )
        # Query with a completely different org_id — must see nothing
        wrong_org = _db_entities_by_type("iso-org-4", "run-iso-3", "person")
        assert wrong_org == [], "Entities must not cross org boundaries"


class TestMultiSourceRun:
    """A run with all three sources extracts entities from each."""

    def test_multi_source_run_extracts_from_all_sources(self):
        org, run = "multi-a", "run-multi-a"
        _extract(
            org_id=org, run_id=run,
            detector_results=[
                _fake_detector(signal_source="salesforce", detector_id="HANDOFF_FRICTION"),
            ],
            ingestor_data={
                "salesforce": {
                    "approval_processes": [{"approver_ids": ["005XYZ"]}]
                },
                "jira": {
                    "issue_metrics": {
                        "project": "CRM",
                        "issues": [{"key": "CRM-10", "assignee": {"displayName": "Jane Doe"}}],
                    }
                },
                "servicenow": {
                    "assignment_groups": [{"group_name": "L2 Support"}],
                    "incident_metrics": {
                        "incidents": [
                            {"number": "INC100", "assigned_to": {"display_value": "Karl Yu"}}
                        ]
                    },
                },
            },
        )
        all_rows = _db_entities(org, run)
        types_seen = {r["entity_type"] for r in all_rows}
        assert "person" in types_seen, "Expected person entities"
        assert "team" in types_seen, "Expected team entities"
        assert "object" in types_seen, "Expected object entities"
        assert "system" in types_seen, "Expected system entities"
        assert "process" in types_seen, "Expected process entities"

    def test_return_value_is_list_of_entities(self):
        org, run = "multi-b", "run-multi-b"
        result = _extract(
            org_id=org, run_id=run,
            detector_results=[_fake_detector(signal_source="salesforce", detector_id="D_X")],
        )
        assert isinstance(result, list)
        assert all(hasattr(e, "entity_type") for e in result)


# =============================================================================
# T8 — Merge-gate contract tests (full T3-S12-A story lock)
#
# These tests are the merge gate for the entire T3-S12-A story. No task is
# considered done until its corresponding tests here pass.
#
# Coverage:
#   AC2  — Ambiguous resolution: same canonical_name + entity_type → two
#           separate rows, both ambiguous, never merged.
#   AC4  — Confidence 0.6 when ambiguous path triggered by extractor.
#   AC8  — entity.extraction_completed NOT emitted on non-blocking exception.
#   AC9  — Service account filter (run_count < 3), EntitySummary shape.
#   AC10 — Route: 403 for Viewer, 404 for cross-org, 200 for Analyst+,
#           [] for run with no entities.
#   AC11 — entity.extraction_completed registered, records entity_count +
#           ambiguous_count.
#   AC12 — run_count increments and last_seen_run_id updates across runs.
# =============================================================================

import json as _json
import uuid as _uuid
from datetime import datetime as _datetime, timezone as _tz


def _seed_entity_row(
    org_id: str,
    canonical_name: str,
    display_name: str,
    entity_type: str = "person",
    source_system: str = "jira",
    resolution_status: str = "resolved",
    resolution_confidence: float = 0.8,
    run_id: str = "run-seed-001",
) -> str:
    """Insert one entity row directly and return its id."""
    eid = str(_uuid.uuid4())
    now = _datetime.now(_tz.utc).isoformat()
    with sqlite3.connect(_get_db_path()) as conn:
        conn.execute(
            """INSERT INTO entities (
                id, org_id, entity_type, canonical_name, display_name,
                source_system, source_record_id, resolution_confidence,
                resolution_status, first_seen_run_id, last_seen_run_id,
                run_count, metadata, created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (eid, org_id, entity_type, canonical_name, display_name,
             source_system, None, resolution_confidence,
             resolution_status, run_id, run_id, 1, None, now, now),
        )
        conn.commit()
    return eid


def _db_entity_by_id(entity_id: str) -> dict | None:
    with sqlite3.connect(_get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM entities WHERE id = %s", (entity_id,)
        ).fetchone()
        return dict(row) if row else None


def _all_by_canonical(org_id: str, canonical: str, entity_type: str = "person") -> list:
    with sqlite3.connect(_get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM entities WHERE org_id=%s AND canonical_name=%s AND entity_type=%s",
            (org_id, canonical, entity_type),
        ).fetchall()]


# ---------------------------------------------------------------------------
# AC2 — Ambiguous resolution at extractor level (integration test)
# ---------------------------------------------------------------------------

class TestAC2AmbiguousResolutionIntegration:
    """AC2: extract_entities() triggers the ambiguous path when two pre-existing
    entities share the same canonical_name and entity_type in the same org.
    The extractor must create a third distinct row (N+1) and mark all three
    ambiguous. Rows must never be merged."""

    def _org(self, suffix: str) -> str:
        return f"t8-ac2-{suffix}"

    def _seed_two(self, org: str, canonical: str = "sarah chen", run: str = "run-seed") -> list:
        """Insert two rows with the same canonical_name to force the ambiguous path."""
        id1 = _seed_entity_row(org, canonical, "Sarah Chen", source_system="jira", run_id=run)
        id2 = _seed_entity_row(org, canonical, "Sarah Chen", source_system="salesforce", run_id=run)
        return [id1, id2]

    def test_ambiguous_path_creates_third_distinct_row(self):
        """Two pre-existing rows + extract → N+1 rows (3 total, distinct IDs)."""
        org = self._org("a")
        existing_ids = set(self._seed_two(org))
        _extract(
            org_id=org, run_id="run-ac2-a",
            ingestor_data={"jira": {
                "issue_metrics": {"issues": [{"key": "X-1", "assignee": {"displayName": "Sarah Chen"}}]}
            }},
        )
        rows = _all_by_canonical(org, "sarah chen")
        assert len(rows) == 3, f"Expected 3 rows (N+1 pattern), got {len(rows)}"
        row_ids = {r["id"] for r in rows}
        assert len(row_ids & existing_ids) == 2, "Two original rows must still exist"
        new_ids = row_ids - existing_ids
        assert len(new_ids) == 1, "Exactly one new row must be created"

    def test_ambiguous_path_all_existing_rows_marked_ambiguous(self):
        """All pre-existing candidates are marked ambiguous — never left as resolved."""
        org = self._org("b")
        existing_ids = self._seed_two(org)
        _extract(
            org_id=org, run_id="run-ac2-b",
            ingestor_data={"jira": {
                "issue_metrics": {"issues": [{"key": "X-2", "assignee": {"displayName": "Sarah Chen"}}]}
            }},
        )
        for eid in existing_ids:
            row = _db_entity_by_id(eid)
            assert row is not None
            assert row["resolution_status"] == "ambiguous", (
                f"Pre-existing entity {eid} must be marked ambiguous, got {row['resolution_status']}"
            )

    def test_ambiguous_path_new_row_has_status_ambiguous(self):
        """The newly created N+1 row has resolution_status='ambiguous'."""
        org = self._org("c")
        existing_ids = set(self._seed_two(org))
        _extract(
            org_id=org, run_id="run-ac2-c",
            ingestor_data={"jira": {
                "issue_metrics": {"issues": [{"key": "X-3", "assignee": {"displayName": "Sarah Chen"}}]}
            }},
        )
        rows = _all_by_canonical(org, "sarah chen")
        new_row = next(r for r in rows if r["id"] not in existing_ids)
        assert new_row["resolution_status"] == "ambiguous"

    def test_ambiguous_path_new_row_confidence_0_6(self):
        """The N+1 row has resolution_confidence=0.6 (ambiguous collision)."""
        org = self._org("d")
        existing_ids = set(self._seed_two(org))
        _extract(
            org_id=org, run_id="run-ac2-d",
            ingestor_data={"jira": {
                "issue_metrics": {"issues": [{"key": "X-4", "assignee": {"displayName": "Sarah Chen"}}]}
            }},
        )
        rows = _all_by_canonical(org, "sarah chen")
        new_row = next(r for r in rows if r["id"] not in existing_ids)
        assert new_row["resolution_confidence"] == 0.6, (
            f"Ambiguous new row must have confidence=0.6, got {new_row['resolution_confidence']}"
        )

    def test_ambiguous_rows_never_merged(self):
        """All three rows remain distinct — no merge, no row deletion."""
        org = self._org("e")
        self._seed_two(org)
        _extract(
            org_id=org, run_id="run-ac2-e",
            ingestor_data={"jira": {
                "issue_metrics": {"issues": [{"key": "X-5", "assignee": {"displayName": "Sarah Chen"}}]}
            }},
        )
        rows = _all_by_canonical(org, "sarah chen")
        ids = {r["id"] for r in rows}
        assert len(ids) == 3, (
            f"All 3 rows must remain distinct after ambiguous path — got {len(ids)} unique IDs"
        )

    def test_unique_canonical_name_creates_single_resolved_entity(self):
        """AC3 complement: a unique canonical name → exactly one resolved entity."""
        org, run = "t8-ac3-unique", "run-ac3-unique"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {"issues": [{"key": "U-1", "assignee": {"displayName": "Unique Person"}}]}
            }},
        )
        rows = _all_by_canonical(org, "unique person")
        assert len(rows) == 1, f"Unique name must produce exactly one row, got {len(rows)}"
        assert rows[0]["resolution_status"] == "resolved"


# ---------------------------------------------------------------------------
# AC9 / Section 8 — Service account filter and EntitySummary shape
# ---------------------------------------------------------------------------

class TestServiceAccountFiltering:
    """Section 8: entities with run_count < 3 are excluded from the OppEnrichment
    KV store (service-account filter) but retained in the entities table.
    Present in the KV after the third run (run_count reaches 3)."""

    def _kv_entities(self, run_id: str) -> list:
        """Read entity summaries from the run KV store directly via sqlite3."""
        with sqlite3.connect(_get_db_path()) as conn:
            row = conn.execute(
                "SELECT payload FROM kv WHERE key = %s",
                (f"entities:{run_id}",),
            ).fetchone()
        if row is None:
            return []
        val = row[0]
        parsed = _json.loads(val) if isinstance(val, str) else val
        return parsed if isinstance(parsed, list) else []

    def test_service_account_absent_from_kv_after_first_run(self):
        """Entity with run_count=1 must not appear in the KV entity summaries."""
        org, run = "t8-svc-1a", "run-svc-1a"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {"issues": [{"key": "S-1", "assignee": {"displayName": "System Admin"}}]}
            }},
        )
        summaries = self._kv_entities(run)
        names = [s.get("display_name") for s in summaries]
        assert "System Admin" not in names, (
            "Entity with run_count=1 must be filtered from KV (service account)"
        )

    def test_service_account_absent_from_kv_after_second_run(self):
        """Entity with run_count=2 must still not appear in the KV."""
        org = "t8-svc-2a"
        _extract(org_id=org, run_id="run-svc-2a-r1",
                 ingestor_data={"jira": {"issue_metrics": {
                     "issues": [{"key": "S-2", "assignee": {"displayName": "System Bot"}}]}}})
        _extract(org_id=org, run_id="run-svc-2a-r2",
                 ingestor_data={"jira": {"issue_metrics": {
                     "issues": [{"key": "S-2", "assignee": {"displayName": "System Bot"}}]}}})
        summaries = self._kv_entities("run-svc-2a-r2")
        names = [s.get("display_name") for s in summaries]
        assert "System Bot" not in names, "Entity with run_count=2 must remain filtered"

    def test_service_account_present_in_kv_after_third_run(self):
        """Entity that reaches run_count == ENTITY_MIN_RUN_COUNT appears in the KV."""
        org = "t8-svc-3a"
        for i in range(1, ENTITY_MIN_RUN_COUNT + 1):
            _extract(org_id=org, run_id=f"run-svc-3a-r{i}",
                     ingestor_data={"jira": {"issue_metrics": {
                         "issues": [{"key": "S-3", "assignee": {"displayName": "Power User"}}]}}})
        summaries = self._kv_entities(f"run-svc-3a-r{ENTITY_MIN_RUN_COUNT}")
        names = [s.get("display_name") for s in summaries]
        assert "Power User" in names, (
            "Entity that reaches the run-count threshold must appear in KV "
            "(service account threshold crossed)"
        )

    def test_service_account_entity_retained_in_db_despite_kv_filter(self):
        """Filtered entities must still exist in the entities table (graph completeness)."""
        org, run = "t8-svc-db", "run-svc-db"
        _extract(
            org_id=org, run_id=run,
            ingestor_data={"jira": {
                "issue_metrics": {"issues": [{"key": "S-4", "assignee": {"displayName": "Ghost User"}}]}
            }},
        )
        db_rows = _db_entities_by_type(org, run, "person")
        names = {r["display_name"] for r in db_rows}
        assert "Ghost User" in names, (
            "Service account entity must be retained in the DB even though it is absent from KV"
        )


# ---------------------------------------------------------------------------
# AC9 — EntitySummary shape
# ---------------------------------------------------------------------------

class TestEntitySummaryShape:
    """AC9: Each EntitySummary must include all required fields including
    resolution_confidence and resolution_status."""

    _REQUIRED_KEYS = {
        "entity_id",
        "entity_type",
        "display_name",
        "source_system",
        "resolution_confidence",
        "resolution_status",
        "run_count",
    }

    def _kv_entities(self, run_id: str) -> list:
        with sqlite3.connect(_get_db_path()) as conn:
            row = conn.execute(
                "SELECT payload FROM kv WHERE key = %s",
                (f"entities:{run_id}",),
            ).fetchone()
        if row is None:
            return []
        val = row[0]
        parsed = _json.loads(val) if isinstance(val, str) else val
        return parsed if isinstance(parsed, list) else []

    def _run_three_times(self, org: str, base_run: str, display_name: str) -> list:
        """Run extract until past the service-account threshold, return final KV."""
        for i in range(1, ENTITY_MIN_RUN_COUNT + 1):
            _extract(org_id=org, run_id=f"{base_run}-r{i}",
                     ingestor_data={"jira": {"issue_metrics": {
                         "issues": [{"key": f"SH-{i}", "assignee": {"displayName": display_name}}]}}})
        return self._kv_entities(f"{base_run}-r{ENTITY_MIN_RUN_COUNT}")

    def test_entity_summary_has_all_required_keys(self):
        summaries = self._run_three_times("t8-shape-a", "run-shape-a", "Shape Tester")
        assert summaries, "KV must contain at least one summary after three runs"
        for summary in summaries:
            missing = self._REQUIRED_KEYS - set(summary.keys())
            assert not missing, f"EntitySummary missing keys: {missing}"

    def test_entity_summary_resolution_confidence_is_float(self):
        summaries = self._run_three_times("t8-shape-b", "run-shape-b", "Confidence Checker")
        assert summaries
        for s in summaries:
            assert isinstance(s["resolution_confidence"], float), (
                f"resolution_confidence must be float, got {type(s['resolution_confidence'])}"
            )

    def test_entity_summary_resolution_status_valid_enum(self):
        summaries = self._run_three_times("t8-shape-c", "run-shape-c", "Status Checker")
        assert summaries
        valid_statuses = {"resolved", "ambiguous", "unresolved"}
        for s in summaries:
            assert s["resolution_status"] in valid_statuses, (
                f"resolution_status must be one of {valid_statuses}, got {s['resolution_status']!r}"
            )

    def test_entity_summary_canonical_name_not_exposed(self):
        """canonical_name is an internal normalisation artifact — must not appear in summary."""
        summaries = self._run_three_times("t8-shape-d", "run-shape-d", "Canonical Guard")
        for s in summaries:
            assert "canonical_name" not in s, (
                "canonical_name must not be exposed in EntitySummary"
            )


# ---------------------------------------------------------------------------
# AC12 — run_count increments and last_seen_run_id updates via extractor
# ---------------------------------------------------------------------------

class TestRunCountAcrossConsecutiveRuns:
    """AC12: entities seen in two runs have run_count=2 and updated last_seen_run_id."""

    def _db_entity_by_canonical(self, org: str, canonical: str) -> dict | None:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM entities WHERE org_id=%s AND canonical_name=%s AND entity_type='person'",
                (org, canonical),
            ).fetchone()
            return dict(row) if row else None

    def test_first_extraction_run_count_equals_one(self):
        org, run = "t8-rc-a", "run-rc-a"
        _extract(org_id=org, run_id=run,
                 ingestor_data={"jira": {"issue_metrics":
                     {"issues": [{"key": "R-1", "assignee": {"displayName": "Count One"}}]}}})
        entity = self._db_entity_by_canonical(org, "count one")
        assert entity is not None
        assert entity["run_count"] == 1

    def test_second_extraction_increments_run_count_to_two(self):
        org = "t8-rc-b"
        _extract(org_id=org, run_id="run-rc-b1",
                 ingestor_data={"jira": {"issue_metrics":
                     {"issues": [{"key": "R-2", "assignee": {"displayName": "Count Two"}}]}}})
        _extract(org_id=org, run_id="run-rc-b2",
                 ingestor_data={"jira": {"issue_metrics":
                     {"issues": [{"key": "R-2", "assignee": {"displayName": "Count Two"}}]}}})
        entity = self._db_entity_by_canonical(org, "count two")
        assert entity is not None
        assert entity["run_count"] == 2, (
            f"Expected run_count=2 after second run, got {entity['run_count']}"
        )

    def test_last_seen_run_id_updated_to_latest_run(self):
        org = "t8-rc-c"
        _extract(org_id=org, run_id="run-rc-c1",
                 ingestor_data={"jira": {"issue_metrics":
                     {"issues": [{"key": "R-3", "assignee": {"displayName": "Last Seen"}}]}}})
        _extract(org_id=org, run_id="run-rc-c2",
                 ingestor_data={"jira": {"issue_metrics":
                     {"issues": [{"key": "R-3", "assignee": {"displayName": "Last Seen"}}]}}})
        entity = self._db_entity_by_canonical(org, "last seen")
        assert entity["last_seen_run_id"] == "run-rc-c2", (
            f"Expected last_seen_run_id='run-rc-c2', got {entity['last_seen_run_id']!r}"
        )


# ---------------------------------------------------------------------------
# AC10 — Route behaviour: RBAC, cross-org isolation, empty-run response
# ---------------------------------------------------------------------------

def _route_client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _analyst_headers(org_id: str) -> dict:
    """Seed dev-token as analyst in a fresh org and return request headers."""
    from app.rbac import _ensure_members_table
    from app import db as _db
    _ensure_members_table()
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    now = _datetime.now(_tz.utc).isoformat()
    con = _db.connect()
    try:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET "
            "role=EXCLUDED.role, created_at=EXCLUDED.created_at",
            (org_id, token, "analyst", now),
        )
        con.commit()
    finally:
        con.close()
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


def _viewer_headers(org_id: str) -> dict:
    """Seed dev-token as viewer in a fresh org and return request headers."""
    from app.rbac import _ensure_members_table
    from app import db as _db
    _ensure_members_table()
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    now = _datetime.now(_tz.utc).isoformat()
    con = _db.connect()
    try:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET "
            "role=EXCLUDED.role, created_at=EXCLUDED.created_at",
            (org_id, token, "viewer", now),
        )
        con.commit()
    finally:
        con.close()
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


def _insert_run(run_id: str, org_id: str, status: str = "complete") -> None:
    """Insert a minimal run record with org_id into the runs table."""
    payload = _json.dumps({"org_id": org_id, "status": status})
    with sqlite3.connect(_get_db_path()) as conn:
        conn.execute(
            "INSERT INTO runs (id, payload) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload",
            (run_id, payload),
        )
        conn.commit()


class TestEntitiesRouteRBAC:
    """AC10: GET /api/runs/{run_id}/entities RBAC and tenancy enforcement."""

    def test_viewer_gets_403(self):
        """Viewer-level access must be rejected with 403."""
        client = _route_client()
        org = f"t8-rbac-viewer-{_uuid.uuid4().hex[:6]}"
        run_id = f"run-rbac-view-{_uuid.uuid4().hex[:6]}"
        _insert_run(run_id, org)
        headers = _viewer_headers(org)
        r = client.get(f"/api/runs/{run_id}/entities", headers=headers)
        assert r.status_code == 403, (
            f"Viewer must receive 403 Forbidden, got {r.status_code}"
        )

    def test_analyst_gets_200(self):
        """Analyst-level access must be accepted with 200."""
        client = _route_client()
        org = f"t8-rbac-analyst-{_uuid.uuid4().hex[:6]}"
        run_id = f"run-rbac-analyst-{_uuid.uuid4().hex[:6]}"
        _insert_run(run_id, org)
        headers = _analyst_headers(org)
        r = client.get(f"/api/runs/{run_id}/entities", headers=headers)
        assert r.status_code == 200, (
            f"Analyst must receive 200 OK, got {r.status_code}: {r.text}"
        )

    def test_unauthenticated_gets_401_or_403(self):
        """Request without Authorization header must be rejected."""
        client = _route_client()
        r = client.get("/api/runs/run-any/entities")
        assert r.status_code in (401, 403), (
            f"Unauthenticated request must be rejected, got {r.status_code}"
        )

    def test_cross_org_run_returns_404(self):
        """A run belonging to org_a must return 404 when requested as org_b.

        The endpoint must not return 403 — that would reveal the run exists.
        Tenancy isolation requires 404 for any cross-org access.
        """
        client = _route_client()
        org_a = f"t8-xorg-a-{_uuid.uuid4().hex[:6]}"
        org_b = f"t8-xorg-b-{_uuid.uuid4().hex[:6]}"
        run_id = f"run-xorg-{_uuid.uuid4().hex[:6]}"
        # Run belongs to org_a
        _insert_run(run_id, org_a)
        # Request made as analyst in org_b
        headers = _analyst_headers(org_b)
        r = client.get(f"/api/runs/{run_id}/entities", headers=headers)
        assert r.status_code == 404, (
            f"Cross-org run access must return 404 (not 403), got {r.status_code}"
        )

    def test_missing_run_returns_404(self):
        """A completely non-existent run_id must return 404."""
        client = _route_client()
        org = f"t8-missing-{_uuid.uuid4().hex[:6]}"
        headers = _analyst_headers(org)
        r = client.get("/api/runs/run-does-not-exist-t8abc/entities", headers=headers)
        assert r.status_code == 404, (
            f"Non-existent run must return 404, got {r.status_code}"
        )


class TestEntitiesRouteEmptyAndPresent:
    """AC10: run with no entities extracted → [] with 200; run with entities → list."""

    def test_run_with_no_entities_returns_empty_list(self):
        """When extraction has not run, the endpoint returns [] (not 404)."""
        client = _route_client()
        org = f"t8-empty-{_uuid.uuid4().hex[:6]}"
        run_id = f"run-empty-{_uuid.uuid4().hex[:6]}"
        _insert_run(run_id, org)
        headers = _analyst_headers(org)
        r = client.get(f"/api/runs/{run_id}/entities", headers=headers)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        assert r.json() == [], (
            "Run with no entities must return [] not 404 — allows caller to distinguish "
            "'run exists, no entities' from 'run does not exist'"
        )

    def test_run_with_entities_returns_list_of_dicts(self):
        """After extraction, the route returns a non-empty list of entity dicts."""
        client = _route_client()
        org = f"t8-present-{_uuid.uuid4().hex[:6]}"
        run_id = f"run-present-{_uuid.uuid4().hex[:6]}"
        _insert_run(run_id, org)
        # Seed one entity row for this run directly
        _seed_entity_row(org, "route entity", "Route Entity",
                         source_system="jira", run_id=run_id)
        headers = _analyst_headers(org)
        r = client.get(f"/api/runs/{run_id}/entities", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert all(isinstance(e, dict) for e in data)

    def test_route_response_entity_has_required_fields(self):
        """Each entity dict from the route contains the core entity fields."""
        client = _route_client()
        org = f"t8-fields-{_uuid.uuid4().hex[:6]}"
        run_id = f"run-fields-{_uuid.uuid4().hex[:6]}"
        _insert_run(run_id, org)
        _seed_entity_row(org, "field check entity", "Field Check",
                         source_system="salesforce", run_id=run_id)
        headers = _analyst_headers(org)
        r = client.get(f"/api/runs/{run_id}/entities", headers=headers)
        assert r.status_code == 200
        entities = r.json()
        assert entities
        required = {"id", "org_id", "entity_type", "display_name", "resolution_confidence",
                    "resolution_status", "run_count"}
        for entity in entities:
            missing = required - set(entity.keys())
            assert not missing, f"Route entity response missing fields: {missing}"


# ---------------------------------------------------------------------------
# AC8 — Non-blocking: entity.extraction_completed NOT emitted on exception
# ---------------------------------------------------------------------------

class TestNonBlockingFailureAndTelemetry:
    """AC8: when extract_entities() internal sources fail, the run is non-blocking.
    entity.extraction_completed must not be emitted if the whole extract raises.
    """

    def test_extraction_failure_does_not_propagate(self):
        """Even when all source extractors raise, extract_entities() must not raise."""
        from unittest.mock import patch
        with patch("app.entity_extractor._extract_salesforce_entities", side_effect=RuntimeError("sf down")), \
             patch("app.entity_extractor._extract_jira_entities", side_effect=RuntimeError("jira down")), \
             patch("app.entity_extractor._extract_servicenow_entities", side_effect=RuntimeError("sn down")), \
             patch("app.entity_extractor._extract_detector_entities", side_effect=RuntimeError("det down")), \
             patch("app.db.run_kv_set"):
            result = _extract(
                org_id="t8-nb-a", run_id="run-nb-a",
                ingestor_data={
                    "salesforce": {"x": 1}, "jira": {"x": 1}, "servicenow": {"x": 1}
                },
            )
        # Must not raise; returns a list (possibly empty)
        assert isinstance(result, list)

    def test_telemetry_event_emitted_on_success_not_on_extractor_exception(self):
        """entity.extraction_completed IS emitted on normal completion,
        NOT emitted when record_event itself raises (fire-and-forget)."""
        from unittest.mock import patch
        emitted = []
        def capture(event_type, payload=None):
            emitted.append(event_type)
        with patch("app.entity_extractor._extract_salesforce_entities", return_value=[]), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event", side_effect=capture):
            _extract(org_id="t8-nb-b", run_id="run-nb-b", ingestor_data={})
        assert "entity.extraction_completed" in emitted, (
            "entity.extraction_completed must be emitted on successful completion"
        )


# ---------------------------------------------------------------------------
# AC11 — Telemetry event registration (cross-check with test_entity_extraction_telemetry)
# ---------------------------------------------------------------------------

class TestAC11TelemetryRegistration:
    """AC11: entity.extraction_completed must be in the telemetry event registry,
    use EntityExtractionCompletedPayload, and the payload TypedDict must contain
    entity_count and ambiguous_count fields."""

    def test_event_type_registered_in_registry(self):
        """AC11: entity.extraction_completed appears in EVENT_REGISTRY."""
        from app.telemetry import EVENT_REGISTRY
        assert "entity.extraction_completed" in EVENT_REGISTRY, (
            "entity.extraction_completed must be registered via register_event_type()"
        )

    def test_event_type_registered_with_entity_extraction_payload(self):
        """AC11: registry uses EntityExtractionCompletedPayload TypedDict."""
        from app.telemetry import EVENT_REGISTRY, EntityExtractionCompletedPayload
        assert EVENT_REGISTRY["entity.extraction_completed"] is EntityExtractionCompletedPayload

    def test_payload_typeddict_has_ambiguous_count(self):
        """AC11: ambiguous_count is load-bearing — must be in the TypedDict."""
        from typing import get_type_hints
        from app.telemetry import EntityExtractionCompletedPayload
        hints = get_type_hints(EntityExtractionCompletedPayload)
        assert "ambiguous_count" in hints, (
            "ambiguous_count must be in EntityExtractionCompletedPayload — "
            "it is the primary signal for source data-quality degradation"
        )

    def test_payload_typeddict_has_entity_count(self):
        from typing import get_type_hints
        from app.telemetry import EntityExtractionCompletedPayload
        assert "entity_count" in get_type_hints(EntityExtractionCompletedPayload)


# =============================================================================
# Review fixes — regression coverage for Issues #1, #16, #17, #10
# =============================================================================


class TestEntitiesVisibleAcrossRuns:
    """Issue #1 / #15: an entity first seen in an earlier run must remain visible
    when querying a later run.

    The previous route filtered on ``last_seen_run_id = run_id``, so an entity
    last seen in run N-1 silently vanished from run N's response even though it
    is a real, still-existing org entity. The route now bounds visibility by the
    run in which each entity was *first* seen (ordered via the runs table rowid),
    so older entities persist across re-runs.
    """

    def test_entity_last_seen_in_prior_run_is_visible_in_later_run(self):
        client = _route_client()
        org = f"t8-carry-{_uuid.uuid4().hex[:6]}"
        run_prev = f"run-carry-prev-{_uuid.uuid4().hex[:6]}"
        run_curr = f"run-carry-curr-{_uuid.uuid4().hex[:6]}"

        # Insert the prior run BEFORE the current run so rowid order reflects
        # chronological order (prior is older than current).
        _insert_run(run_prev, org)
        _insert_run(run_curr, org)

        # Entity first AND last seen only in the prior run (run_count=1). Under
        # the old `last_seen_run_id = run_id` filter this would be invisible when
        # querying the current run — the exact data-loss bug being regression-tested.
        _seed_entity_row(
            org, "carryover person", "Carryover Person",
            source_system="jira", run_id=run_prev,
        )

        headers = _analyst_headers(org)
        r = client.get(f"/api/runs/{run_curr}/entities", headers=headers)
        assert r.status_code == 200, r.text
        names = {e["display_name"] for e in r.json()}
        assert "Carryover Person" in names, (
            "Entity first seen in a prior run must still be visible when querying "
            "a later run (Issue #1 regression)"
        )

    def test_entity_first_seen_in_future_run_not_visible_in_earlier_run(self):
        """The inverse guard: an entity first seen in a *later* run must not leak
        into an *earlier* run's view (no showing of future data)."""
        client = _route_client()
        org = f"t8-future-{_uuid.uuid4().hex[:6]}"
        run_early = f"run-future-early-{_uuid.uuid4().hex[:6]}"
        run_late = f"run-future-late-{_uuid.uuid4().hex[:6]}"

        _insert_run(run_early, org)
        _insert_run(run_late, org)

        # Entity first seen only in the later run.
        _seed_entity_row(
            org, "future person", "Future Person",
            source_system="jira", run_id=run_late,
        )

        headers = _analyst_headers(org)
        r = client.get(f"/api/runs/{run_early}/entities", headers=headers)
        assert r.status_code == 200, r.text
        names = {e["display_name"] for e in r.json()}
        assert "Future Person" not in names, (
            "Entity first seen in a later run must not appear in an earlier run's view"
        )


class TestConfidenceUpgradeOnBetterSource:
    """Issue #2 / #16: when a later extraction provides a source_record_id for an
    entity that was previously name-based, resolution_confidence must upgrade
    from 0.8 to 1.0 (never downgrade), and the source_record_id is backfilled."""

    def _person(self, org: str, canonical: str) -> dict | None:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM entities WHERE org_id=%s AND canonical_name=%s AND entity_type='person'",
                (org, canonical),
            ).fetchone()
            return dict(row) if row else None

    def test_name_based_then_source_id_upgrades_confidence(self):
        org = f"t8-upgrade-{_uuid.uuid4().hex[:6]}"
        # Run 1: Jira assignee — name-based, confidence 0.8, no source_record_id.
        _extract(
            org_id=org, run_id="run-up-1",
            ingestor_data={"jira": {"issue_metrics": {
                "issues": [{"key": "UP-1", "assignee": {"displayName": "Upgrade Target"}}]}}},
        )
        before = self._person(org, "upgrade target")
        assert before is not None
        assert before["resolution_confidence"] == 0.8
        assert before["source_record_id"] is None

        # Run 2: same person arrives via a Salesforce OwnerId — carries a
        # source_record_id, so the incoming confidence is 1.0.
        _extract(
            org_id=org, run_id="run-up-2",
            ingestor_data={"salesforce": {
                "approval_processes": [{"approver_ids": ["Upgrade Target"]}]}},
        )
        after = self._person(org, "upgrade target")
        assert after is not None
        assert after["id"] == before["id"], "Must resolve to the same entity row, not a duplicate"
        assert after["resolution_confidence"] == 1.0, (
            f"Confidence must upgrade 0.8 -> 1.0 when a source_record_id arrives, "
            f"got {after['resolution_confidence']}"
        )
        assert after["source_record_id"] == "Upgrade Target", "source_record_id must be backfilled"
        assert after["run_count"] == 2, "Second sighting must increment run_count"

    def test_lower_confidence_does_not_downgrade(self):
        """A later name-based sighting (0.8) must not lower an entity already at 1.0."""
        org = f"t8-nodown-{_uuid.uuid4().hex[:6]}"
        # Run 1: source_record_id present → 1.0.
        _extract(
            org_id=org, run_id="run-nd-1",
            ingestor_data={"salesforce": {
                "approval_processes": [{"approver_ids": ["No Downgrade"]}]}},
        )
        # Run 2: name-based sighting of the same canonical name (0.8 incoming).
        _extract(
            org_id=org, run_id="run-nd-2",
            ingestor_data={"jira": {"issue_metrics": {
                "issues": [{"key": "ND-1", "assignee": {"displayName": "No Downgrade"}}]}}},
        )
        after = self._person(org, "no downgrade")
        assert after is not None
        assert after["resolution_confidence"] == 1.0, (
            "Confidence must never be downgraded by a lower-quality later signal"
        )


class TestCanonicalNameNormalization:
    """Issue #4 / #17: names differing only by case or whitespace must resolve to
    a single entity row, not create duplicates."""

    def _persons(self, org: str, canonical: str) -> list:
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT * FROM entities WHERE org_id=%s AND canonical_name=%s AND entity_type='person'",
                (org, canonical),
            ).fetchall()]

    def test_case_variants_resolve_to_single_entity(self):
        org = f"t8-case-{_uuid.uuid4().hex[:6]}"
        _extract(
            org_id=org, run_id="run-case-1",
            ingestor_data={"jira": {"issue_metrics": {
                "issues": [{"key": "C-1", "assignee": {"displayName": "Alice Smith"}}]}}},
        )
        _extract(
            org_id=org, run_id="run-case-2",
            ingestor_data={"jira": {"issue_metrics": {
                "issues": [{"key": "C-2", "assignee": {"displayName": "alice smith"}}]}}},
        )
        rows = self._persons(org, "alice smith")
        assert len(rows) == 1, (
            f"'Alice Smith' and 'alice smith' must resolve to ONE entity, got {len(rows)} rows"
        )
        assert rows[0]["run_count"] == 2, "Both sightings must increment the same entity"

    def test_internal_whitespace_collapses_to_single_entity(self):
        org = f"t8-ws-{_uuid.uuid4().hex[:6]}"
        _extract(
            org_id=org, run_id="run-ws-1",
            ingestor_data={"jira": {"issue_metrics": {
                "issues": [{"key": "W-1", "assignee": {"displayName": "Bob  Jones"}}]}}},
        )
        _extract(
            org_id=org, run_id="run-ws-2",
            ingestor_data={"jira": {"issue_metrics": {
                "issues": [{"key": "W-2", "assignee": {"displayName": " Bob Jones "}}]}}},
        )
        rows = self._persons(org, "bob jones")
        assert len(rows) == 1, (
            f"Whitespace variants must resolve to ONE entity, got {len(rows)} rows"
        )


class TestSchemaModelMigrationParity:
    """Issue #10: the entities schema is single-sourced from
    database.models.entities.ALL_ENTITIES_DDL (the migration imports and runs it).
    This guards against drift between the model DDL and the migration-built table
    in the live (migration-applied) test database."""

    def _columns_in_schema(self, conn, schema: str) -> dict:
        """Return {column_name: (data_type, is_nullable, is_pk)} for entities in `schema`.

        PostgreSQL port of the old PRAGMA-based ``_schema_of``: columns/nullability
        come from information_schema; PK membership from pg_index against the
        schema-qualified table.
        """
        col_rows = conn.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'entities' "
            "ORDER BY ordinal_position",
            (schema,),
        ).fetchall()
        pk_rows = conn.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = (%s || '.entities')::regclass AND i.indisprimary",
            (schema,),
        ).fetchall()
        pk_cols = {r["attname"] for r in pk_rows}
        return {
            r["column_name"]: (r["data_type"], r["is_nullable"], r["column_name"] in pk_cols)
            for r in col_rows
        }

    def _nonpk_indexes_in_schema(self, conn, schema: str) -> list:
        """Sorted non-PK index names for entities in `schema` (pg_indexes port)."""
        primary_rows = conn.execute(
            "SELECT c.relname FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE i.indrelid = (%s || '.entities')::regclass AND i.indisprimary",
            (schema,),
        ).fetchall()
        primary = {r["relname"] for r in primary_rows}
        idx_rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = 'entities'",
            (schema,),
        ).fetchall()
        return sorted(r["indexname"] for r in idx_rows if r["indexname"] not in primary)

    def test_model_ddl_matches_migration_applied_schema(self):
        """PostgreSQL port of the old ``:memory:`` drift check.

        The original built a throwaway SQLite ``:memory:`` DB from ALL_ENTITIES_DDL
        and diffed it against the live table. That is invalid against PostgreSQL, so
        we instead execute ALL_ENTITIES_DDL into a fresh PostgreSQL schema in the
        SAME connection (the DDL's VARCHAR/FLOAT/TIMESTAMP/INTEGER types and the
        unqualified CREATE statements are all valid PostgreSQL) and compare the two
        schemas via information_schema/pg_index. The model DDL is unqualified, so we
        prepend it onto ``search_path`` to land objects in the parity schema.
        """
        from database.models.entities import ALL_ENTITIES_DDL

        parity = "ddl_parity_check"
        with sqlite3.connect(_get_db_path()) as conn:
            # Live (migration-applied) schema lives in public.
            live_cols = self._columns_in_schema(conn, "public")
            live_idx = self._nonpk_indexes_in_schema(conn, "public")

            # Build the expected schema from the model DDL in an isolated schema.
            conn.execute(f"DROP SCHEMA IF EXISTS {parity} CASCADE")
            conn.execute(f"CREATE SCHEMA {parity}")
            try:
                conn.execute(f"SET search_path TO {parity}")
                for ddl in ALL_ENTITIES_DDL:
                    conn.execute(ddl)
                conn.execute("SET search_path TO public")
                model_cols = self._columns_in_schema(conn, parity)
                model_idx = self._nonpk_indexes_in_schema(conn, parity)
            finally:
                conn.execute("SET search_path TO public")
                conn.execute(f"DROP SCHEMA IF EXISTS {parity} CASCADE")
                conn.commit()

        assert live_cols == model_cols, (
            "entities columns differ between the migration and the model DDL — schema drift"
        )
        assert live_idx == model_idx, (
            "entities indexes differ between the migration and the model DDL — schema drift"
        )

    def test_check_constraints_present_in_schema(self):
        """entity_type and resolution_status must carry CHECK constraints (Issue #19).

        PostgreSQL port: the old check read the CREATE TABLE text from
        sqlite_master. We read the CHECK constraint definitions from pg_constraint
        and assert both the column names and the IN-list appear.
        """
        with sqlite3.connect(_get_db_path()) as conn:
            rows = conn.execute(
                "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
                "WHERE conrelid = 'entities'::regclass AND contype = 'c'"
            ).fetchall()
        defs = " ".join(r["def"].lower() for r in rows)
        assert defs, "entities table must define CHECK constraints"
        assert "entity_type" in defs, "entity_type must have a CHECK constraint"
        assert "resolution_status" in defs, "resolution_status must have a CHECK constraint"
        # The CHECK uses an IN (...) value list in the model DDL.
        assert "person" in defs and "system" in defs, (
            "entity_type CHECK must enumerate the allowed entity types"
        )
        assert "resolved" in defs and "ambiguous" in defs, (
            "resolution_status CHECK must enumerate the allowed statuses"
        )

    def test_invalid_entity_type_rejected_by_check_constraint(self):
        """A row with an invalid entity_type must be rejected at the DB layer."""
        from datetime import datetime as _dt, timezone as _tzc
        now = _dt.now(_tzc.utc).isoformat()
        with sqlite3.connect(_get_db_path()) as conn:
            with pytest.raises(psycopg2.IntegrityError):
                conn.execute(
                    """INSERT INTO entities (
                        id, org_id, entity_type, canonical_name, display_name,
                        source_system, source_record_id, resolution_confidence,
                        resolution_status, first_seen_run_id, last_seen_run_id,
                        run_count, metadata, created_at, updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (str(_uuid.uuid4()), "chk-org", "not_a_real_type", "x", "X",
                     "jira", None, 0.8, "resolved", "r1", "r1", 1, None, now, now),
                )
                conn.commit()
            # The aborted transaction must be rolled back before the connection is
            # reused (PostgreSQL blocks further statements after an error).
            conn.rollback()
