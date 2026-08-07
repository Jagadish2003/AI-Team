"""2.0-B2 T5 — unmerge & re-evaluation flagging (DB-free).

AC4: "Unmerge restores constituents and flags dependent findings for re-evaluation."

The restore itself needs a database to demonstrate, so it lives in
``tests/contract/test_entity_unmerge_contract.py``. What is pinned here is the
reasoning the restore depends on, each piece of which is wrong by default:

  * **the pair keys** — a reversal has to be recognisable as covering "this pair"
    on a later run, when the entity rows may have churned;
  * **the subtree derivation** — a chain of merges has to come apart at the joint
    that was reversed and no other, which the flat constituent list cannot say;
  * **the dependency link** — which findings referenced the entity, and the honest
    treatment of the ones that cannot be told either way;
  * **the fail-closed block read** — an unreadable block must refuse the merge, not
    permit it.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from app import entity_merge as em
from app import entity_unmerge as eu


# ── the pair keys ───────────────────────────────────────────────────────────


def _side(entity_id: str, system: str, name: str) -> Dict[str, Any]:
    return {
        "id": entity_id,
        "entity_type": "system",
        "source_system": system,
        "canonical_name": name,
        "display_name": name,
    }


def test_the_row_pair_key_is_order_independent():
    """A→B and B→A are one pair; a block filed one way must match the other."""
    assert eu.row_pair_key("a", "b") == eu.row_pair_key("b", "a")


def test_the_row_pair_key_refuses_an_unusable_pair():
    with pytest.raises(eu.EntityUnmergeError):
        eu.row_pair_key("a", "a")
    with pytest.raises(eu.EntityUnmergeError):
        eu.row_pair_key("", "b")


def test_the_identity_pair_key_is_order_independent_and_normalised():
    """The churn-resistant key: it must survive the entity rows being replaced, so
    it is built from source system and canonical name only."""
    left = _side("row-1", "servicenow", "Payments  API")
    right = _side("row-2", "jira", "payments api")
    assert eu.identity_pair_key(left, right) == eu.identity_pair_key(right, left)
    # Same identities, different row ids → the same key. This is the whole point.
    churned_left = _side("row-99", "ServiceNow", "payments api")
    assert eu.identity_pair_key(churned_left, right) == eu.identity_pair_key(left, right)


def test_the_identity_pair_key_is_none_when_a_side_cannot_supply_one():
    """A partial key would match the WRONG pair, and blocking the wrong pair is
    worse than not blocking this one — the row key still covers it."""
    assert eu.identity_pair_key(_side("a", "", "payments"), _side("b", "jira", "p")) is None
    assert eu.identity_pair_key(_side("a", "servicenow", ""), _side("b", "jira", "p")) is None


def test_two_distinct_rows_of_one_identity_still_produce_a_key():
    """Same system + same canonical name across two DISTINCT rows is precisely the
    pair name-similarity proposes for merge — so it is the pair a human separation
    most needs to survive row churn. It must therefore yield an identity key
    (``ident:x|x``), not ``None``: otherwise only the row-id key is recorded and a
    later row replacement silently re-merges the deliberately-separated pair.

    The key blocks re-merging ONLY two same-identity rows; a merge of this identity
    with a DIFFERENT one keys as ``ident:x|y`` and is unaffected — precise, not a
    blanket suppression.
    """
    same = _side("a", "servicenow", "Payments")
    other = _side("b", "servicenow", "payments")
    key = eu.identity_pair_key(same, other)
    assert key is not None
    # Normalised + order-independent, and identical after a row churn.
    assert key == eu.identity_pair_key(other, same)
    assert key == eu.identity_pair_key(_side("row-99", "ServiceNow", "PAYMENTS"), other)
    # A different identity on one side keys differently and is not blocked by it.
    assert eu.identity_pair_key(same, _side("c", "jira", "payments")) != key


def test_both_keys_are_recorded_for_a_normal_pair():
    keys = eu.pair_keys_for(
        _side("a", "servicenow", "Payments"), _side("b", "jira", "Payments")
    )
    assert [kind for kind, _ in keys] == [eu.PAIR_KEY_ROWS, eu.PAIR_KEY_IDENTITY]


def test_the_row_key_alone_is_recorded_when_no_identity_key_exists():
    """A pair the identity key cannot name is still blockable — losing the row key
    too would mean an unmerge that blocks nothing."""
    keys = eu.pair_keys_for(_side("a", "servicenow", "Payments"), _side("b", "", ""))
    assert [kind for kind, _ in keys] == [eu.PAIR_KEY_ROWS]


# ── the subtree: a chain comes apart at ONE joint ───────────────────────────


def _row(entity_id: str, merged_into: str | None = None) -> Dict[str, Any]:
    metadata = {"merged_into": {"entity_id": merged_into}} if merged_into else {}
    return {"id": entity_id, "metadata": json.dumps(metadata) if metadata else None}


def test_a_detached_entity_takes_its_own_sub_merge_with_it():
    """A→B→C: detaching B from C must hand back B *with A still merged into it*.

    The sub-merge is not the one being reversed. C's flat constituent list holds A
    and B side by side and cannot express that; the pointers can, because only the
    entity being absorbed ever has its pointer written.
    """
    rows = {"A": _row("A", "B"), "B": _row("B", "C"), "C": _row("C")}
    assert eu.detached_subtree("B", rows) == ["A", "B"]


def test_a_sibling_constituent_is_left_alone():
    """Detaching B must not disturb Z, which was merged into C independently."""
    rows = {"A": _row("A", "B"), "B": _row("B", "C"), "Z": _row("Z", "C"), "C": _row("C")}
    subtree = eu.detached_subtree("B", rows)
    assert subtree == ["A", "B"]
    assert "Z" not in subtree


def test_the_subtree_does_not_depend_on_iteration_order():
    """A child may be visited before its parent; the derivation iterates to a fixed
    point rather than trusting dict order."""
    deep = {
        "D": _row("D", "C"), "C": _row("C", "B"), "B": _row("B", "A"), "A": _row("A")
    }
    assert eu.detached_subtree("B", deep) == ["B", "C", "D"]
    reversed_order = dict(reversed(list(deep.items())))
    assert eu.detached_subtree("B", reversed_order) == ["B", "C", "D"]


def test_an_unknown_entity_has_a_subtree_of_only_itself():
    assert eu.detached_subtree("ghost", {}) == ["ghost"]


def test_an_empty_target_has_no_subtree():
    assert eu.detached_subtree("", {"A": _row("A")}) == []


def test_a_pointer_cycle_does_not_spin():
    """Corrupt metadata must degrade, not hang."""
    rows = {"A": _row("A", "B"), "B": _row("B", "A")}
    assert eu.detached_subtree("A", rows) == ["A", "B"]


# ── the dependency link ─────────────────────────────────────────────────────


def test_a_findings_entity_references_are_read_in_every_stored_shape():
    """The link must match what the enrichment layer already understands: plain id
    lists under three key spellings, and entity SUMMARY objects."""
    opp = {
        "entity_ids": ["e1"],
        "entityIds": ["e2"],
        "entities": [{"entity_id": "e3"}, {"id": "e4"}, "e5"],
    }
    assert eu._entity_ids_of_opp(opp) == {"e1", "e2", "e3", "e4", "e5"}


def test_a_finding_with_no_entity_references_yields_nothing():
    """It must yield nothing rather than something empty-but-truthy: the caller
    counts these as unassessed instead of flagging them."""
    assert eu._entity_ids_of_opp({"id": "opp-1", "title": "x"}) == set()
    assert eu._entity_ids_of_opp({"entities": "not-a-list"}) == set()


def test_malformed_entity_entries_are_skipped_not_fatal():
    assert eu._entity_ids_of_opp({"entities": [None, 42, {}, {"entity_id": ""}, "ok"]}) == {"ok"}


def test_the_sweep_is_a_no_op_without_an_org_or_entities():
    assert eu.dependent_findings("", ["e1"]).identities == ()
    assert eu.dependent_findings("org_a", []).identities == ()


# ── the block: fail closed ──────────────────────────────────────────────────


def test_an_unreadable_block_state_refuses_the_merge(monkeypatch):
    """The harmful direction is re-merging a pair somebody reversed, so a lookup
    failure reports a block rather than permitting the merge — and says why."""
    monkeypatch.setattr(eu, "ensure_reevaluation_tables", lambda: None)

    def _boom():
        raise RuntimeError("database is down")

    monkeypatch.setattr(eu.db, "connect", _boom)
    block = eu.merge_block_for(
        "org_a", _side("a", "servicenow", "Payments"), _side("b", "jira", "Payments")
    )
    assert block is not None
    assert block.is_blocked
    assert "unreadable" in (block.reason or "")


def test_a_pair_that_cannot_be_keyed_is_not_blocked(monkeypatch):
    """An entity paired with itself is refused by apply_merge on its own terms; the
    block lookup must not invent a block for it."""
    monkeypatch.setattr(eu, "ensure_reevaluation_tables", lambda: None)
    same = _side("a", "servicenow", "Payments")
    assert eu.merge_block_for("org_a", same, same) is None


def test_no_org_means_no_block():
    assert eu.merge_block_for("", _side("a", "sn", "p"), _side("b", "jira", "p")) is None


# ── apply_merge consults the block ──────────────────────────────────────────


class _Cur:
    """A cursor that answers apply_merge's reads for two unmerged, same-type rows."""

    def __init__(self, rows: Dict[str, Dict[str, Any]]):
        self._rows = rows
        self._last: List[Any] = []

    def execute(self, sql: str, params=None):
        self._last = [sql, params]

    def fetchone(self):
        sql, params = self._last
        if "FROM entities" in sql:
            return self._rows.get(str(params[1]))
        return None

    def fetchall(self):
        return []


def _merge_rows() -> Dict[str, Dict[str, Any]]:
    base = {
        "entity_type": "system",
        "metadata": None,
        "created_at": "2026-01-01T00:00:00Z",
        "source_record_id": None,
        "canonical_name": "payments",
        "display_name": "Payments",
    }
    return {
        "L": {**base, "id": "L", "source_system": "servicenow"},
        "R": {**base, "id": "R", "source_system": "jira"},
    }


@pytest.fixture
def merge_db(monkeypatch):
    """apply_merge over two mergeable rows, with writes captured."""
    writes: List[Any] = []
    rows = _merge_rows()

    class _Con:
        def cursor(self):
            cur = _Cur(rows)
            original = cur.execute

            def _record(sql, params=None):
                if "UPDATE entities" in str(sql):
                    writes.append(str(sql))
                original(sql, params)

            cur.execute = _record  # type: ignore[method-assign]
            return cur

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(em.db, "connect", lambda: _Con())
    return writes


def test_a_blocked_pair_is_refused_and_never_written(merge_db, monkeypatch):
    """The regression AC4 turns on: the appliers re-run continuously, so a pair
    somebody unmerged must be refused rather than merged again."""
    monkeypatch.setattr(
        em, "_merge_block_for",
        lambda cur, org, left, right: eu.MergeBlock(
            org_id=org, pair_key="rows:L|R", pair_key_kind=eu.PAIR_KEY_ROWS,
            unmerge_id="unm_abc", status=eu.STATUS_BLOCKED,
            survivor_entity_id="L", detached_entity_id="R", entity_type="system",
            previous_rule=em.RULE_CONFIRMED_PROPOSAL, restored_entity_ids=("R",),
            flagged_finding_count=2, unlinked_finding_count=0,
            reason="analyst says these are different services",
            actor_id="analyst-1", created_at="2026-08-01T00:00:00Z",
        ),
    )
    outcome = em.apply_merge("org_a", "L", "R", rule=em.RULE_EXPLICIT_REFERENCE)

    assert outcome.outcome == em.OUTCOME_BLOCKED
    assert outcome.applied is False
    assert "unmerged" in outcome.reason and "unm_abc" in outcome.reason
    assert merge_db == [], "a blocked merge must write nothing at all"


def test_an_unblocked_pair_still_merges(merge_db, monkeypatch):
    """The guard must not break the ordinary path — otherwise T5 silently disables
    merging altogether."""
    monkeypatch.setattr(em, "_merge_block_for", lambda cur, org, left, right: None)
    monkeypatch.setattr(em, "_audit_merge", lambda *a, **k: None)

    outcome = em.apply_merge("org_a", "L", "R", rule=em.RULE_EXPLICIT_REFERENCE)

    assert outcome.outcome == em.OUTCOME_MERGED
    assert len(merge_db) == 2, "survivor provenance + constituent pointer"


def test_blocked_is_distinct_from_skipped_in_the_report():
    """"This applier had no authority" and "a person reversed this" are different
    facts; one count for both would hide the reversal."""
    report = em._tally([
        em.MergeOutcome(outcome=em.OUTCOME_BLOCKED, rule="explicit_reference"),
        em.MergeOutcome(outcome=em.OUTCOME_SKIPPED, rule="name_similarity"),
        em.MergeOutcome(outcome=em.OUTCOME_MERGED, rule="alias_mapping"),
    ])
    assert (report.blocked, report.skipped, report.merged) == (1, 1, 1)
    assert report.to_dict()["blocked"] == 1


def test_the_block_check_runs_for_every_merge_rule(merge_db, monkeypatch):
    """An auto-merge tier must be blocked too: the source cross-reference is still
    there after an unmerge, so exempting the auto tiers would defeat the reversal on
    the very next run."""
    seen: List[str] = []

    def _blocked(cur, org, left, right):
        seen.append("checked")
        return eu.MergeBlock(
            org_id=org, pair_key="k", pair_key_kind=eu.PAIR_KEY_ROWS, unmerge_id="u",
            status=eu.STATUS_BLOCKED, survivor_entity_id="L", detached_entity_id="R",
            entity_type="system", previous_rule=None, restored_entity_ids=(),
            flagged_finding_count=0, unlinked_finding_count=0, reason=None,
            actor_id="a", created_at=None,
        )

    monkeypatch.setattr(em, "_merge_block_for", _blocked)
    for rule in em.MERGE_RULES:
        assert em.apply_merge("org_a", "L", "R", rule=rule).outcome == em.OUTCOME_BLOCKED
    assert len(seen) == len(em.MERGE_RULES)


# ── outcome shapes ──────────────────────────────────────────────────────────


def test_the_unmerge_outcome_reports_the_sweep_it_performed():
    """A caller must be able to see how much of the estate was examined, not just
    the number flagged — an unbounded sweep is not what happened."""
    sweep = eu.DependencySweep(
        identities=("id-1",), findings_examined=12, unlinked=3,
        runs_scanned=25, runs_truncated=4,
    )
    payload = eu.UnmergeOutcome(
        outcome=eu.OUTCOME_UNMERGED, survivor_entity_id="S", detached_entity_id="D",
        unmerge_id="unm_1", restored_entity_ids=("D",), sweep=sweep, flagged_findings=1,
    ).to_dict()

    assert payload["outcome"] == "unmerged"
    assert payload["dependencySweep"]["unlinkedFindings"] == 3
    assert payload["dependencySweep"]["runsTruncated"] == 4


def test_an_unmerge_needs_an_org_and_an_entity():
    with pytest.raises(eu.EntityUnmergeError):
        eu.unmerge_entity("", "e1")
    with pytest.raises(eu.EntityUnmergeError):
        eu.unmerge_entity("org_a", "")


def test_releasing_a_block_must_record_who_did_it():
    """An anonymous release of somebody else's correction is not acceptable."""
    with pytest.raises(eu.EntityUnmergeError):
        eu.release_merge_block("org_a", "unm_1", actor="")
