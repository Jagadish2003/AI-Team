"""R18-B1 T8 — Retrieval-substrate acceptance suite (Section 5, AC1–AC8).

The consolidated contract suite for the retrieval substrate. Where the per-task
contract tests (T3 embedding pipeline, T4 ``retrieve()``, T5 ``ingest_content``,
T6 evidence source, T7 no-bypass) each prove one slice, THIS suite proves every
acceptance criterion in Section 5 through the ONE public pipeline a real caller
uses, end to end:

    ingest_content(org, artifacts)          # T5 producer contract
        → embedder.embed_pending_for_org()   # T3 async, gateway-only
            → retrieve(org, query, …)         # T4 source-aware API
                → assemble_context(…, evidence_source=retrieval_evidence_source)  # T6

Each test is named for the acceptance criterion it proves and exercises behaviour,
not mere function existence (per the T8 ticket). The suite also closes coverage
the per-task tests leave thin: AC1 across ALL three content types, AC4's combined
source_filter + min_score and the scope-to-nothing edge, AC5's "no schema change"
clause asserted against the live table, and AC6's load-bearing "ONE path in"
property — retrieved content reaches a ``ContextPackage`` ONLY through
``assemble_context``.

Embedding always runs through a FAKE provider registered with the REAL model
gateway and selected via ``MODEL_EMBEDDING_PROVIDER`` — the production path
(``get_embedding_provider()`` / ``model_gateway.embed``) is what executes, so no
test makes a direct provider call (AC2). Provider names are unique to this module
to avoid registry collisions with the per-task suites.
"""
from __future__ import annotations

import json
from typing import List

import pytest

from app import db
from app.context_assembly import (
    REASON_BELOW_FLOOR,
    AssemblyPolicy,
    assemble_context,
)
from app.model_gateway import register_provider
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.provenance import OBSERVED
from app.retrieval import embedder, store
from app.retrieval.api import retrieve
from app.retrieval.evidence_source import retrieval_evidence_source
from app.retrieval.ingest import ingest_content
from database.models.retrieval import compute_content_hash

# AC2 is a scan-based guarantee (T7). Reuse its scanner here so the acceptance
# suite also asserts the embedding path is gateway-only — one place proves it and
# there is no duplicated pattern list.
from tests.contract.test_retrieval_no_bypass import (
    EMBEDDING_FORBIDDEN_PATTERNS,
    _retrieval_scope_files,
    _scan_file,
    _scan_for,
)


# ---------------------------------------------------------------------------
# Skip cleanly if this environment has no pgvector-backed store. In CI the
# retrieval migration runs, so the suite executes there.
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


class _AccFakeProvider(ModelProvider):
    """Deterministic, content-shaped embeddings so ranking and filters are real.

    A text containing 'alpha' lands nearest an 'alpha' query, 'beta' nearest
    'beta', and so on — the same technique the T4 contract suite uses — so
    source_filter, min_score, and best-first ranking are all meaningfully tested.
    """

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
        out = []
        for t in texts:
            low = t.lower()
            out.append(
                [
                    1.0 if "alpha" in low else 0.0,
                    1.0 if "beta" in low else 0.0,
                    1.0 if "gamma" in low else 0.0,
                    0.01,
                ]
            )
        return out

    def embedding_identity(self):
        return self._identity


_ACC_A = _AccFakeProvider("acc_embed_a", ("acc:model-a", "1"))
_ACC_B = _AccFakeProvider("acc_embed_b", ("acc:model-b", "2"))
_ACC_FAIL = _AccFakeProvider("acc_embed_fail", ("acc:model-a", "1"), mode="fail")

for _p in (_ACC_A, _ACC_B, _ACC_FAIL):
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
            "       embedding_model, embedding_model_version "
            "FROM retrieval_chunks WHERE org_id = %s AND source_artifact = %s "
            "ORDER BY chunk_position",
            (org_id, source_artifact),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


@pytest.fixture
def org(request, monkeypatch):
    """A unique org id per test, cleaned up before and after; good provider by default."""
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _ACC_A.name)
    name = f"acc_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


# ===========================================================================
# AC1 — handed over → chunked per content-type → hashed → embedded → indexed
# ===========================================================================


def test_ac1_all_content_types_chunked_hashed_embedded_indexed(org):
    """Every content type is chunked per its policy, hashed, provenance-stamped,
    embedded via the gateway, and indexed — then retrievable."""
    prose = "\n\n".join(
        f"Alpha paragraph {i} of the operations review, with enough real words "
        f"for the prose chunker to split it into more than one unit." for i in range(40)
    )
    conversation = "\n\n".join(f"user: beta message number {i}" for i in range(30))
    code = "\n".join(
        f"def gamma_handler_{i}(x):\n    return x + {i}\n" for i in range(20)
    )
    artifacts = [
        dict(source_system="confluence", source_artifact="page/1", content=prose,
             content_type="prose", source_timestamp="2026-07-07T09:00:00+00:00",
             provenance={"space": "OPS", "url": "https://wiki/x"}),
        dict(source_system="slack", source_artifact="thread/1", content=conversation,
             content_type="conversation", provenance={"channel": "#ops"}),
        dict(source_system="git", source_artifact="repo/util.py", content=code,
             content_type="code", provenance={"repo": "core"}),
    ]

    result = ingest_content(org, artifacts)
    assert result.artifacts_indexed == 3
    # Prose with 40 paragraphs must split into more than one chunk (policy applied,
    # not stored whole).
    prose_rows = _rows_for(org, "page/1")
    assert len(prose_rows) > 1

    # Full provenance metadata + content hash, exactly as stored (AC1).
    for row in prose_rows:
        assert row["content_type"] == "prose"
        assert row["source_system"] == "confluence"
        assert row["source_timestamp"] is not None
        assert row["content_hash"] == compute_content_hash(row["content"])
        assert json.loads(row["provenance"])["space"] == "OPS"
    for artifact, ctype in (("thread/1", "conversation"), ("repo/util.py", "code")):
        rows = _rows_for(org, artifact)
        assert rows and all(r["content_type"] == ctype for r in rows)
        assert all(r["content_hash"] == compute_content_hash(r["content"]) for r in rows)

    # Embedding is asynchronous and gateway-driven; after it runs, all three
    # content types are retrievable by a term from each.
    run = embedder.embed_pending_for_org(org)
    assert run.embedded == result.chunks_indexed
    assert retrieve(org, "alpha", k=5, source_filter=["confluence"])
    assert retrieve(org, "beta", k=5, source_filter=["slack"])
    assert retrieve(org, "gamma", k=5, source_filter=["git"])


# ===========================================================================
# AC2 — all embedding routes through the gateway (no direct embedding call)
# ===========================================================================


def test_ac2_embedding_path_is_gateway_only(org):
    """The retrieval package contains no direct embedding call (scan-based, T7),
    AND the vectors the pipeline stored were produced by the gateway-selected
    provider — proving the real embedding path is the gateway, not a bypass."""
    # Static guarantee: no direct provider/embedding literal in the retrieval scope.
    for py_file in _retrieval_scope_files():
        assert not _scan_file(py_file), f"canonical no-bypass violation in {py_file}"
        assert not _scan_for(py_file, EMBEDDING_FORBIDDEN_PATTERNS), (
            f"direct embedding call in {py_file}"
        )

    # Behavioural corroboration: content embedded through model_gateway.embed is
    # stamped with the gateway-selected provider's identity.
    ingest_content(org, [dict(source_system="document", source_artifact="d/1",
                              content="alpha content", content_type="prose")])
    embedder.embed_pending_for_org(org)
    rows = _rows_for(org, "d/1")
    assert rows and all(r["embedding_model"] == "acc:model-a" for r in rows)


# ===========================================================================
# AC3 — cross-tenant isolation extended to the vector store (two-org test)
# ===========================================================================


def test_ac3_two_org_cross_tenant_isolation(monkeypatch):
    """Identical text in two orgs: each org retrieves ONLY its own chunks."""
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _ACC_A.name)
    org_a, org_b = "acc_iso_a", "acc_iso_b"
    _cleanup(org_a)
    _cleanup(org_b)
    try:
        ingest_content(org_a, [dict(source_system="document", source_artifact="a/1",
                                    content="alpha shared content", content_type="prose")])
        ingest_content(org_b, [dict(source_system="document", source_artifact="b/1",
                                    content="alpha shared content", content_type="prose")])
        embedder.embed_pending_all_orgs()

        hits_a = retrieve(org_a, "alpha", k=10)
        hits_b = retrieve(org_b, "alpha", k=10)
        assert hits_a and all(h.source_artifact == "a/1" for h in hits_a)
        assert hits_b and all(h.source_artifact == "b/1" for h in hits_b)
        # Neither org's results ever carry the other's artifact.
        assert not ({h.source_artifact for h in hits_a} & {"b/1"})
    finally:
        _cleanup(org_a)
        _cleanup(org_b)


# ===========================================================================
# AC4 — source_filter scopes to named systems; min_score excludes weak matches
# ===========================================================================


def test_ac4_source_filter_and_min_score(org):
    ingest_content(org, [
        dict(source_system="confluence", source_artifact="c/1",
             content="alpha in confluence", content_type="prose"),
        dict(source_system="slack", source_artifact="s/1",
             content="alpha in slack", content_type="prose"),
    ])
    embedder.embed_pending_for_org(org)

    # source_filter scopes to the named system only.
    only_conf = retrieve(org, "alpha", k=10, source_filter=["confluence"])
    assert only_conf and all(h.source_system == "confluence" for h in only_conf)
    both = retrieve(org, "alpha", k=10, source_filter=["confluence", "slack"])
    assert {h.source_system for h in both} == {"confluence", "slack"}

    # An explicit filter that names nothing valid returns nothing (never widens).
    assert retrieve(org, "alpha", k=10, source_filter=["sharepoint"]) == []
    assert retrieve(org, "alpha", k=10, source_filter=["  "]) == []

    # min_score: a floor above max cosine similarity admits nothing; combined with
    # a source_filter it still excludes weak matches within that scope.
    assert retrieve(org, "alpha", k=10, source_filter=["confluence"], min_score=1.01) == []
    assert retrieve(org, "alpha", k=10, source_filter=["confluence"], min_score=-1.0)


# ===========================================================================
# AC5 — chunk_id + retrieval_result_id populated, with NO schema change
# ===========================================================================


def test_ac5_evidence_pointer_fields_populated_no_schema_change(org):
    ingest_content(org, [dict(source_system="document", source_artifact="d/1",
                              content="alpha content", content_type="prose")])
    embedder.embed_pending_for_org(org)

    hits = retrieve(org, "alpha", k=5)
    assert hits
    hit = hits[0]
    assert hit.chunk_id and hit.retrieval_result_id
    ptr = hit.to_evidence_pointer()
    assert ptr.is_valid() and ptr.origin == OBSERVED
    assert ptr.chunk_id == hit.chunk_id
    assert ptr.retrieval_result_id == hit.retrieval_result_id

    # "No schema change" (AC5): retrieval_result_id is minted per query hit, never
    # a stored column. The live table has chunk_id but NOT retrieval_result_id.
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'retrieval_chunks'"
        )
        columns = {r[0] for r in cur.fetchall()}
    finally:
        con.close()
    assert "chunk_id" in columns
    assert "retrieval_result_id" not in columns


# ===========================================================================
# AC6 — retrieved evidence enters context ONLY via assemble_context()
# ===========================================================================

_OPP = {"title": "Approval bottleneck",
        "description": "Alpha approvals wait on manual review for days."}
_EMPTY_GRAPH = {"entities": [], "relationships": []}


def test_ac6_evidence_enters_only_via_assemble_context(org):
    """The load-bearing property: retrieved content reaches a ContextPackage ONLY
    through assemble_context. Without the retrieval evidence source the package
    carries no retrieved evidence even though the content is indexed and embedded;
    with it, the assembler's cap and selection log govern what is admitted."""
    ingest_content(org, [
        dict(source_system="confluence", source_artifact=f"page/{i}",
             content=f"Alpha operations review paragraph {i}: manual review delays.",
             content_type="prose") for i in range(6)
    ])
    embedder.embed_pending_for_org(org)

    # No evidence_source → retrieved content does NOT leak into context.
    without = assemble_context(_OPP, _EMPTY_GRAPH, AssemblyPolicy(max_evidence_chunks=3))
    assert without.evidence == []

    # With the retrieval source → evidence appears, bounded by the cap, and every
    # exclusion is on the selection log (the assembler decides, not retrieval).
    policy = AssemblyPolicy(max_evidence_chunks=3)
    with_src = assemble_context(
        _OPP, _EMPTY_GRAPH, policy, evidence_source=retrieval_evidence_source(org)
    )
    assert 0 < len(with_src.evidence) <= 3
    evidence_log = [e for e in with_src.selection_log if e["kind"] == "evidence"]
    included = [e for e in evidence_log if e["decision"] == "included"]
    assert len(included) == len(with_src.evidence)
    assert len(evidence_log) > len(with_src.evidence)  # exclusions recorded
    # Selected evidence is pointer-complete (AC5 through the assembler).
    for ev in with_src.evidence:
        assert ev["chunk_id"] and ev["retrieval_result_id"]
        assert ev["evidence_pointer"]["origin"] == "observed"


def test_ac6_confidence_floor_excludes_on_the_record(org):
    """An unreachable confidence floor excludes every proposed chunk — and each
    exclusion is logged by the ASSEMBLER, not silently dropped at retrieval."""
    ingest_content(org, [dict(source_system="confluence", source_artifact="p/1",
                              content="alpha review", content_type="prose")])
    embedder.embed_pending_for_org(org)

    policy = AssemblyPolicy(max_evidence_chunks=5, confidence_floor=2.0)
    package = assemble_context(
        _OPP, _EMPTY_GRAPH, policy, evidence_source=retrieval_evidence_source(org)
    )
    assert package.evidence == []
    assert [e for e in package.selection_log
            if e["kind"] == "evidence" and e["reason"] == REASON_BELOW_FLOOR]


# ===========================================================================
# AC7 — embedding lag never blocks; un-embedded content is absent, not an error
# ===========================================================================


def test_ac7_embedding_lag_is_absence_not_failure(org, monkeypatch):
    # Ingest with a FAILING embedding provider: the handover must still succeed.
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _ACC_FAIL.name)
    result = ingest_content(org, [dict(source_system="document", source_artifact="d/1",
                                       content="alpha content", content_type="prose")])
    assert result.artifacts_indexed == 1
    assert store.count_chunks(org) == result.chunks_indexed          # indexed
    assert store.count_chunks(org, embedded_only=True) == 0          # but not embedded

    # A pass with the failing provider must not raise, and content stays pending.
    run = embedder.embed_pending_for_org(org)   # no raise
    assert run.embedded == 0
    assert retrieve(org, "alpha", k=5) == []     # absent from retrieval — a lag

    # Once a working provider is active, the same content embeds and appears.
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _ACC_A.name)
    embedder.embed_pending_for_org(org)
    assert retrieve(org, "alpha", k=5)


# ===========================================================================
# AC8 — vectors from different models are never compared
# ===========================================================================


def test_ac8_vectors_never_compared_across_models(org, monkeypatch):
    # Chunk A embedded under model A.
    ingest_content(org, [dict(source_system="document", source_artifact="a/1",
                              content="alpha content", content_type="prose")])
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _ACC_A.name)
    embedder.embed_pending_for_org(org)

    # Chunk B embedded under model B.
    ingest_content(org, [dict(source_system="document", source_artifact="b/1",
                              content="alpha content", content_type="prose")])
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _ACC_B.name)
    embedder.embed_pending_for_org(org)

    assert store.count_chunks(org, embedded_only=True) == 2

    # A query under model B only ever compares against model-B vectors …
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _ACC_B.name)
    assert {h.source_artifact for h in retrieve(org, "alpha", k=10)} == {"b/1"}
    # … and under model A, only model-A vectors.
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _ACC_A.name)
    assert {h.source_artifact for h in retrieve(org, "alpha", k=10)} == {"a/1"}
