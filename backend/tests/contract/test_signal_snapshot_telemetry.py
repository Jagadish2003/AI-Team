from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, get_type_hints

from app import temporal
from app.telemetry import (
    TELEMETRY_EVENT_REGISTRY,
    RunSignalSnapshotPayload,
    record_event,
)


class ApplicationStallDetector:
    SIGNAL_METRICS = ["stalled_count", "max_days_stalled", "sme_note"]


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
        "metric_key": str,
        "value": float,
        "baseline": Optional[float],
    }


def test_snapshot_signals_emits_payload_per_signal_snapshot(monkeypatch):
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
                "metric_key": "service_cloud::application_stall::metric_value",
                "value": 21.0,
                "baseline": None,
            },
        ),
        (
            "run.signal_snapshot",
            {
                "metric_key": "service_cloud::application_stall::stalled_count",
                "value": 4.0,
                "baseline": None,
            },
        ),
        (
            "run.signal_snapshot",
            {
                "metric_key": "service_cloud::application_stall::max_days_stalled",
                "value": 31.5,
                "baseline": None,
            },
        ),
    ]
