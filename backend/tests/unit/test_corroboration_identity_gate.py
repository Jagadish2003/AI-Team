"""2.0-B2 T6 — the cross-source corroboration identity gate (DB-free).

AC5: "Corroboration across sources requires resolved identity; unresolved
same-named entities do not raise confidence."

Two independent systems agreeing is the strongest confidence signal the platform
has. It only means anything if both are talking about the same thing. These tests
pin the decision logic that enforces that, with the resolver injected so no
database is needed:

  * a same-named pair across sources with NO resolved identity does not elevate —
    the acceptance criterion;
  * the same pair DOES elevate once the identity is genuinely resolved (explicit
    cross-reference, org alias table, or a human confirmation);
  * a name match can never become a basis by accident;
  * evidence is never destroyed — blocking removes the ELEVATION, not the rules;
  * the gate reports what it did, including when it could not verify.

The end-to-end half (real entities, the real engine, a real graph) is in
``tests/contract/test_corroboration_identity_gate_contract.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from app import corroboration_identity_gate as gate
from app.corroboration_engine import evaluate_corroboration
from discovery.packs.corroboration_rules import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM

RUN_TS = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
DETECTOR = "COVENANT_TRACKING_GAP"
ORG = "org_a"


def _within_window() -> str:
    return (RUN_TS - timedelta(days=10)).isoformat()


def _sn(name: str = "Payments", *, record_id: Optional[str] = None, field: str = "team"):
    record: Dict[str, Any] = {
        "detector_ids": [DETECTOR],
        "state": "Open",
        "sys_created_on": _within_window(),
        field: name,
    }
    if record_id:
        record["ci_sys_id"] = record_id
    return record


def _jira(name: str = "Payments", *, record_id: Optional[str] = None, field: str = "process"):
    record: Dict[str, Any] = {
        "detector_ids": [DETECTOR],
        "status": "Open",
        "created": _within_window(),
        field: name,
    }
    if record_id:
        record["project_id"] = record_id
    return record


def _run_data(sn_records=None, jira_records=None, systems=("salesforce", "servicenow", "jira")):
    return {
        "connected_systems": list(systems),
        "servicenow": {"incidents": list(sn_records if sn_records is not None else [_sn()])},
        "jira": {"issues": list(jira_records if jira_records is not None else [_jira()])},
    }


def _evaluate(run_data, resolver):
    return evaluate_corroboration(
        detector_id=DETECTOR, pack_id="ncino", run_data=run_data,
        run_timestamp=RUN_TS, org_id=ORG, identity_resolver=resolver,
    )


# Resolvers, injected — each stands for one way an identity can (not) be settled.
def _never(_org, _left, _right):
    return None


def _resolved_by(basis):
    def _resolver(_org, _left, _right):
        return basis
    return _resolver


# ── AC5: the acceptance criterion ───────────────────────────────────────────


def test_ac5_same_named_unresolved_entities_do_not_raise_confidence():
    """A ServiceNow "Payments" and a Jira "Payments" that nothing has resolved are
    two words, not one thing — and must not produce a HIGH."""
    result = _evaluate(_run_data(), _never)

    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.confidence_elevated is False
    assert result.identity_gate["identity_verified"] is False
    assert set(result.identity_gate["blocked_rules"]) == {"COR-01", "COR-02", "COR-03"}
    assert result.identity_gate["reason"] == gate.REASON_NOT_RESOLVED


@pytest.mark.parametrize(
    "basis",
    [
        gate.BASIS_EXPLICIT_REFERENCE,
        gate.BASIS_ALIAS_MAPPING,
        gate.BASIS_CONFIRMED_PROPOSAL,
        gate.BASIS_SAME_ENTITY,
    ],
)
def test_ac5_a_genuinely_resolved_identity_does_elevate(basis):
    """The other half of the criterion: a resolved identity is exactly what makes
    cross-source corroboration meaningful, so it must still reach HIGH."""
    result = _evaluate(_run_data(), _resolved_by(basis))

    assert result.elevated_confidence == CONFIDENCE_HIGH
    assert result.identity_gate["identity_verified"] is True
    assert result.identity_gate["basis"] == basis
    assert result.identity_gate["blocked_rules"] == []
    assert result.triple_corroboration is True


def test_blocking_removes_the_elevation_not_the_evidence():
    """A reviewer should still see that both systems had something to say — the
    gate refuses the CONFIDENCE, it does not hide the corroboration."""
    result = _evaluate(_run_data(), _never)

    assert "COR-01" in result.rule_ids
    assert "COR-02" in result.rule_ids
    assert "ServiceNow" in result.corroboration_sources
    assert "Jira" in result.corroboration_sources
    assert result.corroboration_label, "the card still explains what was found"


def test_the_triple_headline_falls_with_the_pair_it_derives_from():
    """COR-03 asserts "all three agree" — the strongest cross-source claim on the
    card. It cannot outlive the identity check that its two halves failed."""
    blocked = _evaluate(_run_data(), _never)
    allowed = _evaluate(_run_data(), _resolved_by(gate.BASIS_EXPLICIT_REFERENCE))

    assert blocked.triple_corroboration is False
    assert allowed.triple_corroboration is True


# ── what counts as an identity claim ────────────────────────────────────────


def test_unresolved_different_named_references_also_do_not_elevate():
    """AC5's first clause: corroboration across sources REQUIRES a resolved
    identity. A ServiceNow *team* and a Jira *process* that nothing resolved are
    not one thing either — the names simply make that less tempting to assume."""
    run_data = _run_data([_sn("lending-ops")], [_jira("covenant")])
    result = _evaluate(run_data, _never)

    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.identity_gate["identity_verified"] is False
    # Reported distinctly from the same-name case, which is the dangerous shape.
    assert result.identity_gate["identity_claim"] is False
    assert result.identity_gate["reason"] == gate.REASON_NO_RESOLVED_IDENTITY


def test_a_different_named_pair_still_elevates_once_resolved():
    """The alias-table case: "Payments API" and "payments-api" look nothing alike
    yet are genuinely one thing. Resolution must be ATTEMPTED for such a pair —
    short-circuiting on the differing names would refuse the very identity an
    Owner recorded."""
    run_data = _run_data([_sn("Payments API")], [_jira("payments-api")])
    result = _evaluate(run_data, _resolved_by(gate.BASIS_ALIAS_MAPPING))

    assert result.elevated_confidence == CONFIDENCE_HIGH
    assert result.identity_gate["identity_verified"] is True
    assert result.identity_gate["basis"] == gate.BASIS_ALIAS_MAPPING


def test_a_record_with_no_entity_reference_cannot_establish_identity():
    """No reference is the ABSENCE of the evidence the elevation requires, not a
    licence to skip the requirement."""
    run_data = {
        "connected_systems": ["salesforce", "servicenow", "jira"],
        "servicenow": {"incidents": [{"detector_ids": [DETECTOR], "state": "Open",
                                      "sys_created_on": _within_window()}]},
        "jira": {"issues": [{"detector_ids": [DETECTOR], "status": "Open",
                             "created": _within_window()}]},
    }
    result = _evaluate(run_data, _never)

    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.identity_gate["reason"] == gate.REASON_NO_REFERENCE
    assert result.identity_gate["identity_verified"] is False


def test_name_matching_is_normalised_not_literal():
    """"Payments API" and "payments  api" are the same claim — the gate uses the
    same canonicalisation as the entity layer, so it cannot disagree with the
    graph about what a name is."""
    left = gate.EntityRef("servicenow", name="Payments  API")
    right = gate.EntityRef("jira", name="payments api")
    verdict = gate.check_identity(ORG, left, right, resolver=_never)

    assert verdict.claim is True
    assert verdict.blocks_elevation is True


def test_one_source_alone_is_not_a_cross_source_claim():
    """The gate governs the AGREEMENT of two sources; with one rule fired there
    is no cross-source elevation for it to govern."""
    outcome = gate.gate_cross_source_corroboration(
        ORG, ["COR-01"],
        left=gate.EntityRef("servicenow", name="Payments"),
        right=gate.EntityRef("jira", name="Payments"),
        resolver=_never,
    )
    assert outcome.applied is False
    assert outcome.blocked_rules == ()


def test_same_source_matches_are_not_this_gates_business():
    """Two same-named entities inside ONE source are the standing engine's
    problem, not a cross-source identity claim."""
    left = gate.EntityRef("servicenow", name="Payments")
    right = gate.EntityRef("servicenow", name="Payments")
    assert gate.check_identity(ORG, left, right, resolver=_never).claim is False


# ── a name match can never become a basis ───────────────────────────────────


def test_a_name_match_is_not_in_the_resolved_bases():
    """The absence of a name basis IS the acceptance criterion — a structural
    guard, so a future edit has to delete this test to break AC5."""
    assert "name_similarity" not in gate.RESOLVED_BASES
    assert gate.RESOLVED_BASES == {
        gate.BASIS_SAME_ENTITY,
        gate.BASIS_EXPLICIT_REFERENCE,
        gate.BASIS_ALIAS_MAPPING,
        gate.BASIS_CONFIRMED_PROPOSAL,
    }


def test_the_auto_merge_bases_are_exactly_t1s_auto_merge_tiers():
    """The gate accepts a tier as proof only because T1 allows that tier to
    merge. If the two ever drift, the gate would be trusting something T1 does
    not."""
    from app import cross_source_resolution as csr

    assert {gate.BASIS_EXPLICIT_REFERENCE, gate.BASIS_ALIAS_MAPPING} == set(
        csr.AUTO_MERGE_TIERS
    )
    assert csr.TIER_NAME_SIMILARITY not in gate.RESOLVED_BASES


def test_an_unrecognised_basis_does_not_resolve():
    """Fail closed: a resolver returning something unexpected is not proof."""
    verdict = gate.check_identity(
        ORG,
        gate.EntityRef("servicenow", name="Payments"),
        gate.EntityRef("jira", name="Payments"),
        resolver=_resolved_by("looked_similar"),
    )
    assert verdict.resolved is False
    assert verdict.blocks_elevation is True


# ── reference extraction ────────────────────────────────────────────────────


def test_a_stable_id_is_preferred_over_a_name():
    ref = gate.entity_ref_from_record(
        "servicenow", _sn("Payments", record_id="sn-1")
    )
    assert ref.record_id == "sn-1"
    assert ref.field_name == "ci_sys_id"
    assert ref.name == "Payments"


def test_a_name_is_used_when_there_is_no_stable_id():
    ref = gate.entity_ref_from_record("jira", _jira("Payments"))
    assert ref.record_id is None
    assert ref.canonical_name == "payments"
    assert ref.field_name == "process"


def test_an_unenumerated_field_is_never_read_as_an_entity_reference():
    """Guessing here would invent identity claims the source never made."""
    ref = gate.entity_ref_from_record(
        "servicenow",
        {"detector_ids": [DETECTOR], "mystery_label": "Payments", "state": "Open"},
    )
    assert ref is None


def test_a_servicenow_display_value_envelope_is_unwrapped():
    """``sysparm_display_value=all`` returns {value, display_value}; reading the
    envelope object as a name would compare dicts, never entities."""
    ref = gate.entity_ref_from_record(
        "servicenow",
        {"detector_ids": [DETECTOR], "team": {"value": "payments", "display_value": "Payments"}},
    )
    assert ref.canonical_name == "payments"


def test_the_first_usable_reference_wins_deterministically():
    records = [
        {"detector_ids": [DETECTOR]},                       # no reference
        _sn("Payments"),
        _sn("Billing"),
    ]
    assert gate.first_entity_ref("servicenow", records).name == "Payments"
    assert gate.first_entity_ref("servicenow", []) is None


# ── degradation is recorded, never silent ───────────────────────────────────


def test_an_unverifiable_identity_fails_closed_and_says_why():
    """An unreadable graph is not evidence of identity. Elevating here would
    reopen exactly this hole whenever the system is unhealthy, and a wrong HIGH is
    the harmful direction — so the claim is refused and the reason recorded."""
    def _boom(_org, _left, _right):
        raise RuntimeError("graph unavailable")

    result = _evaluate(_run_data(), _boom)

    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.identity_gate["identity_verified"] is False
    assert result.identity_gate["reason"] == gate.REASON_UNVERIFIABLE
    assert set(result.identity_gate["blocked_rules"]) == {"COR-01", "COR-02", "COR-03"}, (
        "a refusal must be visible as a refusal, not look like a genuine downgrade"
    )


def test_no_resolver_at_all_is_reported_as_unverifiable():
    verdict = gate.check_identity(
        ORG,
        gate.EntityRef("servicenow", name="Payments"),
        gate.EntityRef("jira", name="Payments"),
        resolver=None,
    )
    assert verdict.resolved is False
    assert verdict.reason == gate.REASON_UNVERIFIABLE


def test_the_gate_always_reports_what_it_did():
    """Every evaluation carries a verdict, so "was this HIGH identity-verified?"
    is always answerable rather than inferred."""
    for resolver in (_never, _resolved_by(gate.BASIS_ALIAS_MAPPING)):
        report = _evaluate(_run_data(), resolver).identity_gate
        for key in ("applied", "identity_verified", "identity_claim",
                    "blocked_rules", "reason", "left", "right"):
            assert key in report


# ── the gate never lowers a scorer's own confidence ─────────────────────────


def test_the_gate_never_downgrades_the_scorers_baseline():
    """Corroboration may only ever raise confidence. A blocked elevation returns
    the finding to its own baseline — it does not push it below."""
    from app.corroboration_engine import apply_corroboration_confidence

    blocked = _evaluate(_run_data(), _never)
    assert apply_corroboration_confidence(CONFIDENCE_HIGH, blocked) == CONFIDENCE_HIGH
    assert apply_corroboration_confidence(CONFIDENCE_MEDIUM, blocked) == CONFIDENCE_MEDIUM


def test_a_resolved_identity_still_elevates_a_medium_scorer():
    from app.corroboration_engine import apply_corroboration_confidence

    allowed = _evaluate(_run_data(), _resolved_by(gate.BASIS_EXPLICIT_REFERENCE))
    assert apply_corroboration_confidence(CONFIDENCE_MEDIUM, allowed) == CONFIDENCE_HIGH


# ── non-cross-source rules are untouched ────────────────────────────────────


def test_operational_corroborators_are_not_gated_by_this_rule():
    """COR-09/COR-10 are single-source observed evidence about the app itself —
    they make no cross-source identity claim, so this gate must leave them
    alone."""
    run_data = _run_data([_sn("Payments")], [_jira("Payments")])
    run_data["connected_systems"] = ["salesforce", "servicenow", "jira", "java_app"]
    run_data["java_app"] = {
        "operational_friction": {
            "fired": True, "detected_at": _within_window(),
            "error_patterns": 3, "source_system": "java_app",
        }
    }
    result = _evaluate(run_data, _never)

    assert "COR-09" in result.rule_ids
    assert set(result.identity_gate["blocked_rules"]) == {"COR-01", "COR-02", "COR-03"}
    assert result.elevated_confidence == CONFIDENCE_HIGH, (
        "an ungated observed corroborator still elevates on its own merits"
    )
