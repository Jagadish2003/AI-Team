"""Unit tests for the retrieval-backed context-assembly evidence source (R18-B1 T6).

Covers the T6 acceptance surface (Section 2 / AC6, plus AC5 carry-through):

* the evidence source calls ``retrieve()`` for the relevant opportunity and
  returns chunks in the shape ``assemble_context`` already understands;
* retrieval PROPOSES, the assembler DECIDES: once the chunks enter
  ``assemble_context()`` the deterministic rules apply — confidence floor,
  observed-first ordering, hard caps (``max_evidence_chunks``), and the
  selection log (AC6);
* the source does NOT pre-filter by the policy floor, so below-floor exclusions
  are recorded by the assembler instead of silently vanishing at retrieval;
* selected chunks carry the populated EvidencePointer fields — ``chunk_id`` and
  ``retrieval_result_id`` (AC5);
* the source is advisory: no org, no query text, or a failing ``retrieve()``
  yields no evidence, never an error;
* retrieval never feeds enrichment directly — structural guard on
  ``app.llm_enrichment``, and the graph-context bridge passes the source into
  the hook rather than calling ``retrieve()`` itself.

``retrieve()`` is monkeypatched at the evidence-source module boundary, so these
run in the unit suite with no PostgreSQL/pgvector or model-gateway dependency;
the DB-backed end-to-end coverage lives in the contract suite
(``test_retrieval_evidence_source_contract.py``).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app import graph_context as gc_mod
from app import llm_enrichment
from app.context_assembly import (
    DEFAULT_MAX_EVIDENCE_CHUNKS,
    REASON_BELOW_FLOOR,
    AssemblyPolicy,
    assemble_context,
)
from app.provenance import OBSERVED
from app.retrieval import evidence_source as es_mod
from app.retrieval.api import RetrievedChunk
from app.retrieval.evidence_source import (
    MAX_QUERY_CHARS,
    PROPOSAL_FACTOR,
    opportunity_query_text,
    retrieval_evidence_source,
    retrieved_chunk_to_evidence,
)


def _chunk(cid: str, similarity: float, *, content: str = "chunk text",
           source_system: str = "confluence", source_artifact: str = "page/1",
           ts: str = "2026-07-01T00:00:00+00:00") -> RetrievedChunk:
    return RetrievedChunk(
        content=content,
        similarity=similarity,
        source_system=source_system,
        source_artifact=source_artifact,
        chunk_id=cid,
        retrieval_result_id=f"rr-{cid}",
        source_timestamp=ts,
    )


@pytest.fixture
def spy_retrieve(monkeypatch):
    """Replace retrieve() at the evidence-source boundary; records every call."""

    class _Spy:
        def __init__(self):
            self.calls: list[dict] = []
            self.result: list[RetrievedChunk] = []
            self.raises: Exception | None = None

        def __call__(self, org_id, query_text, k=10, source_filter=None, min_score=None):
            self.calls.append(
                dict(org_id=org_id, query_text=query_text, k=k,
                     source_filter=source_filter, min_score=min_score)
            )
            if self.raises is not None:
                raise self.raises
            return list(self.result)

    spy = _Spy()
    monkeypatch.setattr(es_mod, "retrieve", spy)
    return spy


_OPP = {"title": "Approval bottleneck", "description": "Loan approvals wait on manual review."}


# ---------------------------------------------------------------------------
# Query derivation — deterministic text from the relevant opportunity
# ---------------------------------------------------------------------------


def test_explicit_query_text_wins():
    opp = {"query_text": "  exact query  ", "title": "ignored"}
    assert opportunity_query_text(opp) == "exact query"


def test_title_and_description_combined_in_fixed_order():
    assert opportunity_query_text(_OPP) == (
        "Approval bottleneck\nLoan approvals wait on manual review."
    )


def test_duplicate_field_values_not_repeated():
    opp = {"title": "Same text", "name": "Same text"}
    assert opportunity_query_text(opp) == "Same text"


def test_object_opportunities_supported():
    class Opp:
        title = "From an object"
        description = None

    assert opportunity_query_text(Opp()) == "From an object"


@pytest.mark.parametrize("bare", [None, {}, {"run_id": "run_1"}, {"title": "   "}])
def test_no_usable_text_yields_none(bare):
    assert opportunity_query_text(bare) is None


def test_query_text_bounded():
    opp = {"description": "x" * (MAX_QUERY_CHARS * 3)}
    text = opportunity_query_text(opp)
    assert text is not None and len(text) <= MAX_QUERY_CHARS


# ---------------------------------------------------------------------------
# The source: calls retrieve() correctly, proposes in the assembler's shape
# ---------------------------------------------------------------------------


def test_source_calls_retrieve_with_org_query_and_oversampled_k(spy_retrieve):
    policy = AssemblyPolicy(max_evidence_chunks=4)
    source = retrieval_evidence_source("org_a")

    source(_OPP, policy)

    assert len(spy_retrieve.calls) == 1
    call = spy_retrieve.calls[0]
    assert call["org_id"] == "org_a"
    assert call["query_text"] == opportunity_query_text(_OPP)
    # Propose MORE than the budget so the assembler's floor/ranking/cap decide.
    assert call["k"] == 4 * PROPOSAL_FACTOR


def test_source_defaults_budget_without_policy(spy_retrieve):
    retrieval_evidence_source("org_a")(_OPP)
    assert spy_retrieve.calls[0]["k"] == DEFAULT_MAX_EVIDENCE_CHUNKS * PROPOSAL_FACTOR


def test_explicit_k_and_filters_pass_through(spy_retrieve):
    source = retrieval_evidence_source(
        "org_a", source_filter=["confluence"], min_score=0.4, k=7
    )
    source(_OPP, AssemblyPolicy())
    call = spy_retrieve.calls[0]
    assert call["k"] == 7
    assert call["source_filter"] == ["confluence"]
    assert call["min_score"] == 0.4


def test_policy_floor_is_not_forwarded_as_min_score(spy_retrieve):
    # Deliberate: the assembler applies (and LOGS) the confidence floor. The
    # source must not pre-filter with it, or below-floor exclusions would vanish
    # from the selection log.
    retrieval_evidence_source("org_a")(_OPP, AssemblyPolicy(confidence_floor=0.8))
    assert spy_retrieve.calls[0]["min_score"] is None


def test_chunk_mapping_shape_and_pointer_fields(spy_retrieve):
    spy_retrieve.result = [_chunk("c1", 0.91)]
    out = retrieval_evidence_source("org_a")(_OPP, AssemblyPolicy())

    assert len(out) == 1
    ev = out[0]
    # Keys the assembler's evidence adapter reads directly.
    assert ev["chunk_id"] == "c1"
    assert ev["origin"] == OBSERVED
    assert ev["confidence"] == 0.91
    assert ev["source_timestamp"] == "2026-07-01T00:00:00+00:00"
    # Provenance payload, including the populated EvidencePointer (AC5).
    assert ev["content"] == "chunk text"
    assert ev["source_system"] == "confluence"
    assert ev["retrieval_result_id"] == "rr-c1"
    pointer = ev["evidence_pointer"]
    assert pointer["chunk_id"] == "c1"
    assert pointer["retrieval_result_id"] == "rr-c1"
    assert pointer["origin"] == OBSERVED
    assert pointer["confidence"] == 0.91


def test_mapping_helper_matches_source_output(spy_retrieve):
    chunk = _chunk("c9", 0.5)
    spy_retrieve.result = [chunk]
    out = retrieval_evidence_source("org_a")(_OPP, AssemblyPolicy())
    assert out == [retrieved_chunk_to_evidence(chunk)]


# ---------------------------------------------------------------------------
# Advisory contract — the source never raises, and never queries on nothing
# ---------------------------------------------------------------------------


def test_no_query_text_proposes_nothing_without_calling_retrieve(spy_retrieve):
    assert retrieval_evidence_source("org_a")({"run_id": "run_1"}, AssemblyPolicy()) == []
    assert spy_retrieve.calls == []


def test_blank_org_proposes_nothing(spy_retrieve):
    assert retrieval_evidence_source("")(_OPP, AssemblyPolicy()) == []
    assert spy_retrieve.calls == []


def test_retrieve_failure_degrades_to_no_evidence(spy_retrieve):
    spy_retrieve.raises = RuntimeError("store down")
    assert retrieval_evidence_source("org_a")(_OPP, AssemblyPolicy()) == []


# ---------------------------------------------------------------------------
# AC6 — through assemble_context(): floor, ordering, caps, selection log
# ---------------------------------------------------------------------------

_EMPTY_GRAPH = {"entities": [], "relationships": []}


def test_ac6_hard_cap_applies_to_retrieved_evidence(spy_retrieve):
    spy_retrieve.result = [_chunk(f"c{i}", 0.9 - i * 0.1) for i in range(6)]
    policy = AssemblyPolicy(max_evidence_chunks=3)

    package = assemble_context(
        _OPP, _EMPTY_GRAPH, policy, evidence_source=retrieval_evidence_source("org_a")
    )

    assert [e["chunk_id"] for e in package.evidence] == ["c0", "c1", "c2"]
    log = [entry for entry in package.selection_log if entry["kind"] == "evidence"]
    assert len(log) == 6  # every proposal decided ON THE RECORD
    decisions = {entry["candidate_id"]: entry["decision"] for entry in log}
    assert decisions == {
        "c0": "included", "c1": "included", "c2": "included",
        "c3": "excluded", "c4": "excluded", "c5": "excluded",
    }


def test_ac6_confidence_floor_excludes_weak_chunks_on_the_record(spy_retrieve):
    spy_retrieve.result = [_chunk("strong", 0.9), _chunk("weak", 0.2)]
    policy = AssemblyPolicy(confidence_floor=0.5)

    package = assemble_context(
        _OPP, _EMPTY_GRAPH, policy, evidence_source=retrieval_evidence_source("org_a")
    )

    assert [e["chunk_id"] for e in package.evidence] == ["strong"]
    weak_entries = [
        entry for entry in package.selection_log
        if entry["candidate_id"] == "weak" and entry["kind"] == "evidence"
    ]
    assert len(weak_entries) == 1
    assert weak_entries[0]["decision"] == "excluded"
    assert weak_entries[0]["reason"] == REASON_BELOW_FLOOR


def test_ac6_retrieved_evidence_enters_as_observed(spy_retrieve):
    spy_retrieve.result = [_chunk("c1", 0.7)]
    package = assemble_context(
        _OPP, _EMPTY_GRAPH, AssemblyPolicy(),
        evidence_source=retrieval_evidence_source("org_a"),
    )
    log = [entry for entry in package.selection_log if entry["kind"] == "evidence"]
    assert log and all(entry["origin"] == OBSERVED for entry in log)


def test_same_input_produces_same_assembled_context(spy_retrieve):
    # Semantic retrieval is probabilistic; context SELECTION must be predictable.
    spy_retrieve.result = [_chunk("b", 0.8), _chunk("a", 0.8), _chunk("c", 0.9)]
    policy = AssemblyPolicy(max_evidence_chunks=2)
    source = retrieval_evidence_source("org_a")

    first = assemble_context(_OPP, _EMPTY_GRAPH, policy, evidence_source=source)
    second = assemble_context(_OPP, _EMPTY_GRAPH, policy, evidence_source=source)

    assert first.evidence == second.evidence
    assert first.selection_log == second.selection_log
    # Ranked by confidence, then the stable id tiebreak at equal confidence.
    assert [e["chunk_id"] for e in first.evidence] == ["c", "a"]


def test_ac5_selected_evidence_is_pointer_complete(spy_retrieve):
    spy_retrieve.result = [_chunk("c1", 0.9)]
    package = assemble_context(
        _OPP, _EMPTY_GRAPH, AssemblyPolicy(),
        evidence_source=retrieval_evidence_source("org_a"),
    )
    pointer = package.evidence[0]["evidence_pointer"]
    assert pointer["chunk_id"] == "c1"
    assert pointer["retrieval_result_id"] == "rr-c1"


# ---------------------------------------------------------------------------
# One path in — retrieval never feeds enrichment directly
# ---------------------------------------------------------------------------


def _imported_modules(py_file: str) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(pathlib.Path(py_file).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    return imported


def test_enrichment_never_imports_retrieval_directly():
    # AC6 structural guard: retrieved evidence reaches enrichment ONLY through
    # assemble_context(). The enrichment module must not import the retrieval
    # package (which would open a direct path around the assembler).
    imported = _imported_modules(llm_enrichment.__file__)
    assert not any("retrieval" in name for name in imported), imported


def test_evidence_source_never_imports_enrichment():
    imported = _imported_modules(es_mod.__file__)
    assert not any("llm_enrichment" in name for name in imported), imported


# ---------------------------------------------------------------------------
# The production hook site — build_graph_context passes the source, not results
# ---------------------------------------------------------------------------


def test_build_graph_context_passes_retrieval_source_into_the_hook(monkeypatch):
    seen = {}
    real_assemble = gc_mod.assemble_context

    def spy_assemble(opportunity, graph, policy=None, evidence_source=None):
        seen["evidence_source"] = evidence_source
        return real_assemble(opportunity, graph, policy, evidence_source=None)

    monkeypatch.setattr(gc_mod, "assemble_context", spy_assemble)
    gc_mod.build_graph_context("org_a", "run_1", entities=[], relationships=[])

    # The hook receives the retrieval-backed source (a callable), not chunks —
    # the assembler stays the one deciding what enters context.
    assert callable(seen["evidence_source"])


def test_build_graph_context_without_org_passes_no_source(monkeypatch):
    seen = {}
    real_assemble = gc_mod.assemble_context

    def spy_assemble(opportunity, graph, policy=None, evidence_source=None):
        seen["evidence_source"] = evidence_source
        return real_assemble(opportunity, graph, policy, evidence_source=None)

    monkeypatch.setattr(gc_mod, "assemble_context", spy_assemble)
    gc_mod.build_graph_context(None, "run_1", entities=[], relationships=[])
    assert seen["evidence_source"] is None


def test_build_graph_context_unaffected_by_failing_source(monkeypatch):
    # The run-level opportunity carries no query text, so the source proposes
    # nothing — and even a broken retrieve() must never break the graph context.
    def boom(*args, **kwargs):
        raise RuntimeError("retrieval down")

    monkeypatch.setattr(es_mod, "retrieve", boom)
    ctx = gc_mod.build_graph_context(
        "org_a", "run_1",
        entities=[{
            "entity_id": "e1", "display_name": "Alice", "entity_type": "person",
            "source_system": "jira", "resolution_status": "resolved",
            "resolution_confidence": 0.9,
        }],
        relationships=[],
    )
    assert ctx.entity_count == 1
