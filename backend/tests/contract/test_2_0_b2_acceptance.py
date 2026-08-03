"""2.0-B2 (Cross-Source Entity Enrichment) — acceptance-criteria validation (T7).

Each of T1–T6 tested its own slice. This file exists for the two things that no
single task could do:

1. **Validate each AC as the story states it**, end to end and from the OUTSIDE —
   through the real routes, the real engine and the real graph — so a reviewer can
   walk AC1–AC5 in one place rather than inferring coverage from six task suites.
2. **Exercise the INTERACTIONS between the tasks**, which is where the gaps were.
   T6 shipped before T5, so the identity gate consulted bases that outlive a
   reversal (a confirmed proposal stays confirmed; a source cross-reference stays in
   the data) but not the block that records one. The consequence was a wrong HIGH:
   a reviewer reversed a merge and the finding kept the confidence it had been given
   for the identity they had just reversed. ``test_ac5_ac4_...`` below is that
   regression, and `graph_identity_resolver` now checks the block first.

Deliberately NOT a copy of the per-task suites. Where a property is already pinned
by the task that built it, this file asserts the customer-visible OUTCOME instead of
re-asserting the mechanism, and says so.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from app import corroboration_identity_gate as gate
from app import cross_source_resolution as csr
from app import db
from app import entity_match_proposals as emp
from app import entity_merge as em
from app import entity_unmerge as eu
from app import finding_reevaluation as fr
from app.corroboration_engine import evaluate_corroboration
from discovery.packs.corroboration_rules import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM

ORG = "default"
OTHER_ORG = "b2-ac-org-b"
RUN = "run_b2_ac"
LATER_RUN = "run_b2_ac_later"
DETECTOR = "COVENANT_TRACKING_GAP"
RUN_TS = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


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
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'resolved',%s,%s,3,%s,%s,%s)
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


def _seed_run_with_finding(
    run_id: str, entity_ids: List[str], *, identity: str, org_id: str = ORG
) -> None:
    """A run whose finding references these entities — the AC4 dependency link."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO runs (id, payload) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload",
            (run_id, json.dumps({"id": run_id, "org_id": org_id, "status": "complete"})),
        )
        con.commit()
    finally:
        con.close()
    db.run_kv_set("opps", run_id, [{
        "id": "opp-ac4",
        "opportunity_identity": identity,
        "title": "Recurring servicing requests",
        "entity_ids": list(entity_ids),
    }])


def _metadata_of(entity_id: str, org_id: str = ORG) -> Dict[str, Any]:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT metadata FROM entities WHERE org_id = %s AND id = %s",
            (org_id, entity_id),
        )
        row = cur.fetchone()
    finally:
        con.close()
    raw = row[0] if row else None
    if isinstance(raw, str) and raw.strip():
        return json.loads(raw)
    return raw if isinstance(raw, dict) else {}


def _cleanup() -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        for org in (ORG, OTHER_ORG):
            for table in (
                "finding_reevaluation_flags", "entity_unmerges",
                "entity_match_proposal_history", "entity_match_proposals",
            ):
                cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org,))
            cur.execute(
                "DELETE FROM entity_relationships WHERE org_id = %s AND first_seen_run_id = %s",
                (org, RUN),
            )
            cur.execute(
                "DELETE FROM entities WHERE org_id = %s AND first_seen_run_id = %s",
                (org, RUN),
            )
        for run_id in (RUN, LATER_RUN):
            cur.execute("DELETE FROM runs WHERE id = %s", (run_id,))
            cur.execute("DELETE FROM kv WHERE key = %s", (f"opps:{run_id}",))
        con.commit()
    finally:
        con.close()
    try:
        from app.entity_alias_mappings import ALIAS_KV_KEY

        for org in (ORG, OTHER_ORG):
            db.kv_set(f"{ALIAS_KV_KEY}:{org}", [])
    except Exception:  # noqa: BLE001 — best-effort teardown of the alias table.
        pass


@pytest.fixture(autouse=True)
def clean_graph():
    _cleanup()
    yield
    _cleanup()


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture
def estate() -> Dict[str, str]:
    """One estate carrying BOTH resolution shapes the story distinguishes.

    * ``sn`` cites ``jira``'s record explicitly — tier 1, auto-merge.
    * ``git`` shares ``jira``'s normalised name and a corroborating observed edge,
      and nothing else — tier 3, propose only.

    Both shapes in one estate on purpose: AC1 is a statement about the DIFFERENCE
    between them, and testing them in separate fixtures would let a change that
    blurred the boundary pass both halves.
    """
    ids = {
        "sn": _insert_entity(
            "Payments", source_system="servicenow", source_record_id="sn-1",
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
        "team": _insert_entity(
            "Platform Team", source_system="servicenow", source_record_id="sn-team",
        ),
    }
    # Tier 3 needs a shared OBSERVED neighbour, not just a shared name.
    _insert_edge(ids["jira"], ids["team"], "owns")
    _insert_edge(ids["git"], ids["team"], "owns")
    return ids


# ── the corroboration input ─────────────────────────────────────────────────


def _run_data(sn_name: str = "Payments", jira_name: str = "Payments") -> Dict[str, Any]:
    within = (RUN_TS - timedelta(days=10)).isoformat()
    return {
        "connected_systems": ["salesforce", "servicenow", "jira"],
        "servicenow": {"incidents": [{
            "detector_ids": [DETECTOR], "state": "Open",
            "sys_created_on": within, "team": sn_name,
        }]},
        "jira": {"issues": [{
            "detector_ids": [DETECTOR], "status": "Open",
            "created": within, "process": jira_name,
        }]},
    }


def _corroborate(org_id: str = ORG) -> Any:
    """Evaluate corroboration through the PRODUCTION path — no injected resolver,
    so the graph-backed resolver and the real graph are what answer."""
    return evaluate_corroboration(
        detector_id=DETECTOR, pack_id="ncino", run_data=_run_data(),
        run_timestamp=RUN_TS, org_id=org_id,
    )


def _proposal_decision(subject_id: str, target_id: str, *, subject_name: str = "payments",
                       target_name: str = "payments") -> Any:
    """A tier-3 decision for the pair, as the engine produces it."""
    def _ent(entity_id: str, name: str, system: str, rec: str):
        return csr.ResolutionEntity(
            entity_id=entity_id, org_id=ORG, entity_type="system",
            display_name=name, canonical_name=name,
            source_system=system, source_record_id=rec,
        )

    rels = csr.build_relationship_index([
        {"from_entity_id": subject_id, "to_entity_id": "team-x",
         "relationship_type": "owns", "inferred": False},
        {"from_entity_id": target_id, "to_entity_id": "team-x",
         "relationship_type": "owns", "inferred": False},
    ])
    return csr.resolve_entity(
        _ent(subject_id, subject_name, "servicenow", "sn-1"),
        [_ent(target_id, target_name, "jira", "PAY")],
        relationship_index=rels,
    )


# ═══════════════════════════════════════════════════════════════════════════
# AC1 — "Entities with explicit cross-references resolve automatically;
#        entities matching only by name similarity are proposed, not merged."
# ═══════════════════════════════════════════════════════════════════════════


def test_ac1_an_explicit_cross_reference_resolves_automatically(client, estate):
    """The auto half, through the real apply route rather than the engine."""
    r = client.post("/api/entity-merges/apply", headers=_auth(), json={})
    assert r.status_code == 200, r.text
    assert r.json()["merged"] >= 1

    con = db.connect()
    try:
        cur = con.cursor()
        head = em.resolve_survivor_id(cur, ORG, estate["jira"])
    finally:
        con.close()
    assert head == estate["sn"], "the cited pair resolved onto one entity"

    provenance = em.get_entity_provenance(ORG, estate["sn"])
    assert provenance is not None and provenance.is_merged
    assert em.RULE_EXPLICIT_REFERENCE in provenance.rules


def test_ac1_a_name_only_match_is_proposed_and_never_merged(client, estate):
    """The propose half. Run the FULL apply pass — the one a scheduled caller runs —
    and then assert the name-only pair is still two entities.

    Asserting the outcome after the real pass, rather than the engine's tier, is the
    point: a future change that let a confirmed-looking name match through would
    still satisfy a tier-level assertion.
    """
    scan = client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    assert scan.status_code == 200, scan.text

    listed = client.get("/api/entity-match-proposals", headers=_auth())
    assert listed.status_code == 200, listed.text
    proposals = listed.json()["proposals"]
    pairs = {tuple(sorted((p["left_entity_id"], p["right_entity_id"]))) for p in proposals}
    name_pair = tuple(sorted((estate["git"], estate["jira"])))
    assert name_pair in pairs, "the name-only pair must reach the review queue"
    assert all(p["status"] == "pending" for p in proposals)

    client.post("/api/entity-merges/apply", headers=_auth(), json={})

    con = db.connect()
    try:
        cur = con.cursor()
        git_head = em.resolve_survivor_id(cur, ORG, estate["git"])
    finally:
        con.close()
    assert git_head == estate["git"], (
        "a name-only match must NOT be merged by any apply pass"
    )


def test_ac1_the_proposal_carries_the_evidence_a_reviewer_needs(client, estate):
    """A proposal nobody can judge is not a proposal. Checked here because AC1's
    "proposed" only means something if the queue is actionable."""
    client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    proposals = client.get("/api/entity-match-proposals", headers=_auth()).json()["proposals"]
    match = next(
        p for p in proposals
        if {p["left_entity_id"], p["right_entity_id"]} == {estate["git"], estate["jira"]}
    )

    evidence = match["evidence"]
    assert evidence["tier"] == csr.TIER_NAME_SIMILARITY
    assert evidence["reason"]
    assert {evidence["subject"]["source_system"], evidence["target"]["source_system"]} == {
        "git", "jira"
    }
    assert evidence["corroborating_relationships"], "the shared neighbour must be shown"


# ═══════════════════════════════════════════════════════════════════════════
# AC2 — "A resolved entity exposes all constituent source identities and the
#        rule that resolved it."
# ═══════════════════════════════════════════════════════════════════════════


def test_ac2_a_resolved_entity_exposes_every_constituent_and_its_rule(client, estate):
    client.post("/api/entity-merges/apply", headers=_auth(), json={})

    r = client.get(f"/api/entities/{estate['sn']}/provenance", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["is_merged"] is True
    identities = {
        (c["source_system"], c["source_record_id"]) for c in body["constituents"]
    }
    assert ("servicenow", "sn-1") in identities, "the survivor's OWN identity"
    assert ("jira", "PAY") in identities, "and the constituent's"
    assert sorted(body["source_systems"]) == ["jira", "servicenow"]

    by_id = {c["entity_id"]: c for c in body["constituents"]}
    assert by_id[estate["sn"]]["is_origin"] is True
    assert by_id[estate["sn"]]["rule"] is None, "the origin was not merged in"
    assert by_id[estate["jira"]]["rule"] == em.RULE_EXPLICIT_REFERENCE


def test_ac2_provenance_travels_with_the_entity_read_a_finding_surface_uses(client, estate):
    """The story's "any finding traversing it can show that".

    Provenance is stored on the entity's own metadata, so it arrives with
    ``GET /api/runs/{runId}/entities`` — no second call and no separate artifact to
    keep in step. This is the property that makes the claim true in practice.
    """
    client.post("/api/entity-merges/apply", headers=_auth(), json={})
    _seed_run_with_finding(RUN, [estate["sn"]], identity="identity-ac2")

    r = client.get(f"/api/runs/{RUN}/entities", headers=_auth())
    assert r.status_code == 200, r.text
    rows = {e["id"]: e for e in r.json()}
    assert estate["sn"] in rows, "the merged entity is visible to the run"

    block = (rows[estate["sn"]].get("metadata") or {}).get(em.METADATA_MERGE_PROVENANCE)
    assert isinstance(block, dict), "provenance must ride along with the entity"
    assert block["is_merged"] is True
    assert sorted(block["source_systems"]) == ["jira", "servicenow"]


def test_ac2_the_bulk_seam_resolves_many_entities_in_one_call(client, estate):
    """A finding view resolves provenance for every node it traverses; one request
    per node would make the interrogation surface unusable."""
    client.post("/api/entity-merges/apply", headers=_auth(), json={})

    r = client.post(
        "/api/entities/provenance", headers=_auth(),
        json={"entity_ids": [estate["sn"], estate["git"], str(uuid.uuid4())]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provenance"][estate["sn"]]["is_merged"] is True
    # An unmerged entity answers honestly rather than 404ing the whole request.
    assert body["provenance"][estate["git"]]["is_merged"] is False
    assert body["resolved"] == 2, "the unknown id is absent, not fatal"


# ═══════════════════════════════════════════════════════════════════════════
# AC3 — "Confirmed/rejected proposals persist across runs and are not
#        re-proposed."
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("action,expected", [("confirm", "confirmed"), ("reject", "rejected")])
def test_ac3_an_answered_pair_is_not_re_proposed_by_a_later_scan(
    client, estate, action, expected
):
    """Both answers are durable: "not the same thing" is as much an answer as "the
    same thing", and re-asking either would discard it."""
    client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    proposals = client.get("/api/entity-match-proposals", headers=_auth()).json()["proposals"]
    target = next(
        p for p in proposals
        if {p["left_entity_id"], p["right_entity_id"]} == {estate["git"], estate["jira"]}
    )

    decided = client.post(
        f"/api/entity-match-proposals/{target['proposal_id']}/decision",
        headers=_auth(), json={"action": action},
    )
    assert decided.status_code == 200, decided.text

    rescan = client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    assert rescan.status_code == 200, rescan.text
    assert rescan.json()["skipped_already_decided"] >= 1, (
        "the pair left alone must be REPORTED, not silently dropped"
    )

    after = client.get(
        f"/api/entity-match-proposals/{target['proposal_id']}", headers=_auth()
    ).json()["proposal"]
    assert after["status"] == expected, "a later scan must not revert the answer"
    pending = client.get(
        "/api/entity-match-proposals?status=pending", headers=_auth()
    ).json()["proposals"]
    assert target["proposal_id"] not in {p["proposal_id"] for p in pending}


def test_ac3_the_decision_survives_the_entity_rows_being_replaced(client, estate):
    """"Across runs" has to mean across the row churn runs actually cause: a
    connector that starts supplying record ids inserts NEW resolved rows for the
    same real things, and a decision keyed on row ids alone would stop covering its
    own pair."""
    client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    proposals = client.get("/api/entity-match-proposals", headers=_auth()).json()["proposals"]
    target = next(
        p for p in proposals
        if {p["left_entity_id"], p["right_entity_id"]} == {estate["git"], estate["jira"]}
    )
    client.post(
        f"/api/entity-match-proposals/{target['proposal_id']}/decision",
        headers=_auth(), json={"action": "reject"},
    )

    # The same two identities, brand-new rows.
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "DELETE FROM entities WHERE org_id = %s AND id = ANY(%s)",
            (ORG, [estate["git"], estate["jira"]]),
        )
        con.commit()
    finally:
        con.close()
    fresh_git = _insert_entity("payments", source_system="git", source_record_id="repo-1-new")
    fresh_jira = _insert_entity("Payments", source_system="jira", source_record_id="PAY-new")
    _insert_edge(fresh_git, estate["team"], "owns")
    _insert_edge(fresh_jira, estate["team"], "owns")

    rescan = client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    assert rescan.status_code == 200, rescan.text

    pending = client.get(
        "/api/entity-match-proposals?status=pending", headers=_auth()
    ).json()["proposals"]
    revived = [
        p for p in pending
        if {p["left_entity_id"], p["right_entity_id"]} == {fresh_git, fresh_jira}
    ]
    assert revived == [], "the answered question must not be asked again under new ids"


# ═══════════════════════════════════════════════════════════════════════════
# AC4 — "Unmerge restores constituents and flags dependent findings for
#        re-evaluation."
# ═══════════════════════════════════════════════════════════════════════════


def test_ac4_unmerge_restores_the_constituents(client, estate):
    client.post("/api/entity-merges/apply", headers=_auth(), json={})
    assert em.get_entity_provenance(ORG, estate["sn"]).is_merged

    r = client.post(
        f"/api/entities/{estate['jira']}/unmerge",
        headers=_auth(), json={"reason": "different services", "max_runs": 500},
    )
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "unmerged"

    assert em.METADATA_MERGED_INTO not in _metadata_of(estate["jira"])
    assert em.get_entity_provenance(ORG, estate["sn"]).is_merged is False
    con = db.connect()
    try:
        cur = con.cursor()
        assert em.resolve_survivor_id(cur, ORG, estate["jira"]) == estate["jira"]
    finally:
        con.close()


def test_ac4_dependent_findings_are_flagged_and_the_next_run_clears_them(client, estate):
    """Both halves of the second clause: flagged now, and "on the next run" made a
    fact by the run that re-observed the finding naming itself."""
    client.post("/api/entity-merges/apply", headers=_auth(), json={})
    _seed_run_with_finding(RUN, [estate["sn"]], identity="identity-ac4")

    client.post(
        f"/api/entities/{estate['jira']}/unmerge",
        headers=_auth(), json={"max_runs": 500},
    )

    flags = client.get("/api/findings/reevaluation-flags", headers=_auth()).json()
    assert flags["pending"] == 1
    assert flags["flags"][0]["opportunityIdentity"] == "identity-ac4"

    cleared = fr.clear_flags_for_run(ORG, LATER_RUN, ["identity-ac4"])
    assert cleared == ["identity-ac4"]
    flag = fr.get_flag(ORG, "identity-ac4")
    assert flag is not None and flag.cleared_run_id == LATER_RUN


def test_ac4_the_reversal_survives_the_next_merge_pass(client, estate):
    """A reversal the next run undoes is not a reversal. The cross-reference that
    caused the merge is still in the source data, so the pass must refuse rather
    than rejoin."""
    client.post("/api/entity-merges/apply", headers=_auth(), json={})
    client.post(
        f"/api/entities/{estate['jira']}/unmerge",
        headers=_auth(), json={"max_runs": 500},
    )

    again = client.post("/api/entity-merges/apply", headers=_auth(), json={})
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["blocked"] >= 1, "the refusal must be reported, not silent"
    assert em.get_entity_provenance(ORG, estate["sn"]).is_merged is False


# ═══════════════════════════════════════════════════════════════════════════
# AC5 — "Corroboration across sources requires resolved identity; unresolved
#        same-named entities do not raise confidence."
# ═══════════════════════════════════════════════════════════════════════════


def test_ac5_unresolved_same_named_entities_do_not_raise_confidence():
    """Two systems using the same word is not two systems agreeing about one thing."""
    _insert_entity("Payments", source_system="servicenow", source_record_id="sn-x")
    _insert_entity("Payments", source_system="jira", source_record_id="PAY-x")

    result = _corroborate()

    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.identity_gate["identity_verified"] is False
    assert result.triple_corroboration is False
    # The evidence is NOT removed — only the elevation.
    assert {"COR-01", "COR-02"} <= set(result.rule_ids)


def test_ac5_a_genuinely_resolved_identity_does_raise_confidence(estate):
    """The positive half, which matters just as much: the gate must not be a blanket
    refusal, or cross-source corroboration would be dead rather than honest."""
    em.apply_merge(ORG, estate["sn"], estate["jira"], rule=em.RULE_EXPLICIT_REFERENCE)

    result = _corroborate()

    assert result.elevated_confidence == CONFIDENCE_HIGH
    assert result.identity_gate["identity_verified"] is True
    assert result.identity_gate["basis"] in gate.RESOLVED_BASES
    assert result.identity_gate["blocked_rules"] == []


def test_ac5_a_name_match_is_never_a_basis():
    """The structural boundary, asserted at the outcome level.

    The two sides share an exact normalised name and a corroborating observed
    neighbour — everything tier 3 needs to PROPOSE — and nothing else. A proposal on
    the record must not verify an identity; only an answer to it can. Seeded without
    a cross-reference deliberately: with one, the identity would be genuinely
    resolved and HIGH would be correct.
    """
    sn = _insert_entity("Payments", source_system="servicenow", source_record_id="sn-n")
    jira = _insert_entity("Payments", source_system="jira", source_record_id="PAY-n")
    team = _insert_entity("Platform Team", source_system="servicenow",
                          source_record_id="sn-team-n")
    _insert_edge(sn, team, "owns")
    _insert_edge(jira, team, "owns")

    decision = _proposal_decision(sn, jira)
    assert decision.status == csr.STATUS_PROPOSED, "tier 3 proposes, never merges"
    emp.record_proposals(ORG, [decision])

    result = _corroborate()

    assert result.elevated_confidence == CONFIDENCE_MEDIUM
    assert result.identity_gate["basis"] is None
    assert "name" not in " ".join(gate.RESOLVED_BASES).lower(), (
        "no resolved basis may be name-derived"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Cross-task interactions — where the AC boundaries actually meet
# ═══════════════════════════════════════════════════════════════════════════


def test_ac5_ac4_a_reversed_identity_stops_raising_confidence(estate):
    """**The defect this T7 sweep found**, now a regression test.

    Every basis the gate trusts OUTLIVES a reversal by design: a confirmed proposal
    stays confirmed (T4 made that durable deliberately) and the source
    cross-reference T1 re-derives from is still in the data. So before this was
    fixed, a reviewer reversed a merge and the finding kept the HIGH it had been
    given for the identity they had just reversed — the exact dishonesty AC5 exists
    to close, arriving through AC4's door.

    T6 could not have caught it: T5's block did not exist yet.
    """
    merged = em.apply_merge(
        ORG, estate["sn"], estate["jira"], rule=em.RULE_EXPLICIT_REFERENCE
    )
    assert _corroborate().elevated_confidence == CONFIDENCE_HIGH

    eu.unmerge_entity(
        ORG, merged.merged_entity_id, actor="analyst-1",
        reason="not the same service", max_runs=500,
    )

    after = _corroborate()
    assert after.elevated_confidence == CONFIDENCE_MEDIUM, (
        "a reversed identity must not keep elevating confidence"
    )
    assert after.identity_gate["identity_verified"] is False
    assert after.identity_gate["basis"] is None
    # And the evidence still stands — the elevation was withdrawn, not the finding.
    assert {"COR-01", "COR-02"} <= set(after.rule_ids)


def test_a_confirmed_proposal_does_not_survive_its_own_reversal_as_a_basis(estate):
    """The same defect by its other route: a HUMAN confirmation is the strongest
    basis short of the merge itself, and it is exactly the record that outlives an
    unmerge. Pinned separately because the two paths reach the gate differently."""
    decision = _proposal_decision(estate["sn"], estate["jira"])
    emp.record_proposals(ORG, [decision])
    pid = emp.proposal_id_for("system", estate["sn"], estate["jira"])
    emp.decide(ORG, pid, action=emp.ACTION_CONFIRM, actor_id="analyst-1")
    merged = em.apply_merge(
        ORG, estate["sn"], estate["jira"],
        rule=em.RULE_CONFIRMED_PROPOSAL, actor="analyst-1",
    )
    assert _corroborate().identity_gate["basis"] == gate.BASIS_CONFIRMED_PROPOSAL

    eu.unmerge_entity(ORG, merged.merged_entity_id, actor="analyst-1", max_runs=500)

    after = _corroborate()
    assert after.elevated_confidence == CONFIDENCE_MEDIUM
    assert emp.get_proposal(ORG, pid).status == emp.STATUS_CONFIRMED, (
        "the confirmation itself is still on record — T4's durability is unaffected; "
        "it simply no longer establishes an identity the graph does not hold"
    )


def test_releasing_the_block_restores_the_elevation_when_the_merge_returns(estate):
    """Reversibility cuts both ways. An Owner who releases the block and lets the
    pair merge again must get the elevation back — otherwise the fix above would be
    a one-way ratchet that silently caps a legitimate identity forever."""
    merged = em.apply_merge(
        ORG, estate["sn"], estate["jira"], rule=em.RULE_EXPLICIT_REFERENCE
    )
    out = eu.unmerge_entity(
        ORG, merged.merged_entity_id, actor="analyst-1", max_runs=500
    )
    assert _corroborate().elevated_confidence == CONFIDENCE_MEDIUM

    assert eu.release_merge_block(ORG, out.unmerge_id, actor="owner-1") == 2
    re_merged = em.apply_merge(
        ORG, estate["sn"], estate["jira"], rule=em.RULE_EXPLICIT_REFERENCE
    )
    assert re_merged.applied

    assert _corroborate().elevated_confidence == CONFIDENCE_HIGH


def test_the_full_loop_propose_confirm_merge_elevate_unmerge_flag(client, estate):
    """AC1 → AC3 → AC2 → AC5 → AC4 in one pass, as an operator experiences it.

    Each task proved its own step. This proves the steps compose: a proposal that is
    confirmed becomes a merge, the merge makes the corroboration honest, the merged
    entity can explain itself, and reversing it withdraws both the elevation and the
    findings' standing.
    """
    _seed_run_with_finding(RUN, [estate["sn"]], identity="identity-loop")

    # AC1 — the name-only pair is proposed, not merged.
    client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    decision = _proposal_decision(estate["sn"], estate["jira"])
    emp.record_proposals(ORG, [decision])
    pid = emp.proposal_id_for("system", estate["sn"], estate["jira"])

    # AC3 — a human answers, durably.
    client.post(
        f"/api/entity-match-proposals/{pid}/decision",
        headers=_auth(), json={"action": "confirm"},
    )
    assert emp.get_proposal(ORG, pid).status == emp.STATUS_CONFIRMED

    # The answer becomes a merge, credited to the confirmation and not to the tier.
    applied = client.post(
        "/api/entity-merges/apply", headers=_auth(), json={"include_confirmed": True}
    )
    assert applied.status_code == 200, applied.text

    # AC2 — the merged entity explains itself.
    provenance = client.get(
        f"/api/entities/{estate['sn']}/provenance", headers=_auth()
    ).json()
    assert provenance["is_merged"] is True
    assert sorted(provenance["source_systems"]) == ["jira", "servicenow"]

    # AC5 — corroboration is now about one thing, so it may elevate.
    assert _corroborate().elevated_confidence == CONFIDENCE_HIGH

    # AC4 — and reversing it withdraws the elevation and flags the finding.
    unmerged = client.post(
        f"/api/entities/{estate['jira']}/unmerge",
        headers=_auth(), json={"reason": "reviewed again", "max_runs": 500},
    )
    assert unmerged.status_code == 200, unmerged.text
    assert unmerged.json()["flaggedFindings"] == 1

    assert _corroborate().elevated_confidence == CONFIDENCE_MEDIUM
    flags = client.get("/api/findings/reevaluation-flags", headers=_auth()).json()
    assert flags["flags"][0]["opportunityIdentity"] == "identity-loop"


# ═══════════════════════════════════════════════════════════════════════════
# AC6 (story-level) — "Two-org isolation holds for all mappings and proposals."
# Not in T7's AC1–AC5 brief, but every surface above is org-scoped and the sweep
# is cheap; a leak here would invalidate all five.
# ═══════════════════════════════════════════════════════════════════════════


def test_ac6_another_orgs_resolution_never_satisfies_this_orgs_corroboration():
    """The isolation that matters most: one org's merge must not raise another's
    confidence.

    This org gets the same two names with NO resolution of its own — the estate
    fixture's ServiceNow entity carries a cross-reference, which would resolve the
    identity here legitimately and make the test vacuous.
    """
    _insert_entity("Payments", source_system="servicenow", source_record_id="sn-mine")
    _insert_entity("Payments", source_system="jira", source_record_id="PAY-mine")

    a = _insert_entity("Payments", source_system="servicenow", source_record_id="sn-1",
                       org_id=OTHER_ORG,
                       metadata={"cross_references": [{"system": "jira", "record_id": "PAY"}]})
    b = _insert_entity("Payments", source_system="jira", source_record_id="PAY",
                       org_id=OTHER_ORG)
    assert em.apply_merge(OTHER_ORG, a, b, rule=em.RULE_EXPLICIT_REFERENCE).applied

    assert _corroborate(ORG).elevated_confidence == CONFIDENCE_MEDIUM
    assert _corroborate(OTHER_ORG).elevated_confidence == CONFIDENCE_HIGH, (
        "and the org that DID resolve it still gets its elevation"
    )


def test_ac6_proposals_and_unmerges_are_org_scoped(client, estate):
    client.post("/api/entity-match-proposals/scan", headers=_auth(), json={})
    mine = client.get("/api/entity-match-proposals", headers=_auth()).json()["proposals"]
    assert mine, "this org has proposals"
    assert emp.list_proposals(OTHER_ORG) == [], "the other org has none"

    merged = em.apply_merge(
        ORG, estate["sn"], estate["jira"], rule=em.RULE_EXPLICIT_REFERENCE
    )
    eu.unmerge_entity(ORG, merged.merged_entity_id, actor="analyst-1", max_runs=500)
    assert eu.list_unmerges(OTHER_ORG) == []
    assert fr.pending_identities(OTHER_ORG) == []


def test_ac6_another_orgs_proposal_id_is_indistinguishable_from_an_unknown_one(
    client, estate
):
    """A 403 that exists would confirm the id."""
    other = _insert_entity("Payments", source_system="servicenow",
                           source_record_id="sn-o", org_id=OTHER_ORG)
    other_b = _insert_entity("Payments", source_system="jira",
                             source_record_id="PAY-o", org_id=OTHER_ORG)
    foreign_id = emp.proposal_id_for("system", other, other_b)

    r = client.get(f"/api/entity-match-proposals/{foreign_id}", headers=_auth())
    assert r.status_code == 404
