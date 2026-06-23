"""Contract tests for T3-S12-A T2 — resolve_or_create_entity().

AC2 — Two entities sharing canonical_name + entity_type → two separate rows,
       both resolution_status='ambiguous'. Never merged.
AC3 — Unique canonical_name → one entity, resolution_status='resolved'.
AC4 — Salesforce entity with source_record_id → confidence=1.0.
       Jira name-only entity → confidence=0.8.
AC5 — System and Process entities → confidence=1.0 (stable AgentIQ IDs).

Additional coverage:
  - New entity (zero candidates) is created with correct fields.
  - Single candidate: run_count incremented, last_seen_run_id updated,
    confidence upgraded to max(existing, incoming) when a higher-quality signal
    arrives (e.g. a source_record_id for a previously name-based row) — never
    downgraded (Issue #2).
  - Cross-org isolation: same canonical_name in different orgs → no collision.
  - ambiguous path creates N+1 rows (all candidates + new row).
"""
import os
import sqlite3

import pytest

from database.models.entities import ALL_ENTITIES_DDL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> str:
    return os.environ.get("DB_PATH", "")


def _call(
    *,
    org_id: str = "test-org",
    entity_type: str = "person",
    display_name: str = "Sarah Chen",
    source_system: str = "jira",
    source_record_id=None,
    run_id: str = "run-001",
    metadata=None,
):
    from app.entity_resolution import resolve_or_create_entity
    return resolve_or_create_entity(
        org_id=org_id,
        entity_type=entity_type,
        display_name=display_name,
        source_system=source_system,
        source_record_id=source_record_id,
        run_id=run_id,
        metadata=metadata,
    )


def _all_entities(org_id: str = "test-org", entity_type: str = "person", canonical: str = "sarah chen"):
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM entities WHERE org_id=%s AND entity_type=%s AND canonical_name=%s",
                (org_id, entity_type, canonical),
            ).fetchall()
        ]


def _get_entity(entity_id: str):
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM entities WHERE id=%s", (entity_id,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# AC3 — unique canonical_name → resolved
# ---------------------------------------------------------------------------

class TestNewEntityCreation:
    """AC3: unique canonical_name → one entity, resolution_status='resolved'."""

    def test_creates_entity_with_resolved_status(self):
        e = _call(display_name="Jordan Lee", org_id="org-ac3a")
        assert e.resolution_status == "resolved"

    def test_canonical_name_is_lowercased_and_stripped(self):
        e = _call(display_name="  Alice Wan  ", org_id="org-ac3b")
        assert e.canonical_name == "alice wan"

    def test_display_name_preserved_as_original(self):
        e = _call(display_name="  Alice Wan  ", org_id="org-ac3c")
        assert e.display_name == "  Alice Wan  "

    def test_first_seen_and_last_seen_run_match(self):
        e = _call(display_name="Chris Park", org_id="org-ac3d", run_id="run-xyz")
        assert e.first_seen_run_id == "run-xyz"
        assert e.last_seen_run_id == "run-xyz"

    def test_run_count_starts_at_one(self):
        e = _call(display_name="Dana Moore", org_id="org-ac3e")
        assert e.run_count == 1

    def test_entity_persisted_in_db(self):
        e = _call(display_name="Eli Torres", org_id="org-ac3f")
        fetched = _get_entity(str(e.id))
        assert fetched is not None
        assert fetched["display_name"] == "Eli Torres"

    def test_metadata_stored_as_json(self):
        import json
        e = _call(display_name="Fay Kim", org_id="org-ac3g", metadata={"team": "credit"})
        fetched = _get_entity(str(e.id))
        md = json.loads(fetched["metadata"])
        # Caller metadata round-trips as JSON; R16-B1 additionally stamps an
        # observed EvidencePointer onto every created entity.
        assert md["team"] == "credit"
        assert md["evidence_pointer"]["origin"] == "observed"


# ---------------------------------------------------------------------------
# AC4 — confidence by source evidence
# ---------------------------------------------------------------------------

class TestConfidenceAssignment:
    """AC4: source_record_id present → 1.0; name-only Jira → 0.8."""

    def test_salesforce_with_source_record_id_confidence_1(self):
        e = _call(
            display_name="Sarah Chen",
            org_id="org-ac4a",
            source_system="salesforce",
            source_record_id="005Qy000001AbcDEF",
        )
        assert e.resolution_confidence == 1.0

    def test_jira_name_only_confidence_0_8(self):
        e = _call(
            display_name="Sarah Chen",
            org_id="org-ac4b",
            source_system="jira",
            source_record_id=None,
        )
        assert e.resolution_confidence == 0.8

    def test_servicenow_with_source_record_id_confidence_1(self):
        e = _call(
            display_name="Ben Ross",
            org_id="org-ac4c",
            source_system="servicenow",
            source_record_id="INC0012345",
        )
        assert e.resolution_confidence == 1.0

    def test_single_candidate_match_confidence_upgrades_with_better_source(self):
        """Issue #2: a later, higher-quality signal upgrades confidence (never down).

        A resolved entity first seen name-based (0.8) must be raised to 1.0 when a
        subsequent run supplies a source_record_id, and the source_record_id is
        backfilled onto the existing row (not a new duplicate). Confidence is only
        ever increased — a lower incoming value would leave it unchanged.
        """
        # First call creates with confidence 0.8 (name-only Jira), no source id.
        first = _call(display_name="Gail Sun", org_id="org-ac4d", source_system="jira")
        assert first.resolution_confidence == 0.8
        assert first.source_record_id is None
        # Second call with source_record_id — confidence upgrades 0.8 -> 1.0.
        second = _call(
            display_name="Gail Sun",
            org_id="org-ac4d",
            source_system="salesforce",
            source_record_id="005XYZ",
            run_id="run-002",
        )
        assert second.id == first.id, "Must resolve to the same row, not a duplicate"
        assert second.resolution_confidence == 1.0, (
            "Single-candidate match must upgrade confidence when a higher-quality "
            "signal (source_record_id) arrives"
        )
        assert second.source_record_id == "005XYZ", "source_record_id must be backfilled"


# ---------------------------------------------------------------------------
# AC5 — System and Process entities → confidence 1.0
# ---------------------------------------------------------------------------

class TestStableEntityTypes:
    """AC5: System and Process entities always get confidence=1.0."""

    def test_system_entity_confidence_1(self):
        e = _call(
            display_name="salesforce",
            org_id="org-ac5a",
            entity_type="system",
            source_system="salesforce",
            source_record_id=None,
        )
        assert e.resolution_confidence == 1.0

    def test_process_entity_confidence_1(self):
        e = _call(
            display_name="case_auto_assign",
            org_id="org-ac5b",
            entity_type="process",
            source_system="agentiq",
            source_record_id=None,
        )
        assert e.resolution_confidence == 1.0

    def test_system_entity_resolved_status(self):
        e = _call(
            display_name="servicenow",
            org_id="org-ac5c",
            entity_type="system",
            source_system="servicenow",
        )
        assert e.resolution_status == "resolved"


# ---------------------------------------------------------------------------
# AC2 — two entities, same canonical_name + entity_type → both ambiguous
# ---------------------------------------------------------------------------

class TestAmbiguousResolution:
    """AC2: multiple candidates → separate rows, both ambiguous. No merge."""

    def _seed_two_entities(self, org: str):
        """Insert two existing entities with the same canonical name directly."""
        import json
        from datetime import datetime, timezone
        from uuid import uuid4
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (str(uuid4()), org, "person", "sarah chen", "Sarah Chen",
             "jira", None, 0.8, "resolved", "run-001", "run-001", 1, None, now, now),
            (str(uuid4()), org, "person", "sarah chen", "Sarah Chen",
             "salesforce", None, 0.8, "resolved", "run-001", "run-001", 1, None, now, now),
        ]
        with sqlite3.connect(_db()) as conn:
            conn.executemany(
                """INSERT INTO entities (
                    id, org_id, entity_type, canonical_name, display_name,
                    source_system, source_record_id, resolution_confidence,
                    resolution_status, first_seen_run_id, last_seen_run_id,
                    run_count, metadata, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                rows,
            )
            conn.commit()
        return [r[0] for r in rows]

    def test_ambiguous_creates_new_distinct_entity(self):
        org = "org-ac2a"
        ids_before = self._seed_two_entities(org)
        new = _call(display_name="Sarah Chen", org_id=org, run_id="run-002")
        rows = _all_entities(org)
        assert len(rows) == 3, f"Expected 3 rows (N+1), got {len(rows)}"
        assert str(new.id) not in ids_before

    def test_ambiguous_all_existing_marked_ambiguous(self):
        org = "org-ac2b"
        ids = self._seed_two_entities(org)
        _call(display_name="Sarah Chen", org_id=org, run_id="run-002")
        for eid in ids:
            fetched = _get_entity(eid)
            assert fetched["resolution_status"] == "ambiguous", (
                f"Entity {eid} must be marked ambiguous"
            )

    def test_ambiguous_new_entity_has_status_ambiguous(self):
        org = "org-ac2c"
        self._seed_two_entities(org)
        new = _call(display_name="Sarah Chen", org_id=org, run_id="run-002")
        assert new.resolution_status == "ambiguous"

    def test_ambiguous_new_entity_confidence_0_6(self):
        org = "org-ac2d"
        self._seed_two_entities(org)
        new = _call(display_name="Sarah Chen", org_id=org, run_id="run-002")
        assert new.resolution_confidence == 0.6

    def test_ambiguous_entities_never_merged(self):
        org = "org-ac2e"
        self._seed_two_entities(org)
        _call(display_name="Sarah Chen", org_id=org, run_id="run-002")
        rows = _all_entities(org)
        ids = {r["id"] for r in rows}
        assert len(ids) == 3, "All three rows must remain distinct — never merged"


# ---------------------------------------------------------------------------
# Run-count and last_seen_run_id updates (single candidate path)
# ---------------------------------------------------------------------------

class TestRunCountUpdate:
    """AC12: entities seen in two consecutive runs → run_count=2, updated last_seen_run_id."""

    def test_repeated_sighting_in_same_run_does_not_increment(self):
        org = "org-rc-same-run"
        _call(display_name="Pat Quinn", org_id=org, run_id="run-001")
        updated = _call(display_name="Pat Quinn", org_id=org, run_id="run-001")
        assert updated.run_count == 1

    def test_run_count_incremented_on_second_run(self):
        org = "org-rc-a"
        _call(display_name="Pat Quinn", org_id=org, run_id="run-001")
        updated = _call(display_name="Pat Quinn", org_id=org, run_id="run-002")
        assert updated.run_count == 2

    def test_last_seen_run_id_updated(self):
        org = "org-rc-b"
        _call(display_name="Robin Shaw", org_id=org, run_id="run-001")
        updated = _call(display_name="Robin Shaw", org_id=org, run_id="run-002")
        assert updated.last_seen_run_id == "run-002"

    def test_first_seen_run_id_unchanged(self):
        org = "org-rc-c"
        _call(display_name="Sam Hall", org_id=org, run_id="run-001")
        updated = _call(display_name="Sam Hall", org_id=org, run_id="run-002")
        assert updated.first_seen_run_id == "run-001"

    def test_three_consecutive_runs_count(self):
        org = "org-rc-d"
        _call(display_name="Tia Webb", org_id=org, run_id="run-001")
        _call(display_name="Tia Webb", org_id=org, run_id="run-002")
        updated = _call(display_name="Tia Webb", org_id=org, run_id="run-003")
        assert updated.run_count == 3


# ---------------------------------------------------------------------------
# Cross-org isolation
# ---------------------------------------------------------------------------

class TestCrossOrgIsolation:
    """AC10 prerequisite: same canonical_name in different orgs never collide."""

    def test_same_name_different_orgs_independent(self):
        e1 = _call(display_name="Sam Lee", org_id="org-iso-1")
        e2 = _call(display_name="Sam Lee", org_id="org-iso-2")
        assert str(e1.id) != str(e2.id)

    def test_org1_entity_not_visible_to_org2_query(self):
        _call(display_name="Quinn Blake", org_id="org-iso-3")
        rows = _all_entities(org_id="org-iso-4", canonical="quinn blake")
        assert rows == [], "Entities from org-iso-3 must not appear for org-iso-4"

    def test_same_name_same_type_different_orgs_both_resolved(self):
        e1 = _call(display_name="Morgan Kim", org_id="org-iso-5")
        e2 = _call(display_name="Morgan Kim", org_id="org-iso-6")
        assert e1.resolution_status == "resolved"
        assert e2.resolution_status == "resolved"
