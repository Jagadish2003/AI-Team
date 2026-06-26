"""R16-B2 (T7) — Context Assembly Foundation: Section 6 acceptance contract tests.

These contract tests pin the *public contract* of the context assembly service.
Everything is driven end to end through :func:`assemble_context` — the single
entry point downstream enrichment, retrieval (1.8) and future reasoning consume —
so the package the rest of the platform actually receives is what gets asserted.
Together they prove the service is **deterministic, bounded, and safe**, exactly
as Section 6 of the R16-B2 document requires:

  AC1  identical (opportunity, graph, policy) => byte-identical ContextPackage
       ordering, run to run. Fully deterministic.
  AC2  context below ``confidence_floor`` never appears, regardless of remaining
       budget.
  AC3  observed candidates fill the budget first; inferred only fill leftover
       space, and an inferred item never displaces an observed item that fit.
  AC4  hard caps enforced: never more than ``max_entities`` / ``max_relationships``
       / ``max_evidence_chunks`` in the package.
  AC5  ranking ties resolve via the stable tiebreaker (candidate id), so equal
       confidence + freshness always order identically across runs.
  AC6  ``selection_log`` records an entry for every candidate with its decision
       and reason; excluded candidates show why (below floor / budget / ranked
       out); included entries record their position.
  AC7  ``assemble_context(evidence_source=None)`` works with an empty evidence
       list, and the same signature accepts a retrieval source unchanged
       (verified with a stub).

This complements ``tests/contract/test_context_assembly.py`` (which drills into
the ``select_candidates`` rules engine) by exercising the same acceptance criteria
through the assembled public contract. Pure / DB-free: the service is a total,
deterministic function of its inputs.
"""
from __future__ import annotations

from app.context_assembly import (
    DEFAULT_MAX_EVIDENCE_CHUNKS,
    REASON_BELOW_FLOOR,
    REASON_BUDGET_EXHAUSTED,
    REASON_RANKED_OUT,
    AssemblyPolicy,
    ContextPackage,
    assemble_context,
)
from app.graph_constants import (
    GRAPH_CONTEXT_MAX_ENTITIES,
    GRAPH_CONTEXT_MAX_RELATIONSHIPS,
)
from app.provenance import INFERRED, OBSERVED

OPP = {"id": "opp-r16b2-t7"}


# --------------------------------------------------------------------------- #
# builders — the real producer field spellings the service must accept
# --------------------------------------------------------------------------- #

def ent(eid, confidence, origin=OBSERVED, freshness_days=None, source_timestamp=None):
    row = {"entity_id": eid, "confidence": confidence, "origin": origin}
    if freshness_days is not None:
        row["freshness_days"] = freshness_days
    if source_timestamp is not None:
        row["source_timestamp"] = source_timestamp
    return row


def rel(frm, to, rtype, confidence, inferred=False):
    return {
        "from_id": frm, "to_id": to, "relationship_type": rtype,
        "confidence": confidence, "inferred": inferred,
    }


def chunk(cid, confidence, origin=OBSERVED):
    return {"chunk_id": cid, "confidence": confidence, "origin": origin}


def graph(entities=None, relationships=None):
    return {"entities": list(entities or []), "relationships": list(relationships or [])}


def ids(items, key="entity_id"):
    return [i[key] for i in items]


def log_by_id(pkg):
    return {e["candidate_id"]: e for e in pkg.selection_log}


# ════════════════════════════════════════════════════════════════════════════
# AC1 — deterministic output: same inputs => byte-identical package
# ════════════════════════════════════════════════════════════════════════════

class TestAC1Deterministic:
    def test_same_inputs_produce_identical_package(self):
        g = graph(
            entities=[ent("e3", 0.5), ent("e1", 0.9), ent("e2", 0.9)],
            relationships=[rel("a", "b", "x", 0.8), rel("c", "d", "y", 0.6, inferred=True)],
        )
        policy = AssemblyPolicy()
        first = assemble_context(OPP, g, policy)
        second = assemble_context(OPP, g, policy)
        # The whole package is equal: selected payloads, ordering, and the full
        # audit log — determinism is total, not just the visible ordering.
        assert first == second
        assert ids(first.entities) == ids(second.entities)
        assert first.selection_log == second.selection_log

    def test_ordering_is_independent_of_input_order(self):
        items = [ent("e3", 0.5), ent("e1", 0.9), ent("e2", 0.9)]
        policy = AssemblyPolicy()
        forward = assemble_context(OPP, graph(entities=list(items)), policy)
        reverse = assemble_context(OPP, graph(entities=list(reversed(items))), policy)
        assert ids(forward.entities) == ids(reverse.entities)
        assert forward.selection_log == reverse.selection_log

    def test_no_wall_clock_dependency_with_timestamps(self):
        # Freshness is derived from the inputs (newest candidate == age 0), never
        # the wall clock, so two calls separated in time rank identically.
        g = graph(entities=[
            ent("e1", 0.9, source_timestamp="2026-01-01T00:00:00+00:00"),
            ent("e2", 0.9, source_timestamp="2026-03-01T00:00:00+00:00"),
        ])
        a = assemble_context(OPP, g, AssemblyPolicy())
        b = assemble_context(OPP, g, AssemblyPolicy())
        assert a.selection_log == b.selection_log
        assert ids(a.entities) == ids(b.entities)


# ════════════════════════════════════════════════════════════════════════════
# AC2 — confidence floor: weak context never appears, even with budget to spare
# ════════════════════════════════════════════════════════════════════════════

class TestAC2ConfidenceFloor:
    def test_below_floor_excluded_even_with_ample_budget(self):
        g = graph(entities=[ent("hi", 0.9), ent("lo", 0.2)])
        pkg = assemble_context(OPP, g, AssemblyPolicy(confidence_floor=0.5, max_entities=15))
        assert "hi" in ids(pkg.entities)
        assert "lo" not in ids(pkg.entities)  # budget had room; the floor still excludes
        assert log_by_id(pkg)["lo"]["reason"] == REASON_BELOW_FLOOR

    def test_floor_is_a_strict_lower_bound(self):
        # Equal to the floor is kept; strictly below is dropped.
        g = graph(entities=[ent("at_floor", 0.5), ent("below", 0.49)])
        pkg = assemble_context(OPP, g, AssemblyPolicy(confidence_floor=0.5))
        assert "at_floor" in ids(pkg.entities)
        assert "below" not in ids(pkg.entities)

    def test_floor_applies_across_every_kind(self):
        g = graph(
            entities=[ent("e_lo", 0.1)],
            relationships=[rel("a", "b", "x", 0.1)],
        )
        pkg = assemble_context(
            OPP, g,
            AssemblyPolicy(confidence_floor=0.5),
            evidence_source=lambda opp, policy: [chunk("c_lo", 0.1)],
        )
        assert pkg.entities == [] and pkg.relationships == [] and pkg.evidence == []
        by = log_by_id(pkg)
        assert by["e_lo"]["reason"] == REASON_BELOW_FLOOR
        assert by["a->b:x"]["reason"] == REASON_BELOW_FLOOR
        assert by["c_lo"]["reason"] == REASON_BELOW_FLOOR


# ════════════════════════════════════════════════════════════════════════════
# AC3 — observed fills the budget first; inferred never displaces observed
# ════════════════════════════════════════════════════════════════════════════

class TestAC3ObservedFirst:
    def test_observed_fill_budget_before_higher_confidence_inferred(self):
        g = graph(entities=[
            ent("o1", 0.90, OBSERVED), ent("o2", 0.80, OBSERVED),
            ent("i1", 0.95, INFERRED), ent("i2", 0.99, INFERRED),  # higher confidence!
        ])
        pkg = assemble_context(OPP, g, AssemblyPolicy(max_entities=2))
        assert ids(pkg.entities) == ["o1", "o2"]
        by = log_by_id(pkg)
        assert by["i1"]["reason"] == REASON_BUDGET_EXHAUSTED
        assert by["i2"]["reason"] == REASON_BUDGET_EXHAUSTED

    def test_inferred_fills_only_the_remaining_space(self):
        g = graph(entities=[
            ent("o1", 0.9, OBSERVED),
            ent("i1", 0.7, INFERRED), ent("i2", 0.6, INFERRED),
        ])
        pkg = assemble_context(OPP, g, AssemblyPolicy(max_entities=3))
        assert ids(pkg.entities) == ["o1", "i1", "i2"]  # observed first, then inferred by rank

    def test_inferred_never_displaces_an_observed_that_fit(self):
        # A far higher-confidence inferred item must not push out a low observed one.
        g = graph(entities=[ent("obs", 0.3, OBSERVED), ent("inf", 0.99, INFERRED)])
        pkg = assemble_context(OPP, g, AssemblyPolicy(max_entities=1))
        assert ids(pkg.entities) == ["obs"]

    def test_observed_first_holds_for_relationships_too(self):
        g = graph(relationships=[
            rel("x", "y", "owns", 0.99, inferred=True),   # inferred, highest confidence
            rel("a", "b", "uses", 0.50, inferred=False),  # observed, lower confidence
        ])
        pkg = assemble_context(OPP, g, AssemblyPolicy(max_relationships=1))
        assert pkg.relationships[0]["from_id"] == "a"  # observed edge wins the only slot


# ════════════════════════════════════════════════════════════════════════════
# AC4 — hard caps are enforced
# ════════════════════════════════════════════════════════════════════════════

class TestAC4HardCaps:
    def test_explicit_caps_enforced(self):
        g = graph(
            entities=[ent(f"e{i:02d}", 0.5) for i in range(8)],
            relationships=[rel(f"a{i}", f"b{i}", "x", 0.5) for i in range(8)],
        )
        pkg = assemble_context(OPP, g, AssemblyPolicy(max_entities=2, max_relationships=3))
        assert len(pkg.entities) == 2
        assert len(pkg.relationships) == 3

    def test_default_caps_are_15_and_20(self):
        g = graph(
            entities=[ent(f"e{i:02d}", 0.5) for i in range(40)],
            relationships=[rel(f"a{i:02d}", f"b{i:02d}", "x", 0.5) for i in range(40)],
        )
        pkg = assemble_context(OPP, g, AssemblyPolicy())
        assert len(pkg.entities) == GRAPH_CONTEXT_MAX_ENTITIES == 15
        assert len(pkg.relationships) == GRAPH_CONTEXT_MAX_RELATIONSHIPS == 20

    def test_evidence_cap_enforced(self):
        def source(opp, policy):
            return [chunk(f"c{i:02d}", 0.9) for i in range(25)]
        pkg = assemble_context(OPP, graph(), AssemblyPolicy(), evidence_source=source)
        assert len(pkg.evidence) == DEFAULT_MAX_EVIDENCE_CHUNKS == 10

    def test_package_never_exceeds_caps_under_pressure(self):
        g = graph(
            entities=[ent(f"e{i:02d}", 0.9, INFERRED if i % 2 else OBSERVED) for i in range(50)],
            relationships=[rel(f"a{i:02d}", f"b{i:02d}", "t", 0.9, inferred=bool(i % 2)) for i in range(50)],
        )
        pkg = assemble_context(OPP, g, AssemblyPolicy())
        assert len(pkg.entities) <= GRAPH_CONTEXT_MAX_ENTITIES
        assert len(pkg.relationships) <= GRAPH_CONTEXT_MAX_RELATIONSHIPS
        assert len(pkg.evidence) <= DEFAULT_MAX_EVIDENCE_CHUNKS


# ════════════════════════════════════════════════════════════════════════════
# AC5 — ties resolve via the stable tiebreaker (candidate id)
# ════════════════════════════════════════════════════════════════════════════

class TestAC5StableTiebreaker:
    def test_equal_confidence_and_freshness_order_by_id(self):
        items = [ent("e_c", 0.5), ent("e_a", 0.5), ent("e_b", 0.5)]
        forward = assemble_context(OPP, graph(entities=list(items)), AssemblyPolicy())
        reverse = assemble_context(OPP, graph(entities=list(reversed(items))), AssemblyPolicy())
        assert ids(forward.entities) == ["e_a", "e_b", "e_c"]
        assert ids(forward.entities) == ids(reverse.entities)

    def test_freshness_breaks_confidence_ties_before_id(self):
        # Equal confidence; the fresher item ranks ahead regardless of id.
        g = graph(entities=[
            ent("z_new", 0.5, freshness_days=1),
            ent("a_old", 0.5, freshness_days=100),
        ])
        pkg = assemble_context(OPP, g, AssemblyPolicy(max_entities=2))
        assert ids(pkg.entities) == ["z_new", "a_old"]

    def test_freshness_via_source_timestamp_breaks_ties(self):
        g = graph(entities=[
            ent("a_old", 0.9, source_timestamp="2026-01-01T00:00:00+00:00"),
            ent("z_new", 0.9, source_timestamp="2026-06-01T00:00:00+00:00"),
        ])
        pkg = assemble_context(OPP, g, AssemblyPolicy(max_entities=2))
        assert ids(pkg.entities) == ["z_new", "a_old"]  # later timestamp ranks first


# ════════════════════════════════════════════════════════════════════════════
# AC6 — selection_log records every candidate with a decision and a reason
# ════════════════════════════════════════════════════════════════════════════

class TestAC6SelectionLog:
    REQUIRED_FIELDS = {
        "candidate_id", "kind", "origin", "decision", "reason",
        "confidence", "freshness_days",
    }

    def test_every_candidate_across_kinds_logged_exactly_once(self):
        g = graph(
            entities=[ent("e1", 0.9), ent("e2", 0.8)],
            relationships=[rel("a", "b", "x", 0.7)],
        )
        pkg = assemble_context(
            OPP, g, AssemblyPolicy(),
            evidence_source=lambda opp, policy: [chunk("c1", 0.9)],
        )
        logged = [e["candidate_id"] for e in pkg.selection_log]
        assert sorted(logged) == sorted(["e1", "e2", "a->b:x", "c1"])
        assert len(logged) == len(set(logged))  # exactly one entry per candidate

    def test_every_entry_carries_the_documented_schema(self):
        pkg = assemble_context(OPP, graph(entities=[ent("e1", 0.9)]), AssemblyPolicy())
        assert pkg.selection_log
        for entry in pkg.selection_log:
            assert self.REQUIRED_FIELDS <= set(entry)
            assert entry["origin"] in (OBSERVED, INFERRED)
            assert entry["decision"] in ("included", "excluded")

    def test_included_entries_record_their_position(self):
        g = graph(entities=[ent("hi", 0.9), ent("mid", 0.8)])
        pkg = assemble_context(OPP, g, AssemblyPolicy())
        included = [e for e in pkg.selection_log if e["decision"] == "included"]
        assert sorted(e["reason"] for e in included) == [
            "included@position_1", "included@position_2",
        ]

    def test_all_exclusion_reasons_surface_in_one_run(self):
        g = graph(entities=[
            ent("obs_hi", 0.90, OBSERVED),   # included@position_1
            ent("obs_lo", 0.55, OBSERVED),   # ranked_out (cap filled by obs_hi)
            ent("low", 0.10, OBSERVED),      # below_confidence_floor
            ent("inf", 0.95, INFERRED),      # budget_exhausted (observed took budget)
        ])
        pkg = assemble_context(OPP, g, AssemblyPolicy(max_entities=1, confidence_floor=0.4))
        by = log_by_id(pkg)
        assert by["obs_hi"]["decision"] == "included"
        assert by["obs_hi"]["reason"] == "included@position_1"
        assert by["obs_lo"]["reason"] == REASON_RANKED_OUT
        assert by["low"]["reason"] == REASON_BELOW_FLOOR
        assert by["inf"]["reason"] == REASON_BUDGET_EXHAUSTED

    def test_log_records_kind_and_origin_per_candidate(self):
        g = graph(
            entities=[ent("o", 0.9, OBSERVED), ent("i", 0.9, INFERRED)],
            relationships=[rel("a", "b", "x", 0.9)],
        )
        pkg = assemble_context(
            OPP, g, AssemblyPolicy(),
            evidence_source=lambda opp, policy: [chunk("c1", 0.9)],
        )
        by = log_by_id(pkg)
        assert by["o"]["kind"] == "entity" and by["o"]["origin"] == OBSERVED
        assert by["i"]["origin"] == INFERRED
        assert by["a->b:x"]["kind"] == "relationship"
        assert by["c1"]["kind"] == "evidence"


# ════════════════════════════════════════════════════════════════════════════
# AC7 — evidence_source hook: None in 1.6, the same signature accepts a source
# ════════════════════════════════════════════════════════════════════════════

class TestAC7EvidenceSourceHook:
    def test_none_source_yields_empty_evidence_and_a_valid_package(self):
        pkg = assemble_context(OPP, graph(entities=[ent("e1", 0.9)]), AssemblyPolicy(),
                               evidence_source=None)
        assert isinstance(pkg, ContextPackage)
        assert pkg.evidence == []
        assert ids(pkg.entities) == ["e1"]  # the rest of the package still assembles

    def test_default_evidence_source_is_none(self):
        # Calling without the kwarg at all (the 1.6 default) must work identically.
        pkg = assemble_context(OPP, graph(entities=[ent("e1", 0.9)]), AssemblyPolicy())
        assert pkg.evidence == []

    def test_stub_callable_source_flows_through_the_same_rules(self):
        def source(opportunity, policy):
            return [
                chunk("c_obs", 0.9, OBSERVED),
                chunk("c_low", 0.1, OBSERVED),
                chunk("c_inf", 0.95, INFERRED),
            ]
        policy = AssemblyPolicy(max_evidence_chunks=1, confidence_floor=0.5)
        pkg = assemble_context(OPP, graph(), policy, evidence_source=source)
        # floor + observed-first + cap, exactly as for graph context.
        assert ids(pkg.evidence, key="chunk_id") == ["c_obs"]
        by = log_by_id(pkg)
        assert by["c_low"]["reason"] == REASON_BELOW_FLOOR
        assert by["c_inf"]["reason"] == REASON_BUDGET_EXHAUSTED

    def test_retrieval_style_object_source_is_accepted_unchanged(self):
        # A 1.8-style retrieval object plugs into the unchanged signature.
        class StubRetrievalSource:
            def retrieve(self, opportunity):
                return [chunk("c-strong", 0.95), chunk("c-weak", 0.10)]

        policy = AssemblyPolicy(confidence_floor=0.5, max_evidence_chunks=5)
        pkg = assemble_context(OPP, graph(), policy, evidence_source=StubRetrievalSource())
        assert ids(pkg.evidence, key="chunk_id") == ["c-strong"]

    def test_plain_iterable_source_is_accepted(self):
        chunks = [chunk("c1", 0.8)]
        pkg = assemble_context(OPP, graph(), AssemblyPolicy(), evidence_source=chunks)
        assert ids(pkg.evidence, key="chunk_id") == ["c1"]

    def test_failing_source_is_advisory_not_fatal(self):
        def boom(opportunity, policy):
            raise RuntimeError("retrieval down")
        pkg = assemble_context(OPP, graph(entities=[ent("e1", 0.9)]), AssemblyPolicy(),
                               evidence_source=boom)
        assert pkg.evidence == []          # advisory: no evidence, no crash
        assert ids(pkg.entities) == ["e1"]  # the rest of the package still assembles
