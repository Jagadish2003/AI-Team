"""2.0-B2 T2 contract tests — merged-entity provenance end to end.

AC2: "A resolved entity exposes all constituent source identities and the rule
that resolved it."

The DB-free half (provenance shape, survivor selection, rule attribution) is in
``tests/unit/test_entity_merge_provenance.py``. This file drives the REAL routes,
the real tenancy/RBAC middleware, and the real ``entities`` table, because the
properties that matter most only exist once a merge is actually written:

  * a merged entity exposes EVERY constituent source identity and the rule per
    constituent — through the dedicated provenance route AND through the entity
    read a finding surface already uses;
  * nothing is deleted: the constituent row, its identity, and its edges survive,
    which is what keeps the merge inspectable and (T4) reversible;
  * merges compose transitively and applying twice writes once;
  * only the auto-merge tiers and human-confirmed pairs merge — a name-similarity
    proposal never does;
  * every merge is audited, and two-org isolation holds.
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
from app import entity_merge as em

ORG = "default"
OTHER_ORG = "merge-org-b"
RUN = "run_merge_t2"


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
    created_at: Optional[str] = None,
) -> str:
    entity_id = str(uuid.uuid4())
    now = created_at or datetime.now(timezone.utc).isoformat()
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
            (str(uuid.uuid4()), org_id, from_id, to_id, rel, 0.9, RUN, RUN,
             datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()


def _entity_row(entity_id: str, org_id: str = ORG) -> Optional[Dict[str, Any]]:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT * FROM entities WHERE org_id = %s AND id = %s", (org_id, entity_id))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _metadata(entity_id: str, org_id: str = ORG) -> Dict[str, Any]:
    row = _entity_row(entity_id, org_id) or {}
    raw = row.get("metadata")
    if isinstance(raw, str) and raw.strip():
        return json.loads(raw)
    return raw if isinstance(raw, dict) else {}


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
    """ServiceNow 'Payments Platform' explicitly cites the Jira record — an
    auto-merge pair. A third git entity shares the Jira entity's name only.

    The git and Jira entities are BOTH given an ``owns`` edge to the same team,
    because tier 3 needs more than a shared name: it requires a corroborating
    observed relationship (a shared neighbour, keyed on relationship type AND
    target). Without both edges the engine correctly proposes nothing, and the
    tests that need a proposal have nothing to work with.
    """
    _cleanup()
    ids = {
        "sn": _insert_entity(
            "Payments Platform", source_system="servicenow", source_record_id="sn-1",
            metadata={"cross_references": [{"system": "jira", "record_id": "PAY"}]},
            created_at="2026-01-01T00:00:00+00:00",
        ),
        "jira": _insert_entity(
            "Payments", source_system="jira", source_record_id="PAY",
            created_at="2026-02-01T00:00:00+00:00",
        ),
        "git": _insert_entity(
            "payments", source_system="git", source_record_id="repo-1",
            created_at="2026-03-01T00:00:00+00:00",
        ),
        "team": _insert_entity("Platform Team", source_system="servicenow",
                               source_record_id="sn-team"),
    }
    # The corroborating relationship tier 3 requires: the same edge type to the
    # same third entity from both sides of the name match.
    _insert_edge(ids["jira"], ids["team"], "owns")
    _insert_edge(ids["git"], ids["team"], "owns")
    yield ids
    _cleanup()


def _apply(client: TestClient, **body: Any) -> Dict[str, Any]:
    r = client.post("/api/entity-merges/apply", headers=_auth(), json=body or {})
    assert r.status_code == 200, r.text
    return r.json()


def _provenance(client: TestClient, entity_id: str) -> Dict[str, Any]:
    r = client.get(f"/api/entities/{entity_id}/provenance", headers=_auth())
    assert r.status_code == 200, r.text
    return r.json()


# ── AC2: constituents + rule are exposed ────────────────────────────────────


def test_ac2_a_merged_entity_exposes_every_constituent_and_the_rule(
    client: TestClient, estate
):
    report = _apply(client, include_confirmed=False)
    assert report["merged"] >= 1, report

    survivor_id = estate["sn"]  # oldest, stable id, so the deterministic survivor
    provenance = _provenance(client, survivor_id)

    assert provenance["is_merged"] is True
    assert set(provenance["source_systems"]) == {"servicenow", "jira"}
    assert provenance["rules"] == [em.RULE_EXPLICIT_REFERENCE]
    assert provenance["constituent_count"] == 2

    by_id = {c["entity_id"]: c for c in provenance["constituents"]}
    assert set(by_id) == {estate["sn"], estate["jira"]}
    # The survivor's OWN identity is in the list — the node speaks for both.
    assert by_id[estate["sn"]]["is_origin"] is True
    assert by_id[estate["sn"]]["source_record_id"] == "sn-1"
    assert by_id[estate["sn"]]["rule"] is None
    # ...and the absorbed identity names the rule that merged it.
    absorbed = by_id[estate["jira"]]
    assert absorbed["is_origin"] is False
    assert absorbed["source_system"] == "jira"
    assert absorbed["source_record_id"] == "PAY"
    assert absorbed["rule"] == em.RULE_EXPLICIT_REFERENCE
    assert absorbed["merged_at"]
    assert absorbed["merged_by"]


def test_ac2_the_provenance_travels_with_the_entity_read_a_finding_uses(
    client: TestClient, estate
):
    """A finding surface reads entities, not a bespoke endpoint — so provenance
    has to be ON the entity, not only behind a second call."""
    _apply(client, include_confirmed=False)

    metadata = _metadata(estate["sn"])
    block = metadata.get(em.METADATA_MERGE_PROVENANCE)
    assert block, "provenance must live on the survivor's metadata"
    assert block["rules"] == [em.RULE_EXPLICIT_REFERENCE]
    assert sorted(block["source_systems"]) == ["jira", "servicenow"]


def test_an_unmerged_entity_answers_honestly_rather_than_emptily(
    client: TestClient, estate
):
    provenance = _provenance(client, estate["team"])
    assert provenance["is_merged"] is False
    assert provenance["constituent_count"] == 1
    assert provenance["constituents"][0]["is_origin"] is True
    assert provenance["rules"] == []


def test_bulk_provenance_resolves_many_entities_in_one_call(client: TestClient, estate):
    _apply(client, include_confirmed=False)
    r = client.post(
        "/api/entities/provenance",
        headers=_auth(),
        json={"entity_ids": [estate["sn"], estate["team"], "does-not-exist"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] == 3
    assert body["resolved"] == 2, "an unknown id degrades one node, not the request"
    assert body["provenance"][estate["sn"]]["is_merged"] is True
    assert body["provenance"][estate["team"]]["is_merged"] is False


# ── nothing is destroyed ────────────────────────────────────────────────────


def test_the_constituent_row_its_identity_and_its_edges_all_survive(
    client: TestClient, estate
):
    """Deleting the absorbed row would destroy the evidence AC2 requires and make
    unmerge impossible."""
    _apply(client, include_confirmed=False)

    absorbed = _entity_row(estate["jira"])
    assert absorbed is not None, "the merged-away entity must still exist"
    assert absorbed["source_record_id"] == "PAY"
    assert absorbed["resolution_status"] == "resolved", (
        "resolution_status records how the STANDING engine resolved the row — "
        "a different fact from 'this was merged'"
    )

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM entity_relationships "
            "WHERE org_id = %s AND (from_entity_id = %s OR to_entity_id = %s)",
            (ORG, estate["jira"], estate["jira"]),
        )
        assert cur.fetchone()[0] == 1, "the constituent's edges survive the merge"
    finally:
        con.close()


def test_the_constituent_points_at_its_survivor(client: TestClient, estate):
    _apply(client, include_confirmed=False)
    pointer = _metadata(estate["jira"]).get(em.METADATA_MERGED_INTO)
    assert pointer["entity_id"] == estate["sn"]
    assert pointer["rule"] == em.RULE_EXPLICIT_REFERENCE
    assert pointer["merged_at"]


# ── composition and idempotency ─────────────────────────────────────────────


def test_applying_twice_writes_once(client: TestClient, estate):
    first = _apply(client, include_confirmed=False)
    second = _apply(client, include_confirmed=False)

    assert first["merged"] >= 1
    assert second["merged"] == 0
    assert second["already_merged"] >= 1

    provenance = _provenance(client, estate["sn"])
    assert provenance["constituent_count"] == 2, "no duplicate constituent"


def test_merges_compose_transitively(client: TestClient, estate):
    """Merging the git entity into the already-merged Jira entity must land it on
    the surviving head, not create a second one."""
    _apply(client, include_confirmed=False)

    outcome = em.apply_merge(
        ORG, estate["git"], estate["jira"], rule=em.RULE_ALIAS_MAPPING, actor="owner-1"
    )
    assert outcome.outcome == em.OUTCOME_MERGED
    assert outcome.survivor_id == estate["sn"], "resolved through the existing survivor"

    provenance = _provenance(client, estate["sn"])
    assert {c["entity_id"] for c in provenance["constituents"]} == {
        estate["sn"], estate["jira"], estate["git"]
    }
    assert set(provenance["source_systems"]) == {"servicenow", "jira", "git"}
    # Each constituent keeps the rule that actually merged it.
    by_id = {c["entity_id"]: c for c in provenance["constituents"]}
    assert by_id[estate["jira"]]["rule"] == em.RULE_EXPLICIT_REFERENCE
    assert by_id[estate["git"]]["rule"] == em.RULE_ALIAS_MAPPING
    assert by_id[estate["git"]]["merged_by"] == "owner-1"
    assert sorted(provenance["rules"]) == sorted(
        [em.RULE_ALIAS_MAPPING, em.RULE_EXPLICIT_REFERENCE]
    )


def test_a_merge_across_entity_types_is_refused(client: TestClient, estate):
    team_project = _insert_entity("Payments", source_system="jira",
                                  source_record_id="PROJ", entity_type="project")
    outcome = em.apply_merge(
        ORG, estate["sn"], team_project, rule=em.RULE_ALIAS_MAPPING
    )
    assert outcome.outcome == em.OUTCOME_SKIPPED
    assert "entity types" in outcome.reason


def test_an_entity_from_another_org_cannot_be_merged(client: TestClient, estate):
    foreign = _insert_entity("Payments", source_system="jira", source_record_id="PAY",
                             org_id=OTHER_ORG)
    with pytest.raises(em.EntityMergeError):
        em.apply_merge(ORG, estate["sn"], foreign, rule=em.RULE_ALIAS_MAPPING)
    assert _metadata(foreign, OTHER_ORG).get(em.METADATA_MERGED_INTO) is None


# ── only what T1/T3 authorised may merge ────────────────────────────────────


def test_a_name_similarity_pair_is_never_merged_by_the_applier(
    client: TestClient, estate
):
    """The propose-only tier stays propose-only: the git and Jira entities share
    a canonical name, and applying auto-merges must not join them."""
    decisions = csr.resolve_org_entity_type(ORG, "system")
    proposed = [d for d in decisions if d.status == csr.STATUS_PROPOSED]
    assert proposed, "the estate must actually produce a name-similarity proposal"

    _apply(client, include_confirmed=False)

    git_pointer = _metadata(estate["git"]).get(em.METADATA_MERGED_INTO)
    assert git_pointer is None, "a proposal must never merge on its own"


def test_a_confirmed_proposal_merges_and_records_the_human_rule(
    client: TestClient, estate
):
    """The handoff T3 promised: confirming records an answer, and THIS is where
    the answer is applied — credited to the confirmation, not to the tier."""
    scan = client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    assert scan.status_code == 200, scan.text
    queue = client.get("/api/entity-match-proposals?status=pending", headers=_auth()).json()
    assert queue["proposals"], "expected a pending proposal to confirm"
    pid = queue["proposals"][0]["proposal_id"]
    client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": "confirm"},
    )

    assert emp.confirmed_pairs(ORG), "the confirmation must reach the merge handoff"

    report = _apply(client)
    assert report["merged"] >= 1

    # Read the SURVIVOR, not the pair members: once merged, both sides of the
    # confirmed pair are constituents of one head, so neither is itself a merged
    # entity. `sn` is that head — oldest, stable id, and already a survivor from
    # the auto-merge, so choose_survivor keeps accumulating onto it.
    provenance = _provenance(client, estate["sn"])
    by_id = {c["entity_id"]: c for c in provenance["constituents"]}
    assert estate["git"] in by_id, "the confirmed pair must actually be merged"
    assert by_id[estate["git"]]["rule"] == em.RULE_CONFIRMED_PROPOSAL, (
        "a name match never authorises a merge — the person who confirmed it did, "
        "and the provenance must credit the confirmation rather than the tier"
    )
    assert em.RULE_CONFIRMED_PROPOSAL in provenance["rules"]


def test_a_rejected_proposal_is_never_merged(client: TestClient, estate):
    client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    queue = client.get("/api/entity-match-proposals?status=pending", headers=_auth()).json()
    pid = queue["proposals"][0]["proposal_id"]
    client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": "reject"},
    )

    _apply(client)

    assert emp.confirmed_pairs(ORG) == []
    assert _metadata(estate["git"]).get(em.METADATA_MERGED_INTO) is None


# ── audit ───────────────────────────────────────────────────────────────────


def test_every_merge_writes_an_audit_row(client: TestClient, estate):
    _apply(client, include_confirmed=False)

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT user_id, payload FROM audit_log "
            "WHERE org_id = %s AND event_type = 'entity_merged' "
            "ORDER BY timestamp DESC LIMIT 1",
            (ORG,),
        )
        row = cur.fetchone()
    finally:
        con.close()

    assert row is not None, "a merge is a state-changing action and must be audited"
    payload = json.loads(row[1]) if isinstance(row[1], str) else row[1]
    assert payload["survivor_entity_id"] == estate["sn"]
    assert payload["merged_entity_id"] == estate["jira"]
    assert payload["rule"] == em.RULE_EXPLICIT_REFERENCE
    assert sorted(payload["source_systems"]) == ["jira", "servicenow"]


# ── RBAC + tenancy ──────────────────────────────────────────────────────────


def test_unauthenticated_requests_are_rejected(client: TestClient, estate):
    assert client.get(f"/api/entities/{estate['sn']}/provenance").status_code == 401
    assert client.post("/api/entity-merges/apply", json={}).status_code == 401


def test_a_viewer_cannot_read_provenance_or_apply_merges(client: TestClient, estate):
    viewer = {"Authorization": f"Bearer {os.getenv('VIEWER_JWT', 'viewer-token')}"}
    assert client.get(
        f"/api/entities/{estate['sn']}/provenance", headers=viewer
    ).status_code == 403
    assert client.post(
        "/api/entity-merges/apply", headers=viewer, json={}
    ).status_code == 403
    # The gate DISCRIMINATES: analyst+ still succeeds.
    assert client.get(
        f"/api/entities/{estate['sn']}/provenance", headers=_auth()
    ).status_code == 200


def test_another_orgs_entity_is_indistinguishable_from_a_missing_one(client: TestClient):
    foreign = _insert_entity("Secret Service", source_system="servicenow",
                             source_record_id="x-1", org_id=OTHER_ORG)
    try:
        assert client.get(
            f"/api/entities/{foreign}/provenance", headers=_auth()
        ).status_code == 404
        assert client.get(
            "/api/entities/00000000-0000-0000-0000-000000000000/provenance",
            headers=_auth(),
        ).status_code == 404
        bulk = client.post(
            "/api/entities/provenance", headers=_auth(), json={"entity_ids": [foreign]}
        ).json()
        assert bulk["provenance"] == {}
    finally:
        _cleanup()


def test_a_merge_in_one_org_never_touches_another(client: TestClient, estate):
    foreign_a = _insert_entity("Payments Platform", source_system="servicenow",
                               source_record_id="sn-1", org_id=OTHER_ORG)
    try:
        _apply(client, include_confirmed=False)
        assert _metadata(foreign_a, OTHER_ORG).get(em.METADATA_MERGE_PROVENANCE) is None
        assert _metadata(foreign_a, OTHER_ORG).get(em.METADATA_MERGED_INTO) is None
    finally:
        _cleanup()


def test_a_bulk_request_is_bounded(client: TestClient):
    """An unbounded id list is an easy way to turn one request into a full-table
    read."""
    from app.routes_entity_merges import MAX_BULK_ENTITY_IDS

    too_many = [str(uuid.uuid4()) for _ in range(MAX_BULK_ENTITY_IDS + 1)]
    r = client.post(
        "/api/entities/provenance", headers=_auth(), json={"entity_ids": too_many}
    )
    assert r.status_code == 400
    assert "entity ids per request" in r.text
