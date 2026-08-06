"""2.0-B1 T2 — Assembly decision transparency tests.

Covers this subtask's acceptance criterion:
  AC3 — which retrieval candidates were proposed vs. which were actually
        used in the finding are both shown in the trace. "Retrieval
        proposes, assembly decides" — both sides of that decision must be
        visible.

Two layers:
  * app.retrieval_trace — pure/injectable unit tests (no DB, no real
    retrieval store): assemble_evidence_candidates_for_opportunity() with an
    injected evidence_source_factory, and store/get round-tripping via a
    monkeypatched db module.
  * app.trace_graph — the retrieval-candidate section of the trace
    (RetrievalCandidateTrace / FindingTrace.retrieval_candidates), built from
    stored candidate records the same shape retrieval_trace.py produces.

DB-free throughout.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _fake_evidence_source_factory(candidates: List[Dict[str, Any]]):
    """Build an evidence_source_factory that proposes a fixed candidate set,
    regardless of org/opportunity — the shape context_assembly.py expects:
    (opportunity, policy) -> list[dict], each dict carrying at minimum
    chunk_id/origin/confidence/source_system/source_artifact/content/is_stale.
    """
    def factory(org_id: str):
        def source(opportunity, policy=None):
            return list(candidates)
        return source
    return factory


def _candidate(
    chunk_id: str, *, confidence: float = 0.9, origin: str = "observed",
    source_system: str = "confluence", source_artifact: str = "page-1",
    content: str = "some retrieved content", is_stale: bool = False,
) -> Dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "origin": origin,
        "confidence": confidence,
        "source_system": source_system,
        "source_artifact": source_artifact,
        "content": content,
        "is_stale": is_stale,
        "similarity": confidence,
    }


# ─────────────────────────────────────────────────────────────────────────────
# app.retrieval_trace.assemble_evidence_candidates_for_opportunity
# ─────────────────────────────────────────────────────────────────────────────

def test_assemble_evidence_candidates_records_both_used_and_unused():
    """AC3: retrieval proposes N candidates; assembly's confidence floor/cap
    excludes some — both used and unused must be recorded, with reasons."""
    from app.context_assembly import AssemblyPolicy
    from app.retrieval_trace import assemble_evidence_candidates_for_opportunity

    candidates = [_candidate(f"chunk_{i}", confidence=1.0 - (i * 0.05)) for i in range(15)]
    factory = _fake_evidence_source_factory(candidates)

    opp = {"id": "opp_001", "title": "Repetitive manual approval routing"}
    records = assemble_evidence_candidates_for_opportunity(
        "org_a", opp, evidence_source_factory=factory,
    )

    assert len(records) == 15  # every proposed candidate gets exactly one record
    used = [r for r in records if r["used"]]
    unused = [r for r in records if not r["used"]]
    # AssemblyPolicy.max_evidence_chunks default caps included evidence.
    assert 0 < len(used) <= AssemblyPolicy().max_evidence_chunks
    assert len(unused) > 0
    for r in used:
        assert r["decision"] == "included"
        assert r["reason"].startswith("included@position_")
    for r in unused:
        assert r["decision"] == "excluded"
        assert r["reason"]  # e.g. "ranked_out"


def test_assemble_evidence_candidates_enriches_with_source_fields():
    """Both used and unused records carry source_system/source_artifact/content
    snippet — the selection_log alone doesn't carry these, so this proves the
    capturing wrapper actually merges them back in."""
    from app.retrieval_trace import assemble_evidence_candidates_for_opportunity

    candidates = [
        _candidate("chunk_used", confidence=0.95, source_system="slack", source_artifact="thread-9"),
        _candidate("chunk_unused", confidence=0.01, source_system="git", source_artifact="README.md"),
    ]
    factory = _fake_evidence_source_factory(candidates)
    opp = {"id": "opp_002", "title": "Escalation handoff friction"}

    records = assemble_evidence_candidates_for_opportunity(
        "org_a", opp, evidence_source_factory=factory,
    )
    by_id = {r["chunk_id"]: r for r in records}

    assert by_id["chunk_used"]["used"] is True
    assert by_id["chunk_used"]["source_system"] == "slack"
    assert by_id["chunk_used"]["source_artifact"] == "thread-9"
    assert by_id["chunk_used"]["content_snippet"] == "some retrieved content"

    # Below the (default 0.0) confidence floor it would be excluded for a
    # different reason, but here it's simply ranked out by the cap — either
    # way its source fields must still be recoverable.
    assert by_id["chunk_unused"]["source_system"] == "git"
    assert by_id["chunk_unused"]["source_artifact"] == "README.md"


def test_assemble_evidence_candidates_truncates_long_content_to_snippet():
    from app.retrieval_trace import _MAX_CONTENT_SNIPPET_CHARS, assemble_evidence_candidates_for_opportunity

    long_content = "x" * 1000
    candidates = [_candidate("chunk_long", content=long_content)]
    factory = _fake_evidence_source_factory(candidates)
    opp = {"id": "opp_003", "title": "Queue ageing pattern"}

    records = assemble_evidence_candidates_for_opportunity(
        "org_a", opp, evidence_source_factory=factory,
    )
    snippet = records[0]["content_snippet"]
    assert len(snippet) <= _MAX_CONTENT_SNIPPET_CHARS + 3  # + "..."
    assert snippet.endswith("...")


def test_assemble_evidence_candidates_no_org_returns_empty():
    from app.retrieval_trace import assemble_evidence_candidates_for_opportunity

    factory = _fake_evidence_source_factory([_candidate("chunk_1")])
    assert assemble_evidence_candidates_for_opportunity(
        None, {"id": "opp_x", "title": "t"}, evidence_source_factory=factory,
    ) == []
    assert assemble_evidence_candidates_for_opportunity(
        "", {"id": "opp_x", "title": "t"}, evidence_source_factory=factory,
    ) == []


def test_assemble_evidence_candidates_no_query_text_returns_empty():
    """No title/aiRationale/description on the opportunity => nothing to
    search for => the source is never even invoked (mirrors evidence_source.py's
    own 'no query text => propose nothing' rule, checked one layer up here)."""
    from app.retrieval_trace import assemble_evidence_candidates_for_opportunity

    calls = []

    def factory(org_id):
        def source(opportunity, policy=None):
            calls.append(opportunity)
            return [_candidate("chunk_1")]
        return source

    records = assemble_evidence_candidates_for_opportunity(
        "org_a", {"id": "opp_x"}, evidence_source_factory=factory,
    )
    assert records == []
    assert calls == []  # the source was never called — nothing to query for


def test_assemble_evidence_candidates_never_raises_on_source_failure():
    from app.retrieval_trace import assemble_evidence_candidates_for_opportunity

    def factory(org_id):
        def source(opportunity, policy=None):
            raise RuntimeError("retrieval store unavailable")
        return source

    records = assemble_evidence_candidates_for_opportunity(
        "org_a", {"id": "opp_x", "title": "t"}, evidence_source_factory=factory,
    )
    assert records == []


def test_assemble_evidence_candidates_no_candidates_proposed():
    from app.retrieval_trace import assemble_evidence_candidates_for_opportunity

    factory = _fake_evidence_source_factory([])
    records = assemble_evidence_candidates_for_opportunity(
        "org_a", {"id": "opp_x", "title": "t"}, evidence_source_factory=factory,
    )
    assert records == []


# ─────────────────────────────────────────────────────────────────────────────
# store_retrieval_candidates / get_retrieval_candidates_for_opportunity
# ─────────────────────────────────────────────────────────────────────────────

def test_store_and_get_retrieval_candidates_roundtrip(monkeypatch):
    from app import retrieval_trace as rt

    store: Dict[str, Any] = {}
    monkeypatch.setattr(rt.db, "run_kv_set", lambda key, run_id, value: store.__setitem__(f"{key}:{run_id}", value))
    monkeypatch.setattr(rt.db, "run_kv_get", lambda key, run_id, default=None: store.get(f"{key}:{run_id}", default))

    records = [
        {"chunk_id": "c1", "used": True, "decision": "included", "reason": "included@position_1",
         "confidence": 0.9, "origin": "observed", "source_system": "slack",
         "source_artifact": "thread-1", "content_snippet": "hi", "is_stale": False},
    ]
    rt.store_retrieval_candidates("run_rt", "opp_001", records)

    got = rt.get_retrieval_candidates_for_opportunity("run_rt", "opp_001")
    assert got == records

    # Unknown opportunity → empty, never raises.
    assert rt.get_retrieval_candidates_for_opportunity("run_rt", "opp_999") == []
    # Unknown run → empty, never raises.
    assert rt.get_retrieval_candidates_for_opportunity("run_unknown", "opp_001") == []


def test_retrieval_candidates_isolated_by_run(monkeypatch):
    from app import retrieval_trace as rt

    store: Dict[str, Any] = {}
    monkeypatch.setattr(rt.db, "run_kv_set", lambda key, run_id, value: store.__setitem__(f"{key}:{run_id}", value))
    monkeypatch.setattr(rt.db, "run_kv_get", lambda key, run_id, default=None: store.get(f"{key}:{run_id}", default))

    rt.store_retrieval_candidates("run_a", "opp_001", [{"chunk_id": "c1", "used": True}])
    assert len(rt.get_retrieval_candidates_for_opportunity("run_a", "opp_001")) == 1
    assert rt.get_retrieval_candidates_for_opportunity("run_b", "opp_001") == []


def test_record_retrieval_candidates_for_opportunity_assembles_and_persists(monkeypatch):
    from app import retrieval_trace as rt

    store: Dict[str, Any] = {}
    monkeypatch.setattr(rt.db, "run_kv_set", lambda key, run_id, value: store.__setitem__(f"{key}:{run_id}", value))
    monkeypatch.setattr(rt.db, "run_kv_get", lambda key, run_id, default=None: store.get(f"{key}:{run_id}", default))

    factory = _fake_evidence_source_factory([_candidate("chunk_1")])
    opp = {"id": "opp_004", "title": "Reassignment ping-pong"}
    records = rt.record_retrieval_candidates_for_opportunity(
        "org_a", "run_004", opp, evidence_source_factory=factory,
    )
    assert len(records) == 1
    stored = rt.get_retrieval_candidates_for_opportunity("run_004", "opp_004")
    assert stored == records


def test_record_retrieval_candidates_no_candidates_stores_nothing(monkeypatch):
    """A run whose retrieval store is empty must not write a spurious empty
    index entry — get_retrieval_candidates_for_opportunity still degrades to []
    either way, but this pins the storage call is skipped when there's nothing
    to store."""
    from app import retrieval_trace as rt

    calls = []
    monkeypatch.setattr(rt.db, "run_kv_set", lambda key, run_id, value: calls.append((key, run_id, value)))
    monkeypatch.setattr(rt.db, "run_kv_get", lambda key, run_id, default=None: default)

    factory = _fake_evidence_source_factory([])
    opp = {"id": "opp_005", "title": "Queue ageing"}
    records = rt.record_retrieval_candidates_for_opportunity(
        "org_a", "run_005", opp, evidence_source_factory=factory,
    )
    assert records == []
    assert calls == []


def test_record_retrieval_candidates_never_raises_without_opp_id():
    from app.retrieval_trace import record_retrieval_candidates_for_opportunity

    assert record_retrieval_candidates_for_opportunity("org_a", "run_x", {}) == []
    assert record_retrieval_candidates_for_opportunity("org_a", "run_x", {"id": ""}) == []


# ─────────────────────────────────────────────────────────────────────────────
# app.trace_graph — retrieval_candidates surfaced on the FindingTrace (AC3)
# ─────────────────────────────────────────────────────────────────────────────

def _stored_candidate(chunk_id: str, *, used: bool, reason: str, **extra) -> Dict[str, Any]:
    record = {
        "chunk_id": chunk_id,
        "used": used,
        "decision": "included" if used else "excluded",
        "reason": reason,
        "confidence": 0.9 if used else 0.1,
        "origin": "observed",
        "source_system": "confluence",
        "source_artifact": f"page-{chunk_id}",
        "content_snippet": "some content",
        "is_stale": False,
    }
    record.update(extra)
    return record


def test_build_finding_trace_surfaces_used_and_unused_retrieval_candidates():
    """AC3: both used and unused retrieval candidates appear in the trace,
    each carrying its decision and reason."""
    from app.trace_graph import build_finding_trace

    opp = {"id": "opp_001", "title": "x", "evidenceIds": []}
    candidates = [
        _stored_candidate("c1", used=True, reason="included@position_1"),
        _stored_candidate("c2", used=False, reason="ranked_out"),
        _stored_candidate("c3", used=False, reason="below_confidence_floor"),
    ]
    trace = build_finding_trace(opp, "run_001", retrieval_candidates=candidates)

    assert len(trace.retrieval_candidates) == 3
    used = [c for c in trace.retrieval_candidates if c.used]
    unused = [c for c in trace.retrieval_candidates if not c.used]
    assert len(used) == 1 and used[0].chunk_id == "c1"
    assert len(unused) == 2
    reasons = {c.chunk_id: c.reason for c in trace.retrieval_candidates}
    assert reasons["c2"] == "ranked_out"
    assert reasons["c3"] == "below_confidence_floor"

    d = trace.to_dict()
    assert d["retrieval_candidates_used_count"] == 1
    assert d["retrieval_candidates_unused_count"] == 2
    assert len(d["retrieval_candidates"]) == 3
    for entry in d["retrieval_candidates"]:
        for key in ("chunk_id", "used", "decision", "reason", "source_system", "source_artifact"):
            assert key in entry


def test_build_finding_trace_no_retrieval_candidates_yields_empty_list():
    from app.trace_graph import build_finding_trace

    opp = {"id": "opp_002", "evidenceIds": []}
    trace = build_finding_trace(opp, "run_002")
    assert trace.retrieval_candidates == []
    d = trace.to_dict()
    assert d["retrieval_candidates"] == []
    assert d["retrieval_candidates_used_count"] == 0
    assert d["retrieval_candidates_unused_count"] == 0


def test_build_finding_trace_skips_malformed_retrieval_candidates():
    from app.trace_graph import build_finding_trace

    opp = {"id": "opp_003", "evidenceIds": []}
    trace = build_finding_trace(
        opp, "run_003",
        retrieval_candidates=[
            "not-a-dict",
            {"reason": "no chunk_id here"},
            _stored_candidate("valid", used=True, reason="included@position_1"),
        ],
    )
    assert len(trace.retrieval_candidates) == 1
    assert trace.retrieval_candidates[0].chunk_id == "valid"


def test_load_finding_trace_reads_stored_retrieval_candidates(monkeypatch):
    """End-to-end (monkeypatched DB): load_finding_trace() pulls the persisted
    retrieval-candidate record for this (run, opp) and surfaces it."""
    from app import trace_graph as tg

    opps = [{"id": "opp_001", "evidenceIds": []}]
    monkeypatch.setattr(tg.db, "run_kv_get", lambda key, run_id, default=None: (
        opps if key == "opps" else (default if default is not None else [])
    ))
    monkeypatch.setattr(tg.db, "get_run", lambda run_id: {"id": run_id})

    stored_candidates = [_stored_candidate("c1", used=True, reason="included@position_1")]
    monkeypatch.setattr(
        "app.retrieval_trace.get_retrieval_candidates_for_opportunity",
        lambda run_id, opp_id: stored_candidates,
    )

    trace = tg.load_finding_trace("run_x", "opp_001")
    assert trace is not None
    assert len(trace.retrieval_candidates) == 1
    assert trace.retrieval_candidates[0].chunk_id == "c1"


def test_load_finding_trace_degrades_when_retrieval_candidate_load_fails(monkeypatch):
    """A retrieval-candidate lookup failure must not break the rest of the
    trace — hops/joins still build, retrieval_candidates just comes back empty."""
    from app import trace_graph as tg

    opps = [{"id": "opp_001", "evidenceIds": []}]
    monkeypatch.setattr(tg.db, "run_kv_get", lambda key, run_id, default=None: (
        opps if key == "opps" else (default if default is not None else [])
    ))
    monkeypatch.setattr(tg.db, "get_run", lambda run_id: {"id": run_id})
    monkeypatch.setattr(
        "app.retrieval_trace.get_retrieval_candidates_for_opportunity",
        lambda run_id, opp_id: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    trace = tg.load_finding_trace("run_x", "opp_001")
    assert trace is not None
    assert trace.retrieval_candidates == []
    assert len(trace.hops) == 1  # finding root still built


# ─────────────────────────────────────────────────────────────────────────────
# app.graph_context.record_opportunity_retrieval_candidates — the sanctioned
# retrieval bridge llm_enrichment.py calls (never imports retrieval directly)
# ─────────────────────────────────────────────────────────────────────────────

def test_record_opportunity_retrieval_candidates_delegates_to_retrieval_trace(monkeypatch):
    from app import graph_context as gc

    calls = []

    def fake_record(org_id, run_id, opportunity, **kwargs):
        calls.append((org_id, run_id, opportunity.get("id")))
        return [{"chunk_id": "c1", "used": True}]

    monkeypatch.setattr(
        "app.retrieval_trace.record_retrieval_candidates_for_opportunity", fake_record
    )
    result = gc.record_opportunity_retrieval_candidates("org_a", "run_a", {"id": "opp_001"})
    assert result == [{"chunk_id": "c1", "used": True}]
    assert calls == [("org_a", "run_a", "opp_001")]


def test_record_opportunity_retrieval_candidates_never_raises_on_failure(monkeypatch):
    from app import graph_context as gc

    def fake_record(org_id, run_id, opportunity, **kwargs):
        raise RuntimeError("retrieval store unavailable")

    monkeypatch.setattr(
        "app.retrieval_trace.record_retrieval_candidates_for_opportunity", fake_record
    )
    assert gc.record_opportunity_retrieval_candidates("org_a", "run_a", {"id": "opp_001"}) == []


def test_llm_enrichment_never_imports_anything_retrieval_named():
    """Pins the same structural guard test_retrieval_evidence_source.py owns,
    scoped to this ticket's own change: llm_enrichment.py must call the
    retrieval-candidate bridge through graph_context.py, never import
    app.retrieval_trace (or anything else retrieval-named) itself."""
    import ast
    import pathlib

    from app import llm_enrichment

    imported = set()
    tree = ast.parse(pathlib.Path(llm_enrichment.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("retrieval" in name for name in imported), imported
