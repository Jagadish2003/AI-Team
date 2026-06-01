"""
T2-S11-A  |  db.ingestor_completed Telemetry — Contract Tests
AgentIQ 2.0  |  Track 2 — Enterprise Technology  |  Sprint 11

Covers acceptance criteria AC16 and AC20.

AC16  record_event('db.ingestor_completed') is called at the end of every
      ingestor execution with correct payload.  Failure of record_event()
      does not affect ingestor output.

AC20  db.ingestor_completed event type is in the T1-S10-C telemetry registry
      with DBIngestorCompletedPayload TypedDict before merge.

Also verifies:
  - DBIngestorCompletedPayload has all Sprint 11 required fields.
  - emit_ingestor_completed() is fire-and-forget: never raises on any failure.
  - Payload includes connector_id, pack_id, query_count, signal_count,
    degraded_count, duration_ms.
  - count_degraded_signals() and count_signal_metrics() helpers work.

Run:
  cd backend
  pytest tests/contract/test_db_ingestor_completed_telemetry.py -v
"""

from __future__ import annotations

import logging
from typing import Any, Dict, get_type_hints
from unittest.mock import MagicMock, patch, call

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# AC20  |  Registry and TypedDict
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistryAndTypeDict:
    """AC20 — db.ingestor_completed in registry with DBIngestorCompletedPayload."""

    def test_db_ingestor_completed_in_event_registry(self):
        """AC20 — event type registered in T1-S10-C registry."""
        from app.telemetry import EVENT_REGISTRY
        assert "db.ingestor_completed" in EVENT_REGISTRY

    def test_db_ingestor_completed_registered_with_sprint11_typeddict(self):
        """AC20 — registry entry uses DBIngestorCompletedPayload (Sprint 11)."""
        from app.telemetry import EVENT_REGISTRY, DBIngestorCompletedPayload
        assert EVENT_REGISTRY["db.ingestor_completed"] is DBIngestorCompletedPayload

    def test_dBingestor_completed_payload_importable(self):
        """AC20 — DBIngestorCompletedPayload is importable from app.telemetry."""
        from app.telemetry import DBIngestorCompletedPayload  # noqa: F401

    def test_payload_has_connector_id(self):
        from app.telemetry import DBIngestorCompletedPayload
        assert "connector_id" in get_type_hints(DBIngestorCompletedPayload)

    def test_payload_has_pack_id(self):
        """Sprint 11 field: identifies which pack consumed the signals."""
        from app.telemetry import DBIngestorCompletedPayload
        assert "pack_id" in get_type_hints(DBIngestorCompletedPayload)

    def test_payload_has_query_count(self):
        """Sprint 11 field: number of execute_query() calls."""
        from app.telemetry import DBIngestorCompletedPayload
        assert "query_count" in get_type_hints(DBIngestorCompletedPayload)

    def test_payload_has_signal_count(self):
        """Sprint 11 field: number of signal metrics extracted."""
        from app.telemetry import DBIngestorCompletedPayload
        assert "signal_count" in get_type_hints(DBIngestorCompletedPayload)

    def test_payload_has_degraded_count(self):
        """Sprint 11 field: number of metrics with degraded_signal=True."""
        from app.telemetry import DBIngestorCompletedPayload
        assert "degraded_count" in get_type_hints(DBIngestorCompletedPayload)

    def test_payload_has_duration_ms(self):
        from app.telemetry import DBIngestorCompletedPayload
        assert "duration_ms" in get_type_hints(DBIngestorCompletedPayload)

    def test_payload_field_types_are_correct(self):
        from app.telemetry import DBIngestorCompletedPayload
        hints = get_type_hints(DBIngestorCompletedPayload)
        assert hints["connector_id"] is str
        assert hints["pack_id"] is str
        assert hints["query_count"] is int
        assert hints["signal_count"] is int
        assert hints["degraded_count"] is int
        assert hints["duration_ms"] is int

    def test_event_type_registry_alias_importable(self):
        """EVENT_TYPE_REGISTRY alias must be importable (T1-S10-C unit test compat)."""
        from app.telemetry import EVENT_TYPE_REGISTRY
        assert "db.ingestor_completed" in EVENT_TYPE_REGISTRY

    def test_legacy_dBingestor_completed_event_still_importable(self):
        """DbIngestorCompletedEvent preserved for backward compatibility."""
        from app.telemetry import DbIngestorCompletedEvent  # noqa: F401


# ─────────────────────────────────────────────────────────────────────────────
# AC16  |  emit_ingestor_completed() calls record_event with correct payload
# ─────────────────────────────────────────────────────────────────────────────

class TestEmitIngestorCompleted:
    """AC16 — record_event called with correct payload; failure does not propagate."""

    def _emit(self, **overrides) -> Dict[str, Any]:
        """Call emit_ingestor_completed and return captured payload."""
        from connectors.db.ingestor_telemetry import emit_ingestor_completed
        captured = {}

        def _capture(event_type, payload=None):
            captured["event_type"] = event_type
            captured["payload"] = payload or {}

        defaults = dict(
            org_id="org-acme",
            run_id="run-001",
            connector_id="sqlserver",
            pack_id="sqlserver_opsignal",
            query_count=3,
            signal_count=3,
            degraded_count=0,
            duration_ms=420,
        )
        defaults.update(overrides)

        with patch("connectors.db.ingestor_telemetry.record_event", side_effect=_capture):
            emit_ingestor_completed(**defaults)

        return captured

    def test_record_event_called_once(self):
        """AC16 — record_event() called exactly once per emit."""
        from connectors.db.ingestor_telemetry import emit_ingestor_completed
        with patch("connectors.db.ingestor_telemetry.record_event") as mock_rec:
            emit_ingestor_completed(
                org_id="org-1", run_id="run-1",
                connector_id="sqlserver", pack_id="sqlserver_opsignal",
                query_count=3, signal_count=3, degraded_count=0, duration_ms=300,
            )
        mock_rec.assert_called_once()

    def test_event_type_is_db_ingestor_completed(self):
        captured = self._emit()
        assert captured["event_type"] == "db.ingestor_completed"

    def test_payload_connector_id_sqlserver(self):
        captured = self._emit(connector_id="sqlserver")
        assert captured["payload"]["connector_id"] == "sqlserver"

    def test_payload_pack_id_sqlserver_opsignal(self):
        captured = self._emit(pack_id="sqlserver_opsignal")
        assert captured["payload"]["pack_id"] == "sqlserver_opsignal"

    def test_payload_query_count(self):
        captured = self._emit(query_count=3)
        assert captured["payload"]["query_count"] == 3

    def test_payload_signal_count(self):
        captured = self._emit(signal_count=3)
        assert captured["payload"]["signal_count"] == 3

    def test_payload_degraded_count_zero_when_clean(self):
        captured = self._emit(degraded_count=0)
        assert captured["payload"]["degraded_count"] == 0

    def test_payload_degraded_count_nonzero_when_partial(self):
        captured = self._emit(degraded_count=1)
        assert captured["payload"]["degraded_count"] == 1

    def test_payload_duration_ms(self):
        captured = self._emit(duration_ms=420)
        assert captured["payload"]["duration_ms"] == 420

    def test_payload_includes_org_id(self):
        captured = self._emit(org_id="org-acme")
        assert captured["payload"]["org_id"] == "org-acme"

    def test_payload_includes_run_id(self):
        captured = self._emit(run_id="run-xyz")
        assert captured["payload"]["run_id"] == "run-xyz"

    def test_payload_source_is_connector(self):
        captured = self._emit()
        assert captured["payload"]["source"] == "connector"

    def test_payload_success_true_when_no_degraded(self):
        """success=True when all signals extracted cleanly."""
        captured = self._emit(query_count=3, degraded_count=0)
        assert captured["payload"]["success"] is True

    def test_payload_success_false_when_all_degraded(self):
        """success=False when degraded_count >= query_count."""
        captured = self._emit(query_count=3, degraded_count=3)
        assert captured["payload"]["success"] is False

    def test_payload_count_equals_signal_count(self):
        """count field mirrors signal_count for telemetry store compatibility."""
        captured = self._emit(signal_count=7)
        assert captured["payload"]["count"] == 7


# ─────────────────────────────────────────────────────────────────────────────
# AC16  |  Fire-and-forget — telemetry failure never propagates
# ─────────────────────────────────────────────────────────────────────────────

class TestFireAndForget:
    """AC16 — failure of record_event() must NOT affect ingestor output."""

    def _call_emit(self, **kwargs):
        from connectors.db.ingestor_telemetry import emit_ingestor_completed
        defaults = dict(
            org_id="org-1", run_id="run-1",
            connector_id="sqlserver", pack_id="sqlserver_opsignal",
            query_count=3, signal_count=3, degraded_count=0, duration_ms=300,
        )
        defaults.update(kwargs)
        emit_ingestor_completed(**defaults)  # must not raise

    def test_does_not_raise_when_record_event_raises_runtime_error(self):
        with patch(
            "connectors.db.ingestor_telemetry.record_event",
            side_effect=RuntimeError("telemetry DB down"),
        ):
            self._call_emit()  # must not raise

    def test_does_not_raise_when_record_event_raises_import_error(self):
        with patch(
            "connectors.db.ingestor_telemetry.record_event",
            side_effect=ImportError("app.telemetry not found"),
        ):
            self._call_emit()

    def test_does_not_raise_when_telemetry_import_fails(self):
        """Even if the app.telemetry import itself fails, emit never raises."""
        import builtins
        real_import = builtins.__import__

        def _fail_telemetry(name, *args, **kwargs):
            if name == "app.telemetry":
                raise ImportError("simulated import failure")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fail_telemetry):
            self._call_emit()  # must not raise

    def test_does_not_raise_on_exception_anywhere(self):
        with patch(
            "connectors.db.ingestor_telemetry.record_event",
            side_effect=Exception("unexpected error"),
        ):
            self._call_emit()

    def test_returns_none(self):
        """emit_ingestor_completed always returns None."""
        from connectors.db.ingestor_telemetry import emit_ingestor_completed
        with patch("connectors.db.ingestor_telemetry.record_event"):
            result = emit_ingestor_completed(
                org_id="org-1", run_id="run-1",
                connector_id="sqlserver", pack_id="sqlserver_opsignal",
                query_count=3, signal_count=3, degraded_count=0, duration_ms=300,
            )
        assert result is None

    def test_error_logged_when_record_event_fails(self, caplog):
        with patch(
            "connectors.db.ingestor_telemetry.record_event",
            side_effect=RuntimeError("sink down"),
        ):
            with caplog.at_level(logging.ERROR, logger="connectors.db.ingestor_telemetry"):
                self._call_emit()
        # An ERROR log must be emitted when telemetry fails silently
        assert any("emit_ingestor_completed" in r.message for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# Helper utilities
# ─────────────────────────────────────────────────────────────────────────────

class TestHelperFunctions:
    """count_degraded_signals() and count_signal_metrics() helpers."""

    def _result(self, degraded_flags: list[bool]) -> dict:
        """Build a mock ingestor result with the given degraded_signal flags."""
        sections = ["ticket_volume", "sla_breach", "queue_depth"]
        return {
            sections[i]: {"degraded_signal": flag, "value": 1}
            for i, flag in enumerate(degraded_flags)
        }

    def test_count_degraded_none_degraded(self):
        from connectors.db.ingestor_telemetry import count_degraded_signals
        result = self._result([False, False, False])
        assert count_degraded_signals(result) == 0

    def test_count_degraded_all_degraded(self):
        from connectors.db.ingestor_telemetry import count_degraded_signals
        result = self._result([True, True, True])
        assert count_degraded_signals(result) == 3

    def test_count_degraded_partial(self):
        from connectors.db.ingestor_telemetry import count_degraded_signals
        result = self._result([True, False, True])
        assert count_degraded_signals(result) == 2

    def test_count_signals_all_clean(self):
        from connectors.db.ingestor_telemetry import count_signal_metrics
        result = self._result([False, False, False])
        assert count_signal_metrics(result) == 3

    def test_count_signals_none_clean(self):
        from connectors.db.ingestor_telemetry import count_signal_metrics
        result = self._result([True, True, True])
        assert count_signal_metrics(result) == 0

    def test_count_signals_ignores_non_dict_values(self):
        from connectors.db.ingestor_telemetry import count_signal_metrics
        result = {
            "connector_id": "sqlserver",
            "org_id": "org-1",
            "ticket_volume": {"degraded_signal": False, "value": 1},
        }
        # Only the dict value with degraded_signal counts
        assert count_signal_metrics(result) == 1

    def test_count_degraded_empty_result(self):
        from connectors.db.ingestor_telemetry import count_degraded_signals
        assert count_degraded_signals({}) == 0

    def test_count_signals_empty_result(self):
        from connectors.db.ingestor_telemetry import count_signal_metrics
        assert count_signal_metrics({}) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Integration — record_event() accepts Sprint 11 payload without warning
# ─────────────────────────────────────────────────────────────────────────────

class TestRecordEventIntegration:
    """record_event() accepts db.ingestor_completed payload without warnings."""

    def test_record_event_accepts_sprint11_payload_no_warning(self, caplog):
        from app.telemetry import record_event

        with patch("app.telemetry.get_db_session") as mock_sess:
            mock_sess.return_value.__enter__ = lambda s, *a: mock_sess.return_value
            mock_sess.return_value.__exit__ = lambda s, *a: False
            mock_sess.return_value.add = lambda e: None
            mock_sess.return_value.commit = lambda: None

            with caplog.at_level(logging.WARNING, logger="app.telemetry"):
                record_event(
                    "db.ingestor_completed",
                    {
                        "org_id":         "org-test",
                        "run_id":         "run-001",
                        "source":         "connector",
                        "connector_id":   "sqlserver",
                        "pack_id":        "sqlserver_opsignal",
                        "query_count":    3,
                        "signal_count":   3,
                        "degraded_count": 0,
                        "duration_ms":    420,
                        "success":        True,
                        "count":          3,
                    },
                )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings, f"Unexpected warnings: {[r.message for r in warnings]}"

    def test_record_event_never_raises_on_sprint11_payload(self):
        """record_event() is fire-and-forget — must not raise."""
        from app.telemetry import record_event
        with patch("app.telemetry.get_db_session") as mock_sess:
            mock_sess.return_value.__enter__ = lambda s, *a: mock_sess.return_value
            mock_sess.return_value.__exit__ = lambda s, *a: False
            mock_sess.return_value.add = lambda e: None
            mock_sess.return_value.commit = lambda: None
            # Must not raise
            record_event(
                "db.ingestor_completed",
                {
                    "connector_id": "sqlserver",
                    "pack_id": "sqlserver_opsignal",
                    "query_count": 3,
                    "signal_count": 2,
                    "degraded_count": 1,
                    "duration_ms": 600,
                },
            )
