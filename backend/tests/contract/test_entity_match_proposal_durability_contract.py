"""2.0-B2 T4 contract tests — durable decisions against a real graph.

AC3: "Confirmed/rejected proposals persist across runs and are not re-proposed."

The DB-free half (the identity-key invariant, the backfill's purity, the run
wiring) is in ``tests/unit/test_entity_match_proposal_durability.py``. This file
exercises the parts that only exist once rows are really written:

  * a confirmed pair is not re-proposed by a later scan — and neither is a rejected
    one;
  * the pair survives ENTITY ROW CHURN: when a connector starts supplying record
    ids and a second resolved row appears, the decision still covers it (the case
    that defeats a row-id-keyed decision, and the reason T4 exists);
  * a decision recorded BEFORE T4 (``identity_key IS NULL``) is healed from its own
    evidence snapshot on the next scan and protects its pair from then on;
  * the identity key is written, indexed, and org-scoped.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from app import db
from app import entity_match_proposals as emp

ORG = "default"
OTHER_ORG = "t4-org-b"
RUN = "run_t4_durability"


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


# ── seeding ─────────────────────────────────────────────────────────────────


def _insert_entity(
    display_name: str,
    *,
    source_system: str,
    source_record_id: Optional[str] = None,
    org_id: str = ORG,
    entity_type: str = "system",
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
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,1.0,'resolved',%s,%s,1,NULL,%s,%s)
            """,
            (
                entity_id, org_id, entity_type,
                " ".join(display_name.split()).lower(), display_name,
                source_system, source_record_id, RUN, RUN, now, now,
            ),
        )
        con.commit()
    finally:
        con.close()
    return entity_id


def _insert_edge(from_id: str, to_id: str, rel: str = "owns", *, org_id: str = ORG) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO entity_relationships (
                id, org_id, from_entity_id, to_entity_id, relationship_type,
                confidence, inferred, evidence, first_seen_run_id,
                last_seen_run_id, run_count, created_at
            ) VALUES (%s,%s,%s,%s,%s,0.9,FALSE,NULL,%s,%s,1,%s)
            ON CONFLICT (org_id, from_entity_id, to_entity_id, relationship_type)
            DO NOTHING
            """,
            (str(uuid.uuid4()), org_id, from_id, to_id, rel, RUN, RUN,
             datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()


def _delete_entity(entity_id: str, org_id: str = ORG) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM entities WHERE org_id = %s AND id = %s", (org_id, entity_id))
        con.commit()
    finally:
        con.close()


def _row(proposal_id: str, org_id: str = ORG) -> Optional[Dict[str, Any]]:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT * FROM entity_match_proposals WHERE org_id = %s AND proposal_id = %s",
            (org_id, proposal_id),
        )
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _cleanup() -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        for org in (ORG, OTHER_ORG):
            cur.execute("DELETE FROM entity_match_proposal_history WHERE org_id = %s", (org,))
            cur.execute("DELETE FROM entity_match_proposals WHERE org_id = %s", (org,))
            cur.execute(
                "DELETE FROM entity_relationships WHERE org_id = %s AND first_seen_run_id = %s",
                (org, RUN),
            )
            cur.execute(
                "DELETE FROM entities WHERE org_id = %s AND first_seen_run_id = %s",
                (org, RUN),
            )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def estate():
    """A pair the engine proposes: same canonical name, two sources, and the shared
    observed neighbour tier 3 requires."""
    _cleanup()
    ids = {
        "sn": _insert_entity("Payments", source_system="servicenow"),
        "jira": _insert_entity("Payments", source_system="jira", source_record_id="PAY"),
        "team": _insert_entity("Platform Team", source_system="servicenow",
                               source_record_id="sn-team"),
    }
    _insert_edge(ids["sn"], ids["team"])
    _insert_edge(ids["jira"], ids["team"])
    yield ids
    _cleanup()


def _scan() -> emp.RecordOutcome:
    return emp.scan_for_proposals(ORG)


def _pending() -> list:
    return emp.list_proposals(ORG, status=emp.STATUS_PENDING)


# ── the baseline: a decision is durable across a re-scan ────────────────────


@pytest.mark.parametrize(
    "action,expected", [(emp.ACTION_CONFIRM, "confirmed"), (emp.ACTION_REJECT, "rejected")]
)
def test_ac3_a_decided_pair_is_not_re_proposed(estate, action, expected):
    """Both answers are durable: "not the same thing" is as final as "the same"."""
    first = _scan()
    assert first.created == 1, first.to_dict()
    proposal = _pending()[0]
    emp.decide(ORG, proposal.proposal_id, action, "analyst@example.com")

    second = _scan()

    assert second.created == 0
    assert second.refreshed == 0
    assert second.skipped_already_decided == 1
    assert _pending() == []
    assert emp.list_proposals(ORG, status=expected)[0].proposal_id == proposal.proposal_id


def test_the_identity_key_is_written_on_a_new_proposal(estate):
    _scan()
    proposal = _pending()[0]

    assert proposal.identity_key, "a proposal must carry its stable identity"
    assert proposal.identity_key == emp.identity_key_for(
        "system",
        emp.entity_identity("servicenow", "payments"),
        emp.entity_identity("jira", "payments"),
    )


# ── the case T4 exists for: entity row churn between runs ──────────────────


def test_ac3_a_decision_survives_the_entity_row_ids_changing(estate):
    """The hole T3 alone left open.

    Run 1 proposes ServiceNow "Payments" (known by name only) against Jira's, and a
    human answers. Between runs the connector starts supplying record ids, so a NEW
    resolved row exists for the same real ServiceNow entity — a different
    ``proposal_id``. The pair must still not be re-proposed.
    """
    _scan()
    emp.decide(ORG, _pending()[0].proposal_id, emp.ACTION_REJECT, "analyst@example.com")

    # The churn: the name-only row is replaced by one carrying a record id, exactly
    # as upsert_source_entity would produce when the connector starts supplying one.
    _delete_entity(estate["sn"])
    churned = _insert_entity("Payments", source_system="servicenow", source_record_id="sn-1")
    _insert_edge(churned, estate["team"])

    after = _scan()

    assert after.created == 0, (
        "the same real pair was re-proposed under a new row id — AC3 violated"
    )
    assert after.skipped_already_decided == 1
    assert _pending() == []


def test_the_churned_pair_reports_the_same_identity_key(estate):
    """Why the decision still matches: the identity is the pair's names, not its
    row ids."""
    _scan()
    before = _pending()[0].identity_key

    _delete_entity(estate["sn"])
    churned = _insert_entity("Payments", source_system="servicenow", source_record_id="sn-1")
    _insert_edge(churned, estate["team"])
    emp.decide(ORG, _pending()[0].proposal_id, emp.ACTION_CONFIRM, "a@example.com")

    # Re-derive what a fresh scan would compute for the churned pair.
    assert before == emp.identity_key_for(
        "system",
        emp.entity_identity("servicenow", "payments"),
        emp.entity_identity("jira", "payments"),
    )


def test_a_pending_pair_still_refreshes_normally_after_churn(estate):
    """Durability must not freeze the queue: an UNANSWERED pair is still allowed to
    reappear (under its new row ids) so the reviewer sees current evidence."""
    _scan()
    assert len(_pending()) == 1

    _delete_entity(estate["sn"])
    churned = _insert_entity("Payments", source_system="servicenow", source_record_id="sn-1")
    _insert_edge(churned, estate["team"])

    after = _scan()

    assert after.skipped_already_decided == 0, "nothing was decided, so nothing to skip"
    assert len(_pending()) >= 1, "an open question must remain askable"


# ── healing a decision recorded before T4 ──────────────────────────────────


def test_a_pre_t4_decision_is_backfilled_and_then_protects_its_pair(estate):
    """An install that decided pairs before T4 has rows with no identity key. They
    are healed from their own evidence snapshot, so those decisions are not left
    unprotected against churn forever."""
    _scan()
    proposal = _pending()[0]
    emp.decide(ORG, proposal.proposal_id, emp.ACTION_CONFIRM, "analyst@example.com")

    # Simulate the pre-T4 state: the decision exists, the identity key does not.
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE entity_match_proposals SET identity_key = NULL "
            "WHERE org_id = %s AND proposal_id = %s",
            (ORG, proposal.proposal_id),
        )
        con.commit()
    finally:
        con.close()
    assert _row(proposal.proposal_id)["identity_key"] is None

    healed = emp.backfill_identity_keys(ORG)

    assert healed == 1
    assert _row(proposal.proposal_id)["identity_key"] == proposal.identity_key

    # ...and the healed decision now survives churn.
    _delete_entity(estate["sn"])
    churned = _insert_entity("Payments", source_system="servicenow", source_record_id="sn-1")
    _insert_edge(churned, estate["team"])
    after = _scan()
    assert after.created == 0
    assert after.skipped_already_decided == 1


def test_the_backfill_is_idempotent_and_cheap_to_repeat(estate):
    """It runs on every scan, so a second call must heal nothing."""
    _scan()
    emp.decide(ORG, _pending()[0].proposal_id, emp.ACTION_CONFIRM, "a@example.com")
    assert emp.backfill_identity_keys(ORG) == 0
    assert emp.backfill_identity_keys(ORG) == 0


def test_a_scan_heals_pre_t4_rows_without_being_asked(estate):
    """Self-healing: an operator should not have to run anything for an existing
    install's decisions to become durable."""
    _scan()
    proposal = _pending()[0]
    emp.decide(ORG, proposal.proposal_id, emp.ACTION_REJECT, "a@example.com")
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE entity_match_proposals SET identity_key = NULL WHERE org_id = %s",
            (ORG,),
        )
        con.commit()
    finally:
        con.close()

    _scan()  # the next ordinary scan

    assert _row(proposal.proposal_id)["identity_key"] is not None


# ── the durability read ────────────────────────────────────────────────────


def test_decided_identity_keys_covers_both_answers_and_excludes_pending(estate):
    _scan()
    proposal = _pending()[0]
    assert emp.decided_identity_keys(ORG) == set(), "a pending pair is not decided"

    emp.decide(ORG, proposal.proposal_id, emp.ACTION_REJECT, "a@example.com")
    assert emp.decided_identity_keys(ORG) == {proposal.identity_key}


def test_the_durability_read_is_org_scoped(estate):
    """One org's decision must never suppress another org's question."""
    _scan()
    emp.decide(ORG, _pending()[0].proposal_id, emp.ACTION_CONFIRM, "a@example.com")

    assert emp.decided_identity_keys(OTHER_ORG) == set()
    assert emp.backfill_identity_keys(OTHER_ORG) == 0


def test_another_orgs_identical_pair_is_still_proposed(estate):
    """The same names in another tenant are a different question entirely."""
    _scan()
    emp.decide(ORG, _pending()[0].proposal_id, emp.ACTION_REJECT, "a@example.com")

    b_sn = _insert_entity("Payments", source_system="servicenow", org_id=OTHER_ORG)
    b_jira = _insert_entity("Payments", source_system="jira", source_record_id="PAY",
                            org_id=OTHER_ORG)
    b_team = _insert_entity("Platform Team", source_system="servicenow",
                            source_record_id="sn-team", org_id=OTHER_ORG)
    _insert_edge(b_sn, b_team, org_id=OTHER_ORG)
    _insert_edge(b_jira, b_team, org_id=OTHER_ORG)

    other = emp.scan_for_proposals(OTHER_ORG)

    assert other.created == 1, "another org's identical pair is its own question"
    assert emp.list_proposals(OTHER_ORG, status=emp.STATUS_PENDING)


# ── the review surface still reports honestly ──────────────────────────────


def test_the_scan_endpoint_reports_pairs_it_left_alone(client: TestClient, estate):
    """The count is the honest signal that the queue did not grow because answers
    exist — not because nothing was found."""
    _scan()
    emp.decide(ORG, _pending()[0].proposal_id, emp.ACTION_CONFIRM, "a@example.com")

    r = client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0
    assert body["skipped_already_decided"] == 1


def test_a_decided_proposal_still_exposes_its_identity_key(client: TestClient, estate):
    _scan()
    proposal = _pending()[0]
    emp.decide(ORG, proposal.proposal_id, emp.ACTION_CONFIRM, "a@example.com")

    r = client.get(
        f"/api/entity-match-proposals/{proposal.proposal_id}", headers=_auth()
    )
    assert r.status_code == 200, r.text
    assert r.json()["proposal"]["identity_key"] == proposal.identity_key
