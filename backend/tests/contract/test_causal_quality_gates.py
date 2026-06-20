"""Unit tests for T5 — evaluate_causal_quality_gates() (ENT-6 / T3-S16-A).

Covers the three causal quality gates of Section 1 and the acceptance criteria
that belong to T5:

  AC1 — Gate 1: run_count < 10 → preliminary=True with the gate 1 reason.
  AC3 — Gate 2: any unresolved entity (evidence_links or enrichment.entities)
        → preliminary=True; preliminary_reason identifies the gate.
  AC4 — Gate 3: a cause_chain step referencing an inferred relationship as
        primary evidence → preliminary=True.
  AC5 — preliminary=False only when all three gates pass.

Plus the Definition-of-Done specifics:
  - each single-gate failure, the multi-gate priority (Gate 2 > Gate 3 > Gate 1),
    and the all-pass case;
  - the live run_count is read from signal_snapshots (never gate_run_count) and
    threaded out for T6;
  - the result behaves like the documented (preliminary, reason) tuple;
  - no gate is configurable / bypassable.

The two read-only data sources (_primary_signal_run_count and
_entity_resolution_status_map) are monkeypatched, so no live DB is required.
"""
from __future__ import annotations

import pytest

from app.causal_engine import (
    GATE1_MIN_RUN_COUNT,
    GateResult,
    cause_chain_uses_inferred,
    evaluate_causal_quality_gates,
    step_references_inferred_relationship,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

_OPP_ID = "opp-001"
_SIGNAL_KEY = "ncino::COVENANT_TRACKING_GAP::metric_value"

_OBSERVED_CHAIN = [
    "Loan origination volume rose 40% above baseline [OBSERVED, rising, anomalous].",
    "Commercial Credit team capacity was not scaled [OBSERVED via OwnerId].",
    "Covenant review queue backed up [OBSERVED: avg 23 days overdue].",
]

_INFERRED_CHAIN = [
    "Commercial Credit accounts for 62% of SLA breaches [OBSERVED: ServiceNow].",
    "The same team has the highest Jira open issue count [OBSERVED: Jira].",
    "[inferred: 0.6] Backlog pressure from Jira is reducing the team's capacity.",
]


def _entity(entity_id: str, resolution_status: str = "resolved") -> dict:
    return {"entity_id": entity_id, "resolution_status": resolution_status}


def _enrichment(*entities: dict) -> dict:
    return {"entities": list(entities)}


def _opp(cause_chain=None, evidence_links=None) -> dict:
    return {
        "cause_chain": list(cause_chain) if cause_chain is not None else list(_OBSERVED_CHAIN),
        "evidence_links": list(evidence_links) if evidence_links is not None else [],
    }


@pytest.fixture
def patch_run_count(monkeypatch):
    """Return a setter that fixes the live primary-signal run count."""

    def _set(count: int):
        monkeypatch.setattr(
            "app.causal_engine._primary_signal_run_count",
            lambda org_id, signal_key: count,
        )

    return _set


@pytest.fixture
def patch_resolution(monkeypatch):
    """Return a setter that fixes the entities-table resolution lookup."""

    def _set(status_map: dict):
        monkeypatch.setattr(
            "app.causal_engine._entity_resolution_status_map",
            lambda org_id, entity_ids: dict(status_map),
        )

    return _set


def _evaluate(opp=None, enrichment=None, signal_key=_SIGNAL_KEY):
    return evaluate_causal_quality_gates(
        opp if opp is not None else _opp(),
        signal_key,
        _OPP_ID,
        enrichment if enrichment is not None else _enrichment(_entity("e1")),
        causal_context=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — all gates pass → confirmed
# ─────────────────────────────────────────────────────────────────────────────

def test_all_gates_pass_returns_confirmed(patch_run_count, patch_resolution):
    patch_run_count(GATE1_MIN_RUN_COUNT)  # exactly 10 — Gate 1 passes
    patch_resolution({"e2": "resolved"})  # evidence_links id resolved via the table
    result = _evaluate(
        opp=_opp(cause_chain=_OBSERVED_CHAIN, evidence_links=["e2"]),
        enrichment=_enrichment(_entity("e1", "resolved")),  # inline-resolved entity
    )
    assert result.preliminary is False
    assert result.reason is None
    assert result.run_count == GATE1_MIN_RUN_COUNT


def test_all_gates_pass_with_no_entities_is_vacuously_confirmed(patch_run_count):
    """Gate 2 passes vacuously when there are no entities to resolve."""
    patch_run_count(15)
    result = _evaluate(opp=_opp(evidence_links=[]), enrichment=_enrichment())
    assert result.preliminary is False
    assert result.reason is None


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — Gate 1: insufficient temporal history
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("count", [0, 1, 7, 8, 9])
def test_gate1_below_threshold_is_preliminary(patch_run_count, count):
    # Defaults keep Gate 2 and Gate 3 passing so only Gate 1 is under test:
    # observed cause_chain, empty evidence_links, one inline-resolved entity.
    patch_run_count(count)
    result = _evaluate()
    assert result.preliminary is True
    assert result.reason.startswith("gate1_insufficient_run_count")
    # The "{n} of 10" substring is parsed by T9 — keep it intact.
    assert f"{count} of {GATE1_MIN_RUN_COUNT}" in result.reason
    assert result.run_count == count


def test_gate1_boundary_exactly_threshold_passes(patch_run_count):
    patch_run_count(GATE1_MIN_RUN_COUNT)
    result = _evaluate()
    assert result.preliminary is False


def test_gate1_just_below_threshold_fails(patch_run_count):
    patch_run_count(GATE1_MIN_RUN_COUNT - 1)
    result = _evaluate()
    assert result.preliminary is True
    assert result.reason.startswith("gate1_insufficient_run_count")


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — Gate 2: unresolved entity chain
# ─────────────────────────────────────────────────────────────────────────────

def test_gate2_unresolved_enrichment_entity_is_preliminary(patch_run_count):
    patch_run_count(20)  # Gate 1 passes
    result = _evaluate(
        opp=_opp(cause_chain=_OBSERVED_CHAIN, evidence_links=[]),
        enrichment=_enrichment(_entity("e1", "resolved"), _entity("e2", "ambiguous")),
    )
    assert result.preliminary is True
    assert result.reason.startswith("gate2_unresolved_entities")
    assert "1 entities require resolution" in result.reason


def test_gate2_unresolved_via_evidence_links_db_lookup(patch_run_count, patch_resolution):
    """AC3: an evidence_links id whose entities-table status != 'resolved' fails."""
    patch_run_count(20)
    patch_resolution({"e1": "resolved", "e2": "unresolved"})
    result = _evaluate(
        opp=_opp(cause_chain=_OBSERVED_CHAIN, evidence_links=["e1", "e2"]),
        enrichment=_enrichment(),
    )
    assert result.preliminary is True
    assert result.reason.startswith("gate2_unresolved_entities")
    assert "1 entities require resolution" in result.reason


def test_gate2_missing_entity_counts_as_unresolved(patch_run_count, patch_resolution):
    """An id absent from the entities table cannot be confirmed → unresolved."""
    patch_run_count(20)
    patch_resolution({})  # e9 not found
    result = _evaluate(
        opp=_opp(cause_chain=_OBSERVED_CHAIN, evidence_links=["e9"]),
        enrichment=_enrichment(),
    )
    assert result.preliminary is True
    assert result.reason.startswith("gate2_unresolved_entities")


def test_gate2_counts_distinct_unresolved_entities(patch_run_count):
    patch_run_count(20)
    result = _evaluate(
        opp=_opp(cause_chain=_OBSERVED_CHAIN, evidence_links=[]),
        enrichment=_enrichment(
            _entity("e1", "ambiguous"),
            _entity("e2", "unresolved"),
            _entity("e3", "resolved"),
        ),
    )
    assert result.preliminary is True
    assert "2 entities require resolution" in result.reason


def test_gate2_object_form_entities(patch_run_count):
    """enrichment.entities may be objects, not dicts."""

    class _Ent:
        def __init__(self, entity_id, resolution_status):
            self.entity_id = entity_id
            self.resolution_status = resolution_status

    class _Enr:
        def __init__(self, entities):
            self.entities = entities

    patch_run_count(20)
    result = _evaluate(
        opp=_opp(evidence_links=[]),
        enrichment=_Enr([_Ent("e1", "resolved"), _Ent("e2", "ambiguous")]),
    )
    assert result.preliminary is True
    assert result.reason.startswith("gate2_unresolved_entities")


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — Gate 3: inferred cause-chain step
# ─────────────────────────────────────────────────────────────────────────────

def test_gate3_inferred_step_is_preliminary(patch_run_count):
    patch_run_count(20)  # Gate 1 passes
    # Empty evidence_links + inline-resolved entity keep Gate 2 passing.
    result = _evaluate(
        opp=_opp(cause_chain=_INFERRED_CHAIN, evidence_links=[]),
        enrichment=_enrichment(_entity("e1", "resolved")),
    )
    assert result.preliminary is True
    assert result.reason.startswith("gate3_inferred_primary_step")
    assert "step 3" in result.reason  # 1-based index of the inferred step


def test_gate3_reports_first_inferred_step_index(patch_run_count):
    patch_run_count(20)
    chain = [
        "Step one [OBSERVED].",
        "[inferred: 0.6] Step two rests on an inferred edge.",
        "[inferred: 0.6] Step three also inferred.",
    ]
    result = _evaluate(
        opp=_opp(cause_chain=chain, evidence_links=[]),
        enrichment=_enrichment(_entity("e1", "resolved")),
    )
    assert "step 2" in result.reason


# ─────────────────────────────────────────────────────────────────────────────
# Multi-gate priority: Gate 2 > Gate 3 > Gate 1
# ─────────────────────────────────────────────────────────────────────────────

def test_priority_all_three_fail_reports_gate2(patch_run_count):
    patch_run_count(3)  # Gate 1 fails
    result = _evaluate(
        opp=_opp(cause_chain=_INFERRED_CHAIN, evidence_links=[]),  # Gate 3 fails
        enrichment=_enrichment(_entity("e1", "ambiguous")),         # Gate 2 fails
    )
    assert result.preliminary is True
    assert result.reason.startswith("gate2_unresolved_entities")


def test_priority_gate3_over_gate1_when_gate2_passes(patch_run_count):
    patch_run_count(3)  # Gate 1 fails
    result = _evaluate(
        opp=_opp(cause_chain=_INFERRED_CHAIN, evidence_links=[]),  # Gate 3 fails
        enrichment=_enrichment(_entity("e1", "resolved")),         # Gate 2 passes
    )
    assert result.preliminary is True
    assert result.reason.startswith("gate3_inferred_primary_step")


def test_priority_gate1_only_when_gate2_and_gate3_pass(patch_run_count):
    patch_run_count(4)  # Gate 1 fails
    result = _evaluate(
        opp=_opp(cause_chain=_OBSERVED_CHAIN, evidence_links=[]),  # Gate 3 passes
        enrichment=_enrichment(_entity("e1", "resolved")),         # Gate 2 passes
    )
    assert result.preliminary is True
    assert result.reason.startswith("gate1_insufficient_run_count")


# ─────────────────────────────────────────────────────────────────────────────
# run_count threading + tuple compatibility
# ─────────────────────────────────────────────────────────────────────────────

def test_run_count_threaded_even_when_a_higher_gate_fails(patch_run_count):
    """T6 needs gate_run_count = exactly what Gate 1 saw, even on a Gate 2 fail."""
    patch_run_count(12)
    result = _evaluate(
        opp=_opp(evidence_links=[]),
        enrichment=_enrichment(_entity("e1", "ambiguous")),  # Gate 2 fails
    )
    assert result.preliminary is True
    assert result.reason.startswith("gate2_unresolved_entities")
    assert result.run_count == 12  # not lost despite the Gate 2 short-name


def test_result_behaves_like_preliminary_reason_tuple(patch_run_count):
    patch_run_count(2)  # Gate 1 fails; Gates 2/3 pass via defaults
    result = _evaluate()

    # Unpacks as the documented (preliminary, reason) tuple.
    preliminary, reason = result
    assert preliminary is True
    assert reason == result.reason

    # Compares equal to the plain 2-tuple and indexes/len like one.
    assert result == (True, result.reason)
    assert isinstance(result, tuple)
    assert result[0] is True
    assert len(result) == 2


def test_missing_signal_key_fails_gate1_with_zero_runs():
    """No signal_key → no temporal history → Gate 1 fails with run_count 0."""
    result = evaluate_causal_quality_gates(
        _opp(evidence_links=[]),
        None,  # no signal_key
        _OPP_ID,
        _enrichment(_entity("e1", "resolved")),
        causal_context=None,
    )
    assert result.preliminary is True
    assert result.run_count == 0
    assert result.reason.startswith("gate1_insufficient_run_count")


# ─────────────────────────────────────────────────────────────────────────────
# Shared inferred-step detector (Gate 3 / T6 inferred column)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "step",
    [
        "[inferred: 0.6] Backlog pressure reduces capacity.",
        "3. [inferred: 0.6] Some step.",
        "[INFERRED: 0.7] case-insensitive.",
        "[ inferred: 0.6] tolerant of spacing.",
    ],
)
def test_step_detector_flags_inferred(step):
    assert step_references_inferred_relationship(step) is True


@pytest.mark.parametrize(
    "step",
    [
        "Loan origination volume rose 40% [OBSERVED, rising, anomalous].",
        "Covenant review queue backed up [OBSERVED: avg 23 days].",
        "",
        None,
    ],
)
def test_step_detector_ignores_observed(step):
    assert step_references_inferred_relationship(step) is False


def test_cause_chain_uses_inferred_matches_gate3():
    assert cause_chain_uses_inferred(_INFERRED_CHAIN) is True
    assert cause_chain_uses_inferred(_OBSERVED_CHAIN) is False
    assert cause_chain_uses_inferred([]) is False


# ─────────────────────────────────────────────────────────────────────────────
# Gates are NOT configurable / bypassable (ENT-6 Sections 1 & 8)
# ─────────────────────────────────────────────────────────────────────────────

def test_gate1_threshold_is_hardcoded_ten():
    assert GATE1_MIN_RUN_COUNT == 10


def test_no_env_var_can_lower_gate1_threshold(patch_run_count, monkeypatch):
    """Setting baseline/threshold-style env vars must not weaken Gate 1."""
    for var in ("BASELINE_MIN_RUNS", "GATE1_MIN_RUN_COUNT", "CAUSAL_MIN_RUNS"):
        monkeypatch.setenv(var, "1")
    patch_run_count(7)  # below the hardcoded 10
    result = _evaluate()  # Gates 2/3 pass via defaults
    assert result.preliminary is True
    assert result.reason.startswith("gate1_insufficient_run_count")
