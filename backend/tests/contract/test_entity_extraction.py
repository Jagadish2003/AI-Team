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


def _get_db_path() -> str:
    return os.environ["DB_PATH"]


class TestEntitiesTableSchema:
    """AC1: entities table created with all required columns and indexes."""

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

    def _index_columns(self, index_name: str) -> list[str]:
        """Return ordered column names for a specific entities index."""
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
        return [r["name"] for r in rows]

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
                    :id, :org_id, :entity_type, :canonical_name, :display_name,
                    :source_system, :source_record_id, :resolution_confidence,
                    :resolution_status, :first_seen_run_id, :last_seen_run_id,
                    :run_count, :metadata, :created_at, :updated_at
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
                "SELECT * FROM entities WHERE id = ?",
                (row["id"],),
            ).fetchone())
            by_org = dict(conn.execute(
                "SELECT * FROM entities WHERE org_id = ? AND id = ?",
                ("test-org", row["id"]),
            ).fetchone())
            by_run = dict(conn.execute(
                "SELECT * FROM entities WHERE org_id = ? AND last_seen_run_id = ?",
                ("test-org", "run-001"),
            ).fetchone())
            by_canonical = dict(conn.execute(
                """
                SELECT * FROM entities
                WHERE org_id = ? AND entity_type = ? AND canonical_name = ?
                """,
                ("test-org", "person", "sarah chen"),
            ).fetchone())
            by_source_record = dict(conn.execute(
                "SELECT * FROM entities WHERE org_id = ? AND source_record_id = ?",
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
                "SELECT * FROM entities WHERE org_id = ? AND last_seen_run_id = ?",
                (org_id, run_id),
            ).fetchall()
        ]


def _db_entities_by_type(org_id: str, run_id: str, entity_type: str) -> List[Dict]:
    with sqlite3.connect(_get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r) for r in conn.execute(
                """SELECT * FROM entities
                   WHERE org_id = ? AND last_seen_run_id = ? AND entity_type = ?""",
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
