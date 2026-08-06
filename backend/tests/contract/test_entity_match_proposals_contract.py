"""2.0-B2 T3 contract tests — the review surface end to end.

The DB-free half (pair id, evidence snapshot, audit record, route gating) is in
``tests/unit/test_entity_match_proposals.py``. This file drives the REAL routes,
the real tenancy/RBAC middleware, and the real tables, because the properties
that matter most only exist in SQL:

  * a proposed pair appears in the queue WITH the evidence behind it, and an
    auto-merged pair never does (AC1 at the surface);
  * confirm/reject is recorded, durable, and the pair is never re-proposed — a
    later scan leaves an answered question alone (the story's item 3);
  * decisions are append-only: reversing one adds a row rather than editing one;
  * a confirmation records an answer and does NOT merge the graph;
  * two-org isolation across the queue, the decision, and the history.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from app import cross_source_resolution as csr
from app import db
from app import entity_match_proposals as emp

ORG = "default"          # the dev token's org — the routes resolve it server-side
OTHER_ORG = "emp-org-b"
RUN = "run_emp_t3"


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


def _insert_edge(from_id: str, to_id: str, rel: str, *, org_id: str = ORG) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO entity_relationships (
                id, org_id, from_entity_id, to_entity_id, relationship_type,
                confidence, inferred, evidence, first_seen_run_id,
                last_seen_run_id, run_count, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,FALSE,NULL,%s,%s,1,%s)
            ON CONFLICT (org_id, from_entity_id, to_entity_id, relationship_type)
            DO NOTHING
            """,
            (
                str(uuid.uuid4()), org_id, from_id, to_id, rel, 0.9, RUN, RUN,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        con.commit()
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
    """A seeded estate with exactly one reviewable question.

      name-only pair  — ServiceNow 'Billing' and git 'billing' share a name and a
                        corroborating observed relationship → PROPOSED;
      referenced pair — ServiceNow cites the Jira record's id → AUTO-MERGED, so it
                        must never reach the review queue.
    """
    _cleanup()
    ids = {
        "sn_billing": _insert_entity("Billing", source_system="servicenow",
                                     source_record_id="sn-2"),
        "git_billing": _insert_entity("billing", source_system="git",
                                      source_record_id="repo-1"),
        "team": _insert_entity("Platform Team", source_system="servicenow",
                               source_record_id="sn-team"),
        "sn_payments": _insert_entity(
            "Payments Platform", source_system="servicenow", source_record_id="sn-1",
            metadata={"cross_references": [{"system": "jira", "record_id": "PAY"}]},
        ),
        "jira_payments": _insert_entity("Payments", source_system="jira",
                                        source_record_id="PAY"),
    }
    _insert_edge(ids["sn_billing"], ids["team"], "depends_on")
    _insert_edge(ids["git_billing"], ids["team"], "depends_on")
    yield ids
    _cleanup()


def _scan(client: TestClient) -> Dict[str, Any]:
    r = client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    assert r.status_code == 200, r.text
    return r.json()


def _queue(client: TestClient, status: Optional[str] = None) -> Dict[str, Any]:
    path = "/api/entity-match-proposals"
    if status:
        path += f"?status={status}"
    r = client.get(path, headers=_auth())
    assert r.status_code == 200, r.text
    return r.json()


# ── AC1 at the surface ──────────────────────────────────────────────────────


def test_ac1_only_the_name_match_reaches_the_review_queue(client: TestClient, estate):
    """Explicit cross-references resolve automatically and are never queued; the
    name-only pair is proposed and IS queued."""
    scan = _scan(client)
    assert scan["created"] == 1, scan

    data = _queue(client, "pending")
    assert len(data["proposals"]) == 1
    proposal = data["proposals"][0]
    assert proposal["status"] == "pending"
    assert proposal["tier"] == csr.TIER_NAME_SIMILARITY
    assert {proposal["left_entity_id"], proposal["right_entity_id"]} == {
        estate["sn_billing"], estate["git_billing"]
    }

    queued = {
        eid
        for p in data["proposals"]
        for eid in (p["left_entity_id"], p["right_entity_id"])
    }
    assert estate["sn_payments"] not in queued, (
        "an auto-merged pair has nothing to review and must not be queued"
    )


def test_the_queued_proposal_carries_the_evidence_for_the_decision(
    client: TestClient, estate
):
    _scan(client)
    proposal = _queue(client, "pending")["proposals"][0]
    evidence = proposal["evidence"]

    # Both sides, named in their own system.
    sides = {evidence["subject"]["source_system"], evidence["target"]["source_system"]}
    assert sides == {"servicenow", "git"}
    assert evidence["subject"]["source_record_id"]
    assert evidence["target"]["source_record_id"]
    # And WHY — the reason plus the corroborating relationship.
    assert evidence["reason"]
    assert evidence["corroborating_relationships"] == [
        {"relationship_type": "depends_on", "entity_id": estate["team"]}
    ]


def test_the_symmetric_pair_is_one_question(client: TestClient, estate):
    """The engine resolves both entities, so it proposes the pair twice."""
    _scan(client)
    assert len(_queue(client, "pending")["proposals"]) == 1


def test_a_rescan_does_not_duplicate_the_queue(client: TestClient, estate):
    _scan(client)
    second = _scan(client)
    assert second["created"] == 0
    assert second["refreshed"] == 1
    assert len(_queue(client, "pending")["proposals"]) == 1


# ── confirm / reject: recorded and durable ──────────────────────────────────


def _one_pending_id(client: TestClient) -> str:
    _scan(client)
    return _queue(client, "pending")["proposals"][0]["proposal_id"]


@pytest.mark.parametrize("action,expected", [("confirm", "confirmed"), ("reject", "rejected")])
def test_a_decision_is_recorded_with_its_actor(client: TestClient, estate, action, expected):
    pid = _one_pending_id(client)

    r = client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": action, "note": "reviewed"},
    )
    assert r.status_code == 200, r.text
    outcome = r.json()
    assert outcome["changed"] is True
    assert outcome["previous_status"] == "pending"
    assert outcome["resulting_status"] == expected
    assert outcome["proposal"]["decided_by"]
    assert outcome["proposal"]["note"] == "reviewed"

    assert _queue(client, "pending")["proposals"] == []
    assert len(_queue(client, expected)["proposals"]) == 1


def test_an_answered_question_is_never_asked_again(client: TestClient, estate):
    """The story's item 3, and the property that makes the queue finishable."""
    pid = _one_pending_id(client)
    client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": "reject"},
    )

    rescan = _scan(client)

    assert rescan["created"] == 0
    assert rescan["refreshed"] == 0
    assert rescan["skipped_already_decided"] == 1, (
        "the pair was proposed again and deliberately left alone — reported, not hidden"
    )
    assert _queue(client, "pending")["proposals"] == []
    assert _queue(client, "rejected")["proposals"][0]["proposal_id"] == pid


def test_a_rescan_never_overwrites_the_evidence_a_decision_was_given_against(
    client: TestClient, estate
):
    pid = _one_pending_id(client)
    before = client.get(f"/api/entity-match-proposals/{pid}", headers=_auth()).json()
    client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": "confirm"},
    )
    _scan(client)
    after = client.get(f"/api/entity-match-proposals/{pid}", headers=_auth()).json()

    assert after["proposal"]["evidence"] == before["proposal"]["evidence"]
    assert after["proposal"]["status"] == "confirmed"


def test_repeating_a_decision_is_idempotent(client: TestClient, estate):
    pid = _one_pending_id(client)
    path = f"/api/entity-match-proposals/{pid}/decision"
    first = client.post(path, headers=_auth(), json={"action": "confirm"}).json()
    second = client.post(path, headers=_auth(), json={"action": "confirm"}).json()

    assert first["changed"] is True
    assert second["changed"] is False
    assert second["revision"] == first["revision"], "no duplicate history row"
    history = client.get(f"/api/entity-match-proposals/{pid}", headers=_auth()).json()
    assert len(history["history"]) == 1


def test_reversing_a_decision_appends_rather_than_rewrites(client: TestClient, estate):
    """An analyst who mis-clicked must be able to correct it — and the original
    answer stays in the record."""
    pid = _one_pending_id(client)
    path = f"/api/entity-match-proposals/{pid}/decision"
    client.post(path, headers=_auth(), json={"action": "confirm"})
    client.post(path, headers=_auth(), json={"action": "reject"})

    detail = client.get(f"/api/entity-match-proposals/{pid}", headers=_auth()).json()
    assert detail["proposal"]["status"] == "rejected"
    assert detail["proposal"]["revision"] == 2
    history = detail["history"]
    assert [h["action"] for h in history] == ["reject", "confirm"], "newest first"
    assert history[1]["previous_status"] == "pending"
    assert history[0]["previous_status"] == "confirmed"


def test_counts_cover_every_status(client: TestClient, estate):
    pid = _one_pending_id(client)
    assert _queue(client)["counts"] == {"pending": 1, "confirmed": 0, "rejected": 0}
    client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": "confirm"},
    )
    assert _queue(client)["counts"] == {"pending": 0, "confirmed": 1, "rejected": 0}


# ── confirming records; it does not merge ───────────────────────────────────


def test_confirming_records_an_answer_and_does_not_merge_the_graph(
    client: TestClient, estate
):
    """The boundary the review copy promises: nothing about the two entities
    changes when a match is confirmed."""
    def _snapshot():
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT id, canonical_name, display_name, resolution_status, "
                "resolution_confidence, metadata FROM entities "
                "WHERE org_id = %s AND first_seen_run_id = %s ORDER BY id",
                (ORG, RUN),
            )
            return [tuple(r) for r in cur.fetchall()]
        finally:
            con.close()

    pid = _one_pending_id(client)
    before = _snapshot()
    client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": "confirm"},
    )
    assert _snapshot() == before

    # ...but the confirmation IS available to a later merge applier.
    assert (
        "system", *sorted((estate["sn_billing"], estate["git_billing"]))
    ) in emp.confirmed_pairs(ORG)


# ── audit ───────────────────────────────────────────────────────────────────


def test_a_decision_writes_an_audit_row(client: TestClient, estate):
    pid = _one_pending_id(client)
    client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": "confirm"},
    )

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT user_id, payload FROM audit_log "
            "WHERE org_id = %s AND event_type = 'entity_match_proposal_decided' "
            "ORDER BY timestamp DESC LIMIT 1",
            (ORG,),
        )
        row = cur.fetchone()
    finally:
        con.close()

    assert row is not None, "a human identity decision must reach the audit trail"
    payload = json.loads(row[1]) if isinstance(row[1], str) else row[1]
    assert row[0] == os.getenv("DEV_JWT", "dev-token-change-me")
    assert payload["proposal_id"] == pid
    assert payload["action"] == "confirm"
    assert payload["resulting_status"] == "confirmed"


# ── validation / errors ─────────────────────────────────────────────────────


def test_an_unknown_proposal_is_404(client: TestClient):
    r = client.get("/api/entity-match-proposals/emp_missing", headers=_auth())
    assert r.status_code == 404
    r = client.post(
        "/api/entity-match-proposals/emp_missing/decision",
        headers=_auth(), json={"action": "confirm"},
    )
    assert r.status_code == 404


def test_an_unknown_action_is_400(client: TestClient, estate):
    pid = _one_pending_id(client)
    r = client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": "maybe"},
    )
    assert r.status_code == 400
    assert "confirm" in r.text


def test_an_unknown_status_filter_is_400(client: TestClient):
    r = client.get("/api/entity-match-proposals?status=everything", headers=_auth())
    assert r.status_code == 400


# ── RBAC + tenancy ──────────────────────────────────────────────────────────


def test_unauthenticated_requests_are_rejected(client: TestClient):
    assert client.get("/api/entity-match-proposals").status_code == 401
    assert client.post(
        "/api/entity-match-proposals/emp_x/decision", json={"action": "confirm"}
    ).status_code == 401


def test_a_viewer_cannot_read_or_decide(client: TestClient, estate):
    """A review surface is an analyst+ write workflow — a viewer has nothing
    actionable there."""
    viewer = {"Authorization": f"Bearer {os.getenv('VIEWER_JWT', 'viewer-token')}"}
    pid = _one_pending_id(client)

    assert client.get("/api/entity-match-proposals", headers=viewer).status_code == 403
    assert client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=viewer, json={"action": "confirm"},
    ).status_code == 403
    assert client.post(
        "/api/entity-match-proposals/scan", headers=viewer, json={}
    ).status_code == 403
    # The gate DISCRIMINATES: analyst+ still succeeds.
    assert client.get("/api/entity-match-proposals", headers=_auth()).status_code == 200


def test_another_orgs_proposal_is_indistinguishable_from_a_typo(client: TestClient, estate):
    """A 403-that-exists would confirm the id belongs to someone."""
    pid = emp.proposal_id_for("system", "x-1", "x-2")
    now = datetime.now(timezone.utc).isoformat()
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO entity_match_proposals (
                org_id, proposal_id, entity_type, left_entity_id, right_entity_id,
                tier, confidence, status, evidence_payload, revision,
                first_proposed_at, last_proposed_at, created_at, updated_at
            ) VALUES (%s,%s,'system','x-1','x-2','name_similarity',0.7,'pending','{}',0,
                      %s,%s,%s,%s)
            """,
            (OTHER_ORG, pid, now, now, now, now),
        )
        con.commit()
    finally:
        con.close()

    assert client.get(f"/api/entity-match-proposals/{pid}", headers=_auth()).status_code == 404
    assert client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": "confirm"},
    ).status_code == 404
    # ...and it is absent from this org's queue and counts.
    data = _queue(client)
    assert all(p["proposal_id"] != pid for p in data["proposals"])


def test_one_orgs_decision_never_touches_another_orgs_row(client: TestClient, estate):
    pid = _one_pending_id(client)
    client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": "confirm"},
    )
    assert emp.list_proposals(OTHER_ORG) == []
    assert emp.confirmed_pairs(OTHER_ORG) == []
