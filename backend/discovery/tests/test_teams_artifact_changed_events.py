"""
R17-A1 / AT-435 (T6) — ingestion.artifact_changed emission for Teams artifacts.

T6 reuses the change-event path established in R16-A1 (AT-381): the change runner
emits one ``ingestion.artifact_changed`` event per changed record in every
fully-processed batch. These tests prove that the REAL ``TeamsIngestor`` — driven
through that runner — emits an event for every changed Teams artifact carrying all
required fields:

    org_id, connector_id='teams', artifact_id ("{team}/{channel}:{message}"),
    change_kind ('created' | 'updated' | 'deleted'), observed_at (UTC ISO).

(The existing ``tests/contract/test_artifact_changed_events.py`` proves the runner
emits using a *fake* connector; this binds AC7 to the real Teams connector and its
real record shape — Graph delta create/update change kinds.)

These run offline against the deterministic ``teams_sample.json`` fixture and
capture ``record_event`` via monkeypatch, with the checkpoint store wired through
the runner's in-memory seam — so no DB is needed.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.telemetry import REGISTERED_EVENT_TYPES
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.teams import TeamsIngestor, _encode_checkpoint

EVENT = "ingestion.artifact_changed"

# Fixture artifact identities ("{team_id}/{channel_id}:{message_id}"). Only the two
# granted, standard, non-archived channels are read (AC4): ops + deploys.
_OPS = [
    "T-eng/19:ops:m100",
    "T-eng/19:ops:m200",
    "T-eng/19:ops:m300",
    "T-eng/19:ops:m400",  # the fixture's edited message → change_kind 'updated'
]
_DEPLOYS = [
    "T-eng/19:deploys:d100",
    "T-eng/19:deploys:d200",
]
_ALL = _OPS + _DEPLOYS
_EDITED = "T-eng/19:ops:m400"


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    """Pin offline so the real TeamsIngestor reads the deterministic fixture.

    The connector chooses live vs offline from ``INGEST_MODE`` at call time. Run in
    isolation (or with a backend/.env that sets ``INGEST_MODE=live``), it would
    otherwise attempt a live Microsoft Graph call and fail for lack of a token.
    Forcing offline makes these AC7 tests deterministic regardless of environment.
    """
    monkeypatch.setenv("INGEST_MODE", "offline")


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


def _teams_events(captured):
    return [p for (e, p) in captured if e == EVENT]


# ─────────────────────────────────────────────────────────────────────────────
# Registration (AC7 precondition: record_event raises for unregistered types)
# ─────────────────────────────────────────────────────────────────────────────
def test_artifact_changed_event_type_is_registered():
    assert EVENT in REGISTERED_EVENT_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — every changed Teams artifact emits an event with all required fields
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_emits_one_event_per_changed_teams_artifact(captured):
    _drive(TeamsIngestor(), "org-1")
    events = _teams_events(captured)
    # One event per accessible message (private/ungranted/archived excluded by AC4).
    assert {e["artifact_id"] for e in events} == set(_ALL)
    assert len(events) == len(_ALL)


def test_every_event_carries_all_required_fields(captured):
    _drive(TeamsIngestor(), "org-9")
    events = _teams_events(captured)
    assert events
    required = {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    for e in events:
        assert required <= set(e.keys())
        assert e["org_id"] == "org-9"
        assert e["connector_id"] == "teams"
        assert e["artifact_id"]                       # team/channel:message, non-empty
        assert e["change_kind"] in ("created", "updated", "deleted")
        assert isinstance(e["observed_at"], str) and e["observed_at"]
        datetime.fromisoformat(e["observed_at"])      # valid UTC ISO timestamp


def test_change_kind_reflects_created_vs_edited(captured):
    _drive(TeamsIngestor(), "org-1")
    kinds = {e["artifact_id"]: e["change_kind"] for e in _teams_events(captured)}
    # The edited fixture message (lastModifiedDateTime != createdDateTime) is an
    # update; every other message is a creation.
    assert kinds[_EDITED] == "updated"
    for aid in _ALL:
        if aid != _EDITED:
            assert kinds[aid] == "created"


def test_excluded_channels_never_emit_events(captured):
    """AC4: private / not-granted / archived channels are never read, so they
    never emit artifact_changed events."""
    _drive(TeamsIngestor(), "org-1")
    ids = {e["artifact_id"] for e in _teams_events(captured)}
    assert not any(
        ("leads-private" in i) or ("not-granted" in i) or ("archived-ops" in i)
        for i in ids
    )


# ─────────────────────────────────────────────────────────────────────────────
# Only CHANGED artifacts emit — unchanged workspace / incremental delta
# ─────────────────────────────────────────────────────────────────────────────
def test_unchanged_workspace_emits_no_events(captured):
    _, store = _drive(TeamsIngestor(), "org-1")          # first run emits all
    captured.clear()
    _drive(TeamsIngestor(), "org-1", store=store)        # nothing new
    assert _teams_events(captured) == []


def test_incremental_emits_only_newly_changed_artifacts(captured):
    # Delta token mid-ops (after message m200) with deploys absent from the map.
    store = Store()
    store.save(
        Checkpoint.create(
            "teams", "org-1", _encode_checkpoint({"T-eng/19:ops": "2026-06-10T09:10:00Z"})
        )
    )
    _drive(TeamsIngestor(), "org-1", store=store)
    emitted = {e["artifact_id"] for e in _teams_events(captured)}
    # Only ops messages strictly newer than m200's marker + all of deploys (never seen).
    assert emitted == {"T-eng/19:ops:m300", "T-eng/19:ops:m400"} | set(_DEPLOYS)
    assert "T-eng/19:ops:m100" not in emitted             # older — not re-emitted
    assert "T-eng/19:ops:m200" not in emitted             # == token, not strictly newer


# ─────────────────────────────────────────────────────────────────────────────
# Fire-and-forget: a telemetry failure must never break Teams ingestion
# ─────────────────────────────────────────────────────────────────────────────
def test_emission_failure_never_breaks_teams_ingestion(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)
    res, _ = _drive(TeamsIngestor(), "org-1")
    assert res.ok                       # ingestion succeeded despite telemetry failure
    assert res.checkpoint_advanced
    assert res.records == len(_ALL)
