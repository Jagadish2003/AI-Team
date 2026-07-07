"""Unit tests for the async batch embedding pipeline (R18-B1 T3).

Covers the T3 acceptance surface (Section 1 "Embedding pipeline"):

* AC2 — embedding routes through the model gateway ONLY. These tests drive a
  FAKE provider registered with the gateway and set ``MODEL_EMBEDDING_PROVIDER``
  to it, so the exact production path (``get_embedding_provider()`` /
  ``model_gateway.embed``) is exercised — no direct provider call anywhere.
* AC7 — embedding is asynchronous with respect to runs: a gateway outage, a
  partial/misaligned response, or a per-row store error leaves content PENDING
  and never raises. The pipeline entry points return a result object instead.
* AC8 — every stamped vector carries the active embedding model's identity +
  version, resolved from the gateway per pass; a model repin changes the stamp.
* Batching — pending chunks are embedded in bounded batches, oldest-first, and
  already-embedded content is never re-embedded.

The ``store`` layer is monkeypatched with an in-memory fake so these run in the
unit suite with no PostgreSQL/pgvector dependency; the DB-backed end-to-end
coverage lives in the contract suite.
"""
from __future__ import annotations

from typing import List

import pytest

from app.model_gateway import get_embedding_provider, register_provider
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.retrieval import embedder


# ---------------------------------------------------------------------------
# Fake embedding provider — registered with the REAL gateway so embedding
# routes through get_embedding_provider()/embed() exactly as in production.
# ---------------------------------------------------------------------------


class _FakeEmbeddingProvider(ModelProvider):
    emits_own_telemetry = True  # keep the gateway telemetry path quiet in tests

    def __init__(self, name: str, identity, *, mode: str = "ok", dim: int = 4):
        self.name = name
        self._identity = identity
        self.mode = mode  # 'ok' | 'fail' | 'partial'
        self.dim = dim
        self.calls: List[List[str]] = []

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        self.calls.append(list(texts))
        if self.mode == "fail":
            return []  # graceful degradation — gateway outage shape
        if self.mode == "partial":
            texts = texts[:-1] if len(texts) > 1 else []  # drop one → misaligned
        # Deterministic, content-derived vector so a stamp can be checked.
        return [[float(len(t) % 7), 0.1, 0.2, 0.3][: self.dim] for t in texts]

    def embedding_identity(self):
        return self._identity


_GOOD = _FakeEmbeddingProvider("fake_embed_good", ("fake:model-a", "1.0"))
_MODEL_B = _FakeEmbeddingProvider("fake_embed_model_b", ("fake:model-b", "2.0"))
_FAIL = _FakeEmbeddingProvider("fake_embed_fail", ("fake:model-a", "1.0"), mode="fail")
_PARTIAL = _FakeEmbeddingProvider("fake_embed_partial", ("fake:model-a", "1.0"), mode="partial")

for _p in (_GOOD, _MODEL_B, _FAIL, _PARTIAL):
    register_provider(_p)


# ---------------------------------------------------------------------------
# In-memory store fake — records what the pipeline reads and stamps.
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self):
        # chunk_id -> {"org_id","content","embedding","model","version"}
        self.rows: dict[str, dict] = {}
        self._seq = 0
        self.stamp_error_for: set[str] = set()

    def add(self, org_id: str, content: str) -> str:
        self._seq += 1
        cid = f"c{self._seq}"
        self.rows[cid] = {
            "org_id": org_id,
            "content": content,
            "embedding": None,
            "model": None,
            "version": None,
            "seq": self._seq,
        }
        return cid

    # --- functions the pipeline calls (same signatures as app.retrieval.store) --

    def fetch_unembedded(self, org_id: str, limit: int = 128):
        pend = [
            {"chunk_id": cid, "content": r["content"]}
            for cid, r in sorted(self.rows.items(), key=lambda kv: kv[1]["seq"])
            if r["org_id"] == org_id and r["embedding"] is None
        ]
        return pend[:limit]

    def orgs_with_unembedded(self, limit: int = 100):
        seen = []
        for r in sorted(self.rows.values(), key=lambda x: x["seq"]):
            if r["embedding"] is None and r["org_id"] not in seen:
                seen.append(r["org_id"])
        return seen[:limit]

    def set_embedding(self, chunk_id, org_id, vector, embedding_model, embedding_model_version):
        if chunk_id in self.stamp_error_for:
            raise RuntimeError("simulated per-row DB error")
        r = self.rows.get(chunk_id)
        if r is None or r["org_id"] != org_id:
            return False
        r["embedding"] = list(vector)
        r["model"] = embedding_model
        r["version"] = embedding_model_version
        return True

    # --- test helpers ---------------------------------------------------------

    def embedded_count(self, org_id):
        return sum(
            1 for r in self.rows.values() if r["org_id"] == org_id and r["embedding"] is not None
        )

    def pending_count(self, org_id):
        return sum(
            1 for r in self.rows.values() if r["org_id"] == org_id and r["embedding"] is None
        )


@pytest.fixture
def store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(embedder.store, "fetch_unembedded", fake.fetch_unembedded)
    monkeypatch.setattr(embedder.store, "orgs_with_unembedded", fake.orgs_with_unembedded)
    monkeypatch.setattr(embedder.store, "set_embedding", fake.set_embedding)
    return fake


@pytest.fixture
def use_good_provider(monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _GOOD.name)
    _GOOD.calls.clear()
    return _GOOD


# ---------------------------------------------------------------------------
# AC8 — model identity + version resolved from the gateway and stamped
# ---------------------------------------------------------------------------


def test_active_embedding_model_reads_from_gateway(use_good_provider):
    assert embedder.active_embedding_model() == ("fake:model-a", "1.0")
    assert embedder.embedding_model_identity() == "fake:model-a"
    assert embedder.embedding_model_version() == "1.0"
    # Resolved through the gateway's provider — same object the gateway returns.
    assert get_embedding_provider() is use_good_provider


def test_every_vector_stamped_with_identity_and_version(store, use_good_provider):
    org = "org_stamp"
    store.add(org, "alpha")
    store.add(org, "beta")

    result = embedder.embed_pending_for_org(org)

    assert result.embedded == 2
    assert result.model_identity == "fake:model-a"
    assert result.model_version == "1.0"
    for r in store.rows.values():
        assert r["model"] == "fake:model-a"
        assert r["version"] == "1.0"
        assert r["embedding"] is not None


def test_repin_to_different_model_stamps_the_new_identity(store, monkeypatch):
    org = "org_repin"
    store.add(org, "one")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _GOOD.name)
    embedder.embed_pending_for_org(org)

    store.add(org, "two")  # new content after a model repin
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _MODEL_B.name)
    embedder.embed_pending_for_org(org)

    stamps = {(r["model"], r["version"]) for r in store.rows.values()}
    assert stamps == {("fake:model-a", "1.0"), ("fake:model-b", "2.0")}


def test_provider_default_identity_is_name_with_empty_version():
    # The base ModelProvider.embedding_identity() is (name, "") — adequate for a
    # provider that does not embed (e.g. hosted), so a stamp is always defined.
    class _Bare(ModelProvider):
        name = "bare_provider"

        def generate(self, req):  # pragma: no cover
            return GenerationResult(text=None, provider=self.name, ok=False)

        def embed(self, texts):  # pragma: no cover
            return []

    assert _Bare().embedding_identity() == ("bare_provider", "")


# ---------------------------------------------------------------------------
# Batching + no re-embedding
# ---------------------------------------------------------------------------


def test_pending_chunks_embedded_in_bounded_batches(store, use_good_provider):
    org = "org_batch"
    for i in range(5):
        store.add(org, f"chunk-{i}")

    result = embedder.embed_pending_for_org(org, batch_size=2)

    assert result.embedded == 5
    assert result.pending_seen == 5
    assert result.batches == 3  # 2 + 2 + 1
    # Each gateway call carried at most the batch size.
    assert all(len(c) <= 2 for c in use_good_provider.calls)
    assert store.pending_count(org) == 0


def test_already_embedded_content_is_never_reembedded(store, use_good_provider):
    org = "org_once"
    store.add(org, "x")
    embedder.embed_pending_for_org(org)
    use_good_provider.calls.clear()

    # Second pass: nothing pending, so the gateway is never called again.
    result = embedder.embed_pending_for_org(org)
    assert result.pending_seen == 0
    assert result.embedded == 0
    assert use_good_provider.calls == []


def test_max_chunks_caps_the_pass(store, use_good_provider):
    org = "org_cap"
    for i in range(5):
        store.add(org, f"c{i}")

    result = embedder.embed_pending_for_org(org, batch_size=2, max_chunks=3)
    assert result.embedded == 3
    assert store.pending_count(org) == 2


# ---------------------------------------------------------------------------
# AC7 — asynchronous w.r.t. runs: never blocks, leaves content pending
# ---------------------------------------------------------------------------


def test_gateway_outage_leaves_content_pending_never_raises(store, monkeypatch):
    org = "org_outage"
    store.add(org, "a")
    store.add(org, "b")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _FAIL.name)

    result = embedder.embed_pending_for_org(org)  # must not raise

    assert result.embedded == 0
    assert store.pending_count(org) == 2  # still retrievable-later, not lost


def test_partial_or_misaligned_batch_left_pending(store, monkeypatch):
    org = "org_partial"
    store.add(org, "a")
    store.add(org, "b")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _PARTIAL.name)

    result = embedder.embed_pending_for_org(org)

    # A vector count that does not match the input count is unsafe to align, so
    # the whole batch is left pending rather than mis-stamped.
    assert result.embedded == 0
    assert store.pending_count(org) == 2


def test_full_page_failure_terminates_and_leaves_pending(store, monkeypatch):
    # A backlog LARGER than one batch that fails to embed must not loop forever
    # re-fetching the same pending rows: a page that isn't fully stamped ends the
    # pass and leaves the rows for the next worker tick.
    org = "org_fullfail"
    for i in range(4):
        store.add(org, f"c{i}")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _FAIL.name)

    result = embedder.embed_pending_for_org(org, batch_size=2)  # must return, not hang

    assert result.embedded == 0
    assert result.batches == 1  # stopped after the first unproductive batch
    assert store.pending_count(org) == 4


def test_no_embedding_model_available_leaves_pending(store, monkeypatch):
    org = "org_no_model"
    store.add(org, "a")

    def _no_identity():
        return ("", "")

    monkeypatch.setattr(embedder, "active_embedding_model", _no_identity)
    result = embedder.embed_pending_for_org(org)
    assert result.embedded == 0
    assert store.pending_count(org) == 1


def test_per_row_stamp_error_isolated(store, use_good_provider):
    org = "org_rowerr"
    c1 = store.add(org, "a")
    store.add(org, "b")
    store.stamp_error_for.add(c1)

    result = embedder.embed_pending_for_org(org)  # must not raise
    # The bad row stayed pending; the other was embedded.
    assert result.embedded == 1
    assert store.pending_count(org) == 1


def test_fetch_error_does_not_raise(store, use_good_provider, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(embedder.store, "fetch_unembedded", _boom)
    result = embedder.embed_pending_for_org("org_x")  # must not raise
    assert result.embedded == 0


# ---------------------------------------------------------------------------
# AC3 — every content read stays org-scoped; worker covers all orgs
# ---------------------------------------------------------------------------


def test_embed_pending_for_org_only_touches_that_org(store, use_good_provider):
    store.add("org_a", "a-content")
    store.add("org_b", "b-content")

    embedder.embed_pending_for_org("org_a")

    assert store.embedded_count("org_a") == 1
    assert store.embedded_count("org_b") == 0  # other org untouched


def test_embed_pending_all_orgs_drains_every_org(store, use_good_provider):
    store.add("org_1", "x")
    store.add("org_2", "y")
    store.add("org_2", "z")

    results = embedder.embed_pending_all_orgs()

    by_org = {r.org_id: r for r in results}
    assert set(by_org) == {"org_1", "org_2"}
    assert store.pending_count("org_1") == 0
    assert store.pending_count("org_2") == 0


def test_all_orgs_enumeration_error_does_not_raise(store, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(embedder.store, "orgs_with_unembedded", _boom)
    assert embedder.embed_pending_all_orgs() == []  # never raises
