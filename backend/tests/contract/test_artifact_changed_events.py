"""Contract tests for R16-A1 / AT-381 (T5) — ingestion.artifact_changed emission.

The change runner emits one ``ingestion.artifact_changed`` telemetry event per
changed artifact in every fully-processed batch (§4, AC4). Tests drive the real
runner with a fake ingestor and capture ``record_event`` (monkeypatched), with
the checkpoint store wired through the runner's in-memory injectable seam — so
the assertions are hermetic (no DB needed).
"""
from __future__ import annotations

import pytest

from app.telemetry import REGISTERED_EVENT_TYPES
from discovery.ingest.base import ChangeBasedIngestor, DeltaBatch
from discovery.ingest import change_runner

EVENT = "ingestion.artifact_changed"


class _Store:
    """In-memory checkpoint store wired through the runner's read/save seam."""

    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp):
        self.data[(cp.org_id, cp.connector_id)] = cp


class _Connector(ChangeBasedIngestor):
    """Fake connector yielding caller-supplied (records, next_cp, is_complete) batches."""

    connector_id = "slack"

    def __init__(self, batches):
        self._batches = batches

    def ingest_changes(self, org_id, since):
        for records, nxt, complete in self._batches:
            yield DeltaBatch(records=records, next_checkpoint=nxt, is_complete=complete)


@pytest.fixture
def captured(monkeypatch):
    """Capture telemetry events (the runner lazily imports app.telemetry.record_event)."""
    events: list = []
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda etype, payload=None: events.append((etype, payload or {})),
    )
    return events


def _drive(connector, store=None):
    store = store or _Store()
    return change_runner.ingest_with_checkpoint(
        connector, "org-1", read_checkpoint=store.read, save_checkpoint=store.save
    )


def _artifacts(captured):
    return [p for (e, p) in captured if e == EVENT]


# --------------------------------------------------------------------------
# AC4: the event type is registered.
# --------------------------------------------------------------------------
def test_event_type_is_registered():
    assert EVENT in REGISTERED_EVENT_TYPES


# --------------------------------------------------------------------------
# AC4: one event per changed artifact, carrying the required fields.
# --------------------------------------------------------------------------
def test_emits_one_event_per_changed_artifact(captured):
    recs = [
        {"artifact_id": "A1", "change_kind": "created"},
        {"artifact_id": "A2", "change_kind": "updated"},
    ]
    _drive(_Connector([(recs, "cp1", True)]))

    arts = _artifacts(captured)
    assert len(arts) == 2
    assert {a["artifact_id"] for a in arts} == {"A1", "A2"}
    for a in arts:
        assert a["org_id"] == "org-1"
        assert a["connector_id"] == "slack"
        assert a["change_kind"] in ("created", "updated")
        assert a["observed_at"]  # stamped at emit time


# --------------------------------------------------------------------------
# change_kind is passed through verbatim — created / updated / deleted.
# --------------------------------------------------------------------------
def test_change_kind_passthrough(captured):
    recs = [
        {"artifact_id": "c", "change_kind": "created"},
        {"artifact_id": "u", "change_kind": "updated"},
        {"artifact_id": "d", "change_kind": "deleted"},
    ]
    _drive(_Connector([(recs, "cp1", True)]))

    kinds = {a["artifact_id"]: a["change_kind"] for a in _artifacts(captured)}
    assert kinds == {"c": "created", "u": "updated", "d": "deleted"}


# --------------------------------------------------------------------------
# An unchanged source (empty delta) emits nothing.
# --------------------------------------------------------------------------
def test_unchanged_source_emits_nothing(captured):
    _drive(_Connector([([], "cp1", True)]))
    assert _artifacts(captured) == []


# --------------------------------------------------------------------------
# artifact_id falls back to a record's "id" when "artifact_id" is absent.
# --------------------------------------------------------------------------
def test_artifact_id_falls_back_to_id(captured):
    _drive(_Connector([([{"id": "X9", "change_kind": "updated"}], "cp1", True)]))
    arts = _artifacts(captured)
    assert arts and arts[0]["artifact_id"] == "X9"


# --------------------------------------------------------------------------
# Events are emitted for records across every batch in the stream.
# --------------------------------------------------------------------------
def test_events_emitted_across_multiple_batches(captured):
    b1 = ([{"artifact_id": "a", "change_kind": "created"}], "cp-mid", False)
    b2 = ([{"artifact_id": "b", "change_kind": "updated"}], "cp2", True)
    _drive(_Connector([b1, b2]))

    assert {a["artifact_id"] for a in _artifacts(captured)} == {"a", "b"}


# --------------------------------------------------------------------------
# Fire-and-forget: a telemetry failure must never break ingestion.
# --------------------------------------------------------------------------
def test_emission_failure_never_breaks_the_run(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)

    result = _drive(_Connector([([{"artifact_id": "A1", "change_kind": "created"}], "cp1", True)]))

    assert result.ok                    # ingestion succeeded despite telemetry failure
    assert result.checkpoint_advanced
    assert result.records == 1
