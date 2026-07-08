"""R16-B2 (T5/AC8) — enrichment context selection flows through one service.

These tests pin the wiring task: the enrichment grounding context produced by
``app.graph_context.build_graph_context`` (what ``app.llm_enrichment`` consumes)
no longer selects its own context. The final selection policy — budget/caps,
deterministic ranking, observed-first, and the selection log — lives in the
context assembly service, and the graph-context bridge delegates to it.

  AC8 — all context fed to downstream enrichment passes through this service; no
        enrichment path selects its own context independently. Verified by:
          * build_graph_context() actually calls assemble_context() (spy), and
          * the assembly policy governs the result (confidence-based retention
            that the old alphabetical-cap selection would not produce).

The existing ENT-3 contracts (15-cap, sparse < 3, truncation, resolved-only
names, input-order-independent determinism) are re-pinned here so the wiring is
shown to preserve behaviour while moving the policy. Pure (no DB): entities and
relationships are passed directly.
"""
from __future__ import annotations

import pytest

from app import graph_context as gc_mod
from app.graph_context import MAX_GRAPH_ENTITIES, build_graph_context


def _entity(name, status="resolved", conf=0.9, etype="person", source="jira", eid=None):
    return {
        "entity_id": eid or f"ent_{name.lower().replace(' ', '_')}",
        "entity_type": etype,
        "display_name": name,
        "source_system": source,
        "resolution_confidence": conf,
        "resolution_status": status,
        "run_count": 5,
    }


def _rel(frm, to, rtype="owns", inferred=False, confidence=None):
    if confidence is None:
        confidence = 0.6 if inferred else 0.9
    return {
        "from_entity_name": frm,
        "to_entity_name": to,
        "relationship_type": rtype,
        "inferred": inferred,
        "confidence": confidence,
    }


# ════════════════════════════════════════════════════════════════════════════
# AC8 — the enrichment grounding context is selected BY the assembly service
# ════════════════════════════════════════════════════════════════════════════

class TestAC8RoutesThroughAssembly:
    def test_build_graph_context_calls_assemble_context(self, monkeypatch):
        """The bridge delegates selection to assemble_context() — it does not do
        its own sort/cap. We spy on the service to prove the call happens with the
        run's entities and relationships."""
        calls = {}
        real = gc_mod.assemble_context

        def spy(opportunity, graph, policy=None, evidence_source=None):
            calls["graph"] = graph
            calls["evidence_source"] = evidence_source
            return real(opportunity, graph, policy, evidence_source)

        monkeypatch.setattr(gc_mod, "assemble_context", spy)

        ents = [_entity(f"P{i}") for i in range(4)]
        rels = [_rel("A", "B")]
        build_graph_context("org", "run", entities=ents, relationships=rels)

        assert calls, "build_graph_context must call assemble_context()"
        assert calls["graph"]["entities"] == ents
        assert calls["graph"]["relationships"] == rels
        # R18-B1 T6: the hook the 1.6 spine left None-capable now carries the
        # retrieval-backed evidence SOURCE (a callable — proposals only; the
        # assembler still decides). Never pre-fetched chunks.
        assert callable(calls["evidence_source"])

    def test_assembly_policy_governs_selection_under_truncation(self):
        """Proof the policy moved: when the graph is truncated, the entity kept is
        the highest-CONFIDENCE one — even if it sorts last by name/id. The old
        in-module selection (alphabetical, first 15) would have dropped it."""
        ents = [_entity(f"P{i:02d}", conf=0.90) for i in range(15)]
        ents.append(_entity("Zzz High", conf=0.99, eid="zzz_high"))  # last by name & id

        gc = build_graph_context("org", "run", entities=ents, relationships=[])

        assert gc.entity_count == 16 and gc.entity_count_shown == 15
        assert "zzz high" in gc.resolved_names, "highest-confidence entity must survive the cap"
        first_bullet = next(l for l in gc.observed_summary.splitlines() if l.startswith("- "))
        assert "Zzz High" in first_bullet, "highest-confidence entity ranks first"

    def test_observed_relationships_rank_before_inferred(self):
        """observed-first, the assembly policy, now governs the edges too."""
        rels = [
            _rel("X", "Y", inferred=True),    # inferred listed first in the input
            _rel("A", "B", inferred=False),   # observed
        ]
        ents = [_entity(f"P{i}") for i in range(3)]
        gc = build_graph_context("org", "run", entities=ents, relationships=rels)

        rel_lines = [l for l in gc.observed_summary.splitlines() if l.startswith("- ")]
        rel_lines = [l for l in rel_lines if "owns" in l]
        # observed edge (no [inferred] tag) appears before the inferred one.
        assert "[inferred]" not in rel_lines[0]
        assert "[inferred]" in rel_lines[1]


# ════════════════════════════════════════════════════════════════════════════
# Behaviour preserved — the wiring moves the policy without changing the contract
# ════════════════════════════════════════════════════════════════════════════

class TestBehaviourPreserved:
    def test_sparse_below_threshold(self):
        gc = build_graph_context("org", "run", entities=[_entity("Jane")], relationships=[])
        assert gc.is_sparse is True and gc.entity_count == 1

    def test_not_sparse_at_threshold(self):
        ents = [_entity(f"P{i}") for i in range(3)]
        gc = build_graph_context("org", "run", entities=ents, relationships=[])
        assert gc.is_sparse is False

    def test_truncates_at_15_with_counts(self):
        ents = [_entity(f"P{i:02d}") for i in range(20)]
        gc = build_graph_context("org", "run", entities=ents, relationships=[])
        assert gc.truncated is True
        assert gc.entity_count == 20
        assert gc.entity_count_shown == MAX_GRAPH_ENTITIES == 15
        assert gc.truncation_note

    def test_not_truncated_under_cap(self):
        ents = [_entity(f"P{i}") for i in range(5)]
        gc = build_graph_context("org", "run", entities=ents, relationships=[])
        assert gc.truncated is False
        assert gc.truncation_note == ""
        assert gc.entity_count_shown == 5

    def test_resolved_names_only_includes_resolved(self):
        ents = [
            _entity("Jane Doe", status="resolved"),
            _entity("Maybe Person", status="ambiguous"),
            _entity("Bob Lee", status="resolved"),
        ]
        gc = build_graph_context("org", "run", entities=ents, relationships=[])
        assert "jane doe" in gc.resolved_names
        assert "bob lee" in gc.resolved_names
        assert "maybe person" not in gc.resolved_names

    def test_build_is_deterministic_regardless_of_input_order(self):
        ents = [_entity(f"Person {i}", eid=str(i)) for i in range(6)]
        a = build_graph_context("org", "run", entities=list(ents), relationships=[])
        b = build_graph_context("org", "run", entities=list(reversed(ents)), relationships=[])
        assert a.observed_summary == b.observed_summary
        assert a.resolved_names == b.resolved_names


# ════════════════════════════════════════════════════════════════════════════
# The selection log is surfaced for auditability
# ════════════════════════════════════════════════════════════════════════════

class TestSelectionLogSurfaced:
    def test_selection_log_has_an_entry_per_candidate(self):
        ents = [_entity(f"P{i:02d}") for i in range(18)]  # > 15 -> some excluded
        gc = build_graph_context("org", "run", entities=ents, relationships=[])
        entity_log = [e for e in gc.selection_log if e["kind"] == "entity"]
        assert len(entity_log) == 18
        assert any(e["decision"] == "included" for e in entity_log)
        assert any(e["decision"] == "excluded" for e in entity_log)

    def test_relationship_cap_applied_and_logged(self):
        rels = [_rel(f"A{i}", f"B{i}") for i in range(25)]  # > 20 -> capped
        ents = [_entity(f"P{i}") for i in range(3)]
        gc = build_graph_context("org", "run", entities=ents, relationships=rels)
        assert gc.relationship_count == 20
        rel_log = [e for e in gc.selection_log if e["kind"] == "relationship"]
        assert len(rel_log) == 25
