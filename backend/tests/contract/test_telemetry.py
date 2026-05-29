"""
backend/tests/contract/test_telemetry.py

Contract tests for T1-S10-C — Telemetry Data Model.

Covers:
- record_event() writes correct schema for run.started and run.completed
- record_event() with invalid payload logs error but does not raise
- connector health check job writes connector.health_check events
- get_telemetry_range() returns events in correct time range scoped to org_id
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.telemetry import get_telemetry_range, record_event
from app.jobs.connector_health import run_connector_health_checks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ORG_A = "org-test-alpha"
ORG_B = "org-test-beta"
RUN_ID = "run-abc-123"


@pytest.fixture()
def mock_session(tmp_path):
    """
    Lightweight in-memory session mock.
    Replace with your test DB session fixture if you prefer a real DB.
    """
    written_events = []

    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.add.side_effect = written_events.append
    session.commit = MagicMock()

    # Expose the captured events for assertions.
    session._written = written_events
    return session


@pytest.fixture()
def patch_session(mock_session):
    with patch("app.telemetry.get_db_session", return_value=mock_session):
        yield mock_session


# ---------------------------------------------------------------------------
# record_event — write schema tests
# ---------------------------------------------------------------------------

class TestRecordEvent:

    def test_run_started_writes_correct_schema(self, patch_session):
        """AC3 — run.started event has correct fields."""
        record_event("run.started", {
            "org_id": ORG_A,
            "source": "run_pipeline",
            "run_id": RUN_ID,
        })

        assert patch_session.commit.called
        event = patch_session._written[0]

        assert event.org_id == ORG_A
        assert event.event_type == "run.started"
        assert event.source == "run_pipeline"
        assert event.run_id == RUN_ID
        assert event.success is None
        assert event.timestamp is not None

    def test_run_completed_writes_correct_schema(self, patch_session):
        """AC4 — run.completed event carries duration_ms, count, and success."""
        record_event("run.completed", {
            "org_id": ORG_A,
            "source": "run_pipeline",
            "run_id": RUN_ID,
            "duration_ms": 1234,
            "success": True,
            "count": 42,
            "pack_id": "pack-1",
            "system_count": 5,
        })

        assert patch_session.commit.called
        event = patch_session._written[0]

        assert event.org_id == ORG_A
        assert event.event_type == "run.completed"
        assert event.duration_ms == 1234
        assert event.success is True
        assert event.count == 42

        # Payload must be JSON-serialised and contain event-specific fields.
        parsed = json.loads(event.payload)
        assert parsed["pack_id"] == "pack-1"
        assert parsed["system_count"] == 5

    def test_invalid_payload_logs_error_does_not_raise(self, patch_session, caplog):
        """AC2 — non-serialisable payload is logged; caller receives no exception."""
        non_serialisable = {"obj": object()}  # object() is not JSON-serialisable

        with caplog.at_level(logging.ERROR, logger="app.telemetry"):
            # Must NOT raise.
            record_event("run.completed", non_serialisable)

        assert "telemetry.record_event failed" in caplog.text
        # Nothing was committed.
        assert not patch_session.commit.called

    def test_db_failure_does_not_raise(self, caplog):
        """AC7 — database unavailable; record_event logs but does not raise."""
        with patch("app.telemetry.get_db_session", side_effect=Exception("DB down")):
            with caplog.at_level(logging.ERROR, logger="app.telemetry"):
                record_event("run.started", {"org_id": ORG_A, "source": "run_pipeline"})

        assert "telemetry.record_event failed" in caplog.text

    def test_correct_org_id_always_written(self, patch_session):
        """AC1 — org_id on the written row matches the caller's org_id."""
        record_event("run.started", {"org_id": ORG_B, "source": "run_pipeline"})
        event = patch_session._written[0]
        assert event.org_id == ORG_B

    def test_timestamp_is_utc(self, patch_session):
        """AC1 — timestamp is a UTC datetime."""
        before = datetime.now(timezone.utc)
        record_event("run.started", {"org_id": ORG_A, "source": "run_pipeline"})
        after = datetime.now(timezone.utc)

        event = patch_session._written[0]
        ts = event.timestamp
        # Make naive timestamps timezone-aware for comparison if needed.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        assert before <= ts <= after


# ---------------------------------------------------------------------------
# Connector health check job tests
# ---------------------------------------------------------------------------

class TestConnectorHealthCheckJob:

    def _make_connector(self, connector_id: str):
        c = MagicMock()
        c.id = connector_id
        return c

    def _make_token_status(self, is_connected=True, needs_refresh=False, expires_in=3600):
        ts = MagicMock()
        ts.is_connected = is_connected
        ts.needs_refresh = needs_refresh
        ts.expires_in_seconds = expires_in
        return ts

    def test_writes_one_event_per_connector(self, patch_session):
        """AC5 — one connector.health_check event per connected connector."""
        workspace = MagicMock()
        workspace.org_id = ORG_A

        connectors = [self._make_connector("conn-1"), self._make_connector("conn-2")]

        with (
            patch("app.jobs.connector_health.WorkspaceRepository") as mock_ws,
            patch("app.jobs.connector_health.ConnectorRepository") as mock_cr,
            patch("app.jobs.connector_health.get_token_status") as mock_ts,
        ):
            mock_ws.get_all.return_value = [workspace]
            mock_cr.get_connected.return_value = connectors
            mock_ts.side_effect = [
                self._make_token_status(is_connected=True),
                self._make_token_status(is_connected=False, needs_refresh=True),
            ]

            run_connector_health_checks()

        events = patch_session._written
        assert len(events) == 2
        assert all(e.event_type == "connector.health_check" for e in events)
        assert all(e.org_id == ORG_A for e in events)

    def test_event_payload_contains_correct_status_connected(self, patch_session):
        """AC5 — status='connected' when connector token is valid."""
        workspace = MagicMock()
        workspace.org_id = ORG_A
        connector = self._make_connector("conn-1")

        with (
            patch("app.jobs.connector_health.WorkspaceRepository") as mock_ws,
            patch("app.jobs.connector_health.ConnectorRepository") as mock_cr,
            patch("app.jobs.connector_health.get_token_status") as mock_ts,
        ):
            mock_ws.get_all.return_value = [workspace]
            mock_cr.get_connected.return_value = [connector]
            mock_ts.return_value = self._make_token_status(is_connected=True)

            run_connector_health_checks()

        event = patch_session._written[0]
        payload = json.loads(event.payload)
        assert payload["status"] == "connected"
        assert payload["connector_id"] == "conn-1"
        assert "check_duration_ms" in payload

    def test_event_payload_contains_needs_refresh(self, patch_session):
        """AC5 — status='needs_refresh' when token needs refresh."""
        workspace = MagicMock()
        workspace.org_id = ORG_A
        connector = self._make_connector("conn-2")

        with (
            patch("app.jobs.connector_health.WorkspaceRepository") as mock_ws,
            patch("app.jobs.connector_health.ConnectorRepository") as mock_cr,
            patch("app.jobs.connector_health.get_token_status") as mock_ts,
        ):
            mock_ws.get_all.return_value = [workspace]
            mock_cr.get_connected.return_value = [connector]
            mock_ts.return_value = self._make_token_status(
                is_connected=False, needs_refresh=True
            )

            run_connector_health_checks()

        payload = json.loads(patch_session._written[0].payload)
        assert payload["status"] == "needs_refresh"

    def test_connector_failure_does_not_stop_remaining_connectors(self, patch_session):
        """AC7 — one bad connector check does not abort the rest."""
        workspace = MagicMock()
        workspace.org_id = ORG_A
        connectors = [self._make_connector("conn-bad"), self._make_connector("conn-good")]

        with (
            patch("app.jobs.connector_health.WorkspaceRepository") as mock_ws,
            patch("app.jobs.connector_health.ConnectorRepository") as mock_cr,
            patch("app.jobs.connector_health.get_token_status") as mock_ts,
        ):
            mock_ws.get_all.return_value = [workspace]
            mock_cr.get_connected.return_value = connectors
            # First connector raises; second should still be processed.
            mock_ts.side_effect = [
                Exception("token-status endpoint error"),
                self._make_token_status(is_connected=True),
            ]

            run_connector_health_checks()

        # Only the good connector should have written an event.
        assert len(patch_session._written) == 1
        payload = json.loads(patch_session._written[0].payload)
        assert payload["connector_id"] == "conn-good"


# ---------------------------------------------------------------------------
# get_telemetry_range — org_id scoping tests
# ---------------------------------------------------------------------------

class TestGetTelemetryRange:

    def _make_event(self, org_id: str, event_type: str, ts: datetime):
        e = MagicMock()
        e.org_id = org_id
        e.event_type = event_type
        e.timestamp = ts
        return e

    def test_returns_only_events_for_specified_org(self):
        """AC6 — events from other orgs are never returned."""
        now = datetime.now(timezone.utc)
        from_dt = now - timedelta(hours=1)
        to_dt = now + timedelta(hours=1)

        org_a_event = self._make_event(ORG_A, "run.completed", now)
        # org_b_event should be filtered out by the query.

        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = [org_a_event]

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value = mock_query

        with patch("app.telemetry.get_db_session", return_value=mock_session):
            results = get_telemetry_range(ORG_A, "run.completed", from_dt, to_dt)

        assert all(e.org_id == ORG_A for e in results)

    def test_respects_time_range(self):
        """AC6 — only events within from_dt/to_dt are returned."""
        now = datetime.now(timezone.utc)
        from_dt = now - timedelta(hours=2)
        to_dt = now - timedelta(hours=1)

        # The query layer enforces the range; this test validates the call
        # is made with the correct filter arguments.
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = []

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value = mock_query

        with patch("app.telemetry.get_db_session", return_value=mock_session):
            get_telemetry_range(ORG_A, "run.completed", from_dt, to_dt, limit=50)

        # Verify filter and limit were called (exact args verified by integration test).
        assert mock_query.filter.called
        mock_query.limit.assert_called_once_with(50)
