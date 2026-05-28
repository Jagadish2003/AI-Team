from __future__ import annotations

from datetime import datetime, timezone

from app.db import connect
from app.temporal import DetectorEvaluation, snapshot_signals


def _rows_for_run(run_id: str) -> list[dict]:
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT
                org_id,
                run_id,
                pack_id,
                detector_id,
                signal_key,
                metric_name,
                metric_value,
                threshold,
                fired,
                signal_source,
                captured_at
            FROM signal_snapshots
            WHERE run_id = ?
            ORDER BY detector_id, metric_name
            """,
            (run_id,),
        )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        con.close()


class DetectorWithMetrics:
    SIGNAL_METRICS = ["extra_metric", "bool_flag", "missing_metric"]


class DetectorWithoutMetrics:
    pass


def test_snapshot_signals_writes_primary_and_additional_rows(monkeypatch):
    run_completed_at = datetime(2026, 5, 28, 10, 30, tzinfo=timezone.utc)
    event_calls = []

    def fake_record_event(**kwargs):
        event_calls.append(
            {"row_count_at_event": len(_rows_for_run("run_task4_main")), **kwargs}
        )

    monkeypatch.setattr("app.temporal.record_event", fake_record_event)

    snapshot_signals(
        org_id="org_task4",
        run_id="run_task4_main",
        pack_id="pack_task4",
        detector_results=[],
        all_evaluated=[
            DetectorEvaluation(
                detector_id="DET_FIRED",
                detector_cls=DetectorWithMetrics,
                signal_source="salesforce",
                metric_value=7.5,
                threshold=5.0,
                fired=True,
                raw_evidence={
                    "extra_metric": 3.25,
                    "bool_flag": True,
                    "ignored_text": "not numeric",
                },
            ),
            DetectorEvaluation(
                detector_id="DET_BELOW",
                detector_cls=DetectorWithoutMetrics,
                signal_source="jira",
                metric_value=0.5,
                threshold=1.0,
                fired=False,
                raw_evidence={"numeric_but_not_declared": 9},
            ),
        ],
        run_completed_at=run_completed_at,
    )

    rows = _rows_for_run("run_task4_main")
    assert len(rows) == 3
    assert {row["org_id"] for row in rows} == {"org_task4"}
    assert {row["captured_at"] for row in rows} == {str(run_completed_at)}

    primary_fired = next(
        row
        for row in rows
        if row["detector_id"] == "DET_FIRED" and row["metric_name"] == "metric_value"
    )
    assert primary_fired["signal_key"] == "pack_task4::DET_FIRED::metric_value"
    assert primary_fired["metric_value"] == 7.5
    assert primary_fired["threshold"] == 5.0
    assert primary_fired["fired"] in (1, True)

    below_threshold = next(row for row in rows if row["detector_id"] == "DET_BELOW")
    assert below_threshold["signal_key"] == "pack_task4::DET_BELOW::metric_value"
    assert below_threshold["fired"] in (0, False)

    additional = next(row for row in rows if row["metric_name"] == "extra_metric")
    assert additional["signal_key"] == "pack_task4::DET_FIRED::extra_metric"
    assert additional["metric_value"] == 3.25
    assert additional["threshold"] is None
    assert additional["fired"] in (0, False)
    assert "bool_flag" not in {row["metric_name"] for row in rows}

    assert len(event_calls) == 1
    event = event_calls[0]
    assert event["row_count_at_event"] == 3
    assert event["event_type"] == "run.signal_snapshot"
    assert event["source"] == "temporal_engine"
    assert event["count"] == 3
    assert event["payload"] == {
        "pack_id": "pack_task4",
        "signal_count": 3,
        "detector_count": 2,
        "fired_count": 1,
        "below_threshold": 1,
    }


def test_snapshot_signals_defaults_missing_signal_metrics(monkeypatch):
    monkeypatch.setattr("app.temporal.record_event", lambda **kwargs: None)

    snapshot_signals(
        org_id="org_task4_no_metrics",
        run_id="run_task4_no_metrics",
        pack_id="pack_task4",
        detector_results=[],
        all_evaluated=[
            DetectorEvaluation(
                detector_id="DET_NO_METRICS",
                detector_cls=DetectorWithoutMetrics,
                signal_source="salesforce",
                metric_value=2.0,
                threshold=4.0,
                fired=False,
                raw_evidence={"would_be_metric": 100},
            )
        ],
        run_completed_at=datetime(2026, 5, 28, 11, 0, tzinfo=timezone.utc),
    )

    rows = _rows_for_run("run_task4_no_metrics")
    assert len(rows) == 1
    assert rows[0]["signal_key"] == "pack_task4::DET_NO_METRICS::metric_value"


def test_snapshot_signals_failure_never_raises(monkeypatch):
    def broken_connect():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.temporal.connect", broken_connect)

    snapshot_signals(
        org_id="org_task4_failure",
        run_id="run_task4_failure",
        pack_id="pack_task4",
        detector_results=[],
        all_evaluated=[
            DetectorEvaluation(
                detector_id="DET_FAILURE",
                detector_cls=DetectorWithoutMetrics,
                signal_source="salesforce",
                metric_value=1.0,
                threshold=2.0,
                fired=False,
                raw_evidence={"sample": 1},
            )
        ],
        run_completed_at=datetime(2026, 5, 28, 11, 30, tzinfo=timezone.utc),
    )


def test_snapshot_signals_telemetry_failure_does_not_abort(monkeypatch):
    def broken_record_event(**kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr("app.temporal.record_event", broken_record_event)

    snapshot_signals(
        org_id="org_task4_telemetry",
        run_id="run_task4_telemetry",
        pack_id="pack_task4",
        detector_results=[],
        all_evaluated=[
            DetectorEvaluation(
                detector_id="DET_TELEMETRY",
                detector_cls=DetectorWithoutMetrics,
                signal_source="salesforce",
                metric_value=4.0,
                threshold=3.0,
                fired=True,
                raw_evidence={"sample": 4},
            )
        ],
        run_completed_at=datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
    )

    rows = _rows_for_run("run_task4_telemetry")
    assert len(rows) == 1
    assert rows[0]["signal_key"] == "pack_task4::DET_TELEMETRY::metric_value"


def test_signal_history_is_scoped_by_org(monkeypatch):
    monkeypatch.setattr("app.temporal.record_event", lambda **kwargs: None)
    signal_key = "pack_task4::DET_SHARED::metric_value"

    for org_id, run_id, value in [
        ("org_task4_a", "run_task4_org_a", 10.0),
        ("org_task4_b", "run_task4_org_b", 99.0),
    ]:
        snapshot_signals(
            org_id=org_id,
            run_id=run_id,
            pack_id="pack_task4",
            detector_results=[],
            all_evaluated=[
                DetectorEvaluation(
                    detector_id="DET_SHARED",
                    detector_cls=DetectorWithoutMetrics,
                    signal_source="salesforce",
                    metric_value=value,
                    threshold=1.0,
                    fired=True,
                    raw_evidence={"sample": value},
                )
            ],
            run_completed_at=datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc),
        )

    from app.temporal import get_signal_history

    rows = get_signal_history("org_task4_a", "DET_SHARED", signal_key, limit=10)
    assert rows
    assert {row["org_id"] for row in rows} == {"org_task4_a"}
    assert {row["metric_value"] for row in rows} == {10.0}
