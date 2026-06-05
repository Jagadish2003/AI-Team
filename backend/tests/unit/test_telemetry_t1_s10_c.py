"""Unit tests for T1-S10-C telemetry registry T2 event types.

Validates that:
- db.query_executed and db.ingestor_completed are registered in EVENT_TYPE_REGISTRY.
- DbQueryExecutedEvent and DbIngestorCompletedEvent TypedDicts have the required fields.
- record_event() accepts these event types without raising or logging a warning.

Run from backend/:
    python -m pytest tests/unit/test_telemetry_t1_s10_c.py -q
"""
from __future__ import annotations

import logging
from typing import get_type_hints

import pytest


# ---------------------------------------------------------------------------
# Registry membership
# ---------------------------------------------------------------------------

def test_db_query_executed_in_registry():
    """db.query_executed must be in the telemetry event registry."""
    from app.telemetry import EVENT_REGISTRY
    assert "db.query_executed" in EVENT_REGISTRY


def test_db_ingestor_completed_in_registry():
    """db.ingestor_completed must be in the telemetry event registry."""
    from app.telemetry import EVENT_REGISTRY
    assert "db.ingestor_completed" in EVENT_REGISTRY


# ---------------------------------------------------------------------------
# DbQueryExecutedEvent shape
# ---------------------------------------------------------------------------

def test_db_query_executed_event_importable():
    """DbQueryExecutedEvent must be importable from app.telemetry."""
    from app.telemetry import DbQueryExecutedEvent  # noqa: F401


def test_db_query_executed_event_has_connector_id():
    from app.telemetry import DbQueryExecutedEvent
    hints = get_type_hints(DbQueryExecutedEvent)
    assert "connector_id" in hints


def test_db_query_executed_event_has_query_hash():
    from app.telemetry import DbQueryExecutedEvent
    hints = get_type_hints(DbQueryExecutedEvent)
    assert "query_hash" in hints


def test_db_query_executed_event_has_row_count():
    from app.telemetry import DbQueryExecutedEvent
    hints = get_type_hints(DbQueryExecutedEvent)
    assert "row_count" in hints


def test_db_query_executed_event_has_duration_ms():
    from app.telemetry import DbQueryExecutedEvent
    hints = get_type_hints(DbQueryExecutedEvent)
    assert "duration_ms" in hints


def test_db_query_executed_event_has_driver():
    from app.telemetry import DbQueryExecutedEvent
    hints = get_type_hints(DbQueryExecutedEvent)
    assert "driver" in hints


def test_db_query_executed_event_has_truncated():
    from app.telemetry import DbQueryExecutedEvent
    hints = get_type_hints(DbQueryExecutedEvent)
    assert "truncated" in hints


# ---------------------------------------------------------------------------
# DbIngestorCompletedEvent shape
# ---------------------------------------------------------------------------

def test_db_ingestor_completed_event_importable():
    """DbIngestorCompletedEvent must be importable from app.telemetry."""
    from app.telemetry import DbIngestorCompletedEvent  # noqa: F401


def test_db_ingestor_completed_event_has_connector_id():
    from app.telemetry import DbIngestorCompletedEvent
    hints = get_type_hints(DbIngestorCompletedEvent)
    assert "connector_id" in hints


def test_db_ingestor_completed_event_has_tables_processed():
    from app.telemetry import DbIngestorCompletedEvent
    hints = get_type_hints(DbIngestorCompletedEvent)
    assert "tables_processed" in hints


def test_db_ingestor_completed_event_has_rows_ingested():
    from app.telemetry import DbIngestorCompletedEvent
    hints = get_type_hints(DbIngestorCompletedEvent)
    assert "rows_ingested" in hints


def test_db_ingestor_completed_event_has_duration_ms():
    from app.telemetry import DbIngestorCompletedEvent
    hints = get_type_hints(DbIngestorCompletedEvent)
    assert "duration_ms" in hints


# ---------------------------------------------------------------------------
# record_event() — accepts new event types without warning
# ---------------------------------------------------------------------------

def test_record_event_accepts_db_query_executed(caplog):
    """record_event() must not log a warning for db.query_executed.

    Uses the locked 2-arg signature: record_event(event_type, payload).
    """
    from app.telemetry import record_event
    from unittest.mock import patch

    with patch("app.telemetry.get_db_session") as mock_sess:
        mock_sess.return_value.__enter__ = lambda s, *a: mock_sess.return_value
        mock_sess.return_value.__exit__ = lambda s, *a: False
        mock_sess.return_value.add = lambda e: None
        mock_sess.return_value.commit = lambda: None

        with caplog.at_level(logging.WARNING, logger="app.telemetry"):
            # Locked signature: record_event(event_type, payload). All event
            # metadata travels inside the payload dict (record_event extracts
            # org_id/source/connector_id/duration_ms/success/count from it).
            record_event(
                "db.query_executed",
                {
                    "org_id": "org-test",
                    "source": "salesforce_ingestor",
                    "connector_id": "salesforce",
                    "duration_ms": 42,
                    "success": True,
                    "count": 100,
                    "query_hash": "abc123",
                    "row_count": 100,
                    "driver": "salesforce_soql",
                    "truncated": False,
                },
            )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings, f"Unexpected warnings: {[r.message for r in warnings]}"


def test_record_event_accepts_db_ingestor_completed(caplog):
    """record_event() must not log a warning for db.ingestor_completed.

    Uses the Sprint 11 DBIngestorCompletedPayload shape and the locked
    2-arg signature: record_event(event_type, payload).
    """
    from app.telemetry import record_event
    from unittest.mock import patch

    with patch("app.telemetry.get_db_session") as mock_sess:
        mock_sess.return_value.__enter__ = lambda s, *a: mock_sess.return_value
        mock_sess.return_value.__exit__ = lambda s, *a: False
        mock_sess.return_value.add = lambda e: None
        mock_sess.return_value.commit = lambda: None

        with caplog.at_level(logging.WARNING, logger="app.telemetry"):
            # Locked signature: record_event(event_type, payload).
            record_event(
                "db.ingestor_completed",
                {
                    "org_id":          "org-test",
                    "run_id":          "run-001",
                    "source":          "connector",
                    "connector_id":    "sqlserver",
                    "pack_id":         "sqlserver_opsignal",
                    "query_count":     3,
                    "signal_count":    3,
                    "degraded_count":  0,
                    "duration_ms":     380,
                    "success":         True,
                    "count":           3,
                },
            )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings, f"Unexpected warnings: {[r.message for r in warnings]}"
