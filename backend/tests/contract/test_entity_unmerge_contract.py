"""2.0-B2 T5 contract tests — unmerge & re-evaluation flagging, end to end.

AC4: "Unmerge restores constituents and flags dependent findings for re-evaluation."

The DB-free half (pair keys, subtree derivation, the block guard in ``apply_merge``)
is in ``tests/unit/test_entity_unmerge.py``. This file drives the real routes, the
real tenancy/RBAC middleware, the real ``entities`` table and real merges, because
the properties that decide whether AC4 actually holds only exist once a merge has
been written and then reversed:

  * the constituent is genuinely restored — addressable, resolving to itself, its
    identity and edges intact, and out of the survivor's constituent list;
  * a chain of merges comes apart at the joint that was reversed and no other;
  * **the reversal survives the next merge pass** — the regression that makes the
    difference between an unmerge and a temporary one, since the appliers re-run
    continuously and the source cross-reference is still there;
  * dependent findings are flagged, unrelated findings are not, and findings that
    cannot be assessed are counted rather than quietly ignored;
  * a later run that re-observes a flagged finding clears the flag and records
    WHICH run did it — "re-evaluated on the next run" as a fact, not an intention;
  * two-org isolation holds for every one of the above.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from app import db
from app import entity_merge as em
from app import entity_unmerge as eu
from app import finding_reevaluation as fr

ORG = "default"
OTHER_ORG = "unmerge-org-b"
RUN = "run_unmerge_t5"
LATER_RUN = "run_unmerge_t5_later"


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


def _insert_run(run_id: str, org_id: str = ORG) -> None:
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


def _seed_findings(run_id: str, opps: List[Dict[str, Any]], *, org_id: str = ORG) -> None:
    """A run with findings that reference entities — the link the sweep follows."""
    _insert_run(run_id, org_id)
    db.run_kv_set("opps", run_id, opps)


def _entity_row(entity_id: str, org_id: str = ORG) -> Optional[Dict[str, Any]]:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT * FROM entities WHERE org_id = %s AND id = %s", (org_id, entity_id)
        )
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


def _edges_of(entity_id: str, org_id: str = ORG) -> int:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM entity_relationships WHERE org_id = %s "
            "AND (from_entity_id = %s OR to_entity_id = %s)",
            (org_id, entity_id, entity_id),
        )
        return int((cur.fetchone() or [0])[0])
    finally:
        con.close()


def _cleanup() -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        for org in (ORG, OTHER_ORG):
            cur.execute("DELETE FROM finding_reevaluation_flags WHERE org_id = %s", (org,))
            cur.execute("DELETE FROM entity_unmerges WHERE org_id = %s", (org,))
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
        for run_id in (RUN, LATER_RUN):
            cur.execute("DELETE FROM runs WHERE id = %s", (run_id,))
            cur.execute("DELETE FROM kv WHERE key = %s", (f"opps:{run_id}",))
            cur.execute(
                "DELETE FROM kv WHERE key = %s", (f"llm_enrichment:{run_id}",)
            )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def merged():
    """A REAL merge to reverse: ServiceNow 'Payments Platform' cites the Jira
    record, so tier 1 auto-merges the pair. Both sides keep an edge, so the test
    that nothing was destroyed has something to check.
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
        "team": _insert_entity(
            "Platform Team", source_system="servicenow", source_record_id="sn-team",
        ),
    }
    _insert_edge(ids["sn"], ids["team"], "owns")
    _insert_edge(ids["jira"], ids["team"], "owns")

    outcome = em.apply_merge(
        ORG, ids["sn"], ids["jira"], rule=em.RULE_EXPLICIT_REFERENCE,
    )
    assert outcome.applied, f"the fixture must actually merge: {outcome.reason}"
    ids["survivor"] = outcome.survivor_id
    ids["absorbed"] = outcome.merged_entity_id
    yield ids
    _cleanup()


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


# ── AC4, first half: the constituents are restored ──────────────────────────


def test_ac4_unmerge_restores_the_constituent(merged):
    """The core of AC4. After the reversal the constituent must be an independent
    entity again: no pointer, resolving to itself, and gone from the survivor's
    constituent list."""
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    before = em.get_entity_provenance(ORG, survivor)
    assert before is not None and before.is_merged

    outcome = eu.unmerge_entity(ORG, absorbed, actor="analyst-1", reason="different services")

    assert outcome.applied
    assert outcome.survivor_entity_id == survivor
    assert outcome.detached_entity_id == absorbed
    assert absorbed in outcome.restored_entity_ids

    # The pointer is gone, so resolution lands on the entity itself.
    assert em.METADATA_MERGED_INTO not in _metadata(absorbed)
    con = db.connect()
    try:
        assert em.resolve_survivor_id(con.cursor(), ORG, absorbed) == absorbed
    finally:
        con.close()

    # And the survivor no longer claims that identity.
    after = em.get_entity_provenance(ORG, survivor)
    assert after is not None
    assert after.is_merged is False
    assert absorbed not in [c.entity_id for c in after.constituents]


def test_the_restore_destroys_nothing(merged):
    """Both rows, both identities and both edges must survive a reversal — the same
    guarantee the merge itself gave."""
    absorbed = merged["absorbed"]
    row_before = _entity_row(absorbed)
    edges_before = _edges_of(absorbed)

    eu.unmerge_entity(ORG, absorbed, actor="analyst-1")

    row_after = _entity_row(absorbed)
    assert row_after is not None
    for field in ("source_system", "source_record_id", "canonical_name",
                  "display_name", "entity_type", "resolution_status"):
        assert row_after[field] == row_before[field], f"{field} must be untouched"
    assert _edges_of(absorbed) == edges_before


def test_the_restored_entity_records_what_it_was_released_from(merged):
    """History, not state: the row shows the merge it came out of without taking
    part in resolution again."""
    eu.unmerge_entity(ORG, merged["absorbed"], actor="analyst-1", reason="wrong pair")
    released = _metadata(merged["absorbed"]).get(eu.METADATA_UNMERGED_FROM)

    assert isinstance(released, dict)
    assert released["entity_id"] == merged["survivor"]
    assert released["rule"] == em.RULE_EXPLICIT_REFERENCE
    assert released["unmerged_by"] == "analyst-1"
    assert released["unmerged_at"]


def test_an_unmerged_entity_reports_not_merged_rather_than_failing(merged):
    """A truthful answer to "unmerge this", not an error — and idempotent, so a
    second call cannot double-write."""
    eu.unmerge_entity(ORG, merged["absorbed"], actor="analyst-1")
    again = eu.unmerge_entity(ORG, merged["absorbed"], actor="analyst-1")
    assert again.outcome == eu.OUTCOME_NOT_MERGED
    assert again.applied is False


def test_an_entity_pointing_at_a_missing_survivor_is_still_restored(merged):
    """No code path deletes an entity, so this takes manual surgery to reach — but
    refusing would leave the constituent permanently merged into a ghost, denying the
    one action that helps. It is restored, and no block is recorded because there is
    no pair left to block."""
    absorbed = merged["absorbed"]
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "DELETE FROM entities WHERE org_id = %s AND id = %s",
            (ORG, merged["survivor"]),
        )
        con.commit()
    finally:
        con.close()

    outcome = eu.unmerge_entity(ORG, absorbed, actor="analyst-1")

    assert outcome.applied
    assert outcome.restored_entity_ids == (absorbed,)
    assert em.METADATA_MERGED_INTO not in _metadata(absorbed)
    assert eu.list_unmerges(ORG) == [], "no pair, so no block"


def test_unmerging_an_unknown_entity_is_refused(merged):
    with pytest.raises(eu.EntityUnmergeError):
        eu.unmerge_entity(ORG, str(uuid.uuid4()), actor="analyst-1")


# ── a chain comes apart at one joint ────────────────────────────────────────


def test_a_chain_comes_apart_at_the_reversed_joint_only(merged):
    """git → (jira → servicenow). Detaching the jira entity must hand it back WITH
    git still merged into it: the sub-merge nobody reversed stays intact.
    """
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    git = _insert_entity("payments", source_system="git", source_record_id="repo-1",
                         created_at="2026-04-01T00:00:00+00:00")
    # Merge git into the absorbed entity's chain; it lands on the current head.
    chained = em.apply_merge(
        ORG, git, absorbed, rule=em.RULE_ALIAS_MAPPING,
    )
    assert chained.applied
    assert chained.survivor_id == survivor, "merges compose onto one head"

    outcome = eu.unmerge_entity(ORG, absorbed, actor="analyst-1")

    assert outcome.applied
    # git was merged onto the SURVIVOR (not through the absorbed entity), so it must
    # stay with the survivor — the subtree is derived from pointers, not from names.
    assert git not in outcome.restored_entity_ids
    remaining = em.get_entity_provenance(ORG, survivor)
    assert remaining is not None
    assert git in [c.entity_id for c in remaining.constituents]
    assert remaining.is_merged is True, "the untouched sub-merge survives"


def test_a_true_sub_merge_travels_with_the_detached_entity():
    """The nested case, built the way the system actually builds it.

    A genuine ``git → jira → sn`` pointer chain needs the middle entity to have been
    a survivor BEFORE it was absorbed, and ``choose_survivor`` prefers an existing
    survivor — so it only arises when both sides are already survivors and the
    tie-break (stable record id, then earliest ``created_at``) decides. Constructed
    here through ``apply_merge`` alone, because a test over a state the system cannot
    produce proves nothing about the system.

    Detaching the middle entity must hand it back WITH its own constituent still
    merged into it: that sub-merge is not the one being reversed.
    """
    _cleanup()
    git = _insert_entity("payments", source_system="git", source_record_id="repo-1",
                         created_at="2026-04-01T00:00:00+00:00")
    jira = _insert_entity("Payments", source_system="jira", source_record_id="PAY",
                          created_at="2026-02-01T00:00:00+00:00")
    sn = _insert_entity("Payments Platform", source_system="servicenow",
                        source_record_id="sn-1",
                        created_at="2026-01-01T00:00:00+00:00")
    squad = _insert_entity("Platform Squad", source_system="servicenow",
                           source_record_id="sn-team2",
                           created_at="2026-06-01T00:00:00+00:00")
    try:
        # jira absorbs git (older created_at wins), sn absorbs the squad, and then
        # sn absorbs jira — leaving git pointing at jira, and jira at sn.
        assert em.apply_merge(ORG, git, jira, rule=em.RULE_ALIAS_MAPPING).survivor_id == jira
        assert em.apply_merge(ORG, squad, sn, rule=em.RULE_ALIAS_MAPPING).survivor_id == sn
        chained = em.apply_merge(ORG, jira, sn, rule=em.RULE_EXPLICIT_REFERENCE)
        assert chained.survivor_id == sn and chained.merged_entity_id == jira
        assert _metadata(git)[em.METADATA_MERGED_INTO]["entity_id"] == jira
        assert _metadata(jira)[em.METADATA_MERGED_INTO]["entity_id"] == sn

        outcome = eu.unmerge_entity(ORG, jira, actor="analyst-1")

        assert git in outcome.restored_entity_ids, "the sub-merge follows its parent out"
        assert jira in outcome.restored_entity_ids
        assert squad not in outcome.restored_entity_ids, "the sibling merge is untouched"

        # git keeps its own pointer — its merge was not the one reversed — so it now
        # resolves to jira, which resolves to itself.
        assert _metadata(git)[em.METADATA_MERGED_INTO]["entity_id"] == jira
        assert em.METADATA_MERGED_INTO not in _metadata(jira)
        con = db.connect()
        try:
            cur = con.cursor()
            assert em.resolve_survivor_id(cur, ORG, git) == jira
            assert em.resolve_survivor_id(cur, ORG, jira) == jira
        finally:
            con.close()

        # jira is a survivor again in its own right, carrying git.
        restored = em.get_entity_provenance(ORG, jira)
        assert restored is not None
        assert restored.is_merged is True
        assert git in [c.entity_id for c in restored.constituents]

        # And sn has lost git AND jira, but kept the squad.
        remaining = em.get_entity_provenance(ORG, sn)
        assert remaining is not None
        assert {c.entity_id for c in remaining.constituents} == {sn, squad}
    finally:
        _cleanup()


def test_unmerge_all_splits_every_constituent(merged):
    """Each detachment is its own reversal, so a full split is inspectable as the
    set of reversals it is."""
    survivor = merged["survivor"]
    third = _insert_entity("pay platform", source_system="git", source_record_id="repo-3")
    assert em.apply_merge(ORG, third, survivor, rule=em.RULE_ALIAS_MAPPING).applied

    outcomes = eu.unmerge_all(ORG, survivor, actor="analyst-1")

    assert len(outcomes) == 2
    assert all(o.applied for o in outcomes)
    after = em.get_entity_provenance(ORG, survivor)
    assert after is not None and after.is_merged is False


def test_unmerge_all_leaves_a_nested_sub_merge_intact():
    """"Completely" means the merges THIS entity performed.

    The flat constituent list also offers the members of a chain, so iterating it
    would reverse a merge nobody asked about and block a pair nobody separated —
    and it would contradict single-entity unmerge, which preserves a sub-merge.
    """
    _cleanup()
    git = _insert_entity("payments", source_system="git", source_record_id="repo-1",
                         created_at="2026-04-01T00:00:00+00:00")
    jira = _insert_entity("Payments", source_system="jira", source_record_id="PAY",
                          created_at="2026-02-01T00:00:00+00:00")
    sn = _insert_entity("Payments Platform", source_system="servicenow",
                        source_record_id="sn-1",
                        created_at="2026-01-01T00:00:00+00:00")
    squad = _insert_entity("Platform Squad", source_system="servicenow",
                           source_record_id="sn-team2",
                           created_at="2026-06-01T00:00:00+00:00")
    try:
        em.apply_merge(ORG, git, jira, rule=em.RULE_ALIAS_MAPPING)
        em.apply_merge(ORG, squad, sn, rule=em.RULE_ALIAS_MAPPING)
        em.apply_merge(ORG, jira, sn, rule=em.RULE_EXPLICIT_REFERENCE)
        # sn's flat list holds git as well, but git's pointer names jira, not sn.
        listed = em.get_entity_provenance(ORG, sn)
        assert listed is not None
        assert git in [c.entity_id for c in listed.constituents]

        outcomes = eu.unmerge_all(ORG, sn, actor="analyst-1")

        detached = {o.detached_entity_id for o in outcomes}
        assert detached == {jira, squad}, "only the merges sn itself performed"
        assert git not in detached
        # git is still merged into jira, and that pair was never blocked.
        assert _metadata(git)[em.METADATA_MERGED_INTO]["entity_id"] == jira
        restored = em.get_entity_provenance(ORG, jira)
        assert restored is not None and restored.is_merged is True
        assert em.get_entity_provenance(ORG, sn).is_merged is False
    finally:
        _cleanup()


# ── the reversal survives the next merge pass ───────────────────────────────


def test_ac4_the_next_merge_pass_does_not_undo_the_unmerge(merged):
    """The regression that decides whether "unmerge" means anything.

    The appliers are idempotent and re-run continuously; the ServiceNow record still
    carries its cross-reference to the Jira record, so a plain reversal would be
    re-merged by the very next pass and the operator would see the merge reappear
    with no explanation.
    """
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    eu.unmerge_entity(ORG, absorbed, actor="analyst-1", reason="not the same service")

    # Exactly what a run does: resolve the org and apply everything authorised.
    report = em.apply_org_merges(ORG, entity_types=["system"])

    assert report.merged == 0, "a reversed pair must not be re-merged"
    assert report.blocked >= 1, "and the refusal must be REPORTED, not silent"
    assert any("unmerged" in (o.reason or "") for o in report.outcomes)
    assert em.METADATA_MERGED_INTO not in _metadata(absorbed)
    provenance = em.get_entity_provenance(ORG, survivor)
    assert provenance is not None and provenance.is_merged is False


def test_the_block_is_recorded_under_both_pair_keys(merged):
    """Both keys, because they fail in opposite directions: the row-id key breaks
    when entity rows churn, the identity key when a side is renamed."""
    eu.unmerge_entity(ORG, merged["absorbed"], actor="analyst-1")
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT pair_key_kind FROM entity_unmerges WHERE org_id = %s AND status = %s",
            (ORG, eu.STATUS_BLOCKED),
        )
        kinds = sorted(str(r[0]) for r in cur.fetchall())
    finally:
        con.close()
    assert kinds == [eu.PAIR_KEY_ROWS, eu.PAIR_KEY_IDENTITY]


def test_the_block_survives_the_entity_rows_being_replaced(merged):
    """The T4 lesson applied to the reversal: a source that starts supplying record
    ids produces NEW entity rows for the same real things, so a block keyed only on
    row ids would quietly stop covering its own pair."""
    absorbed = merged["absorbed"]
    survivor_row = _entity_row(merged["survivor"])
    absorbed_row = _entity_row(absorbed)
    eu.unmerge_entity(ORG, absorbed, actor="analyst-1")

    # Same identities, brand-new rows.
    fresh_survivor = _insert_entity(
        survivor_row["display_name"], source_system=survivor_row["source_system"],
        source_record_id="sn-1-new",
    )
    fresh_absorbed = _insert_entity(
        absorbed_row["display_name"], source_system=absorbed_row["source_system"],
        source_record_id="PAY-new",
    )
    outcome = em.apply_merge(
        ORG, fresh_survivor, fresh_absorbed, rule=em.RULE_EXPLICIT_REFERENCE
    )
    assert outcome.outcome == em.OUTCOME_BLOCKED


def test_an_unrelated_pair_is_not_blocked(merged):
    """The block must be about ONE pair. A blanket refusal would be as wrong as no
    refusal at all."""
    eu.unmerge_entity(ORG, merged["absorbed"], actor="analyst-1")
    a = _insert_entity("Billing", source_system="servicenow", source_record_id="sn-9")
    b = _insert_entity("Billing", source_system="jira", source_record_id="BILL")
    assert em.apply_merge(ORG, a, b, rule=em.RULE_ALIAS_MAPPING).applied


def test_releasing_the_block_lets_the_pair_merge_again(merged):
    """"Any resolution is reversible" cuts both ways — including the reversal."""
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    outcome = eu.unmerge_entity(ORG, absorbed, actor="analyst-1")
    assert em.apply_merge(ORG, survivor, absorbed,
                          rule=em.RULE_EXPLICIT_REFERENCE).outcome == em.OUTCOME_BLOCKED

    released = eu.release_merge_block(
        ORG, outcome.unmerge_id, actor="owner-1", reason="checked with the team"
    )
    assert released == 2, "both pair keys"
    assert em.apply_merge(ORG, survivor, absorbed,
                          rule=em.RULE_EXPLICIT_REFERENCE).applied


def test_a_release_keeps_the_record_of_the_unmerge(merged):
    """Nothing is deleted: the row keeps its unmerge and gains who released it."""
    outcome = eu.unmerge_entity(ORG, merged["absorbed"], actor="analyst-1")
    eu.release_merge_block(ORG, outcome.unmerge_id, actor="owner-1", reason="agreed")

    entries = eu.list_unmerges(ORG)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.status == eu.STATUS_RELEASED
    assert entry.released_by == "owner-1"
    assert entry.release_reason == "agreed"
    assert entry.actor_id == "analyst-1", "the original reverser survives the release"


def test_releasing_an_unknown_unmerge_reports_nothing_released(merged):
    assert eu.release_merge_block(ORG, "unm_nope", actor="owner-1") == 0


# ── AC4, second half: dependent findings are flagged ────────────────────────


def _finding(opp_id: str, identity: str, entity_ids: List[str]) -> Dict[str, Any]:
    return {
        "id": opp_id,
        "opportunity_identity": identity,
        "title": "Recurring servicing requests",
        "entity_ids": entity_ids,
    }


def test_ac4_dependent_findings_are_flagged_for_re_evaluation(merged):
    """A finding that referenced either side of the merge must be flagged: what its
    entity meant has changed."""
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    _seed_findings(RUN, [
        _finding("opp-1", "identity-dependent", [survivor]),
        _finding("opp-2", "identity-other", [merged["team"]]),
    ])

    outcome = eu.unmerge_entity(ORG, absorbed, actor="analyst-1", max_runs=500)

    assert outcome.flagged_findings == 1
    flags = fr.list_flags(ORG)
    assert [f.opportunity_identity for f in flags] == ["identity-dependent"]
    flag = flags[0]
    assert flag.status == fr.STATUS_PENDING
    assert flag.reason == fr.REASON_ENTITY_UNMERGED
    assert flag.trigger_ref == outcome.unmerge_id
    assert survivor in flag.entity_ids
    assert flag.flagged_run_id == RUN


def test_a_finding_referencing_the_detached_entity_is_flagged_too(merged):
    """Both sides changed meaning: the survivor narrowed, and the detached entity is
    a separate thing again."""
    absorbed = merged["absorbed"]
    _seed_findings(RUN, [_finding("opp-1", "identity-detached", [absorbed])])

    eu.unmerge_entity(ORG, absorbed, actor="analyst-1", max_runs=500)

    assert [f.opportunity_identity for f in fr.list_flags(ORG)] == ["identity-detached"]


def test_a_finding_with_no_entity_link_is_counted_not_flagged(merged):
    """It cannot be shown to depend on the merge. Flagging it would be a guess;
    ignoring it silently would hide the limit of the sweep — so it is counted."""
    _seed_findings(RUN, [
        {"id": "opp-1", "opportunity_identity": "identity-unlinked", "title": "x"},
    ])

    outcome = eu.unmerge_entity(
        ORG, merged["absorbed"], actor="analyst-1", max_runs=500
    )

    assert outcome.flagged_findings == 0, "an unlinked finding must not be flagged"
    assert outcome.sweep is not None
    # >= 1 rather than == 1: this org is shared with the rest of the suite, so the
    # totals are not this test's to pin. What matters is that the unlinked finding
    # was COUNTED rather than silently ignored, and that nothing was flagged.
    assert outcome.sweep.unlinked >= 1
    assert outcome.sweep.findings_examined >= 1
    assert fr.list_flags(ORG) == []


def test_the_enrichment_entity_list_also_links_a_finding(merged):
    """The per-opportunity enrichment entity list is the other place the link lives;
    both must be read or a real dependency is missed."""
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    _seed_findings(RUN, [
        {"id": "opp-1", "opportunity_identity": "identity-enriched", "title": "x"},
    ])
    db.run_kv_set("llm_enrichment", RUN, {
        "perOpportunity": {"opp-1": {"entities": [{"entity_id": survivor}]}}
    })

    outcome = eu.unmerge_entity(ORG, absorbed, actor="analyst-1", max_runs=500)

    assert outcome.flagged_findings == 1
    assert [f.opportunity_identity for f in fr.list_flags(ORG)] == ["identity-enriched"]


def test_the_unmerge_record_keeps_what_it_did_about_findings(merged):
    """The counts live with the action, so "what did this reversal affect?" is
    answerable later without re-running the sweep."""
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    _seed_findings(RUN, [
        _finding("opp-1", "identity-a", [survivor]),
        {"id": "opp-2", "opportunity_identity": "identity-b", "title": "no link"},
    ])
    eu.unmerge_entity(ORG, absorbed, actor="analyst-1", max_runs=500)

    entry = eu.list_unmerges(ORG)[0]
    # The flagged count is exact (only this test's finding references this test's
    # entity ids); the unlinked count is a total over an org the suite shares.
    assert entry.flagged_finding_count == 1
    assert entry.unlinked_finding_count >= 1


# ── "on the next run" ───────────────────────────────────────────────────────


def test_ac4_the_next_run_clears_the_flag_and_records_which_run(merged):
    """This is what makes "re-evaluation on the next run" checkable: the flag is
    closed by the run that re-observed the finding, and that run is named."""
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    _seed_findings(RUN, [_finding("opp-1", "identity-dependent", [survivor])])
    eu.unmerge_entity(ORG, absorbed, actor="analyst-1", max_runs=500)
    assert fr.pending_identities(ORG) == ["identity-dependent"]

    cleared = fr.clear_flags_for_run(ORG, LATER_RUN, ["identity-dependent"])

    assert cleared == ["identity-dependent"]
    assert fr.pending_identities(ORG) == []
    flag = fr.get_flag(ORG, "identity-dependent")
    assert flag is not None
    assert flag.status == fr.STATUS_CLEARED
    assert flag.cleared_run_id == LATER_RUN
    assert flag.cleared_at


def test_a_finding_the_next_run_did_not_surface_keeps_its_flag(merged):
    """A finding that stops appearing has not been re-evaluated. Clearing it because
    a run happened would be the quiet lie this store exists to avoid."""
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    _seed_findings(RUN, [_finding("opp-1", "identity-dependent", [survivor])])
    eu.unmerge_entity(ORG, absorbed, actor="analyst-1", max_runs=500)

    fr.clear_flags_for_run(ORG, LATER_RUN, ["some-other-identity"])

    assert fr.pending_identities(ORG) == ["identity-dependent"]


def test_clearing_twice_does_not_rewrite_the_clearing_run(merged):
    """Only pending rows are touched, so a re-run never overwrites the record of
    which run actually did the re-evaluation."""
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    _seed_findings(RUN, [_finding("opp-1", "identity-dependent", [survivor])])
    eu.unmerge_entity(ORG, absorbed, actor="analyst-1", max_runs=500)
    fr.clear_flags_for_run(ORG, LATER_RUN, ["identity-dependent"])

    assert fr.clear_flags_for_run(ORG, "run_even_later", ["identity-dependent"]) == []
    flag = fr.get_flag(ORG, "identity-dependent")
    assert flag is not None and flag.cleared_run_id == LATER_RUN


def test_a_second_unmerge_re_raises_a_cleared_flag_without_resetting_the_clock(merged):
    """Re-flagging must move the trigger but NOT ``flagged_at``: a finding that has
    waited through several runs must not look freshly raised."""
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    _seed_findings(RUN, [_finding("opp-1", "identity-dependent", [survivor])])
    eu.unmerge_entity(ORG, absorbed, actor="analyst-1", max_runs=500)
    first = fr.get_flag(ORG, "identity-dependent")
    assert first is not None
    fr.clear_flags_for_run(ORG, LATER_RUN, ["identity-dependent"])

    report = fr.flag_findings(
        ORG, ["identity-dependent"], reason=fr.REASON_ENTITY_UNMERGED,
        trigger_ref="unm_second", actor="analyst-2",
    )

    assert (report.flagged, report.refreshed) == (0, 1)
    again = fr.get_flag(ORG, "identity-dependent")
    assert again is not None
    assert again.status == fr.STATUS_PENDING
    assert again.trigger_ref == "unm_second"
    assert again.cleared_run_id is None
    assert again.flagged_at == first.flagged_at, "the wait started when it was first flagged"


def test_the_materialize_path_clears_flags_on_the_next_run():
    """Structural: the clearing call must be wired into materialization, or "on the
    next run" is a promise nothing keeps. Pinned because it is one call in a large
    function and trivially lost in a refactor."""
    import inspect

    from app import materialize_t2

    source = inspect.getsource(materialize_t2)
    assert "clear_flags_for_run" in source
    assert "opportunity_identity" in source


# ── the routes ──────────────────────────────────────────────────────────────


def test_the_unmerge_route_reverses_and_reports(client, merged):
    r = client.post(
        f"/api/entities/{merged['absorbed']}/unmerge",
        headers=_auth(), json={"reason": "different services"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "unmerged"
    assert body["survivorEntityId"] == merged["survivor"]
    assert body["unmergeId"].startswith("unm_")
    assert "dependencySweep" in body


def test_the_unmerge_route_works_without_a_body(client, merged):
    """A reversal with no note is legitimate; requiring a body would be friction for
    no gain."""
    r = client.post(f"/api/entities/{merged['absorbed']}/unmerge", headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "unmerged"


def test_the_unmerge_route_404s_for_an_unknown_entity(client, merged):
    r = client.post(f"/api/entities/{uuid.uuid4()}/unmerge", headers=_auth())
    assert r.status_code == 404


def test_the_unmerge_all_route_splits_everything(client, merged):
    r = client.post(f"/api/entities/{merged['survivor']}/unmerge-all", headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json()["detached"] == 1


def test_the_unmerge_log_route_lists_one_entry_per_action(client, merged):
    client.post(f"/api/entities/{merged['absorbed']}/unmerge", headers=_auth())
    r = client.get("/api/entity-unmerges", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1, "two pair keys, ONE action"
    assert body["unmerges"][0]["previousRule"] == em.RULE_EXPLICIT_REFERENCE


def test_the_flags_route_shows_pending_work(client, merged):
    survivor = merged["survivor"]
    _seed_findings(RUN, [_finding("opp-1", "identity-dependent", [survivor])])
    client.post(
        f"/api/entities/{merged['absorbed']}/unmerge",
        headers=_auth(), json={"max_runs": 500},
    )

    r = client.get("/api/findings/reevaluation-flags", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending"] == 1
    assert body["flags"][0]["opportunityIdentity"] == "identity-dependent"
    assert body["flags"][0]["reason"] == fr.REASON_ENTITY_UNMERGED


def test_the_release_route_needs_owner(client, merged):
    """Analyst can reverse a merge; only an Owner can re-permit automatic merging of
    a pair somebody deliberately separated."""
    r = client.post(f"/api/entities/{merged['absorbed']}/unmerge", headers=_auth())
    unmerge_id = r.json()["unmergeId"]

    analyst = {"Authorization": f"Bearer {os.getenv('ANALYST_JWT', 'analyst-token')}"}
    refused = client.post(
        f"/api/entity-unmerges/{unmerge_id}/release", headers=analyst
    )
    assert refused.status_code in (401, 403), refused.text

    allowed = client.post(
        f"/api/entity-unmerges/{unmerge_id}/release", headers=_auth(),
        json={"reason": "confirmed with the team"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "released"


def test_the_release_route_404s_for_an_unknown_id(client, merged):
    """Not a 403: a "403 that exists" would confirm the id to a caller who should
    not know it."""
    r = client.post("/api/entity-unmerges/unm_nope/release", headers=_auth())
    assert r.status_code == 404


def test_a_viewer_cannot_unmerge(client, merged):
    viewer = {"Authorization": f"Bearer {os.getenv('VIEWER_JWT', 'viewer-token')}"}
    r = client.post(f"/api/entities/{merged['absorbed']}/unmerge", headers=viewer)
    assert r.status_code in (401, 403)


# ── AC6 territory: two-org isolation ────────────────────────────────────────


def test_another_orgs_entity_cannot_be_unmerged(merged):
    """The reversal is org-scoped: another org's merged entity is simply not there."""
    with pytest.raises(eu.EntityUnmergeError):
        eu.unmerge_entity(OTHER_ORG, merged["absorbed"], actor="analyst-1")
    assert em.METADATA_MERGED_INTO in _metadata(merged["absorbed"])


def test_a_block_in_one_org_does_not_block_another(merged):
    """Two orgs can hold the same identities and reach opposite conclusions."""
    eu.unmerge_entity(ORG, merged["absorbed"], actor="analyst-1")

    a = _insert_entity("Payments Platform", source_system="servicenow",
                       source_record_id="sn-1", org_id=OTHER_ORG)
    b = _insert_entity("Payments", source_system="jira",
                       source_record_id="PAY", org_id=OTHER_ORG)
    assert em.apply_merge(OTHER_ORG, a, b, rule=em.RULE_EXPLICIT_REFERENCE).applied


def test_flags_are_org_scoped(merged):
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    _seed_findings(RUN, [_finding("opp-1", "identity-dependent", [survivor])])
    eu.unmerge_entity(ORG, absorbed, actor="analyst-1")

    assert fr.pending_identities(OTHER_ORG) == []
    assert fr.get_flag(OTHER_ORG, "identity-dependent") is None


def test_the_sweep_ignores_another_orgs_runs(merged):
    """A finding in another org's run must never be flagged by this org's unmerge."""
    survivor, absorbed = merged["survivor"], merged["absorbed"]
    _seed_findings(RUN, [_finding("opp-1", "identity-foreign", [survivor])],
                   org_id=OTHER_ORG)

    outcome = eu.unmerge_entity(ORG, absorbed, actor="analyst-1", max_runs=500)

    assert outcome.flagged_findings == 0
    assert fr.list_flags(ORG) == []


# ── audit ───────────────────────────────────────────────────────────────────


def _audit_events(event_type: str, org_id: str = ORG) -> List[Dict[str, Any]]:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT event_type, payload, user_id FROM audit_log "
            "WHERE org_id = %s AND event_type = %s ORDER BY timestamp DESC",
            (org_id, event_type),
        )
        return [
            {
                "event_type": r[0],
                "payload": json.loads(r[1]) if isinstance(r[1], str) and r[1] else {},
                "user_id": r[2],
            }
            for r in (cur.fetchall() or [])
        ]
    finally:
        con.close()


def test_the_unmerge_is_audited(merged):
    """An unmerge changes what every finding built on that entity is about, so it
    belongs in the org-wide stream with its actor."""
    from app.middleware.audit import ENTITY_UNMERGED

    before = len(_audit_events(ENTITY_UNMERGED))
    outcome = eu.unmerge_entity(
        ORG, merged["absorbed"], actor="analyst-1", reason="wrong pair"
    )
    events = _audit_events(ENTITY_UNMERGED)

    assert len(events) == before + 1
    assert events[0]["user_id"] == "analyst-1"
    payload = events[0]["payload"]
    assert payload["unmerge_id"] == outcome.unmerge_id
    assert payload["survivor_entity_id"] == merged["survivor"]
    assert payload["detached_entity_id"] == merged["absorbed"]
    assert payload["previous_rule"] == em.RULE_EXPLICIT_REFERENCE
    assert payload["reason"] == "wrong pair"


def test_the_release_is_audited_separately(merged):
    """Its own event, because it is the more consequential half: one person undoing
    another's correction."""
    from app.middleware.audit import ENTITY_MERGE_BLOCK_RELEASED

    outcome = eu.unmerge_entity(ORG, merged["absorbed"], actor="analyst-1")
    before = len(_audit_events(ENTITY_MERGE_BLOCK_RELEASED))
    eu.release_merge_block(ORG, outcome.unmerge_id, actor="owner-1", reason="agreed")

    assert len(_audit_events(ENTITY_MERGE_BLOCK_RELEASED)) == before + 1
