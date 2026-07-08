"""Contract tests — ``ingest_content`` producer contract over the real store (R18-B1 T5).

End-to-end verification of the producer contract against the ACTUAL
pgvector-backed ``retrieval_chunks`` store (the pure-logic suite fakes the
store):

* AC1 — content handed to ``ingest_content()`` is chunked per its content-type
  policy, embedded via the gateway (by the async T3 pipeline), and indexed with
  full provenance metadata and content hash — then retrievable via ``retrieve()``.
* AC7 — ingestion itself never embeds: handed-over content is stored with
  ``embedding IS NULL``, absent from retrieval until the async pipeline runs,
  and the handover succeeds even when no embedding provider works.
* AC3 — the handover writes only into the calling org's partition.
* Re-ingest — handing over a changed artifact replaces its previous chunks in
  the store.

Embedding (where exercised) is driven through a FAKE provider registered with
the real gateway and selected via ``MODEL_EMBEDDING_PROVIDER`` — the production
path (``get_embedding_provider()`` / ``model_gateway.embed``) is what runs; no
direct provider call (AC2). Provider names are unique to this module to avoid
registry collisions.
"""
from __future__ import annotations

import json
from typing import List

import pytest

from app import db
from app.model_gateway import register_provider
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.retrieval import embedder, store
from app.retrieval.api import retrieve
from app.retrieval.ingest import ingest_content
from database.models.retrieval import compute_content_hash


# ---------------------------------------------------------------------------
# Skip cleanly if this environment has no pgvector-backed store. In CI the
# migration runs.
# ---------------------------------------------------------------------------


def _retrieval_store_available() -> bool:
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT to_regclass('public.retrieval_chunks')")
            return cur.fetchone()[0] is not None
        finally:
            con.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _retrieval_store_available(),
    reason="retrieval_chunks store (pgvector) not present in this environment",
)


# ---------------------------------------------------------------------------
# Fake embedding providers registered with the real gateway
# ---------------------------------------------------------------------------


class _T5FakeProvider(ModelProvider):
    emits_own_telemetry = True

    def __init__(self, name: str, identity, *, mode: str = "ok"):
        self.name = name
        self._identity = identity
        self.mode = mode

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        if self.mode == "fail":
            return []
        return [[float(len(t) % 5) + 1.0, 0.5, 0.25, 0.125] for t in texts]

    def embedding_identity(self):
        return self._identity


_T5_OK = _T5FakeProvider("t5_embed_ok", ("t5:model-a", "1"))
_T5_FAIL = _T5FakeProvider("t5_embed_fail", ("t5:model-a", "1"), mode="fail")

for _p in (_T5_OK, _T5_FAIL):
    register_provider(_p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        con.commit()
    finally:
        con.close()


def _rows_for(org_id: str, source_artifact: str) -> list[dict]:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT chunk_id, content, content_hash, content_type, source_system, "
            "       source_artifact, source_timestamp, chunk_position, provenance, "
            "       embedding, embedding_model, embedding_model_version "
            "FROM retrieval_chunks "
            "WHERE org_id = %s AND source_artifact = %s "
            "ORDER BY chunk_position",
            (org_id, source_artifact),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


@pytest.fixture
def org(request, monkeypatch):
    """A unique org id per test, cleaned up after; default to the good provider."""
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _T5_OK.name)
    name = f"ct_t5_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


_PROSE = "\n\n".join(
    f"Paragraph {i} of the quarterly operations review, with enough words to "
    f"give the chunker something real to split." for i in range(60)
)

_ARTIFACT = dict(
    source_system="confluence",
    source_artifact="page/ops-review",
    content=_PROSE,
    content_type="prose",
    source_timestamp="2026-07-07T09:30:00+00:00",
    provenance={"url": "https://wiki.example/page/ops-review", "space": "OPS"},
)


# ---------------------------------------------------------------------------
# AC1 — the full flow: handed over -> chunked -> indexed -> embedded -> retrievable
# ---------------------------------------------------------------------------


def test_ac1_handover_is_chunked_indexed_embedded_and_retrievable(org):
    result = ingest_content(org, [dict(_ARTIFACT)])
    assert result.artifacts_indexed == 1
    assert result.chunks_indexed > 1  # chunked per the prose policy, not stored whole

    rows = _rows_for(org, "page/ops-review")
    assert len(rows) == result.chunks_indexed
    for pos, row in enumerate(rows):
        # Full provenance metadata + content hash, exactly as stored (AC1).
        assert row["source_system"] == "confluence"
        assert row["source_artifact"] == "page/ops-review"
        assert row["content_type"] == "prose"
        assert row["chunk_position"] == pos
        assert row["source_timestamp"] is not None
        assert row["content_hash"] == compute_content_hash(row["content"])
        assert json.loads(row["provenance"]) == _ARTIFACT["provenance"]

    # Embedding is asynchronous: the substrate embeds via the gateway (T3)...
    assert store.count_chunks(org, embedded_only=True) == 0
    run = embedder.embed_pending_for_org(org)
    assert run.embedded == result.chunks_indexed

    # ...after which the content is retrievable with pointer fields populated.
    hits = retrieve(org, "quarterly operations review", k=3)
    assert hits
    assert all(h.source_artifact == "page/ops-review" for h in hits)
    assert all(h.chunk_id and h.retrieval_result_id for h in hits)


# ---------------------------------------------------------------------------
# AC7 — ingestion never embeds and never depends on a working provider
# ---------------------------------------------------------------------------


def test_ac7_ingest_never_embeds_synchronously(org):
    ingest_content(org, [dict(_ARTIFACT)])
    # Indexed, provenance-complete, but no vector and no model stamp yet.
    assert store.count_chunks(org) > 0
    assert store.count_chunks(org, embedded_only=True) == 0
    for row in _rows_for(org, "page/ops-review"):
        assert row["embedding"] is None
        assert row["embedding_model"] is None
    # Not yet retrievable — a lag, never an error.
    assert retrieve(org, "operations review", k=5) == []


def test_ac7_handover_succeeds_with_no_working_embedding_provider(org, monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _T5_FAIL.name)
    result = ingest_content(org, [dict(_ARTIFACT)])  # must not raise, must index
    assert result.artifacts_indexed == 1
    assert store.count_chunks(org) == result.chunks_indexed
    assert store.count_chunks(org, embedded_only=True) == 0


# ---------------------------------------------------------------------------
# Re-ingest — a changed artifact replaces its previous chunks
# ---------------------------------------------------------------------------


def test_reingest_replaces_previous_chunks_in_store(org):
    first = ingest_content(org, [dict(_ARTIFACT)])
    old_ids = {r["chunk_id"] for r in _rows_for(org, "page/ops-review")}
    assert len(old_ids) == first.chunks_indexed

    revised = dict(_ARTIFACT, content="A single-paragraph rewrite of the page.")
    second = ingest_content(org, [revised])
    assert second.chunks_replaced == len(old_ids)

    rows = _rows_for(org, "page/ops-review")
    assert [r["content"] for r in rows] == ["A single-paragraph rewrite of the page."]
    assert not (old_ids & {r["chunk_id"] for r in rows})


# ---------------------------------------------------------------------------
# AC3 — the handover only ever writes the calling org's partition
# ---------------------------------------------------------------------------


def test_ac3_handover_writes_only_the_calling_orgs_partition(monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _T5_OK.name)
    org_a, org_b = "ct_t5_part_a", "ct_t5_part_b"
    _cleanup(org_a)
    _cleanup(org_b)
    try:
        ingest_content(
            org_a,
            [dict(_ARTIFACT, content="Org A private page text.", source_artifact="a/1")],
        )
        assert store.count_chunks(org_a) == 1
        assert store.count_chunks(org_b) == 0

        # After embedding, retrieval for org B still never sees org A's content.
        embedder.embed_pending_for_org(org_a)
        assert retrieve(org_b, "private page text", k=10) == []
        hits_a = retrieve(org_a, "private page text", k=10)
        assert hits_a and all(h.source_artifact == "a/1" for h in hits_a)
    finally:
        _cleanup(org_a)
        _cleanup(org_b)
