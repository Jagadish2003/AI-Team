"""Contract tests — the async refresh worker over the real store (R18-B2 T3).

End-to-end verification of ``app.retrieval.refresh`` against the ACTUAL
pgvector-backed ``retrieval_chunks`` store + ``retrieval_refresh_queue`` (the
pure-logic suite fakes them). The model gateway is the ONLY faked dependency here
(a deterministic embedder stands in for ``embedder.embed_texts`` /
``active_embedding_model`` so the test is hermetic — the gateway itself is covered
by the R16-D1 no-bypass tests). Proves the Section-1 ``refresh_worker`` contract on
real rows:

* AC3 — a re-extracted artifact re-embeds ONLY the chunks whose content hash
  actually changed; unchanged chunks keep their stored vector (same vector, same
  ``embedded_at``) and are never sent to the gateway; an update with no real text
  change costs zero embedding calls.
* AC4 — the chunk set is replaced atomically: after a refresh the artifact holds
  exactly the new set (old-only chunks gone), never a half-old/half-new mix.
* Stale is cleared only as part of the replacement — after the swap the artifact's
  chunks are all ``is_stale = FALSE`` and the queue row is drained.
* Org isolation — refreshing one org's artifact never touches another's.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app import db
from app.retrieval import embedder, refresh, refresh_queue, store
from app.retrieval.ingest import ContentArtifact
from database.models.retrieval import RetrievalChunkRecord, compute_content_hash


# ---------------------------------------------------------------------------
# Skip cleanly if this environment has no freshness schema (CI runs migration 0025).
# ---------------------------------------------------------------------------


def _freshness_schema_available() -> bool:
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT to_regclass('public.retrieval_refresh_queue')")
            if cur.fetchone()[0] is None:
                return False
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'retrieval_chunks' AND column_name = 'is_stale'"
            )
            return cur.fetchone() is not None
        finally:
            con.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _freshness_schema_available(),
    reason="retrieval freshness schema (0025) not present in this environment",
)


# ---------------------------------------------------------------------------
# Deterministic embedder — stands in for the model gateway
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_embedder(monkeypatch):
    calls: list[list[str]] = []

    def _active():
        return ("test-embed", "v1")

    def _embed(texts):
        calls.append(list(texts))
        # A distinct, deterministic vector per text (content-independent is fine —
        # the test only asserts which chunks were embedded, not similarity).
        return [[7.0, 8.0, 9.0] for _ in texts]

    monkeypatch.setattr(embedder, "active_embedding_model", _active)
    monkeypatch.setattr(embedder, "embed_texts", _embed)
    return calls


@pytest.fixture(autouse=True)
def _clean_resolvers():
    refresh.clear_content_resolvers()
    yield
    refresh.clear_content_resolvers()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        cur.execute("DELETE FROM retrieval_refresh_queue WHERE org_id = %s", (org_id,))
        con.commit()
    finally:
        con.close()


def _seed_embedded_chunk(org, system, artifact, content, position, chunk_id):
    """Upsert one already-embedded, stale chunk (the state after an 'updated' event)."""
    rec = RetrievalChunkRecord(
        chunk_id=chunk_id,
        org_id=org,
        content=content,
        content_type="prose",
        source_system=system,
        source_artifact=artifact,
        chunk_position=position,
        embedding=[1.0, 2.0, 3.0],
        embedding_model="test-embed",
        embedding_model_version="v1",
        embedded_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    store.upsert_chunks([rec])
    return rec


def _rows(org, artifact):
    return store.get_chunks_for_artifact(org, "confluence", artifact)


@pytest.fixture
def org(request):
    name = f"ct_refresh_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


def _new_record(org, artifact, content, position):
    return RetrievalChunkRecord(
        org_id=org, content=content, content_type="prose",
        source_system="confluence", source_artifact=artifact, chunk_position=position,
    )


# ---------------------------------------------------------------------------
# AC3 — re-embed only changed; AC4 — atomic swap; stale cleared on completion
# ---------------------------------------------------------------------------


def test_refresh_reembeds_only_changed_chunk_and_clears_stale(org, fake_embedder, monkeypatch):
    art = "page/ops"
    a_id, b_id = str(uuid4()), str(uuid4())
    _seed_embedded_chunk(org, "confluence", art, "chunk-A", 0, a_id)
    _seed_embedded_chunk(org, "confluence", art, "chunk-B", 1, b_id)
    _seed_embedded_chunk(org, "confluence", art, "chunk-C", 2, str(uuid4()))
    store.mark_stale(org, "confluence", art)
    refresh_queue.enqueue(org, "confluence", art, change_kind="updated")
    assert store.count_stale(org) == 3
    assert refresh_queue.pending_count(org) == 1

    # Re-extraction yields A and B unchanged, C replaced by C-v2 (one real change).
    refresh.register_content_resolver(
        "confluence",
        lambda o, a: ContentArtifact("confluence", a, "irrelevant", "prose"),
    )
    monkeypatch.setattr(
        refresh, "build_records",
        lambda o, a: [
            _new_record(o, art, "chunk-A", 0),
            _new_record(o, art, "chunk-B", 1),
            _new_record(o, art, "chunk-C-v2", 2),
        ],
    )

    result = refresh.refresh_pending_for_org(org)

    # AC3: only the changed chunk went to the gateway.
    assert fake_embedder == [["chunk-C-v2"]]
    assert result.refreshed == 1
    assert result.reused_chunks == 2
    assert result.reembedded_chunks == 1

    # AC4: the artifact now holds exactly the new set — old 'chunk-C' is gone.
    rows = {r["content_hash"]: r for r in _rows(org, art)}
    assert compute_content_hash("chunk-A") in rows
    assert compute_content_hash("chunk-B") in rows
    assert compute_content_hash("chunk-C-v2") in rows
    assert compute_content_hash("chunk-C") not in rows
    assert store.count_chunks(org) == 3

    # Unchanged chunks kept their ORIGINAL vector + chunk_id (not re-embedded).
    a_row = rows[compute_content_hash("chunk-A")]
    assert a_row["chunk_id"] == a_id
    assert a_row["embedding"] == [1.0, 2.0, 3.0]
    # The changed chunk carries the freshly-embedded vector.
    c_row = rows[compute_content_hash("chunk-C-v2")]
    assert c_row["embedding"] == [7.0, 8.0, 9.0]

    # Stale cleared only after the swap, and the queue drained.
    assert store.count_stale(org) == 0
    assert all(r["is_stale"] is False for r in _rows(org, art))
    assert refresh_queue.pending_count(org) == 0


def test_unchanged_artifact_costs_zero_embedding_calls(org, fake_embedder, monkeypatch):
    art = "page/same"
    _seed_embedded_chunk(org, "confluence", art, "chunk-A", 0, str(uuid4()))
    _seed_embedded_chunk(org, "confluence", art, "chunk-B", 1, str(uuid4()))
    store.mark_stale(org, "confluence", art)
    refresh_queue.enqueue(org, "confluence", art, change_kind="updated")

    refresh.register_content_resolver(
        "confluence", lambda o, a: ContentArtifact("confluence", a, "x", "prose")
    )
    monkeypatch.setattr(
        refresh, "build_records",
        lambda o, a: [_new_record(o, art, "chunk-A", 0), _new_record(o, art, "chunk-B", 1)],
    )

    result = refresh.refresh_pending_for_org(org)

    assert fake_embedder == []          # AC3: nothing changed → nothing re-embedded
    assert result.reused_chunks == 2 and result.reembedded_chunks == 0
    assert store.count_stale(org) == 0  # still cleared stale (cheaply)
    assert refresh_queue.pending_count(org) == 0


def test_no_resolver_leaves_chunks_stale_and_queued(org, fake_embedder):
    art = "page/noresolver"
    _seed_embedded_chunk(org, "confluence", art, "chunk-A", 0, str(uuid4()))
    store.mark_stale(org, "confluence", art)
    refresh_queue.enqueue(org, "confluence", art, change_kind="updated")

    # No resolver registered for 'confluence'.
    result = refresh.refresh_pending_for_org(org)

    assert result.skipped == 1 and result.refreshed == 0
    # Chunks stay stale (never served as current) and the work stays queued.
    assert store.count_stale(org) == 1
    assert refresh_queue.pending_count(org) == 1


def test_refresh_is_org_scoped(org, fake_embedder, monkeypatch):
    other = org + "_other"
    _cleanup(other)
    try:
        art = "page/shared-id"
        _seed_embedded_chunk(org, "confluence", art, "chunk-A", 0, str(uuid4()))
        _seed_embedded_chunk(other, "confluence", art, "chunk-A", 0, str(uuid4()))
        store.mark_stale(org, "confluence", art)
        store.mark_stale(other, "confluence", art)
        refresh_queue.enqueue(org, "confluence", art, change_kind="updated")

        refresh.register_content_resolver(
            "confluence", lambda o, a: ContentArtifact("confluence", a, "x", "prose")
        )
        monkeypatch.setattr(
            refresh, "build_records",
            lambda o, a: [_new_record(o, art, "chunk-A-v2", 0)],
        )

        refresh.refresh_pending_for_org(org)

        # Acting org refreshed (stale cleared); the other org is untouched.
        assert store.count_stale(org) == 0
        assert store.count_stale(other) == 1
        assert store.count_chunks(other) == 1
    finally:
        _cleanup(other)
