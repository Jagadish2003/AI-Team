from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_type_hints

from app import temporal
from app.telemetry import (
    TELEMETRY_EVENT_REGISTRY,
    RunSignalSnapshotPayload,
    record_event,
)


class ApplicationStallDetector:
    SIGNAL_METRICS = ["stalled_count", "max_days_stalled", "sme_note"]


class NoSignalMetricsDetector:
    pass


@dataclass
class Evaluation:
    detector_id: str = "application_stall"
    detector_cls: type = ApplicationStallDetector
    signal_source: str = "salesforce"
    metric_value: float = 21.0
    threshold: float = 20.0
    fired: bool = True
    raw_evidence: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.raw_evidence is None:
            self.raw_evidence = {
                "stalled_count": 4,
                "max_days_stalled": 31.5,
                "sme_note": "not numeric",
            }


def test_record_event_importable_from_backend_app_telemetry():
    repo_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo_root))
    try:
        from backend.app.telemetry import record_event as backend_record_event
    finally:
        sys.path.pop(0)

    assert backend_record_event is not None


def test_run_signal_snapshot_telemetry_contract():
    assert list(inspect.signature(record_event).parameters) == [
        "event_type",
        "payload",
    ]
    assert TELEMETRY_EVENT_REGISTRY["run.signal_snapshot"] is RunSignalSnapshotPayload
    assert get_type_hints(RunSignalSnapshotPayload) == {
        "org_id": str,
        "run_id": str,
        "pack_id": str,
        "signal_count": int,
        "detector_count": int,
        "fired_count": int,
        "below_threshold": int,
    }


def test_snapshot_signals_emits_aggregate_payload_after_write(monkeypatch):
    events: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(
        temporal,
        "_insert_signal_snapshots",
        lambda snapshots: len(snapshots),
    )
    monkeypatch.setattr(
        temporal,
        "record_event",
        lambda event_type, payload: events.append((event_type, payload)),
    )

    temporal.snapshot_signals(
        org_id="org_A",
        run_id="run_001",
        pack_id="service_cloud",
        detector_results=[],
        all_evaluated=[Evaluation()],
        run_completed_at=datetime(2026, 5, 27, 10, 15, tzinfo=timezone.utc),
    )

    assert events == [
        (
            "run.signal_snapshot",
            {
                "org_id": "org_A",
                "run_id": "run_001",
                "pack_id": "service_cloud",
                "signal_count": 3,
                "detector_count": 1,
                "fired_count": 1,
                "below_threshold": 0,
            },
        )
    ]


def test_snapshot_signals_calls_record_event_once_per_run_with_aggregates(monkeypatch):
    """Task 5C AC8/AC9: one post-insert event with counts for the full batch."""
    events: list[tuple[str, dict[str, Any]]] = []
    inserted_batches: list[list[Any]] = []

    def fake_insert_signal_snapshots(snapshots):
        assert events == [], "record_event() must not run before bulk insert completes"
        inserted_batches.append(snapshots)
        return len(snapshots)

    monkeypatch.setattr(
        temporal,
        "_insert_signal_snapshots",
        fake_insert_signal_snapshots,
    )
    monkeypatch.setattr(
        temporal,
        "record_event",
        lambda event_type, payload: events.append((event_type, payload)),
    )

    fired_a = Evaluation(
        detector_id="det_fired_a",
        fired=True,
        raw_evidence={
            "stalled_count": 4,
            "max_days_stalled": 31.5,
            "sme_note": "not numeric",
        },
    )
    below = Evaluation(
        detector_id="det_below",
        detector_cls=NoSignalMetricsDetector,
        metric_value=2.5,
        fired=False,
        raw_evidence={"numeric_but_not_declared": 99},
    )
    fired_b = Evaluation(
        detector_id="det_fired_b",
        fired=True,
        raw_evidence={
            "stalled_count": 1,
            "max_days_stalled": 4.0,
            "sme_note": "not numeric",
        },
    )

    temporal.snapshot_signals(
        org_id="org_A",
        run_id="run_aggregate",
        pack_id="service_cloud",
        detector_results=[fired_a, fired_b],
        all_evaluated=[fired_a, below, fired_b],
        run_completed_at=datetime(2026, 5, 27, 10, 15, tzinfo=timezone.utc),
    )

    # Two fired detectors each write one primary + two numeric SIGNAL_METRICS.
    # The below-threshold detector writes only its primary metric.
    expected_signal_count = 7
    assert len(inserted_batches) == 1, "signal snapshots must be bulk-inserted once"
    assert len(inserted_batches[0]) == expected_signal_count
    assert events == [
        (
            "run.signal_snapshot",
            {
                "org_id": "org_A",
                "run_id": "run_aggregate",
                "pack_id": "service_cloud",
                "signal_count": expected_signal_count,
                "detector_count": 3,
                "fired_count": 2,
                "below_threshold": 1,
            },
        )
    ]


def test_snapshot_signal_count_uses_bulk_insert_return_value(monkeypatch):
    """AC9: signal_count reflects rows written, not merely rows constructed."""
    events: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(
        temporal,
        "_insert_signal_snapshots",
        lambda snapshots: 2,
    )
    monkeypatch.setattr(
        temporal,
        "record_event",
        lambda event_type, payload: events.append((event_type, payload)),
    )

    temporal.snapshot_signals(
        org_id="org_A",
        run_id="run_insert_count",
        pack_id="service_cloud",
        detector_results=[],
        all_evaluated=[Evaluation()],
        run_completed_at=datetime(2026, 5, 27, 10, 15, tzinfo=timezone.utc),
    )

    assert len(events) == 1
    assert events[0][1]["signal_count"] == 2


def test_snapshot_signals_has_no_record_event_call_inside_loop():
    """AC8 guard: telemetry is emitted once after collect-then-bulk-insert."""
    source = textwrap.dedent(inspect.getsource(temporal.snapshot_signals))
    tree = ast.parse(source)
    violations: list[int] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func_name = getattr(inner.func, "attr", None) or getattr(inner.func, "id", None)
            if func_name == "record_event":
                violations.append(inner.lineno)

    assert not violations, (
        "snapshot_signals() must not call record_event() inside a loop; "
        f"found loop-level call(s) at line(s): {violations}"
    )
