"""
R17-A4 / T6 + T7 — ingestion.artifact_changed emission for .NET-app artifacts (AC7).

T6 reuses the change-event path established in R16-A1 (AT-381): the change runner
emits one ``ingestion.artifact_changed`` event per changed record in every
fully-processed batch. These tests prove the REAL ``DotNetAppIngestor`` — driven
through that runner — emits an event for every changed .NET operational artifact
carrying all required fields:

    org_id, connector_id='dotnet_app', artifact_id, change_kind, observed_at (UTC ISO).

They run offline against the deterministic fixture and capture ``record_event`` via
monkeypatch, with the checkpoint store wired through the runner's in-memory seam —
so no DB is needed.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.telemetry import REGISTERED_EVENT_TYPES
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.dotnet_app import DotNetAppIngestor, _encode_checkpoint

EVENT = "ingestion.artifact_changed"


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


@pytest.fixture
def captured(monkeypatch):
    events: list = []
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda etype, payload=None: events.append((etype, payload or {})),
    )
    return events


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, o, c):
        return self.data.get((o, c))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _drive(ingestor, org_id, store=None, **kw):
    store = store or Store()
    res = change_runner.ingest_with_checkpoint(
        ingestor, org_id, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )
    return res, store


def _events(captured):
    return [p for (e, p) in captured if e == EVENT]


def test_artifact_changed_event_type_is_registered():
    assert EVENT in REGISTERED_EVENT_TYPES


def test_first_run_emits_one_event_per_changed_artifact(captured):
    res, _ = _drive(DotNetAppIngestor(), "org-1")
    events = _events(captured)
    assert len(events) == res.records
    assert len(events) == 12


def test_every_event_carries_all_required_fields(captured):
    _drive(DotNetAppIngestor(), "org-9")
    events = _events(captured)
    assert events
    required = {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    for e in events:
        assert required <= set(e.keys())
        assert e["org_id"] == "org-9"
        assert e["connector_id"] == "dotnet_app"
        assert e["artifact_id"]
        assert e["change_kind"] in ("created", "updated", "deleted")
        assert isinstance(e["observed_at"], str) and e["observed_at"]
        datetime.fromisoformat(e["observed_at"])


def test_artifact_ids_cover_metrics_and_logs(captured):
    _drive(DotNetAppIngestor(), "org-1")
    ids = {e["artifact_id"] for e in _events(captured)}
    assert "orders-api:metrics:2026-06-10T08:00:00+00:00" in ids
    assert "orders-api:log:1" in ids


def test_idle_deployment_emits_no_events(captured):
    _, store = _drive(DotNetAppIngestor(), "org-1")
    captured.clear()
    _drive(DotNetAppIngestor(), "org-1", store=store)
    assert _events(captured) == []


def test_incremental_emits_only_new_artifacts(captured):
    store = Store()
    store.save(
        Checkpoint.create(
            "dotnet_app", "org-1",
            _encode_checkpoint(
                {"orders-api": {"log_offset": 5, "metrics_ts": "2026-06-10T08:10:00+00:00"},
                 "inventory-svc": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00"}}
            ),
        )
    )
    _drive(DotNetAppIngestor(), "org-1", store=store)
    assert _events(captured) == []


def test_emission_failure_never_breaks_ingestion(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)
    res, _ = _drive(DotNetAppIngestor(), "org-1")
    assert res.ok
    assert res.checkpoint_advanced
    assert res.records == 12
