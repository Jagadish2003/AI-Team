"""Contract tests — async batch embedding, end-to-end over the real store (R18-B1 T3).

These exercise the T3 embedding pipeline against the ACTUAL pgvector-backed
``retrieval_chunks`` store (unlike the pure-logic unit suite, which fakes the
store), so the acceptance criteria are verified through real SQL:

* AC1 — content indexed without a vector is embedded by the pipeline and becomes
  retrievable, stamped with full model provenance.
* AC3 — the pipeline and retrieval are hard org-partitioned: embedding org A never
  touches org B, and a query never crosses the partition.
* AC7 — a gateway outage leaves content pending (``embedding IS NULL``), absent
  from retrieval, and never raises — embedding lag is not a run failure.
* AC8 — every stored vector carries the active embedding model's identity +
  version, and retrieval only ever matches vectors from the active model.

Embedding is driven through a FAKE provider registered with the real gateway and
selected via ``MODEL_EMBEDDING_PROVIDER``, so the production path
(``get_embedding_provider()`` / ``model_gateway.embed``) is what runs — no direct
provider call. Provider names are unique to this module to avoid registry
collisions with the unit-suite fakes.
"""
from __future__ import annotations

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
from database.models.retrieval import RetrievalChunkRecord

_DIM = 4


# ---------------------------------------------------------------------------
# Skip cleanly if this environment has no pgvector-backed store (e.g. a
# provisioned DB without the retrieval migration). In CI the migration runs.
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


class _CtFakeProvider(ModelProvider):
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
        # A distinct, deterministic vector per text so nearest-neighbour ranking
        # is stable: first component encodes text length, rest are constant.
        return [[float(len(t) % 5) + 1.0, 0.5, 0.25, 0.125] for t in texts]

    def embedding_identity(self):
        return self._identity


_CT_A = _CtFakeProvider("ct_embed_model_a", ("ct:model-a", "1"))
_CT_B = _CtFakeProvider("ct_embed_model_b", ("ct:model-b", "2"))
_CT_FAIL = _CtFakeProvider("ct_embed_fail", ("ct:model-a", "1"), mode="fail")

for _p in (_CT_A, _CT_B, _CT_FAIL):
    register_provider(_p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index_unembedded(org_id: str, content: str, *, source_system="document",
                      source_artifact="doc/1", content_type="prose") -> str:
    """Write one provenance-complete chunk WITHOUT a vector (producer contract)."""
    rec = RetrievalChunkRecord(
        org_id=org_id,
        content=content,
        content_type=content_type,
        source_system=source_system,
        source_artifact=source_artifact,
    )
    store.upsert_chunks([rec])
    return rec.chunk_id


def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        con.commit()
    finally:
        con.close()


@pytest.fixture
def org(request, monkeypatch):
    """A unique org id per test, cleaned up after; default to the good provider."""
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _CT_A.name)
    name = f"ct_t3_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


# ---------------------------------------------------------------------------
# AC1 — indexed-then-embedded content is stamped and becomes retrievable
# ---------------------------------------------------------------------------


def test_ac1_content_embedded_and_retrievable_with_provenance(org):
    _index_unembedded(org, "quarterly revenue grew across regions")
    assert store.count_chunks(org, embedded_only=True) == 0  # not yet retrievable

    result = embedder.embed_pending_for_org(org)
    assert result.embedded == 1
    assert store.count_chunks(org, embedded_only=True) == 1

    hits = retrieve(org, "revenue growth", k=5)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.source_system == "document"
    assert hit.source_artifact == "doc/1"
    assert hit.chunk_id  # EvidencePointer.chunk_id populated (AC5)
    assert hit.retrieval_result_id  # EvidencePointer.retrieval_result_id populated


def test_ac8_stored_vector_carries_model_identity_and_version(org):
    cid = _index_unembedded(org, "some content")
    embedder.embed_pending_for_org(org)

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT embedding_model, embedding_model_version, embedded_at "
            "FROM retrieval_chunks WHERE chunk_id = %s AND org_id = %s",
            (cid, org),
        )
        model, version, embedded_at = cur.fetchone()
    finally:
        con.close()
    assert model == "ct:model-a"
    assert version == "1"
    assert embedded_at is not None


# ---------------------------------------------------------------------------
# AC7 — gateway outage: content stays pending, absent from retrieval, no raise
# ---------------------------------------------------------------------------


def test_ac7_gateway_outage_leaves_content_pending_not_retrievable(org, monkeypatch):
    _index_unembedded(org, "content that cannot embed yet")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _CT_FAIL.name)

    result = embedder.embed_pending_for_org(org)  # must not raise

    assert result.embedded == 0
    assert store.count_chunks(org) == 1  # still indexed
    assert store.count_chunks(org, embedded_only=True) == 0  # but not embedded
    # Retrieval sees no candidates — a lag, never an error. (Query embeds via the
    # failing provider too, so this returns [] regardless.)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _CT_A.name)
    assert retrieve(org, "content", k=5) == []


# ---------------------------------------------------------------------------
# AC8 — vectors from different models are never compared
# ---------------------------------------------------------------------------


def test_ac8_retrieval_only_matches_active_model_vectors(org, monkeypatch):
    # Embed one chunk under model A.
    _index_unembedded(org, "alpha content", source_artifact="doc/a")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _CT_A.name)
    embedder.embed_pending_for_org(org)

    # Embed a second chunk under model B.
    _index_unembedded(org, "beta content", source_artifact="doc/b")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _CT_B.name)
    embedder.embed_pending_for_org(org)

    assert store.count_chunks(org, embedded_only=True) == 2

    # Querying under model B must only ever compare against model-B vectors.
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _CT_B.name)
    hits = retrieve(org, "content", k=10)
    assert {h.source_artifact for h in hits} == {"doc/b"}

    # And under model A, only model-A vectors.
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _CT_A.name)
    hits_a = retrieve(org, "content", k=10)
    assert {h.source_artifact for h in hits_a} == {"doc/a"}


# ---------------------------------------------------------------------------
# AC3 — hard org partition through the pipeline and retrieval
# ---------------------------------------------------------------------------


def test_ac3_embedding_and_retrieval_are_org_partitioned(monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _CT_A.name)
    org_a, org_b = "ct_t3_part_a", "ct_t3_part_b"
    _cleanup(org_a)
    _cleanup(org_b)
    try:
        _index_unembedded(org_a, "org a private content", source_artifact="a/1")
        _index_unembedded(org_b, "org b private content", source_artifact="b/1")

        # Embedding only org A leaves org B pending (per-org scoping).
        embedder.embed_pending_for_org(org_a)
        assert store.count_chunks(org_a, embedded_only=True) == 1
        assert store.count_chunks(org_b, embedded_only=True) == 0

        # Retrieval for org A never returns org B's content.
        hits = retrieve(org_a, "private content", k=10)
        assert hits and all(h.source_artifact == "a/1" for h in hits)

        # The cross-org worker drains B too, still org-scoped.
        embedder.embed_pending_all_orgs()
        assert store.count_chunks(org_b, embedded_only=True) == 1
        hits_b = retrieve(org_b, "private content", k=10)
        assert hits_b and all(h.source_artifact == "b/1" for h in hits_b)
    finally:
        _cleanup(org_a)
        _cleanup(org_b)


# ---------------------------------------------------------------------------
# Batching across multiple gateway calls
# ---------------------------------------------------------------------------


def test_batching_embeds_full_backlog(org):
    for i in range(5):
        _index_unembedded(org, f"chunk number {i}", source_artifact=f"doc/{i}")

    result = embedder.embed_pending_for_org(org, batch_size=2)

    assert result.embedded == 5
    assert result.batches == 3
    assert store.count_chunks(org, embedded_only=True) == 5
