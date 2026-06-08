from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import get_type_hints
from unittest.mock import patch

from app.db import connect
from app.telemetry import (
    EVENT_REGISTRY,
    TELEMETRY_EVENT_REGISTRY,
    RunSignalSnapshotPayload,
    record_event,
    register_event_type,
)
from app.temporal import DetectorEvaluation, snapshot_signals


class DetectorWithoutMetrics:
    pass


def _fields(typed_dict_cls) -> set[str]:
    return set(get_type_hints(typed_dict_cls).keys())


def _rows_for_run(run_id: str) -> list[dict]:
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT run_id, org_id, signal_key
            FROM signal_snapshots
            WHERE run_id = ?
            """,
            (run_id,),
        )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        con.close()


def _logged_events(caplog) -> list[dict]:
    events = []
    for record in caplog.records:
        if isinstance(record.args, dict):
            events.append(record.args)
        elif isinstance(record.args, tuple) and record.args and isinstance(record.args[0], dict):
            events.append(record.args[0])
    return events


def test_run_signal_snapshot_event_is_registered():
    assert EVENT_REGISTRY["run.signal_snapshot"] is RunSignalSnapshotPayload
    assert TELEMETRY_EVENT_REGISTRY["run.signal_snapshot"] is RunSignalSnapshotPayload


def test_run_signal_snapshot_payload_fields_match_track3_contract():
    assert _fields(RunSignalSnapshotPayload) == {
        "org_id",
        "run_id",
        "pack_id",
        "signal_count",
        "detector_count",
        "fired_count",
        "below_threshold",
    }


def test_run_signal_snapshot_registration_is_idempotent():
    register_event_type("run.signal_snapshot", RunSignalSnapshotPayload)
    assert EVENT_REGISTRY["run.signal_snapshot"] is RunSignalSnapshotPayload


def test_record_event_accepts_signal_snapshot_payload(caplog):
    with caplog.at_level(logging.INFO, logger="app.telemetry"):
        record_event(
            "run.signal_snapshot",
            {
                "org_id": "org_tel",
                "run_id": "run_tel",
                "pack_id": "pack_tel",
                "signal_count": 2,
                "detector_count": 1,
                "fired_count": 1,
                "below_threshold": 0,
            },
        )

    event = _logged_events(caplog)[0]
    assert event["event_type"] == "run.signal_snapshot"
    assert event["signal_count"] == 2
    assert event["detector_count"] == 1
    assert event["fired_count"] == 1
    assert event["below_threshold"] == 0
    assert "metric_value" not in event
    assert "raw_evidence" not in event


def test_record_event_accepts_generic_registry_shape(caplog):
    with caplog.at_level(logging.INFO, logger="app.telemetry"):
        record_event(
            "db.query_executed",
            {
                "connector_id": "postgresql",
                "query_hash": "abc123",
                "row_count": 5,
                "duration_ms": 12,
                "driver": "psycopg2",
                "truncated": False,
            },
        )

    event = _logged_events(caplog)[0]
    assert event["event_type"] == "db.query_executed"
    assert event["connector_id"] == "postgresql"


def test_record_event_failure_never_raises():
    with patch("app.telemetry.logger.info", side_effect=RuntimeError("sink down")):
        record_event(
            "run.signal_snapshot",
            {
                "org_id": "org_tel",
                "run_id": "run_tel",
                "pack_id": "pack_tel",
                "signal_count": 1,
                "detector_count": 1,
                "fired_count": 1,
                "below_threshold": 0,
            },
        )


def test_snapshot_signals_emits_registered_telemetry_event_after_write(caplog):
    run_id = "run_task9_telemetry"
    with caplog.at_level(logging.INFO, logger="app.telemetry"):
        snapshot_signals(
            org_id="org_task9",
            run_id=run_id,
            pack_id="pack_task9",
            detector_results=[],
            all_evaluated=[
                DetectorEvaluation(
                    detector_id="DET_TASK9",
                    detector_cls=DetectorWithoutMetrics,
                    signal_source="salesforce",
                    metric_value=4.0,
                    threshold=3.0,
                    fired=True,
                    raw_evidence={"sample": 4.0},
                )
            ],
            run_completed_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        )

    assert len(_rows_for_run(run_id)) == 1

    events = _logged_events(caplog)
    event = next(item for item in events if item["event_type"] == "run.signal_snapshot")
    assert event["org_id"] == "org_task9"
    assert event["run_id"] == run_id
    assert event["pack_id"] == "pack_task9"
    assert event["signal_count"] == 1
    assert event["detector_count"] == 1
    assert event["fired_count"] == 1
    assert event["below_threshold"] == 0
