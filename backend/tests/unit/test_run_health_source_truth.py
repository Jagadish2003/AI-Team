"""Regression coverage for R18-C2 source-of-truth and failure honesty."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import health_aggregation as health
from app.telemetry import EVENT_REGISTRY, PackExecutedPayload
from discovery import runner


def test_connector_store_failure_is_not_converted_to_empty(monkeypatch):
    def unavailable(_org_id: str):
        raise RuntimeError("connector store unavailable")

    monkeypatch.setattr(health.db, "org_connectors_list", unavailable)
    with pytest.raises(RuntimeError, match="connector store unavailable"):
        health.connectors_view("org-a")


def test_run_store_failure_is_not_converted_to_empty(monkeypatch):
    def unavailable(_org_id: str):
        raise RuntimeError("run store unavailable")

    monkeypatch.setattr(health.db, "tenancy_get_runs", unavailable)
    with pytest.raises(RuntimeError, match="run store unavailable"):
        health.runs_view("org-a")
    with pytest.raises(RuntimeError, match="run store unavailable"):
        health.packs_view("org-a")


def test_telemetry_failure_is_not_converted_to_zero_health(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(health, "get_telemetry_range", unavailable)
    monkeypatch.setattr(health.db, "org_connectors_list", lambda _org_id: [])
    with pytest.raises(RuntimeError, match="telemetry unavailable"):
        health.connectors_view("org-a")


def test_checkpoint_failure_is_not_converted_to_missing_checkpoint(monkeypatch):
    def unavailable(*_args):
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(
        health.db,
        "org_connectors_list",
        lambda _org_id: [
            {
                "id": "servicenow",
                "name": "ServiceNow",
                "status": "connected",
                "configured": True,
            }
        ],
    )
    monkeypatch.setattr(health, "_safe_range", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(health, "_read_checkpoint", unavailable)

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        health.connectors_view("org-a")


def test_packs_view_uses_execution_snapshot_not_current_registry(monkeypatch):
    monkeypatch.setattr(
        health,
        "_latest_run",
        lambda _org_id: {
            "id": "run-1",
            "packId": "ncino",
            "packName": "Commercial Lending at execution",
            "packVersion": "historic-7.4.2",
            "executedDetectorIds": ["DET_Z", "DET_A"],
            "packExecutedAt": "2026-07-14T10:00:00+00:00",
        },
    )
    monkeypatch.setattr(health, "_safe_range", lambda *_args, **_kwargs: [])

    result = health.packs_view("org-a")

    pack = result["packs"][0]
    assert pack["pack_version"] == "historic-7.4.2"
    assert pack["detectors"] == ["DET_Z", "DET_A"]
    assert pack["pack_name"] == "Commercial Lending at execution"


def test_selected_pack_without_execution_evidence_is_not_invented(monkeypatch):
    monkeypatch.setattr(
        health,
        "_latest_run",
        lambda _org_id: {"id": "run-failed", "packId": "ncino"},
    )
    monkeypatch.setattr(health, "_safe_range", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(health, "_snapshot_detector_ids", lambda *_args: [])

    assert health.packs_view("org-a") == {"run_id": "run-failed", "packs": []}


def test_runner_emits_exact_pack_execution_snapshot(monkeypatch):
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runner,
        "record_event",
        lambda event_type, payload: recorded.append((event_type, payload)),
    )
    detectors = [
        SimpleNamespace(DETECTOR_ID="DET_ONE"),
        SimpleNamespace(DETECTOR_ID="DET_TWO"),
    ]
    executed_at = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)

    detector_ids = runner._record_pack_execution(
        org_id="org-a",
        run_id="run-1",
        pack_id="ncino",
        pack_name="nCino Lending",
        pack_version="1.0.1",
        detectors=detectors,
        evaluated_count=1,
        executed_at=executed_at,
    )

    assert detector_ids == ["DET_ONE", "DET_TWO"]
    assert recorded == [
        (
            "run.pack_executed",
            {
                "org_id": "org-a",
                "run_id": "run-1",
                "pack_id": "ncino",
                "pack_name": "nCino Lending",
                "pack_version": "1.0.1",
                "detector_ids": ["DET_ONE", "DET_TWO"],
                "detector_count": 2,
                "evaluated_count": 1,
                "not_evaluated_count": 1,
                "executed_at": "2026-07-14T10:00:00+00:00",
            },
        )
    ]
    assert EVENT_REGISTRY["run.pack_executed"] is PackExecutedPayload
