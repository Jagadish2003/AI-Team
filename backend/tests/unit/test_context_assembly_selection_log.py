"""Selection-log tests for the Context Assembly service (R16-B2 / T4, doc §3).

Focused on the acceptance criterion this subtask owns:

  AC6 — selection_log records an entry for EVERY candidate with its decision and
        reason; excluded candidates show why (below floor, budget exhausted,
        ranked out). The log is a FIRST-CLASS output on the ContextPackage, not
        debug logging — later evidence views (routes_sprint4_t6 / llm_enrichment)
        will read it to answer "why was this context used?".

Pure / DB-free: the service is deterministic policy over in-memory candidates.
"""
from __future__ import annotations

from app.context_assembly import (
    REASON_STALE,
    AssemblyPolicy,
    ContextPackage,
    assemble_context,
)

REQUIRED_FIELDS = {
    "candidate_id", "kind", "origin", "decision", "reason",
    "confidence", "freshness_days",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def ent(eid, conf, origin="observed", freshness_days=0):
    return {"entity_id": eid, "confidence": conf, "origin": origin,
            "freshness_days": freshness_days}


def rel(frm, to, rtype, conf, inferred=False):
    return {"from_id": frm, "to_id": to, "relationship_type": rtype,
            "confidence": conf, "inferred": inferred}


def chunk(cid, conf, origin="observed", is_stale=False):
    return {"chunk_id": cid, "confidence": conf, "origin": origin,
            "is_stale": is_stale}


def graph(entities=None, relationships=None):
    return {"entities": entities or [], "relationships": relationships or []}


def by_id(pkg):
    return {l["candidate_id"]: l for l in pkg.selection_log}


# --------------------------------------------------------------------------- #
# the log is a normal output of the service
# --------------------------------------------------------------------------- #

def test_selection_log_is_a_first_class_contextpackage_field():
    pkg = assemble_context({}, graph(entities=[ent("e1", 0.9)]), AssemblyPolicy())
    assert isinstance(pkg, ContextPackage)
    assert isinstance(pkg.selection_log, list)
    assert pkg.selection_log, "selection_log must be populated, not empty"


def test_every_candidate_across_kinds_gets_exactly_one_entry():
    g = graph(
        entities=[ent("e1", 0.9), ent("e2", 0.8)],
        relationships=[rel("a", "b", "x", 0.7)],
    )

    def src(opp, policy):
        return [chunk("c1", 0.9)]

    pkg = assemble_context({}, g, AssemblyPolicy(), evidence_source=src)
    ids = [l["candidate_id"] for l in pkg.selection_log]
    assert sorted(ids) == sorted(["e1", "e2", "a->b:x", "c1"])
    assert len(ids) == len(set(ids)), "exactly one entry per candidate"


def test_every_entry_carries_all_required_fields():
    g = graph(entities=[ent("keep", 0.9), ent("drop", 0.1)])
    pkg = assemble_context({}, g, AssemblyPolicy(confidence_floor=0.5))
    assert pkg.selection_log
    for entry in pkg.selection_log:
        assert REQUIRED_FIELDS <= set(entry), f"missing fields in {entry}"


# --------------------------------------------------------------------------- #
# decisions + reasons
# --------------------------------------------------------------------------- #

def test_included_entries_record_position():
    g = graph(entities=[ent("e1", 0.9), ent("e2", 0.8)])
    pkg = assemble_context({}, g, AssemblyPolicy())
    included = [l for l in pkg.selection_log if l["decision"] == "included"]
    assert len(included) == 2
    # Positions are 1-based, matching the canonical T7 contract test
    # (tests/contract/test_context_assembly.py) — reconciled during R16-B2
    # integration so both suites agree on the included@position_N numbering.
    assert sorted(l["reason"] for l in included) == [
        "included@position_1", "included@position_2",
    ]


def test_reason_below_confidence_floor():
    g = graph(entities=[ent("ok", 0.9), ent("low", 0.1)])
    pkg = assemble_context({}, g, AssemblyPolicy(confidence_floor=0.5))
    low = by_id(pkg)["low"]
    assert low["decision"] == "excluded"
    assert low["reason"] == "below_confidence_floor"


def test_reason_budget_exhausted_when_observed_fills_budget():
    g = graph(entities=[ent("o1", 0.9, "observed"), ent("i1", 0.99, "inferred")])
    pkg = assemble_context({}, g, AssemblyPolicy(max_entities=1))
    i1 = by_id(pkg)["i1"]
    assert i1["decision"] == "excluded"
    assert i1["reason"] == "budget_exhausted"


def test_reason_ranked_out_when_outranked_within_partition():
    g = graph(entities=[ent("hi", 0.9, "observed"), ent("lo", 0.5, "observed")])
    pkg = assemble_context({}, g, AssemblyPolicy(max_entities=1))
    lo = by_id(pkg)["lo"]
    assert lo["decision"] == "excluded"
    assert lo["reason"] == "ranked_out"


def test_all_four_reason_categories_can_appear_in_one_run():
    g = graph(entities=[
        ent("obs_hi", 0.90, "observed"),   # included@position_0
        ent("obs_lo", 0.55, "observed"),   # ranked_out (cap filled by obs_hi)
        ent("low", 0.10, "observed"),      # below_confidence_floor
        ent("inf", 0.95, "inferred"),      # budget_exhausted (observed took budget)
    ])
    pkg = assemble_context({}, g, AssemblyPolicy(max_entities=1, confidence_floor=0.4))
    reasons = {l["candidate_id"]: l["reason"] for l in pkg.selection_log}
    assert reasons["obs_hi"].startswith("included@position_")
    assert reasons["obs_lo"] == "ranked_out"
    assert reasons["low"] == "below_confidence_floor"
    assert reasons["inf"] == "budget_exhausted"


def test_excluded_candidates_across_all_kinds_show_why():
    g = graph(
        entities=[ent("e_low", 0.1)],                       # below floor
        relationships=[                                      # cap 1: inferred budgeted out
            rel("o", "o2", "x", 0.9, inferred=False),
            rel("i", "i2", "y", 0.9, inferred=True),
        ],
    )

    def src(opp, policy):                                    # cap 1: c_lo ranked out
        return [chunk("c_hi", 0.9), chunk("c_lo", 0.8)]

    pkg = assemble_context(
        {}, g,
        AssemblyPolicy(confidence_floor=0.5, max_relationships=1, max_evidence_chunks=1),
        evidence_source=src,
    )
    log = by_id(pkg)
    assert log["e_low"]["reason"] == "below_confidence_floor"
    assert log["i->i2:y"]["reason"] == "budget_exhausted"
    assert log["c_lo"]["reason"] == "ranked_out"
    # ...and the kept ones are recorded as included.
    assert log["o->o2:x"]["decision"] == "included"
    assert log["c_hi"]["decision"] == "included"


# --------------------------------------------------------------------------- #
# stale exclusion (R18-B2 T4 / AC6) — 'excluded: stale' is visible, not silent
# --------------------------------------------------------------------------- #

def test_reason_stale_excludes_stale_evidence_by_default():
    def src(opp, policy):
        return [chunk("fresh", 0.6), chunk("stale", 0.95, is_stale=True)]

    pkg = assemble_context({}, graph(), AssemblyPolicy(), evidence_source=src)
    log = by_id(pkg)
    # The stale chunk is excluded despite its higher confidence...
    assert log["stale"]["decision"] == "excluded"
    assert log["stale"]["reason"] == REASON_STALE
    # ...and does not enter the package, while the fresh one does.
    assert [e["chunk_id"] for e in pkg.evidence] == ["fresh"]


def test_stale_takes_precedence_over_below_floor():
    # A chunk that is BOTH stale and below the floor is reported as stale — the
    # freshness contract's exclusion runs first (Rule 0 before Rule 1).
    def src(opp, policy):
        return [chunk("bad", 0.1, is_stale=True)]

    pkg = assemble_context(
        {}, graph(), AssemblyPolicy(confidence_floor=0.5), evidence_source=src
    )
    assert by_id(pkg)["bad"]["reason"] == REASON_STALE


def test_policy_include_stale_admits_stale_and_does_not_log_stale_exclusion():
    def src(opp, policy):
        return [chunk("stale", 0.9, is_stale=True)]

    pkg = assemble_context(
        {}, graph(), AssemblyPolicy(include_stale=True), evidence_source=src
    )
    entry = by_id(pkg)["stale"]
    assert entry["decision"] == "included"
    assert entry["reason"] != REASON_STALE
    assert [e["chunk_id"] for e in pkg.evidence] == ["stale"]


def test_stale_exclusion_is_deterministic_and_input_order_independent():
    def src_factory(chunks):
        def src(opp, policy):
            return chunks
        return src

    chunks = [chunk("c3", 0.5, is_stale=True), chunk("c1", 0.9, is_stale=True),
              chunk("c2", 0.9, is_stale=True)]
    policy = AssemblyPolicy()
    fwd = assemble_context({}, graph(), policy, evidence_source=src_factory(chunks)).selection_log
    rev = assemble_context(
        {}, graph(), policy, evidence_source=src_factory(list(reversed(chunks)))
    ).selection_log
    assert fwd == rev
    # All three recorded as excluded: stale, ordered deterministically by id.
    stale_ids = [l["candidate_id"] for l in fwd if l["reason"] == REASON_STALE]
    assert stale_ids == ["c1", "c2", "c3"]


# --------------------------------------------------------------------------- #
# per-field correctness
# --------------------------------------------------------------------------- #

def test_kind_is_recorded_per_candidate_type():
    g = graph(entities=[ent("e1", 0.9)], relationships=[rel("a", "b", "x", 0.9)])

    def src(opp, policy):
        return [chunk("c1", 0.9)]

    pkg = assemble_context({}, g, AssemblyPolicy(), evidence_source=src)
    kinds = {l["candidate_id"]: l["kind"] for l in pkg.selection_log}
    assert kinds["e1"] == "entity"
    assert kinds["a->b:x"] == "relationship"
    assert kinds["c1"] == "evidence"


def test_origin_is_recorded():
    g = graph(entities=[ent("o", 0.9, "observed"), ent("i", 0.9, "inferred")])
    pkg = assemble_context({}, g, AssemblyPolicy())
    origins = {l["candidate_id"]: l["origin"] for l in pkg.selection_log}
    assert origins["o"] == "observed"
    assert origins["i"] == "inferred"


def test_confidence_and_freshness_are_recorded_when_available():
    g = graph(entities=[ent("e1", 0.73, "observed", freshness_days=12)])
    pkg = assemble_context({}, g, AssemblyPolicy())
    e1 = by_id(pkg)["e1"]
    assert e1["confidence"] == 0.73
    assert e1["freshness_days"] == 12


def test_included_vs_excluded_counts_are_consistent():
    g = graph(entities=[ent(f"e{i}", 0.5) for i in range(5)])
    pkg = assemble_context({}, g, AssemblyPolicy(max_entities=2))
    included = [l for l in pkg.selection_log if l["decision"] == "included"]
    excluded = [l for l in pkg.selection_log if l["decision"] == "excluded"]
    assert len(included) == 2
    assert len(excluded) == 3
    assert all(l["reason"] == "ranked_out" for l in excluded)


# --------------------------------------------------------------------------- #
# auditability requires the log itself to be deterministic
# --------------------------------------------------------------------------- #

def test_selection_log_is_deterministic_and_input_order_independent():
    items = [ent("e3", 0.5), ent("e1", 0.9), ent("e2", 0.9)]
    policy = AssemblyPolicy()
    log_forward = assemble_context({}, graph(entities=list(items)), policy).selection_log
    log_reverse = assemble_context(
        {}, graph(entities=list(reversed(items))), policy
    ).selection_log
    assert log_forward == log_reverse
