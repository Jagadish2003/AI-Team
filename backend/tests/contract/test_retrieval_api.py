"""Contract tests — source-aware retrieve() over the real pgvector store (R18-B1 T4).

Exercises the retrieval platform API end-to-end against the ACTUAL
``retrieval_chunks`` store: content is indexed, embedded through the T3 pipeline
(driven by a fake gateway provider), then queried through ``retrieve()``. The T4
acceptance criteria are verified through real SQL:

* AC3 — ``retrieve()`` returns ONLY the querying org's chunks (two-org test).
* AC4 — ``source_filter`` scopes results to named source systems; ``min_score``
  excludes weak matches.
* AC5 — each result carries ``chunk_id`` + ``retrieval_result_id`` and builds a
  valid observed EvidencePointer populating those spine fields (no schema change).
* Ranking — results come back best-first and capped at ``k``.

Embedding runs through a FAKE provider registered with the real gateway and
selected via ``MODEL_EMBEDDING_PROVIDER`` (production path — no direct provider
call). Provider names are unique to this module to avoid registry collisions.
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
from app.provenance import OBSERVED
from app.retrieval import embedder, store
from app.retrieval.api import retrieve
from database.models.retrieval import RetrievalChunkRecord


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


class _ApiFakeProvider(ModelProvider):
    emits_own_telemetry = True

    def __init__(self, name: str, identity):
        self.name = name
        self._identity = identity

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        # Deterministic, content-shaped vectors: a query containing "alpha" lands
        # nearest the "alpha" chunk, etc., so ranking and filters are meaningful.
        out = []
        for t in texts:
            low = t.lower()
            out.append([
                1.0 if "alpha" in low else 0.0,
                1.0 if "beta" in low else 0.0,
                1.0 if "gamma" in low else 0.0,
                0.01,
            ])
        return out

    def embedding_identity(self):
        return self._identity


_API_A = _ApiFakeProvider("api_embed_model_a", ("api:model-a", "1"))
register_provider(_API_A)


def _index(org, content, *, source_system="document", source_artifact="doc/1"):
    rec = RetrievalChunkRecord(
        org_id=org,
        content=content,
        content_type="prose",
        source_system=source_system,
        source_artifact=source_artifact,
    )
    store.upsert_chunks([rec])
    return rec.chunk_id


def _cleanup(org):
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org,))
        con.commit()
    finally:
        con.close()


@pytest.fixture
def org(request, monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _API_A.name)
    name = f"t4_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


# ---------------------------------------------------------------------------
# AC3 — hard org partition
# ---------------------------------------------------------------------------


def test_ac3_retrieve_returns_only_querying_orgs_chunks(monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _API_A.name)
    org_a, org_b = "t4_part_a", "t4_part_b"
    _cleanup(org_a)
    _cleanup(org_b)
    try:
        _index(org_a, "alpha content", source_artifact="a/1")
        _index(org_b, "alpha content", source_artifact="b/1")  # same content, other org
        embedder.embed_pending_all_orgs()

        hits = retrieve(org_a, "alpha", k=10)
        assert hits, "org A should retrieve its own content"
        assert all(h.source_artifact == "a/1" for h in hits)  # never org B's
    finally:
        _cleanup(org_a)
        _cleanup(org_b)


# ---------------------------------------------------------------------------
# AC4 — source-aware + min-score
# ---------------------------------------------------------------------------


def test_ac4_source_filter_scopes_to_named_systems(org):
    _index(org, "alpha in confluence", source_system="confluence", source_artifact="c/1")
    _index(org, "alpha in slack", source_system="slack", source_artifact="s/1")
    embedder.embed_pending_for_org(org)

    only_conf = retrieve(org, "alpha", k=10, source_filter=["confluence"])
    assert only_conf and all(h.source_system == "confluence" for h in only_conf)

    only_slack = retrieve(org, "alpha", k=10, source_filter=["slack"])
    assert only_slack and all(h.source_system == "slack" for h in only_slack)

    both = retrieve(org, "alpha", k=10, source_filter=["confluence", "slack"])
    assert {h.source_system for h in both} == {"confluence", "slack"}


def test_ac4_min_score_excludes_weak_matches(org):
    _index(org, "alpha content", source_artifact="a/1")
    embedder.embed_pending_for_org(org)

    # A floor above the maximum cosine similarity (1.0) admits nothing.
    assert retrieve(org, "alpha", k=10, min_score=1.01) == []
    # A permissive floor admits the match.
    assert retrieve(org, "alpha", k=10, min_score=-1.0)


# ---------------------------------------------------------------------------
# AC5 — EvidencePointer fields populated
# ---------------------------------------------------------------------------


def test_ac5_results_populate_evidence_pointer_fields(org):
    cid = _index(org, "alpha content", source_artifact="a/1")
    embedder.embed_pending_for_org(org)

    hits = retrieve(org, "alpha", k=5)
    assert hits
    hit = hits[0]
    assert hit.chunk_id == cid
    assert hit.retrieval_result_id  # minted per query hit

    ptr = hit.to_evidence_pointer()
    assert ptr.is_valid()
    assert ptr.origin == OBSERVED
    assert ptr.chunk_id == cid  # spine field filled (was null since 1.6)
    assert ptr.retrieval_result_id == hit.retrieval_result_id


# ---------------------------------------------------------------------------
# Ranking — best-first, capped at k
# ---------------------------------------------------------------------------


def test_ranked_results_capped_at_k(org):
    _index(org, "alpha content", source_artifact="a/1")
    _index(org, "beta content", source_artifact="b/1")
    _index(org, "gamma content", source_artifact="g/1")
    embedder.embed_pending_for_org(org)

    hits = retrieve(org, "alpha", k=2)
    assert len(hits) == 2  # capped at k
    # Best-first: the "alpha" chunk (nearest the query) ranks top.
    assert hits[0].source_artifact == "a/1"
    assert hits[0].similarity >= hits[1].similarity  # descending similarity


# ---------------------------------------------------------------------------
# R18-B2 T4 / AC1 — stale chunks excluded by default, included on the flag
# ---------------------------------------------------------------------------


def _freshness_schema_available() -> bool:
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'retrieval_chunks' AND column_name = 'is_stale'"
            )
            return cur.fetchone() is not None
        finally:
            con.close()
    except Exception:
        return False


_freshness = pytest.mark.skipif(
    not _freshness_schema_available(),
    reason="retrieval freshness schema (0025) not present in this environment",
)


@_freshness
def test_ac1_stale_chunk_excluded_by_default_and_included_on_flag(org):
    from app.retrieval import store as store_mod

    _index(org, "alpha content", source_system="confluence", source_artifact="page/1")
    embedder.embed_pending_for_org(org)

    # Fresh: retrievable by default.
    assert retrieve(org, "alpha", k=10)

    # The artifact changes upstream → its chunks are marked stale.
    marked = store_mod.mark_stale(org, "confluence", "page/1")
    assert marked == 1

    # Default retrieval now excludes it — a finding is never based on stale evidence.
    assert retrieve(org, "alpha", k=10) == []

    # The explicit policy flag includes it, and the result is flagged stale so the
    # caller can tell fresh from stale.
    with_stale = retrieve(org, "alpha", k=10, include_stale=True)
    assert len(with_stale) == 1
    assert with_stale[0].is_stale is True


@_freshness
def test_fresh_results_report_not_stale(org):
    _index(org, "beta content", source_artifact="b/1")
    embedder.embed_pending_for_org(org)
    hits = retrieve(org, "beta", k=5)
    assert hits and all(h.is_stale is False for h in hits)
