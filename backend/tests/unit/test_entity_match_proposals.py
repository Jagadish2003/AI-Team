"""2.0-B2 T3 — proposal review store + route logic (DB-free).

The review surface only works if three properties hold, and each is easy to get
subtly wrong:

  * **One question per pair.** The engine proposes symmetrically (A→B and B→A),
    so an order-dependent id would file the same question twice and let an
    analyst answer it inconsistently.
  * **An answered question is never asked again** — the story's "confirm/reject
    is recorded and durable across runs".
  * **Recording a decision is not applying one.** Confirming must not merge the
    graph; a merge is irreversible and belongs behind its own task.

The pieces those properties live in that need no database are pinned here: the
deterministic pair id, the evidence snapshot, the decision-request contract, and
the audit record. The SQL half (upsert semantics, decided-row protection,
history, tenancy) is in
``tests/contract/test_entity_match_proposals_contract.py``.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app import cross_source_resolution as csr
from app import entity_match_proposals as emp


# ── helpers ─────────────────────────────────────────────────────────────────


def _entity(entity_id: str, name: str, *, source: str, record_id: str = "r1"):
    return csr.ResolutionEntity(
        entity_id=entity_id,
        org_id="org_a",
        entity_type="system",
        display_name=name,
        canonical_name=" ".join(name.split()).lower(),
        source_system=source,
        source_record_id=record_id,
    )


def _proposal_decision(subject, target, *, corroborating: bool = True):
    """A T1 decision carrying exactly one tier-3 proposal."""
    rels = (
        csr.build_relationship_index([
            {"from_entity_id": subject.entity_id, "to_entity_id": "team-1",
             "relationship_type": "owns", "inferred": False},
            {"from_entity_id": target.entity_id, "to_entity_id": "team-1",
             "relationship_type": "owns", "inferred": False},
        ])
        if corroborating
        else csr.EMPTY_RELATIONSHIP_INDEX
    )
    return csr.resolve_entity(subject, [target], relationship_index=rels)


# ── the pair id: one question per pair ──────────────────────────────────────


def test_the_proposal_id_is_order_independent():
    """A→B and B→A are the same question — the engine asks it both ways."""
    assert emp.proposal_id_for("system", "a", "b") == emp.proposal_id_for("system", "b", "a")


def test_the_proposal_id_is_deterministic_across_runs():
    first = emp.proposal_id_for("system", "a", "b")
    assert first == emp.proposal_id_for("system", "a", "b")
    assert first.startswith("emp_")


def test_the_entity_type_is_part_of_the_id():
    assert emp.proposal_id_for("system", "a", "b") != emp.proposal_id_for("team", "a", "b")


def test_a_different_pair_gets_a_different_id():
    assert emp.proposal_id_for("system", "a", "b") != emp.proposal_id_for("system", "a", "c")


@pytest.mark.parametrize("left,right", [("", "b"), ("a", ""), ("a", "a")])
def test_an_unusable_pair_is_refused(left, right):
    with pytest.raises(emp.ProposalDecisionError):
        emp.proposal_id_for("system", left, right)


def test_the_stored_pair_is_sorted():
    assert emp.sorted_pair("b", "a") == ("a", "b")
    assert emp.sorted_pair("a", "b") == ("a", "b")


# ── the evidence snapshot ───────────────────────────────────────────────────


def test_the_evidence_snapshot_carries_what_a_reviewer_needs():
    """A reviewer must be able to answer "are these the same thing?" from the
    card alone: what each is called, in which system, which record, and why the
    engine thought they might be one."""
    subject = _entity("e1", "Payments", source="servicenow", record_id="sn-1")
    target = _entity("e2", "payments", source="jira", record_id="PAY")
    decision = _proposal_decision(subject, target)
    assert decision.status == csr.STATUS_PROPOSED

    snapshot = emp.build_proposal_evidence(subject, decision.proposals[0])

    assert snapshot["subject"]["display_name"] == "Payments"
    assert snapshot["subject"]["source_system"] == "servicenow"
    assert snapshot["subject"]["source_record_id"] == "sn-1"
    assert snapshot["target"]["display_name"] == "payments"
    assert snapshot["target"]["source_system"] == "jira"
    assert snapshot["tier"] == csr.TIER_NAME_SIMILARITY
    assert snapshot["reason"]
    assert snapshot["corroborating_relationships"] == [
        {"relationship_type": "owns", "entity_id": "team-1"}
    ]


def test_the_snapshot_survives_an_entity_with_no_stable_record_id():
    subject = csr.ResolutionEntity(
        entity_id="e1", org_id="org_a", entity_type="system",
        display_name="Payments", canonical_name="payments",
        source_system="servicenow", source_record_id=None,
    )
    target = _entity("e2", "payments", source="jira")
    snapshot = emp.build_proposal_evidence(
        subject, _proposal_decision(subject, target).proposals[0]
    )
    assert snapshot["subject"]["source_record_id"] is None
    assert snapshot["target"]["source_record_id"] == "r1"


# ── record_proposals: what reaches the queue ────────────────────────────────


@pytest.fixture
def captured(monkeypatch):
    """Capture the rows record_proposals would write, without a database."""
    calls: List[Dict[str, Any]] = []

    class _Cur:
        def execute(self, sql, params=None):
            calls.append({"sql": " ".join(str(sql).split()), "params": params})

        def fetchone(self):
            return [True]  # every upsert reports "inserted"

    class _Con:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(emp.db, "connect", lambda: _Con())
    return calls


def test_only_proposals_reach_the_queue(captured):
    """An auto-merge decision has nothing to review, and an unresolved one has
    nothing to show."""
    merger = _entity("e1", "Payments Platform", source="servicenow", record_id="sn-1")
    merger = csr.ResolutionEntity(
        **{**merger.__dict__,
           "cross_references": csr.extract_cross_references(
               {"external_ids": {"jira": "PAY"}}, own_system="servicenow")},
    )
    merge_target = _entity("e2", "Payments", source="jira", record_id="PAY")
    merge_decision = csr.resolve_entity(merger, [merge_target])
    assert merge_decision.is_merge is True

    lonely = _entity("e3", "Billing", source="servicenow", record_id="sn-2")
    unresolved = csr.resolve_entity(lonely, [])

    outcome = emp.record_proposals("org_a", [merge_decision, unresolved])

    assert outcome.created == 0
    assert outcome.proposal_ids == ()
    assert captured == [], "no write at all when there is nothing to review"


def test_the_symmetric_pair_is_recorded_once(captured):
    """The engine proposes A→B and B→A; the queue must hold ONE question."""
    a = _entity("e1", "Payments", source="servicenow", record_id="sn-1")
    b = _entity("e2", "payments", source="jira", record_id="PAY")
    rels = csr.build_relationship_index([
        {"from_entity_id": "e1", "to_entity_id": "t", "relationship_type": "owns",
         "inferred": False},
        {"from_entity_id": "e2", "to_entity_id": "t", "relationship_type": "owns",
         "inferred": False},
    ])
    decisions = csr.resolve_entities([a, b], [a, b], relationship_index=rels)
    assert all(d.status == csr.STATUS_PROPOSED for d in decisions)

    outcome = emp.record_proposals("org_a", decisions)

    assert outcome.created == 1
    assert len(outcome.proposal_ids) == 1
    assert len(captured) == 1


def test_the_stored_row_is_pending_and_sorted(captured):
    a = _entity("e2", "Payments", source="servicenow", record_id="sn-1")
    b = _entity("e1", "payments", source="jira", record_id="PAY")
    emp.record_proposals("org_a", [_proposal_decision(a, b)])

    params = captured[0]["params"]
    assert params[0] == "org_a"
    assert params[3:5] == ("e1", "e2"), "the pair is stored sorted"
    assert emp.STATUS_PENDING in params
    assert params[5] == csr.TIER_NAME_SIMILARITY


def test_the_upsert_refuses_to_touch_a_decided_row(captured):
    """Rule 2 in SQL: the ON CONFLICT update is gated on status = 'pending', so a
    later pass can never revert an answer or overwrite the evidence it was given
    against."""
    a = _entity("e1", "Payments", source="servicenow", record_id="sn-1")
    b = _entity("e2", "payments", source="jira", record_id="PAY")
    emp.record_proposals("org_a", [_proposal_decision(a, b)])

    sql = captured[0]["sql"]
    assert "ON CONFLICT (org_id, proposal_id) DO UPDATE" in sql
    assert "WHERE entity_match_proposals.status = %s" in sql
    assert "RETURNING (xmax = 0)" in sql


def test_an_empty_batch_writes_nothing(captured):
    assert emp.record_proposals("org_a", []).created == 0
    assert captured == []


def test_a_blank_org_is_refused():
    with pytest.raises(emp.ProposalDecisionError):
        emp.record_proposals("", [])


# ── decision contract ───────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["confirm", "reject"])
def test_the_two_valid_actions_map_to_terminal_statuses(action):
    assert action in emp.DECISION_ACTIONS
    assert emp._STATUS_FOR_ACTION[action] in (emp.STATUS_CONFIRMED, emp.STATUS_REJECTED)


def test_there_is_no_defer_action():
    """A proposal that is neither confirmed nor rejected is already pending —
    deferring is what happens when the reviewer does nothing."""
    assert set(emp.DECISION_ACTIONS) == {"confirm", "reject"}


def test_statuses_are_the_three_the_surface_renders():
    assert set(emp.PROPOSAL_STATUSES) == {"pending", "confirmed", "rejected"}


def test_person_entities_are_not_scanned():
    """Two people sharing a name is the weakest evidence and the highest-risk
    merge in the platform — those questions must not fill the queue."""
    assert "person" not in emp.SCANNABLE_ENTITY_TYPES
    assert "system" in emp.SCANNABLE_ENTITY_TYPES


def test_a_note_is_bounded_and_blank_is_none():
    assert emp._clean_note("  ") is None
    assert emp._clean_note(None) is None
    assert len(emp._clean_note("x" * 5000)) == emp._MAX_NOTE_CHARS


# ── the store never merges ──────────────────────────────────────────────────


def test_the_store_never_writes_to_the_graph():
    """Confirming records an answer; merging is a separate, irreversible step.
    A grep-level guard because the damage is invisible if it ever slips in."""
    import inspect

    source = inspect.getsource(emp)
    for forbidden in (
        "UPDATE entities",
        "INSERT INTO entities",
        "DELETE FROM entities",
        "INSERT INTO entity_relationships",
        "UPDATE entity_relationships",
        "resolve_or_create_entity",
    ):
        assert forbidden not in source, (
            f"the proposal store must not touch the graph, found {forbidden!r}"
        )


def test_confirmed_pairs_is_the_handoff_for_a_merge_applier():
    """The read a later merge step consumes — proof the boundary is deliberate
    rather than an omission."""
    assert callable(emp.confirmed_pairs)
    assert "merge applier" in (emp.confirmed_pairs.__doc__ or "").lower()


# ── route logic ─────────────────────────────────────────────────────────────


def test_the_audit_event_type_is_registered():
    from app.middleware.audit import AUDIT_EVENT_REGISTRY, ENTITY_MATCH_PROPOSAL_DECIDED

    assert ENTITY_MATCH_PROPOSAL_DECIDED in AUDIT_EVENT_REGISTRY


def test_a_decision_is_audited_with_actor_pair_and_transition(monkeypatch):
    from app import routes_entity_match_proposals as routes

    events: List[Any] = []
    monkeypatch.setattr(
        "app.middleware.audit.log_event", lambda et, **kw: events.append((et, kw))
    )

    proposal = emp.EntityMatchProposal(
        org_id="org_a", proposal_id="emp_x", entity_type="system",
        left_entity_id="e1", right_entity_id="e2",
        tier=csr.TIER_NAME_SIMILARITY, confidence=0.7, status=emp.STATUS_CONFIRMED,
    )
    outcome = emp.DecisionOutcome(
        proposal=proposal, action="confirm", previous_status="pending",
        resulting_status="confirmed", revision=1, changed=True,
        actor_id="analyst-1", decided_at="2026-08-03T10:00:00+00:00",
    )

    routes._audit_decision("org_a", "analyst-1", outcome)

    assert len(events) == 1
    event_type, payload = events[0]
    assert event_type == "entity_match_proposal_decided"
    assert payload["user_id"] == "analyst-1"
    assert payload["org_id"] == "org_a"
    assert payload["proposal_id"] == "emp_x"
    assert payload["left_entity_id"] == "e1"
    assert payload["right_entity_id"] == "e2"
    assert payload["action"] == "confirm"
    assert payload["previous_status"] == "pending"
    assert payload["resulting_status"] == "confirmed"
    assert payload["timestamp"]


def test_an_audit_failure_never_fails_the_decision(monkeypatch):
    """The decision is already persisted in its own history; a broken audit store
    must not turn a recorded answer into a 500."""
    from app import routes_entity_match_proposals as routes

    def _boom(*_a, **_k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr("app.middleware.audit.log_event", _boom)
    outcome = emp.DecisionOutcome(
        proposal=emp.EntityMatchProposal(
            org_id="org_a", proposal_id="emp_x", entity_type="system",
            left_entity_id="e1", right_entity_id="e2", tier="name_similarity",
            confidence=0.7, status="confirmed",
        ),
        action="confirm", previous_status="pending", resulting_status="confirmed",
        revision=1, changed=True, actor_id="a", decided_at="2026-08-03T10:00:00+00:00",
    )
    routes._audit_decision("org_a", "a", outcome)  # must not raise


def test_the_routes_are_registered_and_analyst_gated():
    """A viewer has nothing actionable on a review surface, and an unauthenticated
    caller must not read another org's identity questions."""
    import inspect

    from app import routes_entity_match_proposals as routes

    source = inspect.getsource(routes)
    assert source.count('require_role("analyst")') == 4, "every route is analyst+"
    assert "get_current_org_id()" in source
    assert "org_id" not in inspect.signature(
        routes.ProposalDecisionRequest.__init__
    ).parameters, "the org is never taken from the request body"


def test_the_decision_request_accepts_only_an_action_and_a_note():
    from app.routes_entity_match_proposals import ProposalDecisionRequest

    body = ProposalDecisionRequest(action="confirm", note="  same service  ")
    assert body.action == "confirm"
    assert ProposalDecisionRequest(action="reject").note is None
