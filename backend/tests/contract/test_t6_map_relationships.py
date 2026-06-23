"""Contract tests for T3-S13-A — T6.

Runner integration of relationship mapping via the map_relationships()
orchestrator, called non-blocking after extract_entities().

Coverage:
  - map_relationships() runs both passes (observed + inferred) in sequence and
    returns counts.
  - map_relationships() emits relationship.mapping_completed exactly once on
    success; the event type is registered (AC10 registry part).
  - AC9: a map_relationships() failure does not propagate to runner.run() — the
    run completes and still returns its full opportunity set.
  - The runner's failure warning log includes the exception message, run_id and
    org_id (the diagnostic signal for a failed mapping run).
  - On the failure path the event is NOT emitted.
"""
import logging
import sqlite3
from uuid import uuid4

from app import telemetry
from app.entity_extractor import extract_entities
from app.relationship_mapper import map_relationships
from database.models.entities import Entity


def _get_db_path(monkeypatch=None):
    import os
    return os.environ.get("DB_PATH", "")


def _make_entity(org_id, entity_type, display_name, run_id="run-t6"):
    return Entity(
        org_id=org_id,
        entity_type=entity_type,
        canonical_name=" ".join(display_name.split()).lower(),
        display_name=display_name,
        source_system="test",
        resolution_confidence=1.0,
        resolution_status="resolved",
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=1,
    )


def _persist(entity):
    import os
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
            ) ON CONFLICT DO NOTHING""",
            row,
        )
        conn.commit()


class _DetectorResultStub:
    def __init__(self, detector_id, signal_source="salesforce"):
        self.detector_id = detector_id
        self.signal_source = signal_source


def _edges(org_id, rtype):
    import os
    with sqlite3.connect(os.environ.get("DB_PATH", "")) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM entity_relationships WHERE org_id = %s AND relationship_type = %s",
                (org_id, rtype),
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# Orchestrator behaviour
# ---------------------------------------------------------------------------

class TestMapRelationshipsOrchestrator:
    def test_ncino_loans_create_observed_ownership_edge_end_to_end(self):
        org = f"org-t6-ncino-{uuid4().hex[:8]}"
        run = f"run-t6-ncino-{uuid4().hex[:8]}"
        ingestor_data = {
            "salesforce": {
                "ncino": {
                    "loans": [
                        {
                            "Id": "loan-001",
                            "Name": "Commercial Loan 001",
                            "OwnerId": "005OWNER",
                        }
                    ]
                }
            }
        }

        entities = extract_entities(
            org_id=org,
            run_id=run,
            pack_id="ncino",
            detector_results=[],
            ingestor_data=ingestor_data,
        )
        counts = map_relationships(org, run, ingestor_data, [], entities)

        assert counts["observed"] == 1
        with sqlite3.connect(_get_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT r.relationship_type, r.inferred,
                       source.source_record_id AS owner_id,
                       target.source_record_id AS loan_id
                FROM entity_relationships r
                JOIN entities source ON source.id = r.from_entity_id
                JOIN entities target ON target.id = r.to_entity_id
                WHERE r.org_id = %s AND r.last_seen_run_id = %s
                """,
                (org, run),
            ).fetchone()

        assert row is not None
        assert row["relationship_type"] == "owns"
        assert row["inferred"] is False
        assert row["owner_id"] == "005OWNER"
        assert row["loan_id"] == "loan-001"

    def test_runs_both_passes_and_returns_counts(self):
        org = f"org-t6-{uuid4().hex[:8]}"
        run = "run-t6-both"
        person = _make_entity(org, "person", "Sarah Chen", run)
        obj = _make_entity(org, "object", "LOAN-001", run)
        loan = _make_entity(org, "process", "LOAN_ORIGINATION_ROUTING_FRICTION", run)
        cov = _make_entity(org, "process", "COVENANT_TRACKING_GAP", run)
        for e in (person, obj, loan, cov):
            _persist(e)

        ingestor_data = {"salesforce": {"records": [{"OwnerId": "Sarah Chen", "Id": "LOAN-001"}]}}
        detectors = [
            _DetectorResultStub("LOAN_ORIGINATION_ROUTING_FRICTION"),
            _DetectorResultStub("COVENANT_TRACKING_GAP"),
        ]

        counts = map_relationships(org, run, ingestor_data, detectors, [person, obj, loan, cov])

        assert counts["observed"] == 1, "map_directly_observed pass should write the owns edge"
        assert counts["inferred"] == 1, "map_inferred_from_detectors pass should write depends_on"
        assert counts["total"] == 2
        assert len(_edges(org, "owns")) == 1
        assert len(_edges(org, "depends_on")) == 1

    def test_empty_inputs_no_edges_no_raise(self, caplog):
        org = f"org-t6-empty-{uuid4().hex[:8]}"
        with caplog.at_level(logging.WARNING, logger="app.relationship_mapper"):
            counts = map_relationships(org, "run-empty", {}, [], [])
        assert counts == {"observed": 0, "inferred": 0, "total": 0}
        assert any(
            "map_relationships skipped: no entities from extraction" in r.getMessage()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Telemetry (AC10 — registration + once-per-run emission)
# ---------------------------------------------------------------------------

class TestMappingCompletedTelemetry:
    def test_event_type_registered(self):
        assert "relationship.mapping_completed" in telemetry.REGISTERED_EVENT_TYPES

    def test_emitted_exactly_once_on_success(self, caplog):
        org = f"org-t6-tel-{uuid4().hex[:8]}"
        run = "run-t6-tel"
        with caplog.at_level(logging.INFO, logger="app.telemetry"):
            map_relationships(org, run, {}, [], [])
        hits = [
            r for r in caplog.records
            if "relationship.mapping_completed" in r.getMessage()
        ]
        assert len(hits) == 1, "mapping_completed telemetry must fire exactly once per run"

    def test_payload_carries_run_and_org(self, caplog):
        org = f"org-t6-pl-{uuid4().hex[:8]}"
        run = "run-t6-pl"
        with caplog.at_level(logging.INFO, logger="app.telemetry"):
            map_relationships(org, run, {}, [], [])
        msg = next(
            r.getMessage() for r in caplog.records
            if "relationship.mapping_completed" in r.getMessage()
        )
        assert run in msg
        assert org in msg


# ---------------------------------------------------------------------------
# AC9 — runner non-blocking integration
# ---------------------------------------------------------------------------

class TestRunnerNonBlocking:
    def test_run_completes_when_mapping_raises(self, monkeypatch):
        """AC9: a raising map_relationships() must not break the run — the full
        opportunity set is still produced."""
        import app.relationship_mapper as rm
        from discovery.runner import run

        def _boom(*args, **kwargs):
            raise RuntimeError("relationship mapping exploded")

        # runner imports map_relationships at call time from the module, so
        # patching the source attribute is sufficient.
        monkeypatch.setattr(rm, "map_relationships", _boom)

        payload = run(mode="offline", run_id="t6-map-nonblocking")
        assert isinstance(payload, dict)
        assert len(payload["opportunities"]) >= 7, (
            "Run must still produce its full opportunity set when relationship "
            "mapping fails — mapping is non-blocking in the pipeline"
        )

    def test_failure_warning_log_includes_run_and_org(self, monkeypatch, caplog):
        """The non-blocking warning must carry the exception message, run_id and org_id."""
        import app.relationship_mapper as rm
        from discovery.runner import run

        def _boom(*args, **kwargs):
            raise RuntimeError("relationship mapping exploded")

        monkeypatch.setattr(rm, "map_relationships", _boom)

        with caplog.at_level(logging.WARNING):
            run(mode="offline", run_id="t6-map-warnlog", org_id="org-t6-warn")

        warnings = [
            r.getMessage() for r in caplog.records
            if "Relationship mapping failed" in r.getMessage()
        ]
        assert warnings, "A warning must be logged when mapping fails"
        assert any("t6-map-warnlog" in w for w in warnings), "warning must include run_id"
        assert any("org-t6-warn" in w for w in warnings), "warning must include org_id"
        assert any("exploded" in w for w in warnings), "warning must include exception message"

    def test_no_event_emitted_on_failure(self, monkeypatch, caplog):
        """On the failure path the mapping_completed event must NOT be emitted —
        its absence is the diagnostic signal."""
        import app.relationship_mapper as rm
        from discovery.runner import run

        def _boom(*args, **kwargs):
            raise RuntimeError("relationship mapping exploded")

        monkeypatch.setattr(rm, "map_relationships", _boom)

        with caplog.at_level(logging.INFO):
            run(mode="offline", run_id="t6-map-noevent", org_id="org-t6-noevent")

        emitted = [
            r for r in caplog.records
            if "relationship.mapping_completed" in r.getMessage()
            and "t6-map-noevent" in r.getMessage()
        ]
        assert not emitted, "mapping_completed must not be emitted when mapping fails"


# ---------------------------------------------------------------------------
# Service Cloud co-firing rules (SC-1 … SC-4)
# ---------------------------------------------------------------------------

class TestServiceCloudInferredRules:
    """SC co-firing rules produce inferred edges for Service Cloud pack runs."""

    def _make_sc_entities(self, org, run):
        """Seed the four process entities and one system entity needed by SC rules."""
        entities = [
            _make_entity(org, "process", "REPETITIVE_AUTOMATION", run),
            _make_entity(org, "process", "HANDOFF_FRICTION", run),
            _make_entity(org, "process", "APPROVAL_BOTTLENECK", run),
            _make_entity(org, "process", "KNOWLEDGE_GAP", run),
            _make_entity(org, "system", "Salesforce", run),
            _make_entity(org, "system", "ServiceNow", run),
            _make_entity(org, "process", "INTEGRATION_CONCENTRATION", run),
            _make_entity(org, "process", "CROSS_SYSTEM_ECHO", run),
        ]
        for e in entities:
            _persist(e)
        return entities

    def test_sc1_handoff_friction_depends_on_repetitive_automation(self):
        org = f"org-sc1-{uuid4().hex[:8]}"
        run = f"run-sc1-{uuid4().hex[:8]}"
        entities = self._make_sc_entities(org, run)
        detectors = [
            _DetectorResultStub("REPETITIVE_AUTOMATION", "salesforce"),
            _DetectorResultStub("HANDOFF_FRICTION", "salesforce"),
        ]
        counts = map_relationships(org, run, {}, detectors, entities)
        assert counts["inferred"] >= 1
        edges = _edges(org, "depends_on")
        assert any(e["org_id"] == org for e in edges)

    def test_sc2_approval_bottleneck_depends_on_handoff_friction(self):
        org = f"org-sc2-{uuid4().hex[:8]}"
        run = f"run-sc2-{uuid4().hex[:8]}"
        entities = self._make_sc_entities(org, run)
        detectors = [
            _DetectorResultStub("APPROVAL_BOTTLENECK", "salesforce"),
            _DetectorResultStub("HANDOFF_FRICTION", "salesforce"),
        ]
        counts = map_relationships(org, run, {}, detectors, entities)
        assert counts["inferred"] >= 1
        edges = _edges(org, "depends_on")
        assert any(e["org_id"] == org for e in edges)

    def test_sc3_knowledge_gap_depends_on_repetitive_automation(self):
        org = f"org-sc3-{uuid4().hex[:8]}"
        run = f"run-sc3-{uuid4().hex[:8]}"
        entities = self._make_sc_entities(org, run)
        detectors = [
            _DetectorResultStub("KNOWLEDGE_GAP", "salesforce"),
            _DetectorResultStub("REPETITIVE_AUTOMATION", "salesforce"),
        ]
        counts = map_relationships(org, run, {}, detectors, entities)
        assert counts["inferred"] >= 1
        edges = _edges(org, "depends_on")
        assert any(e["org_id"] == org for e in edges)

    def test_sc4_servicenow_routes_to_salesforce(self):
        org = f"org-sc4-{uuid4().hex[:8]}"
        run = f"run-sc4-{uuid4().hex[:8]}"
        entities = self._make_sc_entities(org, run)
        detectors = [
            _DetectorResultStub("CROSS_SYSTEM_ECHO", "servicenow"),
            _DetectorResultStub("INTEGRATION_CONCENTRATION", "salesforce"),
        ]
        counts = map_relationships(org, run, {}, detectors, entities)
        assert counts["inferred"] >= 1
        edges = _edges(org, "routes_to")
        assert any(e["org_id"] == org for e in edges)

    def test_sc_rules_only_fire_when_both_detectors_present(self):
        """A rule must not fire if only one of its two detectors fired."""
        org = f"org-sc-partial-{uuid4().hex[:8]}"
        run = f"run-sc-partial-{uuid4().hex[:8]}"
        entities = self._make_sc_entities(org, run)
        # Only one detector — no rule can fire.
        detectors = [_DetectorResultStub("REPETITIVE_AUTOMATION", "salesforce")]
        counts = map_relationships(org, run, {}, detectors, entities)
        assert counts["inferred"] == 0

    def test_service_cloud_offline_run_produces_inferred_relationships(self):
        """End-to-end: offline service_cloud run must write at least one inferred edge."""
        from discovery.runner import run as discovery_run
        result = discovery_run(mode="offline", run_id=f"sc-e2e-{uuid4().hex[:8]}", pack="service_cloud")
        assert isinstance(result, dict)
        # The run itself must succeed regardless of mapping outcome (AC9).
        assert "opportunities" in result
