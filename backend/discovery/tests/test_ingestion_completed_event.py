"""
``ingestion.completed`` — the connector-agnostic ingestion-completion event.

Run Health's "Last ingestion" had no connector-agnostic completion fact:
``db.ingestor_completed`` covered only native DB ingestors, while legacy
connector-metric overlays covered a few hardcoded connector ids. Every
change-based connector completed ingestion successfully and had nothing to display.

The shared change-based runner is the single completion path every
``ChangeBasedIngestor`` travels, so it now emits ONE ``ingestion.completed`` event
per successful pass. These tests pin that contract:

  * one event per successful pass, carrying the connector's OWN declared id
  * emitted for ANY connector — the runner names none, so a future connector is
    covered by the act of being driven through it
  * emitted for a clean pass that found NOTHING ("checked, nothing new" is a real
    successful ingestion and must be reported as one)
  * NOT emitted when the pass failed (a failure must never stamp a success time)
  * fire-and-forget: a telemetry failure never breaks ingestion
  * the pre-existing ``ingestion.artifact_changed`` behaviour is untouched

Hermetic: the checkpoint store is wired through the runner's injectable seam and
``record_event`` is monkeypatched, so no DB is required.
"""
from __future__ import annotations

import pytest

from app.telemetry import REGISTERED_EVENT_TYPES
from discovery.ingest import change_runner
from discovery.ingest.base import ChangeBasedIngestor, DeltaBatch

EVENT = "ingestion.completed"
ARTIFACT_EVENT = "ingestion.artifact_changed"


class _Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp):
        self.data[(cp.org_id, cp.connector_id)] = cp


class _Connector(ChangeBasedIngestor):
    """Fake connector yielding caller-supplied (records, next_cp, is_complete) batches."""

    connector_id = "slack"

    def __init__(self, batches, *, connector_id=None, raise_after=None):
        if connector_id is not None:
            self.connector_id = connector_id
        self._batches = batches
        self._raise_after = raise_after

    def ingest_changes(self, org_id, since):
        for index, (records, nxt, complete) in enumerate(self._batches):
            if self._raise_after is not None and index >= self._raise_after:
                raise RuntimeError("source blew up")
            yield DeltaBatch(records=records, next_checkpoint=nxt, is_complete=complete)


class _TransportConnector(_Connector):
    """A transport-only event connector — the Azure/AWS native shape."""

    connector_id = "azure_events"
    produces_retrieval_content = False


@pytest.fixture
def captured(monkeypatch):
    events: list = []
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda etype, payload=None: events.append((etype, payload or {})),
    )
    return events


def _drive(connector, org_id="org-1", store=None):
    store = store or _Store()
    result = change_runner.ingest_with_checkpoint(
        connector, org_id, read_checkpoint=store.read, save_checkpoint=store.save
    )
    return result, store


def _completions(captured):
    return [p for (e, p) in captured if e == EVENT]


def _record(artifact_id):
    return {"artifact_id": artifact_id, "change_kind": "created"}


def test_event_type_is_registered():
    # record_event() raises ValueError for an unregistered type, so registration
    # must precede any emission.
    assert EVENT in REGISTERED_EVENT_TYPES


class TestEmittedOnSuccess:

    def test_one_event_per_successful_pass_with_the_outcome(self, captured):
        result, _ = _drive(
            _Connector([([_record("a1"), _record("a2")], "cp-1", True)]), org_id="org-42"
        )
        assert result.error is None
        [payload] = _completions(captured)
        assert payload["org_id"] == "org-42"
        assert payload["connector_id"] == "slack"
        assert payload["count"] == 2
        assert payload["batches"] == 1
        assert payload["complete"] is True
        assert payload["checkpoint_advanced"] is True
        assert payload["first_run"] is True
        assert payload["observed_at"]

    def test_connector_id_comes_from_the_connector_not_a_branch(self, captured):
        # The anti-hardcoding proof: an id the runner has never seen is reported
        # correctly, so a future connector needs no code here.
        _drive(_Connector([([_record("x")], "cp-1", True)], connector_id="brand_new_thing"))
        [payload] = _completions(captured)
        assert payload["connector_id"] == "brand_new_thing"

    def test_emitted_for_a_transport_only_event_connector(self, captured):
        # produces_retrieval_content=False suppresses per-record artifact_changed —
        # this is exactly why that event could not serve as the completion signal.
        # It must NOT suppress the completion fact. This is the Azure/AWS shape.
        _drive(_TransportConnector([([_record("e1")], "cp-1", True)]))
        assert [p["connector_id"] for p in _completions(captured)] == ["azure_events"]
        assert not [p for (e, p) in captured if e == ARTIFACT_EVENT]

    def test_emitted_when_the_pass_found_nothing(self, captured):
        # "Checked, nothing new" IS a successful ingestion. Gating on count > 0
        # would leave an idle connector indistinguishable from one that never ran.
        result, _ = _drive(_Connector([([], "cp-1", True)]))
        assert result.error is None
        [payload] = _completions(captured)
        assert payload["count"] == 0

    def test_emitted_when_no_batches_were_yielded_at_all(self, captured):
        result, _ = _drive(_Connector([]))
        assert result.error is None
        [payload] = _completions(captured)
        assert payload["count"] == 0 and payload["batches"] == 0

    def test_incremental_pass_emits_too(self, captured):
        store = _Store()
        _drive(_Connector([([_record("a1")], "cp-1", True)]), store=store)
        captured.clear()
        _drive(_Connector([([_record("a2")], "cp-2", True)]), store=store)
        [payload] = _completions(captured)
        assert payload["first_run"] is False


class TestNotEmittedOnFailure:

    def test_failed_pass_emits_no_completion(self, captured):
        result, _ = _drive(_Connector([([_record("a1")], "cp-1", True)], raise_after=0))
        assert result.error is not None
        assert _completions(captured) == []

    def test_partially_checkpointed_failed_first_load_emits_nothing(self, captured):
        # Batch 0 processes and checkpoints, batch 1 raises. The pass has a captured
        # error, so no success time is stamped — the loud failure log covers it.
        result, _ = _drive(
            _Connector(
                [([_record("a1")], "cp-1", False), ([_record("a2")], "cp-2", True)],
                raise_after=1,
            )
        )
        assert result.error is not None
        assert result.checkpoint_advanced is True   # resume point preserved
        assert _completions(captured) == []


class TestNeverBreaksIngestion:

    def test_telemetry_failure_does_not_break_the_pass(self, monkeypatch):
        def boom(etype, payload=None):
            raise RuntimeError("telemetry down")

        monkeypatch.setattr("app.telemetry.record_event", boom)
        result, store = _drive(_Connector([([_record("a1")], "cp-1", True)]))
        # Ingestion outcome untouched: no captured error, checkpoint written.
        assert result.error is None
        assert result.records == 1
        assert result.checkpoint_advanced is True
        assert store.data[("org-1", "slack")].value == "cp-1"


class TestArtifactChangedUnaffected:

    def test_artifact_changed_still_emitted_per_record(self, captured):
        _drive(_Connector([([_record("a1"), _record("a2")], "cp-1", True)]))
        artifacts = [p for (e, p) in captured if e == ARTIFACT_EVENT]
        assert [p["artifact_id"] for p in artifacts] == ["a1", "a2"]
        # ...and exactly one completion regardless of record count.
        assert len(_completions(captured)) == 1
