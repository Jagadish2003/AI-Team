"""
R16-A2 / AT-421 (T6) — ingestion.artifact_changed emission for Slack artifacts.

T6 reuses the change-event path established in R16-A1 (AT-381): the change runner
emits one ``ingestion.artifact_changed`` event per changed record in every
fully-processed batch. These tests prove that the REAL ``SlackIngestor`` — driven
through that runner — emits an event for every changed Slack artifact carrying all
required fields:

    org_id, connector_id='slack', artifact_id (the message id), change_kind
    ('created' | 'updated' | 'deleted'), observed_at (UTC ISO).

(The existing ``tests/contract/test_artifact_changed_events.py`` proves the runner
emits using a *fake* connector; this binds AC7 to the real Slack connector and its
real record shape — artifact_id ``channel:ts`` and created/updated change kinds.)

These run offline against the deterministic ``slack_sample.json`` fixture and
capture ``record_event`` via monkeypatch, with the checkpoint store wired through
the runner's in-memory seam — so no DB is needed.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.telemetry import REGISTERED_EVENT_TYPES
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.slack import SlackIngestor, _encode_checkpoint

EVENT = "ingestion.artifact_changed"

# Fixture message identities (channel:ts == artifact_id), oldest-first per channel.
_C001 = [
    "C001:1718000000.000100",
    "C001:1718000600.000200",
    "C001:1718003600.000300",
    "C001:1718090000.000400",  # the fixture's edited message → change_kind 'updated'
]
_C002 = [
    "C002:1718001000.000100",
    "C002:1718004000.000200",
]
_ALL = _C001 + _C002
_EDITED = "C001:1718090000.000400"


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


@pytest.fixture
def captured(monkeypatch):
    """Capture telemetry events (the runner lazily imports record_event at emit)."""
    events: list = []
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda etype, payload=None: events.append((etype, payload or {})),
    )
    return events


def _drive(ingestor, org_id, store=None, **kw):
    store = store or Store()
    res = change_runner.ingest_with_checkpoint(
        ingestor, org_id, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )
    return res, store


def _slack_events(captured):
    return [p for (e, p) in captured if e == EVENT]


# ─────────────────────────────────────────────────────────────────────────────
# Registration (AC7 precondition: record_event raises for unregistered types)
# ─────────────────────────────────────────────────────────────────────────────
def test_artifact_changed_event_type_is_registered():
    assert EVENT in REGISTERED_EVENT_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — every changed Slack artifact emits an event with all required fields
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_emits_one_event_per_changed_slack_artifact(captured):
    _drive(SlackIngestor(), "org-1")
    events = _slack_events(captured)
    # One event per accessible message (private/uninvited/archived excluded by AC4).
    assert {e["artifact_id"] for e in events} == set(_ALL)
    assert len(events) == len(_ALL)


def test_every_event_carries_all_required_fields(captured):
    _drive(SlackIngestor(), "org-9")
    events = _slack_events(captured)
    assert events
    required = {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    for e in events:
        assert required <= set(e.keys())
        assert e["org_id"] == "org-9"
        assert e["connector_id"] == "slack"
        assert e["artifact_id"]                       # message id, non-empty
        assert e["change_kind"] in ("created", "updated", "deleted")
        assert isinstance(e["observed_at"], str) and e["observed_at"]
        datetime.fromisoformat(e["observed_at"])      # valid UTC ISO timestamp


def test_change_kind_reflects_created_vs_edited(captured):
    _drive(SlackIngestor(), "org-1")
    kinds = {e["artifact_id"]: e["change_kind"] for e in _slack_events(captured)}
    # The edited fixture message is reported as an update; all others as created.
    assert kinds[_EDITED] == "updated"
    for aid in _ALL:
        if aid != _EDITED:
            assert kinds[aid] == "created"


# ─────────────────────────────────────────────────────────────────────────────
# Only CHANGED artifacts emit — unchanged workspace / incremental delta
# ─────────────────────────────────────────────────────────────────────────────
def test_unchanged_workspace_emits_no_events(captured):
    _, store = _drive(SlackIngestor(), "org-1")          # first run emits all
    captured.clear()
    _drive(SlackIngestor(), "org-1", store=store)        # nothing new
    assert _slack_events(captured) == []


def test_incremental_emits_only_newly_changed_artifacts(captured):
    # Checkpoint mid-C001 (after message 2) with C002 absent from the map.
    store = Store()
    store.save(
        Checkpoint.create(
            "slack", "org-1", _encode_checkpoint({"C001": "1718000600.000200"})
        )
    )
    _drive(SlackIngestor(), "org-1", store=store)
    emitted = {e["artifact_id"] for e in _slack_events(captured)}
    # Only C001 messages newer than the cursor + all of C002 (never seen).
    assert emitted == set(_C001[2:] + _C002)
    assert "C001:1718000000.000100" not in emitted        # older — not re-emitted


# ─────────────────────────────────────────────────────────────────────────────
# Fire-and-forget: a telemetry failure must never break Slack ingestion
# ─────────────────────────────────────────────────────────────────────────────
def test_emission_failure_never_breaks_slack_ingestion(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)
    res, _ = _drive(SlackIngestor(), "org-1")
    assert res.ok                       # ingestion succeeded despite telemetry failure
    assert res.checkpoint_advanced
    assert res.records == len(_ALL)
