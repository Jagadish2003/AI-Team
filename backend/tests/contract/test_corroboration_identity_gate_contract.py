"""2.0-B2 T6 contract tests — the identity gate against a real graph.

AC5: "Corroboration across sources requires resolved identity; unresolved
same-named entities do not raise confidence."

The DB-free half (decision logic, reference extraction, fail-closed degradation)
is in ``tests/unit/test_corroboration_identity_gate.py``. This file exercises the
REAL graph-backed resolver, because the property that matters is whether the gate
agrees with what the entity layer actually recorded:

  * two same-named entities that nothing has resolved → no elevation;
  * the same pair with an explicit cross-reference in the source data → elevation
    (the resolver reads T1's auto-merge decision);
  * the same pair joined by the org's alias table → elevation;
  * the same pair confirmed by a human in the T3 review surface → elevation;
  * a REJECTED proposal → still no elevation;
  * ambiguity and cross-org leakage never resolve.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from app import corroboration_identity_gate as gate
from app import db
from app import entity_match_proposals as emp
from app.corroboration_engine import evaluate_corroboration
from discovery.packs.corroboration_rules import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM

ORG = "default"
OTHER_ORG = "gate-org-b"
RUN = "run_gate_t6"
DETECTOR = "COVENANT_TRACKING_GAP"
RUN_TS = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


def _within_window() -> str:
    return (RUN_TS - timedelta(days=10)).isoformat()


# ── seeding ─────────────────────────────────────────────────────────────────


def _insert_entity(
    display_name: str,
    *,
    source_system: str,
    source_record_id: Optional[str] = None,
    org_id: str = ORG,
    entity_type: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    entity_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO entities (
                id, org_id, entity_type, canonical_name, display_name,
                source_system, source_record_id, resolution_confidence,
                resolution_status, first_seen_run_id, last_seen_run_id,
                run_count, metadata, created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'resolved',%s,%s,1,%s,%s,%s)
            """,
            (
                entity_id, org_id, entity_type,
                " ".join(display_name.split()).lower(), display_name,
                source_system, source_record_id, 1.0, RUN, RUN,
                json.dumps(metadata) if metadata is not None else None, now, now,
            ),
        )
        con.commit()
    finally:
        con.close()
    return entity_id


def _cleanup() -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        for org in (ORG, OTHER_ORG):
            cur.execute("DELETE FROM entity_match_proposal_history WHERE org_id = %s", (org,))
            cur.execute("DELETE FROM entity_match_proposals WHERE org_id = %s", (org,))
            cur.execute(
                "DELETE FROM entities WHERE org_id = %s AND first_seen_run_id = %s",
                (org, RUN),
            )
        con.commit()
    finally:
        con.close()
    try:
        from app.entity_alias_mappings import ALIAS_KV_KEY

        db.kv_set(f"{ALIAS_KV_KEY}:{ORG}", [])
    except Exception:  # noqa: BLE001 — best-effort teardown of the alias table.
        pass


@pytest.fixture(autouse=True)
def clean_graph():
    _cleanup()
    yield
    _cleanup()


# ── the corroboration input: both sides name "Payments" ─────────────────────


def _run_data(sn_name: str = "Payments", jira_name: str = "Payments") -> Dict[str, Any]:
    return {
        "connected_systems": ["salesforce", "servicenow", "jira"],
        "servicenow": {"incidents": [{
            "detector_ids": [DETECTOR], "state": "Open",
            "sys_created_on": _within_window(), "team": sn_name,
        }]},
        "jira": {"issues": [{
            "detector_ids": [DETECTOR], "status": "Open",
            "created": _within_window(), "process": jira_name,
        }]},
    }


def _evaluate(run_data: Optional[Dict[str, Any]] = None):
    """Evaluate with the REAL graph-backed resolver (no injection)."""
    return evaluate_corroboration(
        detector_id=DETECTOR, pack_id="ncino",
        run_data=run_data if run_data is not None else _run_data(),
        run_timestamp=RUN_TS, org_id=ORG,
    )


# ── AC5 ─────────────────────────────────────────────────────────────────────


def test_ac5_same_named_but_unresolved_entities_do_not_raise_confidence():
    """The honesty gap: a ServiceNow "Payments" and a Jira "Payments" that nothing
    has resolved are two words, not one thing."""
    _insert_entity("Payments", source_system="servicenow", source_record_id="sn-1")
    _insert_entity("Payments", source_system="jira", source_record_id="PAY")

    result = _evaluate()

    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.identity_gate["identity_verified"] is False
    assert result.identity_gate["reason"] == gate.REASON_NOT_RESOLVED
    assert set(result.identity_gate["blocked_rules"]) == {"COR-01", "COR-02", "COR-03"}
    assert result.triple_corroboration is False


def test_ac5_an_explicit_cross_reference_resolves_the_identity_and_elevates():
    """The source data itself states the identity — T1 auto-merges it, so the
    cross-source corroboration is genuinely about one thing."""
    _insert_entity(
        "Payments", source_system="servicenow", source_record_id="sn-1",
        metadata={"cross_references": [{"system": "jira", "record_id": "PAY"}]},
    )
    _insert_entity("Payments", source_system="jira", source_record_id="PAY")

    result = _evaluate()

    assert result.elevated_confidence == CONFIDENCE_HIGH
    assert result.identity_gate["identity_verified"] is True
    assert result.identity_gate["basis"] == gate.BASIS_EXPLICIT_REFERENCE
    assert result.identity_gate["blocked_rules"] == []
    assert result.triple_corroboration is True


def test_ac5_the_org_alias_table_resolves_the_identity_and_elevates():
    """An Owner-asserted identity is a recorded human statement, so it counts —
    and the gate must actually consult the alias table to see it."""
    from app.entity_alias_mappings import put_alias_mappings

    _insert_entity("Payments API", source_system="servicenow", source_record_id="sn-1")
    _insert_entity("payments-api", source_system="jira", source_record_id="PAY")
    put_alias_mappings(ORG, [{
        "entity_type": "system",
        "canonical": "payments-api",
        "aliases": ["Payments API"],
        "created_by": "owner@example.com",
    }])

    result = _evaluate(_run_data(sn_name="Payments API", jira_name="payments-api"))

    # The two names DIFFER, so resolution has to be ATTEMPTED rather than skipped:
    # this pair is genuinely one thing because an Owner said so, and a gate that
    # short-circuited on the differing names would refuse that very identity.
    assert result.elevated_confidence == CONFIDENCE_HIGH
    assert result.identity_gate["identity_verified"] is True
    assert result.identity_gate["basis"] == gate.BASIS_ALIAS_MAPPING
    assert result.identity_gate["blocked_rules"] == []


def test_ac5_a_human_confirmation_resolves_the_identity_and_elevates():
    """The only route by which a name-similarity pair may ever count: a person
    confirmed it in the T3 review surface."""
    left = _insert_entity("Payments", source_system="servicenow", source_record_id="sn-1")
    right = _insert_entity("Payments", source_system="jira", source_record_id="PAY")

    blocked_first = _evaluate()
    assert blocked_first.elevated_confidence == CONFIDENCE_MEDIUM, (
        "before the confirmation the pair must not elevate"
    )

    pid = emp.proposal_id_for("system", left, right)
    _seed_proposal(pid, left, right)
    emp.decide(ORG, pid, emp.ACTION_CONFIRM, "analyst@example.com")

    result = _evaluate()

    assert result.elevated_confidence == CONFIDENCE_HIGH
    assert result.identity_gate["identity_verified"] is True
    assert result.identity_gate["basis"] == gate.BASIS_CONFIRMED_PROPOSAL


def test_ac5_an_applied_merge_is_the_authoritative_basis():
    """Once T2 has actually merged the pair, the gate must report the rule the
    GRAPH acted on rather than re-deriving an answer that could disagree with it."""
    from app import entity_merge as em

    left = _insert_entity(
        "Payments", source_system="servicenow", source_record_id="sn-1",
        metadata={"cross_references": [{"system": "jira", "record_id": "PAY"}]},
    )
    right = _insert_entity("Payments", source_system="jira", source_record_id="PAY")

    outcome = em.apply_merge(
        ORG, left, right, rule=em.RULE_EXPLICIT_REFERENCE, actor="system"
    )
    assert outcome.applied is True

    result = _evaluate()

    assert result.elevated_confidence == CONFIDENCE_HIGH
    assert result.identity_gate["identity_verified"] is True
    assert result.identity_gate["basis"] == gate.BASIS_EXPLICIT_REFERENCE


def test_a_merge_recorded_under_a_human_confirmation_reports_that_rule():
    """A pair merged because a person confirmed it must not be reported as though
    a machine reference proved it — the provenance rule is the honest answer."""
    from app import entity_merge as em

    left = _insert_entity("Payments", source_system="servicenow", source_record_id="sn-1")
    right = _insert_entity("Payments", source_system="jira", source_record_id="PAY")
    em.apply_merge(
        ORG, left, right, rule=em.RULE_CONFIRMED_PROPOSAL, actor="analyst@example.com"
    )

    result = _evaluate()

    assert result.elevated_confidence == CONFIDENCE_HIGH
    assert result.identity_gate["basis"] == gate.BASIS_CONFIRMED_PROPOSAL


def test_ac5_a_rejected_proposal_still_does_not_elevate():
    """A person said these are NOT the same thing — the strongest possible reason
    to refuse the elevation."""
    left = _insert_entity("Payments", source_system="servicenow", source_record_id="sn-1")
    right = _insert_entity("Payments", source_system="jira", source_record_id="PAY")
    pid = emp.proposal_id_for("system", left, right)
    _seed_proposal(pid, left, right)
    emp.decide(ORG, pid, emp.ACTION_REJECT, "analyst@example.com")

    result = _evaluate()

    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.identity_gate["identity_verified"] is False


def _seed_proposal(proposal_id: str, left: str, right: str) -> None:
    """A pending proposal for the pair, as the T3 scan would have recorded it."""
    now = datetime.now(timezone.utc).isoformat()
    a, b = sorted((left, right))
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO entity_match_proposals (
                org_id, proposal_id, entity_type, left_entity_id, right_entity_id,
                tier, confidence, status, evidence_payload, revision,
                first_proposed_at, last_proposed_at, created_at, updated_at
            ) VALUES (%s,%s,'system',%s,%s,'name_similarity',0.7,'pending','{}',0,
                      %s,%s,%s,%s)
            ON CONFLICT (org_id, proposal_id) DO NOTHING
            """,
            (ORG, proposal_id, a, b, now, now, now, now),
        )
        con.commit()
    finally:
        con.close()


# ── the resolver's own conservatism ─────────────────────────────────────────


def test_ambiguous_same_named_rows_never_resolve():
    """Two rows for one name in one source is the standing engine's recorded
    uncertainty. Picking one would launder that into a confident merge."""
    _insert_entity("Payments", source_system="servicenow", source_record_id="sn-1")
    _insert_entity("Payments", source_system="servicenow", source_record_id="sn-2")
    _insert_entity("Payments", source_system="jira", source_record_id="PAY")

    result = _evaluate()

    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.identity_gate["identity_verified"] is False


def test_an_entity_absent_from_the_graph_does_not_resolve():
    """Nothing was extracted for these names, so nothing establishes identity."""
    result = _evaluate()
    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.identity_gate["identity_verified"] is False


def test_another_orgs_resolution_never_satisfies_this_orgs_gate():
    """Cross-tenant identity leakage would be both wrong and a breach."""
    _insert_entity(
        "Payments", source_system="servicenow", source_record_id="sn-1",
        org_id=OTHER_ORG,
        metadata={"cross_references": [{"system": "jira", "record_id": "PAY"}]},
    )
    _insert_entity("Payments", source_system="jira", source_record_id="PAY",
                   org_id=OTHER_ORG)
    # This org sees the same names but has no resolution of its own.
    _insert_entity("Payments", source_system="servicenow", source_record_id="sn-1")
    _insert_entity("Payments", source_system="jira", source_record_id="PAY")

    result = _evaluate()

    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.identity_gate["identity_verified"] is False


def test_the_resolver_returns_a_basis_only_for_a_real_merge():
    """Directly: the graph resolver must answer None for a name-only pair, and a
    tier for a referenced one."""
    _insert_entity("Billing", source_system="servicenow", source_record_id="sn-9")
    _insert_entity("Billing", source_system="jira", source_record_id="BILL")

    left = gate.EntityRef("servicenow", name="Billing")
    right = gate.EntityRef("jira", name="Billing")
    assert gate.graph_identity_resolver(ORG, left, right) is None

    _cleanup()
    _insert_entity(
        "Billing", source_system="servicenow", source_record_id="sn-9",
        metadata={"external_ids": {"jira": "BILL"}},
    )
    _insert_entity("Billing", source_system="jira", source_record_id="BILL")
    assert gate.graph_identity_resolver(ORG, left, right) == (
        gate.BASIS_EXPLICIT_REFERENCE
    )


# ── no regression to the pre-existing basis ─────────────────────────────────


def test_a_detector_link_alone_no_longer_elevates():
    """The behaviour change AC5 asks for, stated as a test: before T6 a shared
    DETECTOR was enough to reach HIGH across two systems. It no longer is — a
    resolved entity identity is required, and these two references have none."""
    result = _evaluate(_run_data(sn_name="lending-ops", jira_name="covenant"))

    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.identity_gate["identity_verified"] is False
    assert result.identity_gate["reason"] == gate.REASON_NO_RESOLVED_IDENTITY
    assert set(result.identity_gate["blocked_rules"]) == {"COR-01", "COR-02", "COR-03"}
    # The evidence survives — only the elevation is refused.
    assert "COR-01" in result.rule_ids and "COR-02" in result.rule_ids
