"""Contract tests — retrieval evidence source through assemble_context, end to end (R18-B1 T6).

The full bridge over the REAL pgvector-backed store: content ingested via the
T5 producer contract, embedded by the T3 pipeline through the real gateway, then
proposed by the T6 evidence source and DECIDED by the R16-B2 assembler:

* AC6 — retrieved evidence enters opportunity context ONLY via
  ``assemble_context()``: the hard cap (``max_evidence_chunks``), the confidence
  floor, and the selection log all apply to retrieved chunks.
* AC5 — evidence selected into the ``ContextPackage`` carries the populated
  EvidencePointer fields (``chunk_id`` + ``retrieval_result_id``).
* AC3 — the source proposes only the calling org's content; another org
  assembling over the same query gets an evidence-free package.

Embedding is driven through a FAKE provider registered with the real gateway and
selected via ``MODEL_EMBEDDING_PROVIDER``, so the production path
(``get_embedding_provider()`` / ``model_gateway.embed``) is what runs — no direct
provider call (AC2). Provider names are unique to this module.
"""
from __future__ import annotations

from typing import List

import pytest

from app import db
from app.context_assembly import REASON_BELOW_FLOOR, AssemblyPolicy, assemble_context
from app.model_gateway import register_provider
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.retrieval import embedder
from app.retrieval.evidence_source import retrieval_evidence_source
from app.retrieval.ingest import ingest_content


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
# Fake embedding provider registered with the real gateway
# ---------------------------------------------------------------------------


class _T6FakeProvider(ModelProvider):
    emits_own_telemetry = True

    def __init__(self, name: str, identity):
        self.name = name
        self._identity = identity

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        # Deterministic, content-derived vectors so ranking is stable.
        return [[float(len(t) % 5) + 1.0, 0.5, 0.25, 0.125] for t in texts]

    def embedding_identity(self):
        return self._identity


_T6_OK = _T6FakeProvider("t6_embed_ok", ("t6:model-a", "1"))
register_provider(_T6_OK)


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


@pytest.fixture
def org(request, monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _T6_OK.name)
    name = f"ct_t6_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


def _ingest_and_embed(org_id: str, n_pages: int = 5) -> None:
    """Feed the substrate through the T5 producer contract, then embed (T3)."""
    artifacts = [
        dict(
            source_system="confluence",
            source_artifact=f"page/{i}",
            content=f"Operations review paragraph {i}: approvals wait on manual review.",
            content_type="prose",
            source_timestamp="2026-07-07T09:00:00+00:00",
            provenance={"space": "OPS"},
        )
        for i in range(n_pages)
    ]
    result = ingest_content(org_id, artifacts)
    assert result.artifacts_indexed == n_pages
    run = embedder.embed_pending_for_org(org_id)
    assert run.embedded == result.chunks_indexed


_OPP = {
    "title": "Approval bottleneck",
    "description": "Loan approvals wait on manual review for days.",
}
_EMPTY_GRAPH = {"entities": [], "relationships": []}


# ---------------------------------------------------------------------------
# AC6 — floor, cap, and selection log govern real retrieved evidence
# ---------------------------------------------------------------------------


def test_ac6_cap_and_log_apply_to_real_retrieved_evidence(org):
    _ingest_and_embed(org, n_pages=5)
    policy = AssemblyPolicy(max_evidence_chunks=2)

    package = assemble_context(
        _OPP, _EMPTY_GRAPH, policy,
        evidence_source=retrieval_evidence_source(org),
    )

    # Hard cap holds even though more chunks were proposed.
    assert 0 < len(package.evidence) <= 2
    evidence_log = [e for e in package.selection_log if e["kind"] == "evidence"]
    assert len(evidence_log) > len(package.evidence)  # exclusions are on the record
    included = [e for e in evidence_log if e["decision"] == "included"]
    assert len(included) == len(package.evidence)


def test_ac6_confidence_floor_excludes_all_on_the_record(org):
    _ingest_and_embed(org, n_pages=3)
    # A floor no cosine similarity can reach: everything proposed is excluded,
    # and every exclusion is logged by the ASSEMBLER (the source does not
    # pre-filter with the policy floor).
    policy = AssemblyPolicy(max_evidence_chunks=5, confidence_floor=2.0)

    package = assemble_context(
        _OPP, _EMPTY_GRAPH, policy,
        evidence_source=retrieval_evidence_source(org),
    )

    assert package.evidence == []
    floor_exclusions = [
        e for e in package.selection_log
        if e["kind"] == "evidence" and e["reason"] == REASON_BELOW_FLOOR
    ]
    assert floor_exclusions  # proposed, then excluded on the record


# ---------------------------------------------------------------------------
# AC5 — selected evidence is pointer-complete
# ---------------------------------------------------------------------------


def test_ac5_package_evidence_carries_pointer_fields(org):
    _ingest_and_embed(org, n_pages=2)

    package = assemble_context(
        _OPP, _EMPTY_GRAPH, AssemblyPolicy(),
        evidence_source=retrieval_evidence_source(org),
    )

    assert package.evidence
    for ev in package.evidence:
        assert ev["chunk_id"]
        assert ev["retrieval_result_id"]
        pointer = ev["evidence_pointer"]
        assert pointer["chunk_id"] == ev["chunk_id"]
        assert pointer["retrieval_result_id"] == ev["retrieval_result_id"]
        assert pointer["origin"] == "observed"
        assert pointer["source_system"] == "confluence"


# ---------------------------------------------------------------------------
# AC3 — another org assembling the same query gets no evidence
# ---------------------------------------------------------------------------


def test_ac3_other_org_assembles_no_evidence(org, monkeypatch):
    _ingest_and_embed(org, n_pages=2)
    other_org = f"{org}_other"[:60]
    _cleanup(other_org)
    try:
        package = assemble_context(
            _OPP, _EMPTY_GRAPH, AssemblyPolicy(),
            evidence_source=retrieval_evidence_source(other_org),
        )
        assert package.evidence == []
    finally:
        _cleanup(other_org)


# ---------------------------------------------------------------------------
# Selection determinism over real retrieval
# ---------------------------------------------------------------------------


def test_selection_is_reproducible_over_real_retrieval(org):
    _ingest_and_embed(org, n_pages=4)
    policy = AssemblyPolicy(max_evidence_chunks=2)
    source = retrieval_evidence_source(org)

    first = assemble_context(_OPP, _EMPTY_GRAPH, policy, evidence_source=source)
    second = assemble_context(_OPP, _EMPTY_GRAPH, policy, evidence_source=source)

    # The SELECTION is deterministic: same chunks, same order, same decisions.
    # (retrieval_result_id is a per-query hit id by design — T4 — so compare the
    # stable identity, not the whole payload.)
    assert [e["chunk_id"] for e in first.evidence] == [e["chunk_id"] for e in second.evidence]
    strip = lambda log: [  # noqa: E731
        {k: v for k, v in entry.items()} for entry in log if entry["kind"] == "evidence"
    ]
    assert strip(first.selection_log) == strip(second.selection_log)
