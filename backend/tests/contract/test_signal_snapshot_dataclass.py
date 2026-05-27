from __future__ import annotations

import sys
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from typing import Optional, get_type_hints
from uuid import UUID

import pytest

from database.models.signal_snapshots import (
    PRIMARY_METRIC_NAME,
    SignalSnapshot,
    build_signal_key,
)


class ApplicationStallDetector:
    SIGNAL_METRICS = [
        "stalled_count",
        "max_days_stalled",
        "jira_corroborated",
        "sme_note",
        "missing_metric",
    ]


class DetectorWithoutSignalMetrics:
    pass


@dataclass
class Evaluation:
    detector_id: str = "application_stall"
    detector_cls: type = ApplicationStallDetector
    signal_source: str = "salesforce"
    metric_value: float = 21.0
    threshold: float = 20.0
    fired: bool = True
    raw_evidence: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.raw_evidence is None:
            self.raw_evidence = {
                "stalled_count": 4,
                "max_days_stalled": 31.5,
                "jira_corroborated": True,
                "sme_note": "not numeric",
            }


def test_signal_snapshot_fields_match_locked_schema_order():
    assert [field.name for field in fields(SignalSnapshot)] == [
        "id",
        "org_id",
        "run_id",
        "pack_id",
        "detector_id",
        "signal_key",
        "metric_name",
        "metric_value",
        "threshold",
        "fired",
        "signal_source",
        "captured_at",
        "baseline_mean",
        "baseline_stddev",
        "baseline_window_days",
        "baseline_calculated_at",
    ]


def test_signal_snapshot_nullable_baseline_type_contract():
    hints = get_type_hints(SignalSnapshot)

    assert hints["baseline_mean"] == Optional[float]
    assert hints["baseline_stddev"] == Optional[float]
    assert hints["baseline_window_days"] == Optional[int]
    assert hints["baseline_calculated_at"] == Optional[datetime]


@pytest.mark.parametrize("org_id", [None, "", "   "])
def test_signal_snapshot_rejects_missing_org_id(org_id: str | None):
    with pytest.raises(ValueError, match="org_id is required"):
        SignalSnapshot(
            org_id=org_id,
            run_id="run_001",
            pack_id="service_cloud",
            detector_id="application_stall",
            signal_key="service_cloud::application_stall::metric_value",
            metric_name=PRIMARY_METRIC_NAME,
            metric_value=21.0,
            threshold=20.0,
            fired=True,
            signal_source="salesforce",
            captured_at=datetime.now(timezone.utc),
        )


def test_primary_metric_helper_builds_required_snapshot_fields():
    completed_at = datetime(2026, 5, 27, 10, 15, tzinfo=timezone.utc)

    snapshot = SignalSnapshot.from_primary_metric(
        org_id="org_A",
        run_id="run_001",
        pack_id="service_cloud",
        evaluation=Evaluation(),
        run_completed_at=completed_at,
    )

    assert isinstance(snapshot.id, UUID)
    assert snapshot.org_id == "org_A"
    assert snapshot.detector_id == "application_stall"
    assert snapshot.signal_key == "service_cloud::application_stall::metric_value"
    assert snapshot.metric_name == PRIMARY_METRIC_NAME
    assert snapshot.metric_value == 21.0
    assert snapshot.threshold == 20.0
    assert snapshot.fired is True
    assert snapshot.signal_source == "salesforce"
    assert snapshot.captured_at is completed_at
    assert snapshot.baseline_mean is None
    assert snapshot.baseline_stddev is None
    assert snapshot.baseline_window_days is None
    assert snapshot.baseline_calculated_at is None


def test_signal_metrics_helper_builds_only_numeric_additional_rows():
    completed_at = datetime(2026, 5, 27, 10, 15, tzinfo=timezone.utc)

    snapshots = SignalSnapshot.from_signal_metrics(
        org_id="org_A",
        run_id="run_001",
        pack_id="service_cloud",
        evaluation=Evaluation(),
        run_completed_at=completed_at,
    )

    assert [snapshot.metric_name for snapshot in snapshots] == [
        "stalled_count",
        "max_days_stalled",
    ]
    assert [snapshot.metric_value for snapshot in snapshots] == [4.0, 31.5]
    assert all(snapshot.threshold is None for snapshot in snapshots)
    assert all(snapshot.fired is False for snapshot in snapshots)
    assert [snapshot.signal_key for snapshot in snapshots] == [
        "service_cloud::application_stall::stalled_count",
        "service_cloud::application_stall::max_days_stalled",
    ]


def test_signal_metrics_helper_handles_detectors_without_signal_metrics():
    snapshots = SignalSnapshot.from_signal_metrics(
        org_id="org_A",
        run_id="run_001",
        pack_id="service_cloud",
        evaluation=Evaluation(detector_cls=DetectorWithoutSignalMetrics),
        run_completed_at=datetime.now(timezone.utc),
    )

    assert snapshots == []


def test_signal_metric_helper_rejects_non_numeric_values():
    with pytest.raises(ValueError, match="must be numeric"):
        SignalSnapshot.from_signal_metric(
            org_id="org_A",
            run_id="run_001",
            pack_id="service_cloud",
            evaluation=Evaluation(),
            metric_name="jira_corroborated",
            run_completed_at=datetime.now(timezone.utc),
        )


def test_signal_snapshot_can_be_imported_from_app_temporal_without_cycle():
    from app.temporal import SignalSnapshot as AppSignalSnapshot

    assert AppSignalSnapshot is SignalSnapshot

    repo_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo_root))
    try:
        from backend.app.temporal import SignalSnapshot as BackendAppSignalSnapshot
    finally:
        sys.path.pop(0)

    assert BackendAppSignalSnapshot.__name__ == "SignalSnapshot"


def test_to_db_row_preserves_all_schema_columns():
    completed_at = datetime(2026, 5, 27, 10, 15, tzinfo=timezone.utc)
    snapshot = SignalSnapshot.from_primary_metric(
        org_id="org_A",
        run_id="run_001",
        pack_id="service_cloud",
        evaluation=Evaluation(),
        run_completed_at=completed_at,
    )

    assert list(snapshot.to_db_row()) == [field.name for field in fields(SignalSnapshot)]
    assert snapshot.to_db_row()["org_id"] == "org_A"


def test_signal_key_helper_uses_locked_format():
    assert (
        build_signal_key("service_cloud", "application_stall", "metric_value")
        == "service_cloud::application_stall::metric_value"
    )
