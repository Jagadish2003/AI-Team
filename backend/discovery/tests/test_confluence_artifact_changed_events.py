"""
R17-A2 / AT-463 (T6) — ingestion.artifact_changed emission for Confluence artifacts.

T6 reuses the change-event path established in R16-A1 (AT-381): the change runner
emits one ``ingestion.artifact_changed`` event per changed record in every
fully-processed batch. These tests prove that the REAL ``ConfluenceIngestor`` —
driven through that runner — emits an event for every changed Confluence artifact
(page / blog post) carrying all required fields:

    org_id, connector_id='confluence' (the source system), artifact_id
    ("{space_key}:{content_id}"), change_kind ('created' | 'updated' | 'deleted'),
    observed_at (UTC ISO timestamp).

Events fire only for genuinely changed artifacts — an unchanged source (empty
delta) emits nothing (AC6). Ungranted / archived spaces and non-page content
(comments) are never read (AC4), so they never emit events.

(The existing ``tests/contract/test_artifact_changed_events.py`` proves the runner
emits using a *fake* connector; this binds AC6 to the real Confluence connector and
its real record shape — content version create/update change kinds.)

These run offline against the deterministic ``confluence_sample.json`` fixture and
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
from discovery.ingest.confluence import ConfluenceIngestor, _encode_checkpoint

EVENT = "ingestion.artifact_changed"

# Fixture artifact identities ("{space_key}:{content_id}"). Only the two granted,
# non-archived spaces are read (AC4): ENG + OPS. Only page/blogpost content is
# ingested — the ENG comment (id 450) is excluded.
_ENG = ["ENG:100", "ENG:200", "ENG:300", "ENG:400"]
_OPS = ["OPS:500", "OPS:600"]
_ALL = _ENG + _OPS
# version.number > 1 → 'updated'; version 1 → 'created'.
_UPDATED = {"ENG:200", "ENG:400", "OPS:600"}
_CREATED = {"ENG:100", "ENG:300", "OPS:500"}


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    """Pin offline so the real ConfluenceIngestor reads the deterministic fixture.

    The connector chooses live vs offline from ``INGEST_MODE`` at call time. Run in
    isolation (or with a backend/.env that sets ``INGEST_MODE=live``), it would
    otherwise attempt a live Confluence Cloud call and fail for lack of a token.
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


def _conf_events(captured):
    return [p for (e, p) in captured if e == EVENT]


# ─────────────────────────────────────────────────────────────────────────────
# Registration (precondition: record_event raises for unregistered types)
# ─────────────────────────────────────────────────────────────────────────────
def test_artifact_changed_event_type_is_registered():
    assert EVENT in REGISTERED_EVENT_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — every changed Confluence artifact emits an event with all required fields
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_emits_one_event_per_changed_confluence_artifact(captured):
    _drive(ConfluenceIngestor(), "org-1")
    events = _conf_events(captured)
    # One event per accessible page/blogpost (ungranted/archived/comment excluded).
    assert {e["artifact_id"] for e in events} == set(_ALL)
    assert len(events) == len(_ALL)


def test_every_event_carries_all_required_fields(captured):
    _drive(ConfluenceIngestor(), "org-9")
    events = _conf_events(captured)
    assert events
    required = {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    for e in events:
        assert required <= set(e.keys())
        assert e["org_id"] == "org-9"
        assert e["connector_id"] == "confluence"       # source system
        assert e["artifact_id"]                         # space:content, non-empty
        assert e["change_kind"] in ("created", "updated", "deleted")
        assert isinstance(e["observed_at"], str) and e["observed_at"]
        datetime.fromisoformat(e["observed_at"])        # valid UTC ISO timestamp


def test_change_kind_reflects_created_vs_updated(captured):
    _drive(ConfluenceIngestor(), "org-1")
    kinds = {e["artifact_id"]: e["change_kind"] for e in _conf_events(captured)}
    for aid in _UPDATED:
        assert kinds[aid] == "updated"
    for aid in _CREATED:
        assert kinds[aid] == "created"


def test_excluded_spaces_and_content_never_emit_events(captured):
    """AC4: ungranted (HR) / archived (OLD) spaces and non-page content (comments)
    are never read, so they never emit artifact_changed events."""
    _drive(ConfluenceIngestor(), "org-1")
    ids = {e["artifact_id"] for e in _conf_events(captured)}
    assert "HR:900" not in ids       # ungranted space
    assert "OLD:950" not in ids      # archived space
    assert "ENG:450" not in ids      # comment type — not a page/blogpost


# ─────────────────────────────────────────────────────────────────────────────
# Only CHANGED artifacts emit — unchanged source / incremental delta
# ─────────────────────────────────────────────────────────────────────────────
def test_unchanged_source_emits_no_events(captured):
    _, store = _drive(ConfluenceIngestor(), "org-1")     # first run emits all
    captured.clear()
    _drive(ConfluenceIngestor(), "org-1", store=store)   # nothing new → empty delta
    assert _conf_events(captured) == []


def test_incremental_emits_only_newly_changed_artifacts(captured):
    # Cursor mid-ENG (after page 200's modified time) with OPS absent from the map.
    store = Store()
    store.save(
        Checkpoint.create(
            "confluence",
            "org-1",
            _encode_checkpoint({"ENG": "2026-06-10T09:10:00Z"}),
        )
    )
    _drive(ConfluenceIngestor(), "org-1", store=store)
    emitted = {e["artifact_id"] for e in _conf_events(captured)}
    # Only ENG content strictly newer than 200's marker + all of OPS (never seen).
    assert emitted == {"ENG:300", "ENG:400", "OPS:500", "OPS:600"}
    assert "ENG:100" not in emitted   # older — not re-emitted
    assert "ENG:200" not in emitted   # == cursor, not strictly newer


# ─────────────────────────────────────────────────────────────────────────────
# Fire-and-forget: a telemetry failure must never break Confluence ingestion
# ─────────────────────────────────────────────────────────────────────────────
def test_emission_failure_never_breaks_confluence_ingestion(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)
    res, _ = _drive(ConfluenceIngestor(), "org-1")
    assert res.ok                       # ingestion succeeded despite telemetry failure
    assert res.checkpoint_advanced
    assert res.records == len(_ALL)
