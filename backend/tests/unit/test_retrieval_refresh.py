"""Unit tests for the async refresh worker (R18-B2 T3).

Pure-logic coverage of ``app.retrieval.refresh`` with the store, refresh queue,
embedder, and content resolver faked, so this runs in the unit suite with no
PostgreSQL or model-gateway dependency (the DB-backed end-to-end coverage lives in
``tests/contract/test_retrieval_refresh_worker_contract.py``).

Covers the Section-1 ``refresh_worker`` contract:

* hash-compare — a chunk whose content hash is unchanged carries its stored vector
  over and is NOT re-embedded (AC3); only changed/new chunks are sent to the gateway;
* an update whose text did not actually change costs ZERO embedding calls (AC3 /
  Section 4 "cost proportional to change");
* the full new chunk set is swapped in via ONE ``swap_artifact_chunks`` call, after
  embedding — retrieval never sees a half-old/half-new mix (AC4), and stale is
  cleared only as part of that swap;
* a vector from a different embedding model is not reused (must re-embed under the
  active model);
* the worker's queue bookkeeping — done on success, failed/retried on error, left
  queued when there is no resolver;
* every failure path is swallowed (the worker must never raise into anything).
"""
from __future__ import annotations

import pytest

from app.retrieval import refresh
from app.retrieval.ingest import ContentArtifact
from database.models.retrieval import RetrievalChunkRecord, compute_content_hash


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self):
        self.existing = []            # rows returned by get_chunks_for_artifact
        self.swaps = []               # (org, system, artifact, records) per swap call
        self.swap_return = (0, 0)

    def get_chunks_for_artifact(self, org_id, source_system, source_artifact):
        return list(self.existing)

    def swap_artifact_chunks(self, org_id, source_system, source_artifact, records):
        self.swaps.append((org_id, source_system, source_artifact, list(records)))
        return self.swap_return


class _FakeEmbedder:
    def __init__(self, identity=("model-x", "v1")):
        self.identity = identity
        self.embed_calls = []          # each call's list of texts
        self.return_mode = "match"     # match | short | empty

    def active_embedding_model(self):
        return self.identity

    def embed_texts(self, texts):
        self.embed_calls.append(list(texts))
        if self.return_mode == "empty":
            return []
        if self.return_mode == "short":
            return [[0.1, 0.2]] * (len(texts) - 1)
        return [[float(i), float(i)] for i in range(len(texts))]


class _FakeQueue:
    def __init__(self):
        self.pending = []              # rows for fetch_pending
        self.done = []                 # (org, id)
        self.failed = []               # (org, id, error)

    def fetch_pending(self, org_id, limit=64):
        return list(self.pending[:limit])

    def mark_done(self, org_id, queue_id):
        self.done.append((org_id, queue_id))
        return 1

    def mark_failed(self, org_id, queue_id, error, max_attempts=5):
        self.failed.append((org_id, queue_id, error))
        return "pending"


@pytest.fixture(autouse=True)
def _clean_resolvers():
    refresh.clear_content_resolvers()
    yield
    refresh.clear_content_resolvers()


@pytest.fixture
def fakes(monkeypatch):
    store = _FakeStore()
    emb = _FakeEmbedder()
    queue = _FakeQueue()
    monkeypatch.setattr(refresh, "store", store)
    monkeypatch.setattr(refresh, "embedder", emb)
    monkeypatch.setattr(refresh, "refresh_queue", queue)
    return store, emb, queue


def _new_records(org, contents, source_system="confluence", source_artifact="page/1"):
    """Build the record list build_records would return for the given chunk texts."""
    return [
        RetrievalChunkRecord(
            org_id=org,
            content=c,
            content_type="prose",
            source_system=source_system,
            source_artifact=source_artifact,
            chunk_position=i,
        )
        for i, c in enumerate(contents)
    ]


def _existing_row(content, *, model="model-x", version="v1", embedding=(1.0, 1.0),
                  chunk_id=None, embedded=True):
    """A stored-chunk row as store.get_chunks_for_artifact returns it."""
    return {
        "chunk_id": chunk_id or f"old-{content}",
        "content_hash": compute_content_hash(content),
        "content_type": "prose",
        "chunk_position": 0,
        "is_stale": True,
        "embedding": list(embedding) if embedded else None,
        "embedding_model": model if embedded else None,
        "embedding_model_version": version if embedded else None,
        "embedded_at": "2026-07-01T00:00:00+00:00" if embedded else None,
    }


def _resolver_returning(text, content_type="prose"):
    def _resolve(org_id, source_artifact):
        return ContentArtifact(
            source_system="confluence",
            source_artifact=source_artifact,
            content=text,
            content_type=content_type,
        )
    return _resolve


# ---------------------------------------------------------------------------
# AC3 — re-embed only what changed
# ---------------------------------------------------------------------------


def test_unchanged_chunks_are_reused_and_only_changed_chunk_is_reembedded(fakes, monkeypatch):
    store, emb, queue = fakes
    # New content chunks to "A", "B", "C" (via the faked builder).
    monkeypatch.setattr(refresh, "build_records", lambda org, art: _new_records(org, ["A", "B", "C"]))
    # A and B already indexed + embedded; C is new.
    store.existing = [_existing_row("A"), _existing_row("B")]
    refresh.register_content_resolver("confluence", _resolver_returning("ignored"))

    out = refresh.refresh_artifact("org1", "confluence", "page/1")

    assert out.status == "refreshed"
    assert out.chunks_reused == 2          # A, B carried over — not re-embedded
    assert out.chunks_reembedded == 1      # only C embedded
    # The gateway was called exactly once, with only the changed chunk.
    assert emb.embed_calls == [["C"]]
    # Everything swapped in one call.
    assert len(store.swaps) == 1
    swapped = store.swaps[0][3]
    assert len(swapped) == 3
    # A, B keep their carried vector + original chunk_id; C got a fresh vector.
    by_content = {r.content: r for r in swapped}
    assert by_content["A"].embedding == [1.0, 1.0]
    assert by_content["A"].chunk_id == "old-A"
    assert by_content["B"].embedding == [1.0, 1.0]
    assert by_content["C"].embedding is not None
    assert by_content["C"].embedding_model == "model-x"


def test_identical_content_costs_zero_embedding_calls(fakes, monkeypatch):
    store, emb, queue = fakes
    monkeypatch.setattr(refresh, "build_records", lambda org, art: _new_records(org, ["A", "B"]))
    store.existing = [_existing_row("A"), _existing_row("B")]
    refresh.register_content_resolver("confluence", _resolver_returning("ignored"))

    out = refresh.refresh_artifact("org1", "confluence", "page/1")

    assert out.status == "refreshed"
    assert out.chunks_reused == 2
    assert out.chunks_reembedded == 0
    assert emb.embed_calls == []           # AC3: nothing re-embedded when nothing changed
    assert len(store.swaps) == 1           # still swapped (stale cleared) — cheaply


def test_a_vector_from_a_different_model_is_not_reused(fakes, monkeypatch):
    store, emb, queue = fakes
    monkeypatch.setattr(refresh, "build_records", lambda org, art: _new_records(org, ["A"]))
    # Same content, but the stored vector was produced by an older model generation.
    store.existing = [_existing_row("A", model="old-model", version="v0")]
    refresh.register_content_resolver("confluence", _resolver_returning("ignored"))

    out = refresh.refresh_artifact("org1", "confluence", "page/1")

    assert out.chunks_reused == 0
    assert out.chunks_reembedded == 1
    assert emb.embed_calls == [["A"]]      # re-embedded under the active model


def test_unembedded_existing_chunk_is_not_reusable(fakes, monkeypatch):
    store, emb, queue = fakes
    monkeypatch.setattr(refresh, "build_records", lambda org, art: _new_records(org, ["A"]))
    store.existing = [_existing_row("A", embedded=False)]  # stored but never embedded
    refresh.register_content_resolver("confluence", _resolver_returning("ignored"))

    out = refresh.refresh_artifact("org1", "confluence", "page/1")

    assert out.chunks_reused == 0
    assert out.chunks_reembedded == 1      # first embed, not a re-embed


# ---------------------------------------------------------------------------
# AC4 — atomic swap; stale cleared only as part of the replacement
# ---------------------------------------------------------------------------


def test_all_chunks_replaced_in_a_single_swap_after_embedding(fakes, monkeypatch):
    store, emb, queue = fakes
    monkeypatch.setattr(refresh, "build_records", lambda org, art: _new_records(org, ["A", "B", "C"]))
    store.existing = [_existing_row("A")]
    refresh.register_content_resolver("confluence", _resolver_returning("ignored"))

    order = []
    real_embed = emb.embed_texts
    monkeypatch.setattr(emb, "embed_texts", lambda t: (order.append("embed"), real_embed(t))[1])
    real_swap = store.swap_artifact_chunks

    def _swap(*a):
        order.append("swap")
        return real_swap(*a)

    monkeypatch.setattr(store, "swap_artifact_chunks", _swap)

    refresh.refresh_artifact("org1", "confluence", "page/1")

    # Exactly one swap, and it happens AFTER embedding the changed chunks.
    assert order == ["embed", "swap"]
    assert len(store.swaps) == 1


def test_gateway_mismatch_leaves_changed_chunks_unembedded_but_still_swaps(fakes, monkeypatch):
    store, emb, queue = fakes
    emb.return_mode = "short"              # gateway returns fewer vectors than asked
    monkeypatch.setattr(refresh, "build_records", lambda org, art: _new_records(org, ["A", "B"]))
    store.existing = []
    refresh.register_content_resolver("confluence", _resolver_returning("ignored"))

    out = refresh.refresh_artifact("org1", "confluence", "page/1")

    assert out.status == "refreshed"
    assert out.chunks_reembedded == 0      # mismatch → left for the embedding worker
    assert len(store.swaps) == 1           # swap still happens; content is fresh
    assert all(r.embedding is None for r in store.swaps[0][3])


# ---------------------------------------------------------------------------
# Re-extraction seam — resolver presence / failure
# ---------------------------------------------------------------------------


def test_no_resolver_leaves_artifact_untouched(fakes, monkeypatch):
    store, emb, queue = fakes
    monkeypatch.setattr(refresh, "build_records", lambda org, art: _new_records(org, ["A"]))
    # No resolver registered for the source system.
    out = refresh.refresh_artifact("org1", "confluence", "page/1")
    assert out.status == "no_resolver"
    assert store.swaps == []               # nothing swapped; chunks stay stale
    assert emb.embed_calls == []


def test_resolver_returning_none_is_no_content(fakes):
    store, emb, queue = fakes
    refresh.register_content_resolver("confluence", lambda o, a: None)
    out = refresh.refresh_artifact("org1", "confluence", "page/1")
    assert out.status == "no_content"
    assert store.swaps == []


def test_resolver_raising_is_swallowed_as_resolver_error(fakes):
    store, emb, queue = fakes

    def _boom(o, a):
        raise RuntimeError("source unreachable")

    refresh.register_content_resolver("confluence", _boom)
    out = refresh.refresh_artifact("org1", "confluence", "page/1")
    assert out.status == "resolver_error"
    assert store.swaps == []               # never raises; chunks stay stale


def test_swap_failure_is_swallowed_as_error(fakes, monkeypatch):
    store, emb, queue = fakes
    monkeypatch.setattr(refresh, "build_records", lambda org, art: _new_records(org, ["A"]))
    refresh.register_content_resolver("confluence", _resolver_returning("ignored"))

    def _boom(*a):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "swap_artifact_chunks", _boom)
    out = refresh.refresh_artifact("org1", "confluence", "page/1")
    assert out.status == "error"           # must not raise into the worker


def test_resolver_return_identifiers_are_forced_to_the_requested_artifact(fakes, monkeypatch):
    store, emb, queue = fakes

    def _resolver(o, a):
        # A misbehaving resolver hands back a different key.
        return ContentArtifact("git", "other/path", "A", "prose")

    refresh.register_content_resolver("confluence", _resolver)
    captured = {}
    monkeypatch.setattr(
        refresh, "build_records",
        lambda org, art: captured.update(sys=art.source_system, art=art.source_artifact) or _new_records(org, ["A"]),
    )
    refresh.refresh_artifact("org1", "confluence", "page/1")
    # The artifact handed to the builder is keyed to what we asked to refresh.
    assert captured == {"sys": "confluence", "art": "page/1"}


# ---------------------------------------------------------------------------
# Queue draining — per-org bookkeeping
# ---------------------------------------------------------------------------


def _pending_row(queue_id, artifact):
    return {
        "id": queue_id,
        "org_id": "org1",
        "source_system": "confluence",
        "source_artifact": artifact,
        "change_kind": "updated",
    }


def test_refresh_pending_for_org_marks_done_failed_and_skipped(fakes, monkeypatch):
    store, emb, queue = fakes
    queue.pending = [
        _pending_row("q1", "page/ok"),
        _pending_row("q2", "page/err"),
        _pending_row("q3", "page/noresolver"),
    ]

    def _fake_refresh(org, system, artifact):
        if artifact == "page/ok":
            return refresh.RefreshOutcome(org, system, artifact, status="refreshed",
                                          chunks_reused=1, chunks_reembedded=2)
        if artifact == "page/err":
            return refresh.RefreshOutcome(org, system, artifact, status="error", detail="boom")
        return refresh.RefreshOutcome(org, system, artifact, status="no_resolver")

    monkeypatch.setattr(refresh, "refresh_artifact", _fake_refresh)

    result = refresh.refresh_pending_for_org("org1")

    assert result.processed == 3
    assert result.refreshed == 1 and result.failed == 1 and result.skipped == 1
    assert result.reused_chunks == 1 and result.reembedded_chunks == 2
    assert queue.done == [("org1", "q1")]
    assert queue.failed and queue.failed[0][:2] == ("org1", "q2")


def test_refresh_pending_for_org_never_raises(fakes, monkeypatch):
    store, emb, queue = fakes

    def _boom(*a, **k):
        raise RuntimeError("queue down")

    monkeypatch.setattr(queue, "fetch_pending", _boom)
    # Must not raise — a pass error leaves work queued for the next tick.
    result = refresh.refresh_pending_for_org("org1")
    assert result.processed == 0
