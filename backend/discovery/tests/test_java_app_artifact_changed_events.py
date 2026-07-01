"""
R17-A3 / T6 + T7 — ingestion.artifact_changed emission for Java-app artifacts (AC6).

T6 reuses the change-event path established in R16-A1 (AT-381): the change runner
emits one ``ingestion.artifact_changed`` event per changed record in every
fully-processed batch. These tests prove the REAL ``JavaAppIngestor`` — driven
through that runner — emits an event for every changed Java operational artifact
carrying all required fields:

    org_id, connector_id='java_app', artifact_id, change_kind, observed_at (UTC ISO).

They run offline against the deterministic fixture and capture ``record_event``
via monkeypatch, with the checkpoint store wired through the runner's in-memory
seam — so no DB is needed.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.telemetry import REGISTERED_EVENT_TYPES
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.java_app import JavaAppIngestor, _encode_checkpoint

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


def _java_events(captured):
    return [p for (e, p) in captured if e == EVENT]


# ─────────────────────────────────────────────────────────────────────────────
# Registration precondition
# ─────────────────────────────────────────────────────────────────────────────
def test_artifact_changed_event_type_is_registered():
    assert EVENT in REGISTERED_EVENT_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — every changed Java artifact emits an event with all required fields
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_emits_one_event_per_changed_artifact(captured):
    res, _ = _drive(JavaAppIngestor(), "org-1")
    events = _java_events(captured)
    # One event per ingested operational record (5 metric samples + 7 log entries).
    assert len(events) == res.records
    assert len(events) == 12


def test_every_event_carries_all_required_fields(captured):
    _drive(JavaAppIngestor(), "org-9")
    events = _java_events(captured)
    assert events
    required = {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    for e in events:
        assert required <= set(e.keys())
        assert e["org_id"] == "org-9"
        assert e["connector_id"] == "java_app"
        assert e["artifact_id"]
        assert e["change_kind"] in ("created", "updated", "deleted")
        assert isinstance(e["observed_at"], str) and e["observed_at"]
        datetime.fromisoformat(e["observed_at"])


def test_artifact_ids_cover_metrics_and_logs(captured):
    _drive(JavaAppIngestor(), "org-1")
    ids = {e["artifact_id"] for e in _java_events(captured)}
    assert "payments-api:metrics:2026-06-10T08:00:00+00:00" in ids
    assert "payments-api:log:1" in ids


# ─────────────────────────────────────────────────────────────────────────────
# Only CHANGED artifacts emit — idle / incremental
# ─────────────────────────────────────────────────────────────────────────────
def test_idle_deployment_emits_no_events(captured):
    _, store = _drive(JavaAppIngestor(), "org-1")          # first run emits all
    captured.clear()
    _drive(JavaAppIngestor(), "org-1", store=store)        # nothing new
    assert _java_events(captured) == []


def test_incremental_emits_only_new_artifacts(captured):
    store = Store()
    store.save(
        Checkpoint.create(
            "java_app", "org-1",
            _encode_checkpoint(
                {"payments-api": {"log_offset": 5, "metrics_ts": "2026-06-10T08:10:00+00:00"},
                 "ledger-svc": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00"}}
            ),
        )
    )
    _drive(JavaAppIngestor(), "org-1", store=store)
    # Both apps fully caught up → no new artifacts emitted.
    assert _java_events(captured) == []


# ─────────────────────────────────────────────────────────────────────────────
# Fire-and-forget: a telemetry failure must never break ingestion
# ─────────────────────────────────────────────────────────────────────────────
def test_emission_failure_never_breaks_ingestion(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)
    res, _ = _drive(JavaAppIngestor(), "org-1")
    assert res.ok
    assert res.checkpoint_advanced
    assert res.records == 12
