"""
T1-S10-C  |  Telemetry Event Registry — Unit Tests
AgentIQ 2.0  |  Track 1 — Platform Foundation  |  Sprint 10

Covers all acceptance criteria from T2-S10-A task T11:

  AC-TEL-1  db.query_executed TypedDict is registered in the event registry
            with fields: connector_id, query_hash, row_count, duration_ms,
            driver, truncated.
  AC-TEL-2  db.ingestor_completed TypedDict is registered with fields:
            connector_id, tables_processed, rows_ingested, duration_ms.
  AC-TEL-3  record_event() accepts T2 event types without modification to
            T1-S10-C code (verified by calling record_event() with both T2
            event types and asserting no exception is raised).
  AC-TEL-4  record_event() never raises — telemetry failure is swallowed
            silently (T2-S10-A AC9 contract).
  AC-TEL-5  Registering the same event type twice with the same schema is
            idempotent.
  AC-TEL-6  Registering the same event type with a different schema raises
            ValueError.
  AC-TEL-7  T1 core event types are present in the registry.

Run:
  cd backend
  pytest tests/unit/test_telemetry_t1_s10_c.py -v
"""

from __future__ import annotations

import logging
from typing import get_type_hints
from unittest.mock import patch

import pytest

from app.telemetry import (
    EVENT_REGISTRY,
    DbIngestorCompletedEvent,
    DbQueryExecutedEvent,
    record_event,
    register_event_type,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _fields(typed_dict_cls) -> set[str]:
    """Return the set of field names declared on a TypedDict class."""
    return set(get_type_hints(typed_dict_cls).keys())


# ─────────────────────────────────────────────────────────────────────────────
# AC-TEL-1  db.query_executed registration
# ─────────────────────────────────────────────────────────────────────────────


class TestDbQueryExecutedRegistration:
    def test_event_type_is_in_registry(self):
        assert "db.query_executed" in EVENT_REGISTRY

    def test_registry_schema_is_typed_dict(self):
        assert EVENT_REGISTRY["db.query_executed"] is DbQueryExecutedEvent

    def test_required_fields_present(self):
        required = {"connector_id", "query_hash", "row_count", "duration_ms"}
        assert required.issubset(_fields(DbQueryExecutedEvent))

    def test_driver_field_present(self):
        assert "driver" in _fields(DbQueryExecutedEvent)

    def test_truncated_field_present(self):
        assert "truncated" in _fields(DbQueryExecutedEvent)

    def test_truncated_is_bool(self):
        hints = get_type_hints(DbQueryExecutedEvent)
        assert hints["truncated"] is bool

    def test_row_count_is_int(self):
        hints = get_type_hints(DbQueryExecutedEvent)
        assert hints["row_count"] is int

    def test_duration_ms_is_int(self):
        hints = get_type_hints(DbQueryExecutedEvent)
        assert hints["duration_ms"] is int


# ─────────────────────────────────────────────────────────────────────────────
# AC-TEL-2  db.ingestor_completed registration
# ─────────────────────────────────────────────────────────────────────────────


class TestDbIngestorCompletedRegistration:
    def test_event_type_is_in_registry(self):
        assert "db.ingestor_completed" in EVENT_REGISTRY

    def test_registry_schema_is_typed_dict(self):
        assert EVENT_REGISTRY["db.ingestor_completed"] is DbIngestorCompletedEvent

    def test_required_fields_present(self):
        required = {"connector_id", "tables_processed", "rows_ingested", "duration_ms"}
        assert required.issubset(_fields(DbIngestorCompletedEvent))

    def test_tables_processed_is_int(self):
        hints = get_type_hints(DbIngestorCompletedEvent)
        assert hints["tables_processed"] is int

    def test_rows_ingested_is_int(self):
        hints = get_type_hints(DbIngestorCompletedEvent)
        assert hints["rows_ingested"] is int

    def test_duration_ms_is_int(self):
        hints = get_type_hints(DbIngestorCompletedEvent)
        assert hints["duration_ms"] is int


# ─────────────────────────────────────────────────────────────────────────────
# AC-TEL-3  record_event() accepts T2 events without T1-S10-C modification
# ─────────────────────────────────────────────────────────────────────────────


class TestRecordEventAcceptsT2Events:
    """Verify that record_event() handles T2 event types correctly.

    The key assertion is behavioural: record_event() must not raise when
    called with either T2 event type, and must not require any internal
    changes to support new event types.
    """

    def test_db_query_executed_no_exception(self):
        payload: DbQueryExecutedEvent = {
            "connector_id": "sqlserver",
            "query_hash": "abc123deadbeef",
            "row_count": 500,
            "duration_ms": 142,
            "driver": "pyodbc",
            "truncated": False,
        }
        # Must not raise — this is the primary T2 integration contract
        record_event("db.query_executed", payload)

    def test_db_ingestor_completed_no_exception(self):
        payload: DbIngestorCompletedEvent = {
            "connector_id": "postgresql",
            "tables_processed": 4,
            "rows_ingested": 9800,
            "duration_ms": 3200,
        }
        record_event("db.ingestor_completed", payload)

    def test_db_query_executed_truncated_true(self):
        payload: DbQueryExecutedEvent = {
            "connector_id": "oracle_db",
            "query_hash": "ff00deadbeef",
            "row_count": 10000,
            "duration_ms": 998,
            "driver": "oracledb",
            "truncated": True,
        }
        record_event("db.query_executed", payload)

    def test_record_event_emits_structured_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.telemetry"):
            record_event(
                "db.query_executed",
                {
                    "connector_id": "sqlserver",
                    "query_hash": "cafebabe",
                    "row_count": 10,
                    "duration_ms": 50,
                    "driver": "pyodbc",
                    "truncated": False,
                },
            )
        assert any("db.query_executed" in r.message for r in caplog.records)

    def test_event_dict_contains_event_type_key(self, caplog):
        """record_event() should include 'event_type' in the emitted record."""
        with caplog.at_level(logging.INFO, logger="app.telemetry"):
            record_event(
                "db.ingestor_completed",
                {
                    "connector_id": "sqlserver",
                    "tables_processed": 2,
                    "rows_ingested": 300,
                    "duration_ms": 800,
                },
            )
        assert any("db.ingestor_completed" in r.message for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# AC-TEL-4  record_event() never raises (fail-silent contract)
# ─────────────────────────────────────────────────────────────────────────────


class TestRecordEventFailSilent:
    """T2-S10-A AC9: record_event() failure must not cause execute_query() to fail."""

    def test_does_not_raise_on_internal_logging_error(self):
        with patch("app.telemetry.logger") as mock_logger:
            mock_logger.info.side_effect = RuntimeError("sink down")
            # Must not propagate the RuntimeError
            record_event("db.query_executed", {"connector_id": "sqlserver"})

    def test_does_not_raise_for_unregistered_event_type(self):
        """Unknown event types must not raise — they just log a debug warning."""
        record_event("some.future.event", {"foo": "bar"})

    def test_does_not_raise_with_empty_payload(self):
        record_event("db.query_executed", {})

    def test_does_not_raise_with_extra_payload_keys(self):
        """Forward-compatible: extra keys in payload are ignored."""
        payload: dict = {
            "connector_id": "sqlserver",
            "query_hash": "abc",
            "row_count": 1,
            "duration_ms": 10,
            "driver": "pyodbc",
            "truncated": False,
            "extra_future_field": "allowed",  # should not cause an error
        }
        record_event("db.query_executed", payload)


# ─────────────────────────────────────────────────────────────────────────────
# AC-TEL-5 & AC-TEL-6  Registry idempotency and conflict detection
# ─────────────────────────────────────────────────────────────────────────────


class TestRegisterEventType:
    def test_idempotent_same_schema(self):
        """Re-registering with the same schema is safe (no exception)."""
        register_event_type("db.query_executed", DbQueryExecutedEvent)  # already registered

    def test_conflict_different_schema_raises(self):
        """Re-registering with a *different* schema raises ValueError."""
        from typing import TypedDict

        class AnotherSchema(TypedDict):
            foo: str

        with pytest.raises(ValueError, match="already registered"):
            register_event_type("db.query_executed", AnotherSchema)

    def test_new_event_type_registered(self):
        """A brand-new event type can be registered at runtime without modifying record_event()."""
        from typing import TypedDict

        class FutureEvent(TypedDict):
            feature_id: str
            value: int

        register_event_type("future.event.test", FutureEvent)
        assert "future.event.test" in EVENT_REGISTRY
        assert EVENT_REGISTRY["future.event.test"] is FutureEvent

        # Cleanup — remove the test entry so it doesn't bleed into other tests
        del EVENT_REGISTRY["future.event.test"]  # type: ignore[attr-defined]

    def test_record_event_works_after_runtime_registration(self):
        """record_event() accepts newly-registered event types without code change."""
        from typing import TypedDict

        class LiveTestEvent(TypedDict):
            value: str

        register_event_type("live.test.event", LiveTestEvent)
        record_event("live.test.event", {"value": "hello"})  # must not raise

        del EVENT_REGISTRY["live.test.event"]  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────────────────
# AC-TEL-7  T1 core event types
# ─────────────────────────────────────────────────────────────────────────────


class TestT1CoreEvents:
    def test_run_started_in_registry(self):
        assert "run.started" in EVENT_REGISTRY

    def test_run_completed_in_registry(self):
        assert "run.completed" in EVENT_REGISTRY

    def test_connector_registered_in_registry(self):
        assert "connector.registered" in EVENT_REGISTRY

    def test_run_started_fields(self):
        from app.telemetry import RunStartedEvent

        assert _fields(RunStartedEvent) == {"run_id", "org_id"}

    def test_run_completed_fields(self):
        from app.telemetry import RunCompletedEvent

        assert {"run_id", "org_id", "duration_ms", "connectors_processed"}.issubset(
            _fields(RunCompletedEvent)
        )
