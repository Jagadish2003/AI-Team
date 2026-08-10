"""2.0-B3 T1 — assembly policy engine: precedence as declared configuration.

AC1: "Assembly policy is declared configuration; changing precedence changes
composition without code changes."

The load-bearing test in this file is
:func:`test_ac1_reordering_the_declaration_changes_composition`. Everything else
protects a property that, if it broke, would let AC1 pass while the engine was
still dishonest:

  * a declaration that changes nothing observable would satisfy the letter of AC1
    and none of its purpose, so the ordering assertions use candidates that
    genuinely DISAGREE across dimensions;
  * precedence is only trustworthy if a typo fails loudly — a silently-dropped
    rule would change composition with nobody knowing;
  * the R16-B2 guarantees (determinism, observed-never-displaced, the stable
    tiebreaker) must survive being expressed as data rather than code.

DB-free: the assembler is pure and the loader reads a file.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app import assembly_policy_config as apc
from app.context_assembly import (
    AssemblyPolicy,
    Candidate,
    assemble_context,
    select_candidates,
)

SHIPPED = Path(apc.DEFAULT_CONFIG_PATH)


def _raw() -> dict:
    return json.loads(SHIPPED.read_text(encoding="utf-8"))


def _declared(**changes) -> apc.DeclaredAssemblyPolicy:
    """The shipped declaration with the given top-level keys replaced."""
    raw = copy.deepcopy(_raw())
    raw.update(changes)
    return apc.parse_declared_policy(raw)


def _policy(declaration=None, **overrides) -> AssemblyPolicy:
    return AssemblyPolicy.declared(declaration or apc.parse_declared_policy(_raw()), **overrides)


def _order(candidates, declaration=None, cap=10):
    selected, _ = select_candidates(
        candidates, cap=cap, policy=_policy(declaration, max_evidence_chunks=cap)
    )
    return [c.candidate_id for c in selected]


# ── AC1: the declaration is what decides ────────────────────────────────────


def test_ac1_the_shipped_declaration_loads_and_is_the_policy():
    """The policy a deployment runs comes from the file, not from constants."""
    declaration = apc.load_declared_policy()

    assert declaration.source_path == str(SHIPPED)
    assert declaration.budget_partitions == (apc.DIMENSION_ORIGIN,)
    assert declaration.ranking == (
        apc.DIMENSION_SOURCE_TYPE,
        apc.DIMENSION_CONFIDENCE,
        apc.DIMENSION_FRESHNESS,
        apc.DIMENSION_CANDIDATE_ID,
    )
    # The caps/floor/half-life come from the declaration too, so a deployment has
    # ONE place to state them rather than two that can disagree.
    policy = AssemblyPolicy.declared(declaration)
    assert policy.max_entities == declaration.max_entities
    assert policy.confidence_floor == declaration.confidence_floor
    assert policy.freshness_halflife_days == declaration.freshness_halflife_days


def test_ac1_reordering_the_declaration_changes_composition():
    """**AC1.** Same code, same inputs, different declaration → different result.

    The two candidates disagree on purpose: the conversation is MORE confident, the
    structured record is a BETTER source type. Under the shipped precedence
    (source_type first) the structured record wins; move confidence first and the
    conversation wins. Nothing but the declaration changed.
    """
    candidates = [
        Candidate("chat-1", "evidence", "observed", confidence=0.95, source_type="conversation"),
        Candidate("inc-1", "evidence", "observed", confidence=0.60, source_type="structured"),
    ]

    structured_first = _order(candidates, cap=2)
    confidence_first = _order(
        candidates,
        _declared(ranking=["confidence", "source_type", "freshness", "candidate_id"]),
        cap=2,
    )

    assert structured_first == ["inc-1", "chat-1"]
    assert confidence_first == ["chat-1", "inc-1"]
    assert structured_first != confidence_first


def test_ac1_changing_precedence_changes_which_items_survive_the_cap():
    """Reordering must change the SELECTED SET, not merely its order — otherwise a
    budget-constrained assembly would compose identically however it was declared,
    and AC1 would be cosmetic."""
    candidates = [
        Candidate("chat-1", "evidence", "observed", confidence=0.95, source_type="conversation"),
        Candidate("inc-1", "evidence", "observed", confidence=0.60, source_type="structured"),
    ]

    # Budget of one: precedence decides who gets in at all.
    assert _order(candidates, cap=1) == ["inc-1"]
    assert _order(
        candidates,
        _declared(ranking=["confidence", "source_type", "freshness", "candidate_id"]),
        cap=1,
    ) == ["chat-1"]


def test_ac1_structured_records_outrank_conversational_content():
    """The story's own words, as a behaviour rather than a principle.

    Before T1 there was no source-type dimension at all, so these two competed on
    confidence and the chat thread won.
    """
    candidates = [
        Candidate("chat", "evidence", "observed", confidence=0.9, source_type="conversation"),
        Candidate("doc", "evidence", "observed", confidence=0.9, source_type="prose"),
        Candidate("repo", "evidence", "observed", confidence=0.9, source_type="code"),
        Candidate("rec", "evidence", "observed", confidence=0.9, source_type="structured"),
    ]
    assert _order(candidates) == ["rec", "doc", "repo", "chat"]


def test_ac1_freshness_can_be_declared_to_outrank_confidence():
    """R18-B2 freshness weighting is a declared dimension, so a deployment that
    cares more about recency than strength can say so without a code change."""
    candidates = [
        Candidate("old-strong", "evidence", "observed", confidence=0.9, freshness_days=400),
        Candidate("new-weak", "evidence", "observed", confidence=0.4, freshness_days=0),
    ]

    assert _order(candidates, _declared(ranking=["confidence", "freshness", "candidate_id"])) == [
        "old-strong", "new-weak",
    ]
    assert _order(candidates, _declared(ranking=["freshness", "confidence", "candidate_id"])) == [
        "new-weak", "old-strong",
    ]


def test_ac1_an_unranked_source_type_sorts_last_never_first():
    """Fail-safe, matching how the module already treats provenance and freshness:
    an item earns precedence by declaring what it is. If an unknown type sorted
    first, anything unclassified would outrank every declared structured record.
    """
    candidates = [
        Candidate("mystery", "evidence", "observed", confidence=0.99, source_type="telepathy"),
        Candidate("chat", "evidence", "observed", confidence=0.10, source_type="conversation"),
    ]
    assert _order(candidates) == ["chat", "mystery"]

    # And an EMPTY source type is treated the same way — undeclared, not privileged.
    blank = [
        Candidate("blank", "evidence", "observed", confidence=0.99, source_type=""),
        Candidate("chat", "evidence", "observed", confidence=0.10, source_type="conversation"),
    ]
    assert _order(blank) == ["chat", "blank"]


# ── hard tiers vs soft preferences ──────────────────────────────────────────


def test_a_hard_tier_cannot_be_displaced_even_by_a_better_ranked_item():
    """R16-B2 AC3, generalised: origin is a declared HARD tier, so an inferred item
    never displaces an observed one that fit — no matter how it ranks otherwise.

    Here the inferred candidate is better on EVERY soft dimension (structured, more
    confident, fresher) and still loses, because the tier decides first.
    """
    candidates = [
        Candidate("inferred-great", "entity", "inferred", confidence=1.0,
                  source_type="structured", freshness_days=0),
        Candidate("observed-poor", "entity", "observed", confidence=0.01,
                  source_type="conversation", freshness_days=999),
    ]
    assert _order(candidates, cap=1) == ["observed-poor"]


def test_a_soft_dimension_declared_as_a_tier_becomes_hard():
    """The distinction is configurable, which is the point: a deployment that wants
    structured records to be undisplaceable can move source_type into the tiers."""
    candidates = [
        Candidate("chat-strong", "evidence", "observed", confidence=1.0, source_type="conversation"),
        Candidate("rec-weak", "evidence", "observed", confidence=0.01, source_type="structured"),
    ]
    hard = _declared(
        budget_partitions=["origin", "source_type"],
        ranking=["confidence", "freshness", "candidate_id"],
    )
    assert _order(candidates, hard, cap=1) == ["rec-weak"]


def test_the_same_dimension_cannot_be_both_hard_and_soft():
    """Ambiguous about whether it may displace, so it is refused rather than
    resolved by an arbitrary precedence between the two lists."""
    with pytest.raises(apc.AssemblyPolicyConfigError, match="BOTH"):
        _declared(
            budget_partitions=["origin"],
            ranking=["origin", "confidence", "candidate_id"],
        )


# ── the declaration must fail loudly, never silently ────────────────────────


def test_an_unknown_dimension_is_refused_at_load():
    """A typo must not silently drop a precedence rule — that would change
    composition with nobody aware of it."""
    with pytest.raises(apc.AssemblyPolicyConfigError, match="unknown dimension"):
        _declared(ranking=["confidenec", "candidate_id"])


def test_a_declaration_must_end_with_the_stable_tiebreaker():
    """Without candidate_id last the key is not a total order, and two equal
    candidates could swap between runs — losing the determinism the whole module
    exists for."""
    with pytest.raises(apc.AssemblyPolicyConfigError, match="tiebreaker"):
        _declared(ranking=["confidence", "freshness"])


def test_a_repeated_dimension_is_refused():
    with pytest.raises(apc.AssemblyPolicyConfigError, match="repeats"):
        _declared(ranking=["confidence", "confidence", "candidate_id"])


def test_a_declared_dimension_with_no_rank_table_is_refused():
    """Declaring source_type while deleting its ranks would leave its precedence
    undefined — every value would tie."""
    with pytest.raises(apc.AssemblyPolicyConfigError, match="source_type_ranks"):
        _declared(source_type_ranks={})


def test_an_empty_ranking_is_refused():
    with pytest.raises(apc.AssemblyPolicyConfigError, match="at least one dimension"):
        _declared(ranking=[])


@pytest.mark.parametrize("floor", [-0.1, 1.5])
def test_an_out_of_range_confidence_floor_is_refused(floor):
    with pytest.raises(apc.AssemblyPolicyConfigError, match="0.0..1.0"):
        _declared(confidence_floor=floor)


def test_a_missing_config_raises_rather_than_defaulting():
    """A deployment that believes it configured precedence and did not would compose
    findings differently from what its operators think. Better to be told."""
    with pytest.raises(apc.AssemblyPolicyConfigError, match="not found"):
        apc.load_declared_policy("/nonexistent/assembly_policy.json")


def test_documentation_keys_are_not_read_as_configuration():
    """The file carries ``_``-prefixed explanation for whoever edits it; those must
    never be mistaken for settings."""
    raw = _raw()
    assert any(k.startswith("_") for k in raw), "the shipped file documents itself"
    declaration = apc.parse_declared_policy(raw)
    assert apc.DIMENSION_CANDIDATE_ID in declaration.ranking


# ── the R16-B2 guarantees survive being data ────────────────────────────────


def test_determinism_holds_under_a_declaration():
    """Same declaration + same inputs => byte-identical selection, twice."""
    candidates = [
        Candidate(f"c{i}", "evidence", "observed", confidence=0.5, source_type="prose")
        for i in range(8)
    ]
    first = _order(candidates, cap=5)
    second = _order(list(reversed(candidates)), cap=5)
    assert first == second, "input order must not affect the outcome"


def test_the_selection_log_records_the_source_type_it_ranked_on():
    """A log that omitted the dimension could not explain the decision — the reader
    would see a weaker item chosen over a stronger one with no reason visible."""
    candidates = [
        Candidate("chat", "evidence", "observed", confidence=0.9, source_type="conversation"),
        Candidate("rec", "evidence", "observed", confidence=0.5, source_type="structured"),
    ]
    _, log = select_candidates(candidates, cap=1, policy=_policy(max_evidence_chunks=1))
    by_id = {e["candidate_id"]: e for e in log}

    assert by_id["rec"]["source_type"] == "structured"
    assert by_id["rec"]["decision"] == "included"
    assert by_id["chat"]["source_type"] == "conversation"
    assert by_id["chat"]["decision"] == "excluded"


def test_the_package_records_the_declaration_that_produced_it():
    """A selection_log read months later must be interpretable against the
    precedence in force when it was written — and that precedence is now editable,
    so the log alone is no longer self-explaining."""
    package = assemble_context(
        {"id": "opp-1"},
        graph={"entities": [{"entity_id": "e1", "confidence": 0.9}]},
        policy=_policy(),
    )
    assert package.policy_declaration is not None
    assert package.policy_declaration["ranking"][0] == apc.DIMENSION_SOURCE_TYPE
    assert package.policy_declaration["budget_partitions"] == [apc.DIMENSION_ORIGIN]


def test_no_declaration_keeps_the_r16_b2_behaviour_exactly():
    """The change is additive: a caller that never opts in gets the original
    in-code precedence, so nothing that predates T1 shifts underneath it."""
    candidates = [
        Candidate("chat", "evidence", "observed", confidence=0.9, source_type="conversation"),
        Candidate("rec", "evidence", "observed", confidence=0.5, source_type="structured"),
    ]
    selected, _ = select_candidates(candidates, cap=2, policy=AssemblyPolicy())
    # Confidence-first, source type ignored — exactly the pre-T1 ordering.
    assert [c.candidate_id for c in selected] == ["chat", "rec"]


def test_graph_candidates_are_classified_as_structured_records():
    """The graph IS the structured record — it is resolved from source-system
    records. If it defaulted to unclassified it would sort last, inverting the very
    precedence the story asks for."""
    package = assemble_context(
        {"id": "opp-1"},
        graph={
            "entities": [{"entity_id": "e1", "confidence": 0.5}],
            "relationships": [
                {"from_entity_id": "e1", "to_entity_id": "e2",
                 "relationship_type": "owns", "confidence": 0.5},
            ],
        },
        policy=_policy(),
    )
    types = {e["kind"]: e["source_type"] for e in package.selection_log}
    assert types["entity"] == apc.SOURCE_TYPE_STRUCTURED
    assert types["relationship"] == apc.SOURCE_TYPE_STRUCTURED


def test_the_dimension_vocabulary_cannot_drift_between_the_two_modules():
    """``context_assembly`` mirrors the dimension names to avoid importing the
    loader on every rank; a divergence would make a valid declaration unrankable."""
    from app import context_assembly as ca

    mirrored = {
        ca._ORIGIN_DIM, ca._SOURCE_TYPE_DIM, ca._CONFIDENCE_DIM,
        ca._FRESHNESS_DIM, ca._CANDIDATE_ID_DIM,
    }
    assert mirrored == set(apc.KNOWN_DIMENSIONS)
