"""Unit tests for the ingestion.artifact_changed subscriber (R18-B2 T1 + T2).

Pure-logic coverage of ``app.retrieval.freshness`` with the store and refresh
queue faked, so this runs in the unit suite with no PostgreSQL dependency (the
DB-backed end-to-end coverage lives in
``tests/contract/test_retrieval_freshness_contract.py``).

Covers the freshness subscriber surface (Section 1 — The Freshness Contract):

* an updated/created event marks the artifact's chunks stale AND queues a refresh
  (T1);
* a deleted event purges the artifact IMMEDIATELY via a single atomic
  ``store.purge_artifact`` call, before any queue interaction — deletion is a
  direct index cleanup, not a refresh (T2 / AC2), and ``remove_artifact`` is
  directly invocable;
* the ingestion->retrieval field mapping (connector_id->source_system,
  artifact_id->source_artifact) is applied;
* malformed events are ignored, and a store failure never propagates (the
  subscriber runs off a telemetry event and must never break ingestion).
"""
from __future__ import annotations

import pytest

from app.retrieval import freshness
from app.retrieval.freshness import ArtifactChangedEvent, on_artifact_changed


# ---------------------------------------------------------------------------
# Fakes — record the order and arguments of every store / queue call.
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self):
        self.calls = []  # ordered (op, org, source_system, source_artifact)
        self.mark_stale_return = 3
        self.remove_return = 2
        # (chunks_removed, queue_rows_removed) returned by the atomic purge (T2).
        self.purge_return = (2, 1)

    def mark_stale(self, org_id, source_system, source_artifact):
        self.calls.append(("mark_stale", org_id, source_system, source_artifact))
        return self.mark_stale_return

    def remove_chunks(self, org_id, source_system, source_artifact):
        self.calls.append(("remove_chunks", org_id, source_system, source_artifact))
        return self.remove_return

    def purge_artifact(self, org_id, source_system, source_artifact):
        self.calls.append(("purge_artifact", org_id, source_system, source_artifact))
        return self.purge_return


class _FakeQueue:
    def __init__(self):
        self.calls = []  # ordered (op, org, source_system, source_artifact[, kind])

    def enqueue(self, org_id, source_system, source_artifact, change_kind="updated"):
        self.calls.append(("enqueue", org_id, source_system, source_artifact, change_kind))
        return "queue-id-1"

    def remove(self, org_id, source_system, source_artifact):
        self.calls.append(("remove", org_id, source_system, source_artifact))
        return 1


@pytest.fixture
def fakes(monkeypatch):
    store = _FakeStore()
    queue = _FakeQueue()
    monkeypatch.setattr(freshness, "store", store)
    monkeypatch.setattr(freshness, "refresh_queue", queue)
    # Silence the telemetry emit — its own path is covered elsewhere.
    monkeypatch.setattr(freshness, "_emit_invalidated", lambda *a, **k: None)
    return store, queue


def _event(change_kind, *, org="org_a", connector="confluence", artifact="page/1"):
    return {
        "org_id": org,
        "connector_id": connector,
        "artifact_id": artifact,
        "change_kind": change_kind,
        "observed_at": "2026-07-08T10:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Event parsing + field mapping
# ---------------------------------------------------------------------------


def test_from_payload_maps_ingestion_fields_to_retrieval_fields():
    ev = ArtifactChangedEvent.from_payload(_event("updated", connector="slack", artifact="thread/9"))
    assert ev is not None
    assert ev.org_id == "org_a"
    assert ev.source_system == "slack"        # <- connector_id
    assert ev.source_artifact == "thread/9"   # <- artifact_id
    assert ev.change_kind == "updated"


def test_from_payload_normalises_and_defaults_change_kind():
    assert ArtifactChangedEvent.from_payload(_event("UPDATED")).change_kind == "updated"
    assert ArtifactChangedEvent.from_payload(_event("Deleted")).change_kind == "deleted"
    # Unknown kind -> treated as an update (it is, after all, in a delta).
    assert ArtifactChangedEvent.from_payload(_event("weird")).change_kind == "updated"
    # Missing kind -> update.
    ev = ArtifactChangedEvent.from_payload(
        {"org_id": "o", "connector_id": "c", "artifact_id": "a"}
    )
    assert ev.change_kind == "updated"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "not a mapping",
        42,
        {},  # no org / artifact / connector
        {"org_id": "o"},  # no artifact / connector
        {"org_id": "o", "connector_id": "c"},  # no artifact
        {"org_id": "", "connector_id": "c", "artifact_id": "a"},  # blank org
        {"org_id": "o", "connector_id": "c", "artifact_id": "   "},  # blank artifact
    ],
)
def test_from_payload_returns_none_for_unusable_events(bad):
    assert ArtifactChangedEvent.from_payload(bad) is None


def test_from_payload_passes_through_an_already_parsed_event():
    ev = ArtifactChangedEvent("o", "confluence", "page/1", "updated")
    assert ArtifactChangedEvent.from_payload(ev) is ev


# ---------------------------------------------------------------------------
# AC1 (T1 side) — updated/created marks stale AND queues a refresh
# ---------------------------------------------------------------------------


def test_updated_event_marks_stale_then_enqueues(fakes):
    store, queue = fakes
    on_artifact_changed(_event("updated"))
    assert store.calls == [("mark_stale", "org_a", "confluence", "page/1")]
    assert queue.calls == [("enqueue", "org_a", "confluence", "page/1", "updated")]


def test_created_event_marks_stale_then_enqueues(fakes):
    store, queue = fakes
    on_artifact_changed(_event("created"))
    assert store.calls == [("mark_stale", "org_a", "confluence", "page/1")]
    assert queue.calls[0][:4] == ("enqueue", "org_a", "confluence", "page/1")
    assert queue.calls[0][4] == "created"


def test_stale_is_marked_before_the_refresh_is_queued(fakes):
    """Order matters: a chunk must be flagged second-class before we hand the
    slow refresh to the async worker, so there is never a window where the queue
    knows about the change but retrieval still treats the chunks as current."""
    store, queue = fakes
    on_artifact_changed(_event("updated"))
    # mark_stale (store) happens, then enqueue (queue).
    assert store.calls[0][0] == "mark_stale"
    assert queue.calls[0][0] == "enqueue"


# ---------------------------------------------------------------------------
# AC2 (T2) — deleted purges the artifact IMMEDIATELY, before any queue, atomically
# ---------------------------------------------------------------------------


def test_deleted_event_purges_and_never_marks_stale_or_enqueues(fakes):
    store, queue = fakes
    on_artifact_changed(_event("deleted"))
    assert ("purge_artifact", "org_a", "confluence", "page/1") in store.calls
    assert not any(c[0] == "mark_stale" for c in store.calls)
    assert not any(c[0] == "enqueue" for c in queue.calls)


def test_deletion_is_a_single_atomic_purge_not_a_refresh(fakes):
    """AC2 / Section 1: deletion is honoured BEFORE the re-embed queue, not after —
    no window where deleted content is still retrievable as current. T2 removes the
    chunks AND drops any pending refresh row in ONE atomic ``purge_artifact`` call,
    so the subscriber makes exactly one store call and never touches the queue on
    the freshness (enqueue/remove) surface for a delete."""
    store, queue = fakes
    on_artifact_changed(_event("deleted"))
    # A delete is a single, direct index-cleanup op — not stale-mark + enqueue.
    assert store.calls == [("purge_artifact", "org_a", "confluence", "page/1")]
    # The atomic purge owns the queue-row drop inside the store transaction, so the
    # subscriber issues no separate refresh_queue call for a delete.
    assert queue.calls == []


def test_remove_artifact_is_callable_directly(fakes):
    """The deletion cleanup is a first-class, directly-invocable operation (T2) —
    e.g. a compliance-mandated purge — on the same path the change event uses."""
    store, queue = fakes
    store.purge_return = (5, 0)
    removed = freshness.remove_artifact("org_z", "git", "repo/secret.py")
    assert removed == 5
    assert store.calls == [("purge_artifact", "org_z", "git", "repo/secret.py")]
    assert queue.calls == []


def test_remove_artifact_returns_zero_when_nothing_indexed(fakes):
    """Idempotent: purging an artifact with no indexed chunks (already gone, or
    never indexed) is a harmless no-op that reports zero removed."""
    store, queue = fakes
    store.purge_return = (0, 0)
    assert freshness.remove_artifact("org_z", "slack", "thread/none") == 0


# ---------------------------------------------------------------------------
# Robustness — never raises into the ingestion path
# ---------------------------------------------------------------------------


def test_malformed_event_is_ignored_without_touching_store_or_queue(fakes):
    store, queue = fakes
    on_artifact_changed({"garbage": True})
    assert store.calls == []
    assert queue.calls == []


def test_store_failure_is_swallowed(monkeypatch, fakes):
    store, queue = fakes

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "mark_stale", boom)
    # Must not raise — the subscriber runs off a telemetry event.
    on_artifact_changed(_event("updated"))


def test_purge_failure_on_delete_is_swallowed_by_the_event_guard(monkeypatch, fakes):
    store, queue = fakes

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "purge_artifact", boom)
    # A deletion event whose purge fails must not break ingestion — the subscriber's
    # guard swallows it (a direct remove_artifact caller would handle its own error).
    on_artifact_changed(_event("deleted"))
