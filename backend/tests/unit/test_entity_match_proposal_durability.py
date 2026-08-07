"""2.0-B2 T4 — durable confirm/reject decisions (DB-free).

AC3: "Confirmed/rejected proposals persist across runs and are not re-proposed."

T3 already refuses to revert a decided row, which covers the easy case: the same
pair, the same entity row ids, a later scan. T4 exists because entity row ids do
NOT stay the same. The clearest case: a connector that starts supplying record ids
makes ``upsert_source_entity`` insert a NEW resolved row for an entity previously
known by name only, so the pair arrives with a different ``proposal_id`` and — on
T3's logic alone — gets asked again despite having been answered.

These tests pin the invariant that closes that: the decision is keyed on the
pair's STABLE source identity, which is the canonical name (the record id is the
part that churns), so it survives row churn between runs.

The end-to-end half — two real runs against a real graph, a real churn, and the
pre-T4 backfill — is in
``tests/contract/test_entity_match_proposal_durability_contract.py``.
"""
from __future__ import annotations


from typing import Any, Dict

import pytest

from app import cross_source_resolution as csr
from app import entity_match_proposals as emp


# ── the invariant: the identity key survives row churn ──────────────────────


def test_the_identity_key_survives_a_record_id_appearing():
    """The AC3 hole T4 closes, stated as a test.

    Run 1: ServiceNow "Payments" is known by name only. Run 2: the connector now
    supplies ``sn-1``, so a second resolved row exists for the same real thing. The
    pair's identity key must be IDENTICAL across the two, or the decision recorded
    in run 1 silently stops covering run 2.
    """
    jira = emp.entity_identity("jira", "Payments")
    run1 = emp.identity_key_for("system", emp.entity_identity("servicenow", "Payments"), jira)
    run2 = emp.identity_key_for("system", emp.entity_identity("servicenow", "Payments"), jira)

    assert run1 == run2


def test_the_record_id_is_deliberately_not_part_of_the_identity():
    """Counter-intuitive but load-bearing: keying on the record id would make the
    key change exactly when durability is needed. A test, so nobody "improves" it
    back."""
    import inspect

    source = inspect.getsource(emp.entity_identity)
    assert "source_record_id" not in inspect.signature(emp.entity_identity).parameters
    assert "CHURNS" in source or "churn" in source.lower()


def test_the_identity_key_is_order_independent():
    """A→B and B→A are one question, exactly as for proposal_id."""
    left = emp.entity_identity("servicenow", "Payments")
    right = emp.entity_identity("jira", "Payments")
    assert emp.identity_key_for("system", left, right) == emp.identity_key_for(
        "system", right, left
    )


def test_the_identity_key_is_scoped_to_the_entity_type():
    left = emp.entity_identity("servicenow", "Payments")
    right = emp.entity_identity("jira", "Payments")
    assert emp.identity_key_for("system", left, right) != emp.identity_key_for(
        "team", left, right
    )


def test_the_identity_key_normalises_names_like_the_entity_layer():
    """If the two layers disagreed about what a name is, a decision would not match
    its own pair."""
    assert emp.entity_identity("ServiceNow", "Payments  API") == emp.entity_identity(
        "servicenow", "payments api"
    )


def test_a_pair_sharing_one_source_identity_is_refused():
    """Two sides with the same identity is one entity, not a match."""
    same = emp.entity_identity("servicenow", "Payments")
    with pytest.raises(emp.ProposalDecisionError):
        emp.identity_key_for("system", same, same)


@pytest.mark.parametrize("left,right", [("", "b|name:x"), ("a|name:x", "")])
def test_an_incomplete_identity_pair_is_refused(left, right):
    with pytest.raises(emp.ProposalDecisionError):
        emp.identity_key_for("system", left, right)


def test_different_sources_with_the_same_name_are_distinct_identities():
    """The pair is only a pair because the two sides are different systems."""
    assert emp.entity_identity("servicenow", "Payments") != emp.entity_identity(
        "jira", "Payments"
    )


# ── backfilling a pre-T4 row from its own evidence snapshot ─────────────────


def _snapshot(sn_name="Payments", jira_name="Payments", entity_type="system"):
    return {
        "subject": {
            "entity_id": "e1", "display_name": sn_name, "canonical_name": sn_name.lower(),
            "entity_type": entity_type, "source_system": "servicenow",
            "source_record_id": "sn-1",
        },
        "target": {
            "entity_id": "e2", "display_name": jira_name, "canonical_name": jira_name.lower(),
            "entity_type": entity_type, "source_system": "jira",
            "source_record_id": "PAY",
        },
    }


def test_a_pre_t4_row_can_be_backfilled_from_its_snapshot():
    """The reason no data migration is needed: T3 already stored both sides' source
    system and name for the reviewer, which is exactly what the key is built from."""
    key = emp.identity_key_from_evidence(_snapshot())
    assert key is not None
    expected = emp.identity_key_for(
        "system",
        emp.entity_identity("servicenow", "Payments"),
        emp.entity_identity("jira", "Payments"),
    )
    assert key == expected


def test_a_backfilled_key_matches_the_live_pair_it_describes():
    """The property that makes the backfill worth doing: a decision recorded before
    T4 must protect the same pair the engine proposes now."""
    subject = csr.ResolutionEntity(
        entity_id="new-row-id", org_id="org_a", entity_type="system",
        display_name="Payments", canonical_name="payments",
        source_system="servicenow", source_record_id="sn-1",
    )
    target = csr.ResolutionEntity(
        entity_id="e2", org_id="org_a", entity_type="system",
        display_name="Payments", canonical_name="payments",
        source_system="jira", source_record_id="PAY",
    )
    live = emp.identity_key_for(
        "system", emp._identity_from_view(subject), emp._identity_from_view(target)
    )
    assert emp.identity_key_from_evidence(_snapshot()) == live


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        {},
        {"subject": {"source_system": "servicenow"}},          # no target
        {"subject": "not-a-mapping", "target": {}},
        {"subject": {"source_system": "servicenow", "canonical_name": "x"},
         "target": {"source_system": "servicenow", "canonical_name": "x"}},  # one entity
    ],
)
def test_an_unbackfillable_snapshot_yields_none_rather_than_a_wrong_key(snapshot):
    """A wrong key would silently suppress a DIFFERENT pair's question — worse than
    leaving the row unhealed."""
    assert emp.identity_key_from_evidence(snapshot) is None


def test_a_snapshot_with_only_a_display_name_still_backfills():
    snapshot = {
        "subject": {"source_system": "servicenow", "display_name": "Payments  API",
                    "entity_type": "system"},
        "target": {"source_system": "jira", "display_name": "payments api",
                   "entity_type": "system"},
    }
    assert emp.identity_key_from_evidence(snapshot) == emp.identity_key_for(
        "system",
        emp.entity_identity("servicenow", "payments api"),
        emp.entity_identity("jira", "payments api"),
    )


# ── record_proposals consults the durable decision ──────────────────────────


def _entity(entity_id: str, name: str, source: str, record_id=None):
    return csr.ResolutionEntity(
        entity_id=entity_id, org_id="org_a", entity_type="system",
        display_name=name, canonical_name=" ".join(name.split()).lower(),
        source_system=source, source_record_id=record_id,
    )


def _proposal_decision(subject, target):
    rels = csr.build_relationship_index([
        {"from_entity_id": subject.entity_id, "to_entity_id": "t",
         "relationship_type": "owns", "inferred": False},
        {"from_entity_id": target.entity_id, "to_entity_id": "t",
         "relationship_type": "owns", "inferred": False},
    ])
    return csr.resolve_entity(subject, [target], relationship_index=rels)


@pytest.fixture
def store(monkeypatch):
    """Capture writes and control what the durability reads return."""
    state: Dict[str, Any] = {"writes": [], "decided": set(), "backfilled": 0}

    class _Cur:
        def execute(self, sql, params=None):
            state["writes"].append({"sql": " ".join(str(sql).split()), "params": params})

        def fetchone(self):
            return [True]

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
    monkeypatch.setattr(emp, "decided_identity_keys", lambda org: state["decided"])

    def _backfill(org):
        state["backfilled"] += 1
        return 0

    monkeypatch.setattr(emp, "backfill_identity_keys", _backfill)
    return state


def test_a_new_pair_is_written_with_its_identity_key(store):
    a = _entity("e1", "Payments", "servicenow")
    b = _entity("e2", "payments", "jira")
    outcome = emp.record_proposals("org_a", [_proposal_decision(a, b)])

    assert outcome.created == 1
    inserts = [w for w in store["writes"] if "INSERT INTO entity_match_proposals" in w["sql"]]
    assert len(inserts) == 1
    assert "identity_key" in inserts[0]["sql"]
    expected = emp.identity_key_for(
        "system",
        emp.entity_identity("servicenow", "payments"),
        emp.entity_identity("jira", "payments"),
    )
    assert expected in inserts[0]["params"]


def test_an_already_decided_pair_is_not_written_even_with_new_row_ids(store):
    """AC3 end to end in the store: run 1's decision, run 2's churned row ids."""
    decided_key = emp.identity_key_for(
        "system",
        emp.entity_identity("servicenow", "payments"),
        emp.entity_identity("jira", "payments"),
    )
    store["decided"].add(decided_key)

    # Run 2: the ServiceNow side is a DIFFERENT row (a record id has appeared).
    a_new = _entity("brand-new-row-id", "Payments", "servicenow", record_id="sn-1")
    b = _entity("e2", "payments", "jira")
    outcome = emp.record_proposals("org_a", [_proposal_decision(a_new, b)])

    assert outcome.created == 0
    assert outcome.skipped_already_decided == 1, (
        "the answered pair must be counted as skipped, not silently dropped"
    )
    assert [w for w in store["writes"] if "INSERT INTO" in w["sql"]] == [], (
        "an answered question must not be written again"
    )


def test_the_pass_heals_pre_t4_rows_before_checking(store):
    """A decision recorded before T4 has no identity key, so the durability read
    would not see it — the pass backfills first, or that decision stays unprotected
    forever."""
    a = _entity("e1", "Payments", "servicenow")
    b = _entity("e2", "payments", "jira")
    emp.record_proposals("org_a", [_proposal_decision(a, b)])
    assert store["backfilled"] == 1


def test_nothing_is_healed_or_read_when_there_is_nothing_to_record(store):
    """No proposals means no reason to touch the database at all."""
    assert emp.record_proposals("org_a", []).created == 0
    assert store["backfilled"] == 0
    assert store["writes"] == []


def test_the_upsert_still_refuses_to_revert_a_decided_row(store):
    """T3's guarantee must survive T4: the status guard is the second line of
    defence behind the identity check."""
    a = _entity("e1", "Payments", "servicenow")
    b = _entity("e2", "payments", "jira")
    emp.record_proposals("org_a", [_proposal_decision(a, b)])
    sql = [w for w in store["writes"] if "INSERT INTO entity_match_proposals" in w["sql"]][0]["sql"]
    assert "WHERE entity_match_proposals.status = %s" in sql
    assert "identity_key     = EXCLUDED.identity_key" in sql or (
        "identity_key = EXCLUDED.identity_key" in sql
    )


# ── the run-level path ──────────────────────────────────────────────────────


def test_the_discovery_runner_scans_for_proposals():
    """AC3 says decisions persist "across runs" — which needs runs to produce
    proposals at all. Before T4 the only producer was a manual endpoint."""
    import inspect

    from discovery import runner

    source = inspect.getsource(runner)
    assert "scan_for_proposals" in source, (
        "the run must refresh the proposal queue, or 'across runs' is untested"
    )


def test_the_run_scan_is_non_blocking_and_runs_after_relationship_mapping():
    """It reads the observed edges the run just wrote (tier-3 corroboration), and a
    review queue is not worth failing a run over."""
    import inspect

    from discovery import runner

    source = inspect.getsource(runner)
    scan_at = source.index("scan_for_proposals")
    mapping_at = source.index("from app.relationship_mapper import map_relationships")
    assert mapping_at < scan_at, "the scan must run after relationship mapping"

    tail = source[scan_at - 900:scan_at + 900]
    assert "non-blocking" in tail.lower()
    assert "except Exception" in tail
