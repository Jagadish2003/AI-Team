"""
R17-A2 / AT-463 (T6) — ingestion.artifact_changed emission for SharePoint artifacts.

T6 reuses the change-event path established in R16-A1 (AT-381): the change runner
emits one ``ingestion.artifact_changed`` event per changed record in every
fully-processed batch. These tests prove that the REAL ``SharePointIngestor`` —
driven through that runner — emits an event for every changed SharePoint artifact
(document / library item) carrying all required fields:

    org_id, connector_id='sharepoint' (the source system), artifact_id
    ("{site_id}/{drive_id}:{item_id}"), change_kind ('created' | 'updated' |
    'deleted'), observed_at (UTC ISO timestamp).

Events fire only for genuinely changed artifacts — an unchanged document estate
(empty delta) emits nothing (AC6). Ungranted sites, ungranted libraries and hidden
libraries are never read (AC4), so they never emit events.

(The existing ``tests/contract/test_artifact_changed_events.py`` proves the runner
emits using a *fake* connector; this binds AC6 to the real SharePoint connector and
its real record shape — Graph drive-delta create/update change kinds.)

These run offline against the deterministic ``sharepoint_sample.json`` fixture and
capture ``record_event`` via monkeypatch, with the checkpoint store wired through
the runner's in-memory seam — so no DB is needed. Mirrors
``test_teams_artifact_changed_events.py`` (the paired Graph connector's T6 tests).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.telemetry import REGISTERED_EVENT_TYPES
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.sharepoint import SharePointIngestor, _encode_checkpoint

EVENT = "ingestion.artifact_changed"

# Fixture artifact identities ("{site_id}/{drive_id}:{item_id}"). Only the granted
# site (S-eng) and its granted, non-hidden libraries (b-docs, b-specs) are read
# (AC4): the ungranted site (S-secret), ungranted library (b-private) and hidden
# library (b-hidden) are excluded.
_DOCS = [
    "S-eng/b-docs:f100",
    "S-eng/b-docs:f200",
    "S-eng/b-docs:fold300",
    "S-eng/b-docs:f400",  # created 08:00, modified 08:05 → change_kind 'updated'
]
_SPECS = [
    "S-eng/b-specs:s100",
    "S-eng/b-specs:s200",
]
_ALL = _DOCS + _SPECS
_EDITED = "S-eng/b-docs:f400"


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    """Pin offline so the real SharePointIngestor reads the deterministic fixture.

    The connector chooses live vs offline from ``INGEST_MODE`` at call time. Run in
    isolation (or with a backend/.env that sets ``INGEST_MODE=live``), it would
    otherwise attempt a live Microsoft Graph call and fail for lack of a token.
    Forcing offline makes these tests deterministic regardless of environment.
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


def _sp_events(captured):
    return [p for (e, p) in captured if e == EVENT]


# ─────────────────────────────────────────────────────────────────────────────
# Registration (precondition: record_event raises for unregistered types)
# ─────────────────────────────────────────────────────────────────────────────
def test_artifact_changed_event_type_is_registered():
    assert EVENT in REGISTERED_EVENT_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — every changed SharePoint artifact emits an event with all required fields
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_emits_one_event_per_changed_sharepoint_artifact(captured):
    _drive(SharePointIngestor(), "org-1")
    events = _sp_events(captured)
    # One event per accessible driveItem (ungranted/hidden libraries excluded).
    assert {e["artifact_id"] for e in events} == set(_ALL)
    assert len(events) == len(_ALL)


def test_every_event_carries_all_required_fields(captured):
    _drive(SharePointIngestor(), "org-9")
    events = _sp_events(captured)
    assert events
    required = {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    for e in events:
        assert required <= set(e.keys())
        assert e["org_id"] == "org-9"
        assert e["connector_id"] == "sharepoint"        # source system
        assert e["artifact_id"]                          # site/drive:item, non-empty
        assert e["change_kind"] in ("created", "updated", "deleted")
        assert isinstance(e["observed_at"], str) and e["observed_at"]
        datetime.fromisoformat(e["observed_at"])         # valid UTC ISO timestamp


def test_change_kind_reflects_created_vs_edited(captured):
    _drive(SharePointIngestor(), "org-1")
    kinds = {e["artifact_id"]: e["change_kind"] for e in _sp_events(captured)}
    # The edited fixture item (lastModifiedDateTime != createdDateTime) is an
    # update; every other item is a creation.
    assert kinds[_EDITED] == "updated"
    for aid in _ALL:
        if aid != _EDITED:
            assert kinds[aid] == "created"


def test_excluded_sites_and_libraries_never_emit_events(captured):
    """AC4: ungranted sites (S-secret), ungranted libraries (b-private) and hidden
    libraries (b-hidden) are never read, so they never emit artifact_changed
    events."""
    _drive(SharePointIngestor(), "org-1")
    ids = {e["artifact_id"] for e in _sp_events(captured)}
    assert not any(
        ("S-secret" in i) or ("b-private" in i) or ("b-hidden" in i) for i in ids
    )


# ─────────────────────────────────────────────────────────────────────────────
# Only CHANGED artifacts emit — unchanged estate / incremental delta
# ─────────────────────────────────────────────────────────────────────────────
def test_unchanged_estate_emits_no_events(captured):
    _, store = _drive(SharePointIngestor(), "org-1")     # first run emits all
    captured.clear()
    _drive(SharePointIngestor(), "org-1", store=store)   # nothing new → empty delta
    assert _sp_events(captured) == []


def test_incremental_emits_only_newly_changed_artifacts(captured):
    # Delta token mid-Documents (after item f200) with Specs absent from the map.
    store = Store()
    store.save(
        Checkpoint.create(
            "sharepoint",
            "org-1",
            _encode_checkpoint({"S-eng/b-docs": "2026-06-10T09:10:00Z"}),
        )
    )
    _drive(SharePointIngestor(), "org-1", store=store)
    emitted = {e["artifact_id"] for e in _sp_events(captured)}
    # Only b-docs items strictly newer than f200's marker + all of b-specs (never seen).
    assert emitted == {
        "S-eng/b-docs:fold300",
        "S-eng/b-docs:f400",
        "S-eng/b-specs:s100",
        "S-eng/b-specs:s200",
    }
    assert "S-eng/b-docs:f100" not in emitted   # older — not re-emitted
    assert "S-eng/b-docs:f200" not in emitted   # == token, not strictly newer


# ─────────────────────────────────────────────────────────────────────────────
# Fire-and-forget: a telemetry failure must never break SharePoint ingestion
# ─────────────────────────────────────────────────────────────────────────────
def test_emission_failure_never_breaks_sharepoint_ingestion(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)
    res, _ = _drive(SharePointIngestor(), "org-1")
    assert res.ok                       # ingestion succeeded despite telemetry failure
    assert res.checkpoint_advanced
    assert res.records == len(_ALL)
