"""2.0-B3 T2 — budgeted composition: deterministic selection + a drop record.

AC2: "Over-budget candidate sets select deterministically and record what was
dropped and why."

R16-B2 already selected deterministically under the per-kind caps and logged a
reason per candidate. What was missing was the ability to ANSWER the question the
log technically contained: "did this finding lose context, and to which budget?"
Parsing every entry to find out meant nobody asked. So the tests here concentrate
on two properties:

  * **the report is true** — it must reconcile with the log it is derived from, and
    ``offered == selected + dropped`` for every kind. A report that does not add up
    is worse than no report, because it will be quoted. (An early version
    double-corrected one count and reported 2 drops where 5 happened; the
    reconciliation tests below exist because of it.)
  * **nothing is dropped silently** — every trimmed candidate is recorded, exactly
    once, with a reason that distinguishes "your kind was oversubscribed" from "the
    finding as a whole was too big".

DB-free: assembly is pure.
"""
from __future__ import annotations

import copy
import json

import pytest

from app import assembly_policy_config as apc
from app.context_assembly import (
    DECISION_EXCLUDED,
    DECISION_INCLUDED,
    KIND_ENTITY,
    KIND_EVIDENCE,
    KIND_RELATIONSHIP,
    REASON_BELOW_FLOOR,
    REASON_BUDGET_EXHAUSTED,
    REASON_RANKED_OUT,
    REASON_STALE,
    REASON_TOTAL_BUDGET,
    AssemblyBudgetReport,
    AssemblyPolicy,
    Candidate,
    KindBudget,
    assemble_context,
)


def _raw() -> dict:
    with open(apc.DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _policy(total_items=..., **overrides) -> AssemblyPolicy:
    raw = copy.deepcopy(_raw())
    if total_items is not ...:
        raw["caps"]["total_items"] = total_items
    return AssemblyPolicy.declared(apc.parse_declared_policy(raw), **overrides)


def _graph(n_entities=20, n_rels=25):
    return {
        "entities": [
            {"entity_id": f"e{i:02d}", "confidence": 0.9 - i / 100} for i in range(n_entities)
        ],
        "relationships": [
            {"relationship_id": f"r{i:02d}", "confidence": 0.9 - i / 100, "inferred": False}
            for i in range(n_rels)
        ],
    }


def _report(package) -> dict:
    assert package.budget_report is not None, "every package must carry a budget report"
    return package.budget_report


# ── the report must be TRUE ─────────────────────────────────────────────────


def test_every_kind_reconciles_offered_against_selected_plus_dropped():
    """The arithmetic invariant. An early version subtracted a count twice and
    reported 2 drops where 5 had happened — a number that would have been quoted."""
    package = assemble_context({"id": "o1"}, _graph(), _policy())

    for kind in _report(package)["per_kind"]:
        assert kind["offered"] == kind["selected"] + kind["dropped"], (
            f"{kind['kind']} does not add up: {kind}"
        )


def test_the_report_reconciles_with_the_selection_log():
    """Derived from the log, so the two can never disagree about what happened."""
    package = assemble_context({"id": "o1"}, _graph(), _policy(total_items=12))

    log_excluded = sum(
        1 for e in package.selection_log if e["decision"] == DECISION_EXCLUDED
    )
    log_included = sum(
        1 for e in package.selection_log if e["decision"] == DECISION_INCLUDED
    )
    report = _report(package)
    assert report["total_dropped"] == log_excluded
    assert report["total_selected"] == log_included


def test_the_report_counts_each_exclusion_reason_separately():
    """Because the remedies differ: widen a budget, lower a floor, or refresh a
    stale artifact. One aggregate "dropped" number would send a reader to the wrong
    lever."""
    candidates = [
        Candidate("keep", KIND_EVIDENCE, "observed", confidence=0.9),
        Candidate("weak", KIND_EVIDENCE, "observed", confidence=0.1),
        Candidate("old", KIND_EVIDENCE, "observed", confidence=0.9, is_stale=True),
        Candidate("extra", KIND_EVIDENCE, "observed", confidence=0.8),
    ]
    policy = _policy(max_evidence_chunks=1, confidence_floor=0.5)
    package = assemble_context({"id": "o1"}, {}, policy, evidence_source=candidates)

    evidence = next(k for k in _report(package)["per_kind"] if k["kind"] == KIND_EVIDENCE)
    assert evidence["dropped_below_floor"] == 1
    assert evidence["dropped_stale"] == 1
    assert evidence["dropped_by_budget"] == 1, "'extra' lost to the budget of 1"
    assert evidence["selected"] == 1
    assert evidence["offered"] == 4


def test_breached_means_a_BUDGET_cost_context_not_merely_that_something_dropped():
    """A below-floor or stale exclusion would have happened with unlimited budget.
    Reporting those as a breach would send an operator to widen a budget that was
    never the constraint."""
    candidates = [
        Candidate("keep", KIND_EVIDENCE, "observed", confidence=0.9),
        Candidate("weak", KIND_EVIDENCE, "observed", confidence=0.1),
    ]
    policy = _policy(max_evidence_chunks=10, confidence_floor=0.5)
    package = assemble_context({"id": "o1"}, {}, policy, evidence_source=candidates)

    evidence = next(k for k in _report(package)["per_kind"] if k["kind"] == KIND_EVIDENCE)
    assert evidence["dropped_below_floor"] == 1
    assert evidence["breached"] is False, "the floor dropped it, not the budget"
    assert evidence["reason"] is None


def test_a_package_within_budget_reports_no_breach_and_no_reason():
    package = assemble_context({"id": "o1"}, _graph(n_entities=2, n_rels=2), _policy())
    report = _report(package)
    assert report["breached"] is False
    assert report["reason"] is None
    assert report["total_dropped"] == 0


# ── nothing is dropped silently ─────────────────────────────────────────────


def test_every_over_budget_candidate_is_recorded_with_a_reason():
    """The core of AC2's second half."""
    package = assemble_context({"id": "o1"}, _graph(), _policy())

    excluded = [e for e in package.selection_log if e["decision"] == DECISION_EXCLUDED]
    assert excluded, "the graph is deliberately over budget"
    recognised = {
        REASON_BELOW_FLOOR, REASON_BUDGET_EXHAUSTED, REASON_RANKED_OUT,
        REASON_STALE, REASON_TOTAL_BUDGET,
    }
    for entry in excluded:
        assert entry["reason"] in recognised, entry
        # And it must say WHICH candidate and of what kind — a reason with no subject
        # cannot be acted on.
        assert entry["candidate_id"]
        assert entry["kind"]


def test_a_total_budget_trim_is_logged_as_its_own_reason():
    """Distinct from ``budget_exhausted`` because the remedy differs: this says the
    finding as a whole was too big, not that one kind was oversubscribed."""
    package = assemble_context({"id": "o1"}, _graph(), _policy(total_items=12))

    trimmed = [
        e for e in package.selection_log if e["reason"] == REASON_TOTAL_BUDGET
    ]
    assert trimmed, "a total budget of 12 against 45 candidates must trim"
    assert all(e["decision"] == DECISION_EXCLUDED for e in trimmed)


def test_no_candidate_is_both_included_and_excluded():
    """The trim RE-LABELS the entry it already wrote rather than appending a second
    one. Two entries for one candidate would make the log self-contradictory, and
    every reader would then need to know which one wins."""
    package = assemble_context({"id": "o1"}, _graph(), _policy(total_items=12))

    ids = [e["candidate_id"] for e in package.selection_log]
    assert len(ids) == len(set(ids)), "one entry per candidate"


def test_the_total_budget_is_actually_enforced():
    package = assemble_context({"id": "o1"}, _graph(), _policy(total_items=12))
    selected = (
        len(package.entities) + len(package.relationships) + len(package.evidence)
    )
    assert selected == 12
    assert _report(package)["total_selected"] == 12


# ── determinism, including which kind yields ────────────────────────────────


def test_over_budget_selection_is_deterministic_including_the_report():
    """AC2's first half. Determinism has to cover the report and the log too — a
    stable selection with a shifting audit trail is not reproducible."""
    first = assemble_context({"id": "o1"}, _graph(), _policy(total_items=12))
    second = assemble_context({"id": "o1"}, _graph(), _policy(total_items=12))

    assert [e["entity_id"] for e in first.entities] == [
        e["entity_id"] for e in second.entities
    ]
    assert first.selection_log == second.selection_log
    assert first.budget_report == second.budget_report


def test_input_order_does_not_change_what_is_dropped():
    graph = _graph()
    shuffled = {
        "entities": list(reversed(graph["entities"])),
        "relationships": list(reversed(graph["relationships"])),
    }
    a = assemble_context({"id": "o1"}, graph, _policy(total_items=12))
    b = assemble_context({"id": "o1"}, shuffled, _policy(total_items=12))

    assert [e["entity_id"] for e in a.entities] == [e["entity_id"] for e in b.entities]
    assert a.budget_report == b.budget_report


def test_kinds_yield_in_reverse_declared_precedence():
    """Deterministic AND declared: the most substitutable kind (last in
    ``kind_precedence``) gives up its items first, so nothing about which kind
    shrinks is arbitrary."""
    candidates = [
        Candidate(f"ev{i}", KIND_EVIDENCE, "observed", confidence=0.9) for i in range(5)
    ]
    package = assemble_context(
        {"id": "o1"},
        _graph(n_entities=5, n_rels=5),
        _policy(total_items=10),
        evidence_source=candidates,
    )
    per_kind = {k["kind"]: k for k in _report(package)["per_kind"]}

    # 15 candidates, budget 10 → evidence (declared last) yields first.
    assert per_kind[KIND_EVIDENCE]["dropped_by_total_budget"] == 5
    assert per_kind[KIND_ENTITY]["dropped_by_total_budget"] == 0
    assert per_kind[KIND_ENTITY]["selected"] == 5, "entities are protected"


def test_the_lowest_ranked_item_of_a_kind_is_the_one_that_yields():
    """Within a kind the already-ranked TAIL is trimmed, so the item lost is always
    the weakest — never a mid-list item chosen by accident of iteration."""
    package = assemble_context({"id": "o1"}, _graph(n_entities=5, n_rels=0), _policy(total_items=3))
    kept = [e["entity_id"] for e in package.entities]

    # Confidence descends with the index, so the three strongest survive.
    assert kept == ["e00", "e01", "e02"]


def test_a_declared_kind_precedence_change_changes_which_kind_shrinks():
    """The trim order is configuration, not code — the T1 discipline carried into T2."""
    candidates = [
        Candidate(f"ev{i}", KIND_EVIDENCE, "observed", confidence=0.9) for i in range(5)
    ]
    raw = copy.deepcopy(_raw())
    raw["caps"]["total_items"] = 10
    raw["kind_precedence"] = ["evidence", "relationship", "entity"]  # entities yield first
    policy = AssemblyPolicy.declared(apc.parse_declared_policy(raw))

    package = assemble_context(
        {"id": "o1"}, _graph(n_entities=5, n_rels=5), policy, evidence_source=candidates
    )
    per_kind = {k["kind"]: k for k in _report(package)["per_kind"]}
    assert per_kind[KIND_ENTITY]["dropped_by_total_budget"] == 5
    assert per_kind[KIND_EVIDENCE]["dropped_by_total_budget"] == 0


# ── the declaration ─────────────────────────────────────────────────────────


def test_the_total_budget_is_disabled_by_default_and_says_why():
    """Shipped as null on purpose: no calibration of prompt size against narrative
    quality exists, and a guessed number would silently trim every finding. The
    per-kind caps still bind, so AC2 is exercised in production regardless."""
    declaration = apc.load_declared_policy()
    assert declaration.max_total_items is None

    raw = _raw()
    assert "_total_items" in raw["caps"], "the null must carry its reasoning"
    assert "UNCALIBRATED" in raw["caps"]["_total_items"]


def test_a_zero_total_budget_is_refused():
    """0 would compose an EMPTY context for every finding — a configuration mistake,
    not a policy choice, so it is refused rather than obeyed."""
    raw = copy.deepcopy(_raw())
    raw["caps"]["total_items"] = 0
    with pytest.raises(apc.AssemblyPolicyConfigError, match="must be > 0 or null"):
        apc.parse_declared_policy(raw)


def test_an_unknown_or_partial_kind_precedence_is_refused():
    raw = copy.deepcopy(_raw())
    raw["kind_precedence"] = ["entity", "relationship", "sandwich"]
    with pytest.raises(apc.AssemblyPolicyConfigError, match="unknown kind"):
        apc.parse_declared_policy(raw)

    raw["kind_precedence"] = ["entity"]
    with pytest.raises(apc.AssemblyPolicyConfigError, match="omits"):
        apc.parse_declared_policy(raw)


def test_the_kind_vocabulary_cannot_drift_between_the_two_modules():
    from app import context_assembly as ca

    assert {ca.KIND_ENTITY, ca.KIND_RELATIONSHIP, ca.KIND_EVIDENCE} == set(apc.KNOWN_KINDS)


# ── the report shape ────────────────────────────────────────────────────────


def test_the_report_is_json_serialisable_for_the_run_record_and_b1_trace():
    """It is stored and rendered, so it must survive a JSON round trip unchanged."""
    package = assemble_context({"id": "o1"}, _graph(), _policy(total_items=12))
    round_tripped = json.loads(json.dumps(package.budget_report))
    assert round_tripped == package.budget_report


def test_the_report_names_the_budget_that_bound():
    """An operator seeing a thin narrative needs to know WHICH budget to look at."""
    package = assemble_context({"id": "o1"}, _graph(), _policy())
    reason = _report(package)["reason"]
    assert reason and "entity budget of 15" in reason


def test_an_empty_kind_reports_zeroes_rather_than_being_absent():
    """Absence would be ambiguous — "no evidence offered" and "evidence not
    assessed" are different facts."""
    package = assemble_context({"id": "o1"}, _graph(n_entities=1, n_rels=0), _policy())
    kinds = {k["kind"] for k in _report(package)["per_kind"]}
    assert kinds == {KIND_ENTITY, KIND_RELATIONSHIP, KIND_EVIDENCE}

    evidence = next(k for k in _report(package)["per_kind"] if k["kind"] == KIND_EVIDENCE)
    assert evidence["offered"] == 0 and evidence["breached"] is False


def test_kind_budget_reason_is_none_when_nothing_was_budget_dropped():
    kind = KindBudget(
        kind=KIND_ENTITY, budget=5, considered=3, selected=3,
        dropped_by_budget=0, dropped_below_floor=0, dropped_stale=0,
    )
    assert kind.breached is False and kind.reason is None
    assert AssemblyBudgetReport(per_kind=(kind,)).reason is None
