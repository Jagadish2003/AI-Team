"""R16-B2 (T7) — Context Assembly: deterministic ordering rules contract tests.

These tests pin the acceptance criteria that belong to the ordering-rules
subtask (document Section 6) directly against ``app/context_assembly.py``:

  AC1 — assemble_context() called twice with identical opportunity, graph and
        policy produces a byte-identical ContextPackage (ordering + log).
  AC2 — context below confidence_floor never appears, regardless of budget left.
  AC3 — observed fills the budget first; an inferred item never displaces an
        observed item that fit.
  AC4 — hard caps are enforced: never more than max_entities / max_relationships.
  AC5 — ranking ties resolve via the stable tiebreaker (candidate id), so equal
        confidence + equal freshness always orders identically across runs.
  AC6 — selection_log records an entry for every candidate with a decision and a
        reason; excluded candidates show why (below floor / budget / ranked out).
  AC7 — assemble_context(evidence_source=None) works with an empty evidence list,
        and the same signature accepts a retrieval source (verified with a stub).

AC8 (every enrichment path flows through this one service) belongs to the
separate wiring task (T5) and is intentionally out of scope here.

The tests are pure (no DB): the rules are total functions of their inputs.
"""
from __future__ import annotations

import pytest

from app.context_assembly import (
    DEFAULT_MAX_EVIDENCE_CHUNKS,
    KIND_ENTITY,
    KIND_EVIDENCE,
    KIND_RELATIONSHIP,
    REASON_BELOW_FLOOR,
    REASON_BUDGET_EXHAUSTED,
    REASON_RANKED_OUT,
    AssemblyPolicy,
    Candidate,
    ContextPackage,
    assemble_context,
    select_candidates,
)
from app.graph_constants import (
    GRAPH_CONTEXT_MAX_ENTITIES,
    GRAPH_CONTEXT_MAX_RELATIONSHIPS,
)
from app.provenance import INFERRED, OBSERVED


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _cand(cid, *, kind=KIND_ENTITY, origin=OBSERVED, confidence=0.9, ts=None, payload=None):
    return Candidate(
        candidate_id=cid,
        kind=kind,
        origin=origin,
        confidence=confidence,
        source_timestamp=ts,
        payload=payload,
    )


def _graph(entities=None, relationships=None):
    """A minimal GraphContext-shaped dict the adapters accept."""
    return {"entities": list(entities or []), "relationships": list(relationships or [])}


def _entity_row(eid, *, confidence=0.9, ts=None):
    row = {"entity_id": eid, "confidence": confidence}
    if ts is not None:
        row["source_timestamp"] = ts
    return row


def _rel_row(rid, *, inferred=False, confidence=None):
    if confidence is None:
        confidence = 0.6 if inferred else 0.9
    return {"relationship_id": rid, "inferred": inferred, "confidence": confidence}


# ════════════════════════════════════════════════════════════════════════════
# AC1 — full determinism: same inputs => byte-identical package
# ════════════════════════════════════════════════════════════════════════════

class TestAC1Determinism:
    def _build_graph(self):
        return _graph(
            entities=[
                _entity_row("e3", confidence=0.7, ts="2026-01-01T00:00:00+00:00"),
                _entity_row("e1", confidence=0.9, ts="2026-03-01T00:00:00+00:00"),
                _entity_row("e2", confidence=0.9, ts="2026-02-01T00:00:00+00:00"),
            ],
            relationships=[
                _rel_row("r2", inferred=True),
                _rel_row("r1", inferred=False),
            ],
        )

    def test_assemble_context_is_byte_identical_across_calls(self):
        opp = {"id": "opp-1"}
        policy = AssemblyPolicy()
        first = assemble_context(opp, self._build_graph(), policy)
        second = assemble_context(opp, self._build_graph(), policy)

        assert [e["entity_id"] for e in first.entities] == [
            e["entity_id"] for e in second.entities
        ]
        assert [r["relationship_id"] for r in first.relationships] == [
            r["relationship_id"] for r in second.relationships
        ]
        # The whole audit log is identical too — determinism is total, not just
        # the visible ordering.
        assert first.selection_log == second.selection_log

    def test_no_wall_clock_dependency(self):
        """Freshness is derived from the inputs, so two calls separated in time
        still rank identically (the newest candidate is always age 0)."""
        opp = {"id": "opp-1"}
        a = assemble_context(opp, self._build_graph())
        b = assemble_context(opp, self._build_graph())
        assert a.selection_log == b.selection_log


# ════════════════════════════════════════════════════════════════════════════
# AC2 — confidence floor: weak context never appears, even with budget to spare
# ════════════════════════════════════════════════════════════════════════════

class TestAC2ConfidenceFloor:
    def test_below_floor_excluded_even_with_budget_remaining(self):
        policy = AssemblyPolicy(confidence_floor=0.5, max_entities=10)
        cands = [_cand("weak", confidence=0.4), _cand("strong", confidence=0.9)]
        selected, log = select_candidates(cands, policy.max_entities, policy)

        ids = [c.candidate_id for c in selected]
        assert ids == ["strong"], "the below-floor candidate must never be selected"
        weak = next(e for e in log if e["candidate_id"] == "weak")
        assert weak["decision"] == "excluded"
        assert weak["reason"] == REASON_BELOW_FLOOR

    def test_confidence_equal_to_floor_is_kept(self):
        """The floor excludes strictly-below; a value equal to the floor stays."""
        policy = AssemblyPolicy(confidence_floor=0.5)
        selected, _ = select_candidates([_cand("edge", confidence=0.5)], 5, policy)
        assert [c.candidate_id for c in selected] == ["edge"]


# ════════════════════════════════════════════════════════════════════════════
# AC3 — observed fills the budget first; inferred never displaces observed
# ════════════════════════════════════════════════════════════════════════════

class TestAC3ObservedFirst:
    def test_inferred_cannot_displace_observed_that_fit(self):
        policy = AssemblyPolicy()  # observed_first=True
        cands = [
            _cand("obs-1", origin=OBSERVED, confidence=0.80),
            _cand("obs-2", origin=OBSERVED, confidence=0.80),
            _cand("inf-hi", origin=INFERRED, confidence=0.99),  # higher confidence!
        ]
        selected, log = select_candidates(cands, cap=2, policy=policy)

        ids = [c.candidate_id for c in selected]
        assert ids == ["obs-1", "obs-2"], "observed fills the budget before inferred"
        inf = next(e for e in log if e["candidate_id"] == "inf-hi")
        assert inf["decision"] == "excluded"
        assert inf["reason"] == REASON_BUDGET_EXHAUSTED

    def test_inferred_only_fills_remaining_space(self):
        policy = AssemblyPolicy()
        cands = [
            _cand("obs-1", origin=OBSERVED, confidence=0.80),
            _cand("inf-1", origin=INFERRED, confidence=0.99),
            _cand("inf-2", origin=INFERRED, confidence=0.95),
        ]
        selected, _ = select_candidates(cands, cap=2, policy=policy)
        ids = [c.candidate_id for c in selected]
        assert ids[0] == "obs-1", "the observed item takes the first slot"
        assert ids[1] == "inf-1", "the highest-ranked inferred item fills the leftover"

    def test_observed_first_off_lets_partitions_compete_on_rank(self):
        policy = AssemblyPolicy(observed_first=False)
        cands = [
            _cand("obs", origin=OBSERVED, confidence=0.80),
            _cand("inf", origin=INFERRED, confidence=0.99),
        ]
        selected, _ = select_candidates(cands, cap=1, policy=policy)
        assert [c.candidate_id for c in selected] == ["inf"]


# ════════════════════════════════════════════════════════════════════════════
# AC4 — hard caps are enforced
# ════════════════════════════════════════════════════════════════════════════

class TestAC4HardCaps:
    def test_entity_cap_enforced(self):
        policy = AssemblyPolicy()
        cands = [_cand(f"e{i:02d}", confidence=0.9) for i in range(30)]
        selected, _ = select_candidates(cands, policy.max_entities, policy)
        assert len(selected) == GRAPH_CONTEXT_MAX_ENTITIES == 15

    def test_relationship_cap_enforced(self):
        policy = AssemblyPolicy()
        cands = [
            _cand(f"r{i:02d}", kind=KIND_RELATIONSHIP, confidence=0.9) for i in range(25)
        ]
        selected, _ = select_candidates(cands, policy.max_relationships, policy)
        assert len(selected) == GRAPH_CONTEXT_MAX_RELATIONSHIPS == 20

    def test_caps_enforced_through_assemble_context(self):
        graph = _graph(
            entities=[_entity_row(f"e{i:02d}") for i in range(40)],
            relationships=[_rel_row(f"r{i:02d}") for i in range(40)],
        )
        pkg = assemble_context({"id": "opp"}, graph, AssemblyPolicy())
        assert len(pkg.entities) <= GRAPH_CONTEXT_MAX_ENTITIES
        assert len(pkg.relationships) <= GRAPH_CONTEXT_MAX_RELATIONSHIPS
        assert len(pkg.evidence) <= DEFAULT_MAX_EVIDENCE_CHUNKS


# ════════════════════════════════════════════════════════════════════════════
# AC5 — ties resolve via the stable tiebreaker (candidate id)
# ════════════════════════════════════════════════════════════════════════════

class TestAC5StableTiebreaker:
    def test_equal_confidence_and_freshness_order_by_id(self):
        # Same confidence, no timestamps (equal freshness): id decides, ASC.
        cands = [_cand("b", confidence=0.9), _cand("a", confidence=0.9), _cand("c", confidence=0.9)]
        selected, _ = select_candidates(cands, cap=10, policy=AssemblyPolicy())
        assert [c.candidate_id for c in selected] == ["a", "b", "c"]

    def test_tiebreaker_is_stable_across_runs(self):
        cands = [_cand("b", confidence=0.9), _cand("a", confidence=0.9)]
        first, _ = select_candidates(cands, cap=10, policy=AssemblyPolicy())
        second, _ = select_candidates(list(reversed(cands)), cap=10, policy=AssemblyPolicy())
        assert [c.candidate_id for c in first] == [c.candidate_id for c in second] == ["a", "b"]

    def test_freshness_breaks_confidence_ties_before_id(self):
        # Equal confidence; fresher (later timestamp) ranks ahead regardless of id.
        cands = [
            _cand("a-old", confidence=0.9, ts="2026-01-01T00:00:00+00:00"),
            _cand("z-new", confidence=0.9, ts="2026-06-01T00:00:00+00:00"),
        ]
        selected, _ = select_candidates(cands, cap=10, policy=AssemblyPolicy())
        assert [c.candidate_id for c in selected] == ["z-new", "a-old"]


# ════════════════════════════════════════════════════════════════════════════
# AC6 — selection_log records every candidate with a decision and reason
# ════════════════════════════════════════════════════════════════════════════

class TestAC6SelectionLog:
    def test_every_candidate_logged_with_decision_and_reason(self):
        policy = AssemblyPolicy(confidence_floor=0.5)
        cands = [
            _cand("below", confidence=0.3),                       # below floor
            _cand("obs-1", origin=OBSERVED, confidence=0.9),      # included
            _cand("obs-2", origin=OBSERVED, confidence=0.9),      # included
            _cand("obs-3", origin=OBSERVED, confidence=0.8),      # ranked out
            _cand("inf-1", origin=INFERRED, confidence=0.95),     # budget exhausted
        ]
        selected, log = select_candidates(cands, cap=2, policy=policy)

        # One entry per candidate, no more, no less.
        assert len(log) == len(cands)
        assert {e["candidate_id"] for e in log} == {c.candidate_id for c in cands}

        by_id = {e["candidate_id"]: e for e in log}
        assert by_id["below"]["reason"] == REASON_BELOW_FLOOR
        assert by_id["inf-1"]["reason"] == REASON_BUDGET_EXHAUSTED
        assert by_id["obs-3"]["reason"] == REASON_RANKED_OUT
        assert by_id["obs-1"]["decision"] == "included"
        assert by_id["obs-1"]["reason"] == "included@position_1"
        assert by_id["obs-2"]["reason"] == "included@position_2"

        # Every excluded entry carries a recognised reason.
        excluded_reasons = {REASON_BELOW_FLOOR, REASON_BUDGET_EXHAUSTED, REASON_RANKED_OUT}
        for entry in log:
            if entry["decision"] == "excluded":
                assert entry["reason"] in excluded_reasons

    def test_log_entry_shape(self):
        _, log = select_candidates([_cand("x", confidence=0.9)], cap=1, policy=AssemblyPolicy())
        entry = log[0]
        assert set(entry) == {
            "candidate_id", "kind", "origin", "decision", "reason",
            "confidence", "freshness_days",
        }
        assert entry["origin"] in (OBSERVED, INFERRED)


# ════════════════════════════════════════════════════════════════════════════
# AC7 — evidence_source is None in 1.6; the same signature accepts a 1.8 source
# ════════════════════════════════════════════════════════════════════════════

class TestAC7EvidenceSourceHook:
    def test_none_source_yields_empty_evidence(self):
        pkg = assemble_context({"id": "opp"}, _graph(), AssemblyPolicy(), evidence_source=None)
        assert pkg.evidence == []
        assert isinstance(pkg, ContextPackage)

    def test_stub_retrieval_source_flows_through_the_same_rules(self):
        """A 1.8-style retrieval source plugs into the unchanged signature and its
        chunks are floored, ranked, capped and logged exactly like graph context."""

        class StubRetrievalSource:
            def retrieve(self, opportunity):
                return [
                    {"chunk_id": "c-weak", "confidence": 0.10, "origin": OBSERVED},
                    {"chunk_id": "c-strong", "confidence": 0.95, "origin": OBSERVED},
                    {"chunk_id": "c-mid", "confidence": 0.80, "origin": OBSERVED},
                ]

        policy = AssemblyPolicy(confidence_floor=0.5, max_evidence_chunks=1)
        pkg = assemble_context(
            {"id": "opp"}, _graph(), policy, evidence_source=StubRetrievalSource()
        )

        # Cap honoured, floor honoured, ranking honoured: only the strongest chunk.
        assert [c["chunk_id"] for c in pkg.evidence] == ["c-strong"]
        ev_log = {e["candidate_id"]: e for e in pkg.selection_log if e["kind"] == KIND_EVIDENCE}
        assert ev_log["c-weak"]["reason"] == REASON_BELOW_FLOOR
        assert ev_log["c-strong"]["decision"] == "included"

    def test_callable_source_is_accepted(self):
        pkg = assemble_context(
            {"id": "opp"},
            _graph(),
            AssemblyPolicy(),
            evidence_source=lambda opp: [{"chunk_id": "c1", "confidence": 0.9}],
        )
        assert [c["chunk_id"] for c in pkg.evidence] == ["c1"]
