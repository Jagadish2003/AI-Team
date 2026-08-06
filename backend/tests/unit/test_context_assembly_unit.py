"""Unit tests for the Context Assembly Foundation (R16-B2, Part One / T1–T4, T6).

Pure / DB-free: the service is deterministic policy over in-memory candidates, so
these exercise it directly and cover the acceptance criteria that belong to this
foundation subtask:

  AC1 — identical inputs => byte-identical ContextPackage ordering (deterministic).
  AC2 — context below confidence_floor never appears, regardless of budget.
  AC3 — observed fills the budget first; inferred only fills what remains and
        never displaces an observed item that fit.
  AC4 — hard caps (max_entities / max_relationships / max_evidence_chunks) hold.
  AC5 — ties resolve via the stable tiebreaker (candidate id).
  AC6 — selection_log records every candidate with its decision and reason.
  AC7 — evidence_source=None works (empty evidence); the same signature accepts a
        stub retrieval source unchanged.

(AC8 — routing all enrichment through this service — is the T5 wiring step, out of
scope for this foundation subtask.)
"""
from __future__ import annotations

from app.context_assembly import (
    AssemblyPolicy,
    ContextPackage,
    assemble_context,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def ent(eid, conf, origin="observed", freshness_days=0):
    return {"entity_id": eid, "confidence": conf, "origin": origin,
            "freshness_days": freshness_days}


def rel(frm, to, rtype, conf, inferred=False, freshness_days=0):
    return {"from_id": frm, "to_id": to, "relationship_type": rtype,
            "confidence": conf, "inferred": inferred, "freshness_days": freshness_days}


def ev(cid, conf, origin="observed", freshness_days=0):
    return {"chunk_id": cid, "confidence": conf, "origin": origin,
            "freshness_days": freshness_days}


def graph(entities=None, relationships=None):
    return {"entities": entities or [], "relationships": relationships or []}


def _ids(items, key="entity_id"):
    return [i[key] for i in items]


# --------------------------------------------------------------------------- #
# AC1 — deterministic
# --------------------------------------------------------------------------- #

def test_ac1_identical_inputs_produce_identical_package():
    g = graph(
        entities=[ent("e3", 0.5), ent("e1", 0.9), ent("e2", 0.9)],
        relationships=[rel("a", "b", "x", 0.8)],
    )
    policy = AssemblyPolicy()
    pkg1 = assemble_context({}, g, policy)
    pkg2 = assemble_context({}, g, policy)
    assert pkg1 == pkg2


def test_ac1_ordering_independent_of_input_order():
    items = [ent("e3", 0.5), ent("e1", 0.9), ent("e2", 0.9)]
    policy = AssemblyPolicy()
    forward = assemble_context({}, graph(entities=list(items)), policy)
    reverse = assemble_context({}, graph(entities=list(reversed(items))), policy)
    assert _ids(forward.entities) == _ids(reverse.entities)
    assert forward.selection_log == reverse.selection_log


# --------------------------------------------------------------------------- #
# AC2 — confidence floor
# --------------------------------------------------------------------------- #

def test_ac2_below_floor_never_appears_even_with_budget():
    g = graph(entities=[ent("hi", 0.9), ent("lo", 0.2)])
    pkg = assemble_context({}, g, AssemblyPolicy(confidence_floor=0.5, max_entities=15))
    ids = _ids(pkg.entities)
    assert "hi" in ids and "lo" not in ids  # budget had room; floor still excludes
    lo = next(l for l in pkg.selection_log if l["candidate_id"] == "lo")
    assert lo["decision"] == "excluded" and lo["reason"] == "below_confidence_floor"


def test_ac2_floor_is_strict_lower_bound():
    # confidence exactly at the floor is kept; strictly below is dropped.
    g = graph(entities=[ent("at_floor", 0.5), ent("below", 0.49)])
    pkg = assemble_context({}, g, AssemblyPolicy(confidence_floor=0.5))
    ids = _ids(pkg.entities)
    assert "at_floor" in ids and "below" not in ids


# --------------------------------------------------------------------------- #
# AC3 — observed-first budget filling
# --------------------------------------------------------------------------- #

def test_ac3_observed_fills_budget_first():
    # cap 2; two observed + two higher-confidence inferred -> observed win the budget.
    g = graph(entities=[
        ent("o1", 0.9, "observed"), ent("o2", 0.8, "observed"),
        ent("i1", 0.95, "inferred"), ent("i2", 0.99, "inferred"),
    ])
    pkg = assemble_context({}, g, AssemblyPolicy(max_entities=2))
    assert _ids(pkg.entities) == ["o1", "o2"]
    reasons = {l["candidate_id"]: l["reason"]
               for l in pkg.selection_log if l["decision"] == "excluded"}
    assert reasons["i1"] == "budget_exhausted"
    assert reasons["i2"] == "budget_exhausted"


def test_ac3_inferred_fills_only_remaining_space():
    g = graph(entities=[
        ent("o1", 0.9, "observed"),
        ent("i1", 0.7, "inferred"), ent("i2", 0.6, "inferred"),
    ])
    pkg = assemble_context({}, g, AssemblyPolicy(max_entities=3))
    assert _ids(pkg.entities) == ["o1", "i1", "i2"]  # observed first, then inferred


def test_ac3_inferred_never_displaces_an_observed_that_fit():
    # A far higher-confidence inferred item must not push out a low observed one.
    g = graph(entities=[ent("obs", 0.3, "observed"), ent("inf", 0.99, "inferred")])
    pkg = assemble_context({}, g, AssemblyPolicy(max_entities=1))
    assert _ids(pkg.entities) == ["obs"]


# --------------------------------------------------------------------------- #
# AC4 — hard caps
# --------------------------------------------------------------------------- #

def test_ac4_hard_caps_enforced():
    g = graph(
        entities=[ent(f"e{i}", 0.5) for i in range(5)],
        relationships=[rel(f"a{i}", f"b{i}", "x", 0.5) for i in range(4)],
    )
    pkg = assemble_context({}, g, AssemblyPolicy(max_entities=2, max_relationships=1))
    assert len(pkg.entities) == 2
    assert len(pkg.relationships) == 1


def test_ac4_default_caps_are_15_and_20():
    g = graph(
        entities=[ent(f"e{i:02d}", 0.5) for i in range(30)],
        relationships=[rel(f"a{i:02d}", f"b{i:02d}", "x", 0.5) for i in range(30)],
    )
    pkg = assemble_context({}, g, AssemblyPolicy())
    assert len(pkg.entities) == 15
    assert len(pkg.relationships) == 20


def test_default_caps_sourced_from_graph_constants():
    from app.graph_constants import (
        GRAPH_CONTEXT_MAX_ENTITIES,
        GRAPH_CONTEXT_MAX_RELATIONSHIPS,
    )
    p = AssemblyPolicy()
    assert p.max_entities == GRAPH_CONTEXT_MAX_ENTITIES
    assert p.max_relationships == GRAPH_CONTEXT_MAX_RELATIONSHIPS


# --------------------------------------------------------------------------- #
# AC5 — stable tiebreaker
# --------------------------------------------------------------------------- #

def test_ac5_ties_resolve_by_stable_tiebreaker():
    # equal confidence AND freshness -> order strictly by candidate id, and that
    # order is independent of the order the candidates were supplied in.
    items = [ent("e_c", 0.5), ent("e_a", 0.5), ent("e_b", 0.5)]
    forward = assemble_context({}, graph(entities=list(items)), AssemblyPolicy())
    reverse = assemble_context({}, graph(entities=list(reversed(items))), AssemblyPolicy())
    assert _ids(forward.entities) == ["e_a", "e_b", "e_c"]
    assert _ids(forward.entities) == _ids(reverse.entities)


def test_freshness_breaks_confidence_ties_before_id():
    # equal confidence; the fresher item (fewer freshness_days) ranks first.
    g = graph(entities=[
        ent("old", 0.5, "observed", freshness_days=100),
        ent("new", 0.5, "observed", freshness_days=1),
    ])
    pkg = assemble_context({}, g, AssemblyPolicy(max_entities=2))
    assert _ids(pkg.entities) == ["new", "old"]


# --------------------------------------------------------------------------- #
# AC6 — selection log
# --------------------------------------------------------------------------- #

def test_ac6_log_records_every_candidate_with_decision_and_reason():
    g = graph(entities=[
        ent("incl", 0.9, "observed"),
        ent("low", 0.1, "observed"),
        ent("inf", 0.8, "inferred"),
    ])
    pkg = assemble_context({}, g, AssemblyPolicy(max_entities=1, confidence_floor=0.4))
    by_id = {l["candidate_id"]: l for l in pkg.selection_log}
    assert set(by_id) == {"incl", "low", "inf"}  # one entry per candidate
    assert by_id["incl"]["decision"] == "included"
    assert by_id["incl"]["reason"].startswith("included@position_")
    assert by_id["low"]["decision"] == "excluded"
    assert by_id["low"]["reason"] == "below_confidence_floor"
    assert by_id["inf"]["decision"] == "excluded"
    assert by_id["inf"]["reason"] == "budget_exhausted"
    for entry in pkg.selection_log:
        assert {"candidate_id", "kind", "origin", "decision", "reason",
                "confidence", "freshness_days"} <= set(entry)


def test_ac6_ranked_out_reason_for_outranked_within_partition():
    g = graph(entities=[ent("hi", 0.9, "observed"), ent("lo", 0.5, "observed")])
    pkg = assemble_context({}, g, AssemblyPolicy(max_entities=1))
    by_id = {l["candidate_id"]: l for l in pkg.selection_log}
    assert by_id["hi"]["decision"] == "included"
    assert by_id["lo"]["decision"] == "excluded"
    assert by_id["lo"]["reason"] == "ranked_out"


# --------------------------------------------------------------------------- #
# AC7 — evidence_source forward-compatibility hook
# --------------------------------------------------------------------------- #

def test_ac7_evidence_source_none_yields_empty_evidence():
    pkg = assemble_context({}, graph(), AssemblyPolicy())
    assert pkg.evidence == []


def test_ac7_stub_callable_source_flows_through_the_same_rules():
    def stub_source(opportunity, policy):
        return [
            ev("c_obs", 0.9, "observed"),
            ev("c_low", 0.1, "observed"),
            ev("c_inf", 0.95, "inferred"),
        ]
    policy = AssemblyPolicy(max_evidence_chunks=1, confidence_floor=0.5)
    pkg = assemble_context({}, graph(), policy, evidence_source=stub_source)
    assert _ids(pkg.evidence, key="chunk_id") == ["c_obs"]  # floor + observed-first + cap
    by_id = {l["candidate_id"]: l for l in pkg.selection_log if l["kind"] == "evidence"}
    assert by_id["c_low"]["reason"] == "below_confidence_floor"
    assert by_id["c_inf"]["reason"] == "budget_exhausted"


def test_ac7_evidence_source_accepts_object_and_iterable():
    chunks = [ev("c1", 0.8)]

    class StubSource:
        def fetch_evidence(self, opportunity, policy):
            return chunks

    pkg_obj = assemble_context({}, graph(), AssemblyPolicy(), evidence_source=StubSource())
    assert _ids(pkg_obj.evidence, key="chunk_id") == ["c1"]

    pkg_iter = assemble_context({}, graph(), AssemblyPolicy(), evidence_source=chunks)
    assert _ids(pkg_iter.evidence, key="chunk_id") == ["c1"]


def test_ac7_failing_evidence_source_is_advisory_not_fatal():
    def boom(opportunity, policy):
        raise RuntimeError("retrieval down")
    pkg = assemble_context({}, graph(entities=[ent("e1", 0.9)]), AssemblyPolicy(),
                           evidence_source=boom)
    assert pkg.evidence == []          # advisory: no evidence, no crash
    assert _ids(pkg.entities) == ["e1"]  # the rest of the package still assembles


# --------------------------------------------------------------------------- #
# package metadata + integration with the real GraphContext shapes
# --------------------------------------------------------------------------- #

def test_policy_used_is_recorded_on_the_package():
    policy = AssemblyPolicy(max_entities=3, confidence_floor=0.2)
    pkg = assemble_context({}, graph(), policy)
    assert isinstance(pkg, ContextPackage)
    assert pkg.policy_used is policy


def test_works_with_real_graphcontext_dataclasses():
    from app.graph_context_builder import (
        EntityContext,
        GraphContext,
        RelationshipContext,
    )
    gc = GraphContext(
        opportunity_id="opp1",
        entities=[
            EntityContext("e1", "Acme", "object", 0, 5, 0.9),
            EntityContext("e2", "Beta", "system", 1, 2, 0.7),
        ],
        relationships=[
            RelationshipContext("Acme", "Beta", "uses", False, 0.9),
            RelationshipContext("Beta", "Gamma", "depends_on", True, 0.6),
        ],
    )
    pkg = assemble_context({"id": "opp1"}, gc, AssemblyPolicy())
    assert len(pkg.entities) == 2
    # observed relationship ranks before the inferred one (Rule 3).
    assert pkg.relationships[0].inferred is False
    assert pkg.relationships[1].inferred is True
    # every entity + relationship candidate is logged.
    assert len(pkg.selection_log) == 4
