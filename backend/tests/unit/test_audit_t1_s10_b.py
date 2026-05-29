"""
T1-S10-B  |  Audit Event Registry — Unit Tests
AgentIQ 2.0  |  Track 1 — Platform Foundation  |  Sprint 10

Acceptance criteria verified here:

  AC-AUD-1  schema_discovered constant is present in audit.py and in the
            AUDIT_EVENT_REGISTRY.
  AC-AUD-2  schema_discovered constant value != connector_queried constant
            value  (distinct strings — different data-access semantics).
  AC-AUD-3  SchemaDiscoveredEvent TypedDict declares the required fields:
            connector_id, schema_count, table_count  (+ org_id from doc).
  AC-AUD-4  log_event() accepts 'schema_discovered' without modification
            to T1-S10-B code — verified by calling log_event() with the
            schema_discovered event type and asserting no exception.
  AC-AUD-5  log_event() never raises — fail-silent contract.
  AC-AUD-6  connector_queried is in the registry with its TypedDict.
  AC-AUD-7  Registry idempotency and conflict detection.
  AC-AUD-8  T1 lifecycle events are registered.

Run:
  cd backend
  pytest tests/unit/test_audit_t1_s10_b.py -v
"""

from __future__ import annotations

import logging
from typing import get_type_hints
from unittest.mock import patch

import pytest

from app.middleware.audit import (
    AUDIT_EVENT_REGISTRY,
    CONNECTOR_QUERIED,
    CONNECTOR_REGISTERED,
    RUN_COMPLETED,
    RUN_STARTED,
    SCHEMA_DISCOVERED,
    SCOPE_DECLARED,
    ConnectorQueriedEvent,
    ConnectorRegisteredAuditEvent,
    RunCompletedAuditEvent,
    RunStartedAuditEvent,
    SchemaDiscoveredEvent,
    ScopeDeclaredEvent,
    log_event,
    register_audit_event_type,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _fields(typed_dict_cls) -> set[str]:
    return set(get_type_hints(typed_dict_cls).keys())


# ─────────────────────────────────────────────────────────────────────────────
# AC-AUD-2  CORE: schema_discovered != connector_queried
# ─────────────────────────────────────────────────────────────────────────────


class TestEventConstantsAreDistinct:
    """The primary acceptance criterion for task T12."""

    def test_schema_discovered_not_equal_to_connector_queried(self):
        """schema_discovered and connector_queried must have distinct string values.

        These two events have different data-access semantics:
          - schema_discovered: system catalogues only, no customer data.
          - connector_queried: declared customer tables, run-scoped.
        Audit consumers route on these strings; equal values would collapse
        two distinct event classes into one.
        """
        assert SCHEMA_DISCOVERED != CONNECTOR_QUERIED

    def test_schema_discovered_string_value(self):
        assert SCHEMA_DISCOVERED == "schema_discovered"

    def test_connector_queried_string_value(self):
        assert CONNECTOR_QUERIED == "connector_queried"

    def test_all_constants_are_unique(self):
        constants = [
            CONNECTOR_QUERIED,
            SCHEMA_DISCOVERED,
            SCOPE_DECLARED,
            CONNECTOR_REGISTERED,
            RUN_STARTED,
            RUN_COMPLETED,
        ]
        assert len(constants) == len(set(constants)), (
            "All audit event-type constants must have unique string values"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC-AUD-1  schema_discovered is in the registry
# ─────────────────────────────────────────────────────────────────────────────


class TestSchemaDiscoveredRegistration:
    def test_constant_in_registry(self):
        assert SCHEMA_DISCOVERED in AUDIT_EVENT_REGISTRY

    def test_registry_maps_to_correct_typed_dict(self):
        assert AUDIT_EVENT_REGISTRY[SCHEMA_DISCOVERED] is SchemaDiscoveredEvent

    def test_string_literal_in_registry(self):
        """Callers may use the raw string — it must also resolve to the TypedDict."""
        assert "schema_discovered" in AUDIT_EVENT_REGISTRY


# ─────────────────────────────────────────────────────────────────────────────
# AC-AUD-3  SchemaDiscoveredEvent TypedDict fields
# ─────────────────────────────────────────────────────────────────────────────


class TestSchemaDiscoveredTypedDict:
    def test_connector_id_field_present(self):
        assert "connector_id" in _fields(SchemaDiscoveredEvent)

    def test_schema_count_field_present(self):
        assert "schema_count" in _fields(SchemaDiscoveredEvent)

    def test_table_count_field_present(self):
        assert "table_count" in _fields(SchemaDiscoveredEvent)

    def test_org_id_field_present(self):
        """org_id required for multi-tenant audit isolation (T1-S10-B contract)."""
        assert "org_id" in _fields(SchemaDiscoveredEvent)

    def test_schema_count_is_int(self):
        hints = get_type_hints(SchemaDiscoveredEvent)
        assert hints["schema_count"] is int

    def test_table_count_is_int(self):
        hints = get_type_hints(SchemaDiscoveredEvent)
        assert hints["table_count"] is int

    def test_connector_id_is_str(self):
        hints = get_type_hints(SchemaDiscoveredEvent)
        assert hints["connector_id"] is str

    def test_no_run_id_field(self):
        """schema_discovered has no run_id — discovery is interactive, not run-scoped."""
        assert "run_id" not in _fields(SchemaDiscoveredEvent)

    def test_no_query_hash_field(self):
        """schema_discovered must not carry query_hash — that belongs to connector_queried."""
        assert "query_hash" not in _fields(SchemaDiscoveredEvent)


# ─────────────────────────────────────────────────────────────────────────────
# AC-AUD-4  log_event() accepts schema_discovered without T1-S10-B modification
# ─────────────────────────────────────────────────────────────────────────────


class TestLogEventAcceptsSchemaDiscovered:
    """Verify log_event() handles schema_discovered with its stable **kwargs interface."""

    def test_schema_discovered_no_exception(self):
        log_event(
            SCHEMA_DISCOVERED,
            org_id="org-abc",
            connector_id="sqlserver",
            schema_count=5,
            table_count=42,
        )

    def test_schema_discovered_string_literal_no_exception(self):
        """Callers may pass the raw string — must work identically to the constant."""
        log_event(
            "schema_discovered",
            org_id="org-xyz",
            connector_id="postgresql",
            schema_count=2,
            table_count=11,
        )

    def test_connector_queried_no_exception(self):
        log_event(
            CONNECTOR_QUERIED,
            org_id="org-abc",
            run_id="run-001",
            connector_id="oracle_db",
            query_hash="deadbeefcafe",
            row_count=500,
            duration_ms=130,
        )

    def test_log_event_emits_structured_log_for_schema_discovered(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.middleware.audit"):
            log_event(
                SCHEMA_DISCOVERED,
                org_id="org-1",
                connector_id="sqlserver",
                schema_count=3,
                table_count=20,
            )
        assert any("schema_discovered" in r.message for r in caplog.records)

    def test_log_event_record_contains_event_type(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.middleware.audit"):
            log_event(
                SCHEMA_DISCOVERED,
                org_id="org-2",
                connector_id="postgresql",
                schema_count=1,
                table_count=7,
            )
        msgs = [r.message for r in caplog.records]
        assert any("schema_discovered" in m for m in msgs)

    def test_log_event_record_contains_org_id(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.middleware.audit"):
            log_event(
                SCHEMA_DISCOVERED,
                org_id="tenant-99",
                connector_id="oracle_db",
                schema_count=4,
                table_count=31,
            )
        assert any("tenant-99" in r.message for r in caplog.records)

    def test_extra_fields_accepted_forward_compatible(self):
        """Extra fields in **fields are ignored — forward-compatible for future schema additions."""
        log_event(
            SCHEMA_DISCOVERED,
            org_id="org-1",
            connector_id="sqlserver",
            schema_count=1,
            table_count=5,
            future_field="allowed",
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC-AUD-5  log_event() never raises (fail-silent contract)
# ─────────────────────────────────────────────────────────────────────────────


class TestLogEventFailSilent:
    def test_does_not_raise_on_internal_logging_error(self):
        with patch("app.middleware.audit.logger") as mock_logger:
            mock_logger.info.side_effect = RuntimeError("sink unavailable")
            log_event(SCHEMA_DISCOVERED, org_id="org-x", connector_id="sqlserver",
                      schema_count=1, table_count=3)

    def test_does_not_raise_for_unregistered_event_type(self):
        log_event("some.future.unregistered.event", org_id="org-x", value=42)

    def test_does_not_raise_with_no_fields(self):
        log_event(SCHEMA_DISCOVERED)

    def test_does_not_raise_with_empty_string_event_type(self):
        log_event("", org_id="org-x")


# ─────────────────────────────────────────────────────────────────────────────
# AC-AUD-6  connector_queried in registry with correct TypedDict
# ─────────────────────────────────────────────────────────────────────────────


class TestConnectorQueriedRegistration:
    def test_in_registry(self):
        assert CONNECTOR_QUERIED in AUDIT_EVENT_REGISTRY

    def test_maps_to_correct_typed_dict(self):
        assert AUDIT_EVENT_REGISTRY[CONNECTOR_QUERIED] is ConnectorQueriedEvent

    def test_required_fields(self):
        required = {"org_id", "run_id", "connector_id", "query_hash", "row_count", "duration_ms"}
        assert required.issubset(_fields(ConnectorQueriedEvent))

    def test_query_hash_is_str(self):
        assert get_type_hints(ConnectorQueriedEvent)["query_hash"] is str

    def test_row_count_is_int(self):
        assert get_type_hints(ConnectorQueriedEvent)["row_count"] is int


# ─────────────────────────────────────────────────────────────────────────────
# AC-AUD-7  Registry idempotency and conflict detection
# ─────────────────────────────────────────────────────────────────────────────


class TestRegisterAuditEventType:
    def test_idempotent_same_schema(self):
        register_audit_event_type(SCHEMA_DISCOVERED, SchemaDiscoveredEvent)

    def test_conflict_different_schema_raises(self):
        from typing import TypedDict

        class OtherSchema(TypedDict):
            foo: str

        with pytest.raises(ValueError, match="already registered"):
            register_audit_event_type(SCHEMA_DISCOVERED, OtherSchema)

    def test_new_event_type_registered_at_runtime(self):
        from typing import TypedDict

        class FutureAuditEvent(TypedDict):
            actor: str
            action: str

        register_audit_event_type("future.audit.test", FutureAuditEvent)
        assert "future.audit.test" in AUDIT_EVENT_REGISTRY
        del AUDIT_EVENT_REGISTRY["future.audit.test"]  # type: ignore[attr-defined]

    def test_log_event_accepts_runtime_registered_type(self):
        from typing import TypedDict

        class LiveAuditEvent(TypedDict):
            result: str

        register_audit_event_type("live.audit.test", LiveAuditEvent)
        log_event("live.audit.test", result="ok")
        del AUDIT_EVENT_REGISTRY["live.audit.test"]  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────────────────
# AC-AUD-8  T1 lifecycle events are registered
# ─────────────────────────────────────────────────────────────────────────────


class TestT1LifecycleEvents:
    def test_connector_registered_in_registry(self):
        assert CONNECTOR_REGISTERED in AUDIT_EVENT_REGISTRY

    def test_run_started_in_registry(self):
        assert RUN_STARTED in AUDIT_EVENT_REGISTRY

    def test_run_completed_in_registry(self):
        assert RUN_COMPLETED in AUDIT_EVENT_REGISTRY

    def test_scope_declared_in_registry(self):
        assert SCOPE_DECLARED in AUDIT_EVENT_REGISTRY

    def test_run_started_maps_to_typed_dict(self):
        assert AUDIT_EVENT_REGISTRY[RUN_STARTED] is RunStartedAuditEvent

    def test_run_completed_maps_to_typed_dict(self):
        assert AUDIT_EVENT_REGISTRY[RUN_COMPLETED] is RunCompletedAuditEvent

    def test_connector_registered_maps_to_typed_dict(self):
        assert AUDIT_EVENT_REGISTRY[CONNECTOR_REGISTERED] is ConnectorRegisteredAuditEvent

    def test_scope_declared_maps_to_typed_dict(self):
        assert AUDIT_EVENT_REGISTRY[SCOPE_DECLARED] is ScopeDeclaredEvent
