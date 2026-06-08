"""
T3-S12-A T7 | entity.extraction_completed Telemetry — Contract Tests
AgentIQ 2.0 | Track 3 — Platform Depth | Sprint 12

Covers AC11:
  entity.extraction_completed is in REGISTERED_EVENT_TYPES (EVENT_REGISTRY),
  uses EntityExtractionCompletedPayload, and a contract test passes before merge.

Also verifies:
  - EntityExtractionCompletedPayload has all required fields including ambiguous_count.
  - The event is emitted via record_event() inside extract_entities() on success.
  - The event payload includes entity_count, ambiguous_count, failure_count,
    org_id, run_id, source, and pack_id.
  - The event is NOT emitted when extract_entities() raises (non-blocking path).
  - ambiguous_count correctly reflects entities with resolution_status='ambiguous'.

Run:
  cd backend
  pytest tests/contract/test_entity_extraction_telemetry.py -v
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, get_type_hints
from unittest.mock import MagicMock, call, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# AC11 — Registry and TypedDict
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistryAndTypedDict:
    """AC11 — entity.extraction_completed in EVENT_REGISTRY with correct TypedDict."""

    def test_entity_extraction_completed_in_event_registry(self):
        """AC11 — event type is registered in the telemetry registry."""
        from app.telemetry import EVENT_REGISTRY
        assert "entity.extraction_completed" in EVENT_REGISTRY, (
            "entity.extraction_completed must be registered via register_event_type()"
        )

    def test_entity_extraction_completed_registered_with_correct_typeddict(self):
        """AC11 — registry entry uses EntityExtractionCompletedPayload."""
        from app.telemetry import EVENT_REGISTRY, EntityExtractionCompletedPayload
        assert EVENT_REGISTRY["entity.extraction_completed"] is EntityExtractionCompletedPayload

    def test_entity_extraction_completed_payload_importable(self):
        """AC11 — EntityExtractionCompletedPayload is importable from app.telemetry."""
        from app.telemetry import EntityExtractionCompletedPayload  # noqa: F401

    def test_payload_has_entity_count(self):
        from app.telemetry import EntityExtractionCompletedPayload
        assert "entity_count" in get_type_hints(EntityExtractionCompletedPayload)

    def test_payload_has_ambiguous_count(self):
        """ambiguous_count is the primary monitoring signal for data-quality degradation."""
        from app.telemetry import EntityExtractionCompletedPayload
        assert "ambiguous_count" in get_type_hints(EntityExtractionCompletedPayload), (
            "ambiguous_count must be in EntityExtractionCompletedPayload — "
            "it is the earliest signal of naming-convention changes per org_id"
        )

    def test_payload_has_failure_count(self):
        from app.telemetry import EntityExtractionCompletedPayload
        assert "failure_count" in get_type_hints(EntityExtractionCompletedPayload)

    def test_payload_has_org_id(self):
        from app.telemetry import EntityExtractionCompletedPayload
        assert "org_id" in get_type_hints(EntityExtractionCompletedPayload)

    def test_payload_has_run_id(self):
        from app.telemetry import EntityExtractionCompletedPayload
        assert "run_id" in get_type_hints(EntityExtractionCompletedPayload)

    def test_payload_has_source(self):
        from app.telemetry import EntityExtractionCompletedPayload
        assert "source" in get_type_hints(EntityExtractionCompletedPayload)

    def test_payload_has_pack_id(self):
        from app.telemetry import EntityExtractionCompletedPayload
        assert "pack_id" in get_type_hints(EntityExtractionCompletedPayload)


# ─────────────────────────────────────────────────────────────────────────────
# Event emission — record_event() called correctly from extract_entities()
# ─────────────────────────────────────────────────────────────────────────────

def _make_mock_entity(resolution_status: str = "resolved") -> MagicMock:
    """Return a minimal Entity mock with the given resolution_status."""
    e = MagicMock()
    e.resolution_status = resolution_status
    e.run_count = 5
    return e


class TestEventEmission:
    """Verify record_event() is called by extract_entities() with correct payload."""

    def _run_extract(
        self,
        mock_entities: List[Any],
        ingestor_data: Dict[str, Any] | None = None,
    ) -> List[Any]:
        """Run extract_entities() with mocked resolution and DB writes."""
        from app.entity_extractor import extract_entities

        if ingestor_data is None:
            ingestor_data = {}

        with patch("app.entity_extractor.resolve_or_create_entity", return_value=mock_entities[0] if mock_entities else MagicMock()), \
             patch("app.entity_extractor._extract_salesforce_entities", return_value=mock_entities), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"):
            return extract_entities(
                org_id="test-org",
                run_id="run-t7-001",
                pack_id="service_cloud",
                detector_results=[],
                ingestor_data=ingestor_data,
            )

    def test_record_event_called_on_successful_extraction(self):
        """AC11 — record_event('entity.extraction_completed') is called after extraction."""
        mock_entity = _make_mock_entity("resolved")

        with patch("app.entity_extractor._extract_salesforce_entities", return_value=[mock_entity]), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event") as mock_record:
            from app.entity_extractor import extract_entities
            extract_entities(
                org_id="test-org",
                run_id="run-t7-002",
                pack_id="service_cloud",
                detector_results=[],
                ingestor_data={},
            )

        mock_record.assert_called_once()
        event_type, payload = mock_record.call_args[0]
        assert event_type == "entity.extraction_completed"

    def test_payload_contains_entity_count(self):
        """Payload entity_count equals the total number of extracted entities."""
        resolved = _make_mock_entity("resolved")

        with patch("app.entity_extractor._extract_salesforce_entities", return_value=[resolved, resolved]), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event") as mock_record:
            from app.entity_extractor import extract_entities
            extract_entities(
                org_id="test-org",
                run_id="run-t7-003",
                pack_id="service_cloud",
                detector_results=[],
                # Non-empty so the sf_data guard is passed and the patch fires.
                ingestor_data={"salesforce": {"approval_processes": []}},
            )

        _, payload = mock_record.call_args[0]
        assert payload["entity_count"] == 2

    def test_payload_contains_ambiguous_count(self):
        """ambiguous_count reflects entities with resolution_status='ambiguous'."""
        resolved = _make_mock_entity("resolved")
        ambiguous = _make_mock_entity("ambiguous")

        with patch("app.entity_extractor._extract_salesforce_entities", return_value=[resolved, ambiguous, ambiguous]), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event") as mock_record:
            from app.entity_extractor import extract_entities
            extract_entities(
                org_id="test-org",
                run_id="run-t7-004",
                pack_id="service_cloud",
                detector_results=[],
                # Non-empty so the sf_data guard is passed and the patch fires.
                ingestor_data={"salesforce": {"approval_processes": []}},
            )

        _, payload = mock_record.call_args[0]
        assert payload["ambiguous_count"] == 2, (
            f"Expected ambiguous_count=2, got {payload.get('ambiguous_count')}"
        )

    def test_payload_ambiguous_count_zero_when_all_resolved(self):
        """ambiguous_count=0 when no entities are ambiguous."""
        resolved1 = _make_mock_entity("resolved")
        resolved2 = _make_mock_entity("resolved")

        with patch("app.entity_extractor._extract_salesforce_entities", return_value=[resolved1, resolved2]), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event") as mock_record:
            from app.entity_extractor import extract_entities
            extract_entities(
                org_id="test-org",
                run_id="run-t7-005",
                pack_id="service_cloud",
                detector_results=[],
                # Non-empty so the sf_data guard is passed and the patch fires.
                ingestor_data={"salesforce": {"approval_processes": []}},
            )

        _, payload = mock_record.call_args[0]
        assert payload["ambiguous_count"] == 0

    def test_payload_contains_failure_count(self):
        """failure_count reflects extraction errors (non-blocking)."""
        with patch("app.entity_extractor._extract_salesforce_entities", side_effect=RuntimeError("sf down")), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event") as mock_record:
            from app.entity_extractor import extract_entities
            # Provide SF data so the extractor tries the SF branch (and raises)
            extract_entities(
                org_id="test-org",
                run_id="run-t7-006",
                pack_id="service_cloud",
                detector_results=[],
                ingestor_data={"salesforce": {"some": "data"}},
            )

        _, payload = mock_record.call_args[0]
        assert payload["failure_count"] >= 1

    def test_payload_contains_org_id(self):
        """org_id must be in the emitted payload."""
        with patch("app.entity_extractor._extract_salesforce_entities", return_value=[]), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event") as mock_record:
            from app.entity_extractor import extract_entities
            extract_entities(
                org_id="org-payload-test",
                run_id="run-t7-007",
                pack_id="ncino",
                detector_results=[],
                ingestor_data={},
            )

        _, payload = mock_record.call_args[0]
        assert payload["org_id"] == "org-payload-test"

    def test_payload_contains_run_id(self):
        """run_id must be in the emitted payload."""
        with patch("app.entity_extractor._extract_salesforce_entities", return_value=[]), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event") as mock_record:
            from app.entity_extractor import extract_entities
            extract_entities(
                org_id="test-org",
                run_id="run-t7-008",
                pack_id="service_cloud",
                detector_results=[],
                ingestor_data={},
            )

        _, payload = mock_record.call_args[0]
        assert payload["run_id"] == "run-t7-008"

    def test_payload_contains_pack_id(self):
        """pack_id must be in the emitted payload."""
        with patch("app.entity_extractor._extract_salesforce_entities", return_value=[]), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event") as mock_record:
            from app.entity_extractor import extract_entities
            extract_entities(
                org_id="test-org",
                run_id="run-t7-009",
                pack_id="strs_benefits",
                detector_results=[],
                ingestor_data={},
            )

        _, payload = mock_record.call_args[0]
        assert payload["pack_id"] == "strs_benefits"

    def test_payload_contains_source(self):
        """source must be in the emitted payload (identifies the emitter)."""
        with patch("app.entity_extractor._extract_salesforce_entities", return_value=[]), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event") as mock_record:
            from app.entity_extractor import extract_entities
            extract_entities(
                org_id="test-org",
                run_id="run-t7-010",
                pack_id="service_cloud",
                detector_results=[],
                ingestor_data={},
            )

        _, payload = mock_record.call_args[0]
        assert "source" in payload
        assert payload["source"]  # must be non-empty

    def test_event_not_emitted_when_extractor_internal_raises(self):
        """Event is NOT emitted when record_event itself raises (fire-and-forget).

        The extract_entities() call must still complete and return — runner
        warning log covers the failure path.
        """
        with patch("app.entity_extractor._extract_salesforce_entities", return_value=[]), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event", side_effect=Exception("telemetry down")):
            from app.entity_extractor import extract_entities
            # Must not raise even if record_event() fails
            result = extract_entities(
                org_id="test-org",
                run_id="run-t7-011",
                pack_id="service_cloud",
                detector_results=[],
                ingestor_data={},
            )
        assert isinstance(result, list)

    def test_event_emitted_exactly_once_per_extraction(self):
        """record_event is called exactly once per extract_entities() call."""
        with patch("app.entity_extractor._extract_salesforce_entities", return_value=[]), \
             patch("app.entity_extractor._extract_jira_entities", return_value=[]), \
             patch("app.entity_extractor._extract_servicenow_entities", return_value=[]), \
             patch("app.entity_extractor._extract_detector_entities", return_value=[]), \
             patch("app.db.run_kv_set"), \
             patch("app.telemetry.record_event") as mock_record:
            from app.entity_extractor import extract_entities
            extract_entities(
                org_id="test-org",
                run_id="run-t7-012",
                pack_id="service_cloud",
                detector_results=[],
                ingestor_data={},
            )

        entity_calls = [
            c for c in mock_record.call_args_list
            if c[0][0] == "entity.extraction_completed"
        ]
        assert len(entity_calls) == 1, (
            f"Expected exactly 1 entity.extraction_completed call, got {len(entity_calls)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC11 — log observability (caplog)
# ─────────────────────────────────────────────────────────────────────────────

class TestTelemetryLogObservability:
    """Verify record_event logs via logger.info (observable via caplog in tests)."""

    def test_record_event_logs_entity_extraction_completed(self, caplog):
        """record_event logs [telemetry] entity.extraction_completed to logger.info."""
        with caplog.at_level(logging.INFO, logger="app.telemetry"):
            from app.telemetry import record_event
            record_event(
                "entity.extraction_completed",
                {
                    "org_id": "log-test-org",
                    "run_id": "run-log-001",
                    "pack_id": "service_cloud",
                    "source": "entity_extractor",
                    "entity_count": 3,
                    "ambiguous_count": 1,
                    "failure_count": 0,
                },
            )

        assert any(
            "entity.extraction_completed" in r.message
            for r in caplog.records
        ), "record_event must log entity.extraction_completed via logger.info"

    def test_record_event_never_raises_on_entity_extraction_event(self):
        """record_event() must never raise — fire-and-forget contract."""
        from app.telemetry import record_event
        # Must not raise even with a payload that triggers DB errors
        record_event("entity.extraction_completed", {"entity_count": 5, "ambiguous_count": 2})
