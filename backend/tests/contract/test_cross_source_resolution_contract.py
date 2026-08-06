"""2.0-B2 T1 contract tests — the ranked resolution engine over real tables.

The engine's decision logic is pinned DB-free in
``tests/unit/test_cross_source_resolution.py``. This file covers the half that
only a database can prove: that the loaders feed the engine correctly from the
real ``entities`` / ``entity_relationships`` tables, and that the tenancy
boundary holds in SQL rather than only in the engine's own gate.

  AC1 (end to end) — a seeded estate where one pair carries an explicit
       cross-reference and another pair only shares a name resolves the first
       automatically and merely PROPOSES the second.

Also covered: the alias table round-tripping through the real ``kv`` layer,
observed-vs-inferred edges in the corroboration index, and two-org isolation
(the AC6 property, built in from the start rather than retrofitted).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from app import cross_source_resolution as csr
from app import db
from app import entity_alias_mappings as eam


ORG = "csr-org-a"
OTHER_ORG = "csr-org-b"
RUN = "run_csr_t1"


# ─────────────────────────────────────────────────────────────────────────────
# Seeding
# ─────────────────────────────────────────────────────────────────────────────


def _insert_entity(
    display_name: str,
    *,
    source_system: str,
    source_record_id: Optional[str] = None,
    org_id: str = ORG,
    entity_type: str = "system",
    status: str = "resolved",
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Insert one entity row and return its id.

    The id is a real UUID because ``Entity.from_db_row`` parses the column as
    one — a readable slug here would fail in the loader, not in the seed.
    """
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
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                entity_id, org_id, entity_type,
                " ".join(display_name.split()).lower(), display_name,
                source_system, source_record_id, 1.0, status, RUN, RUN, 1,
                json.dumps(metadata) if metadata is not None else None, now, now,
            ),
        )
        con.commit()
    finally:
        con.close()
    return entity_id


def _insert_edge(from_id: str, to_id: str, rel_type: str, *, inferred: bool = False,
                 org_id: str = ORG) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO entity_relationships (
                id, org_id, from_entity_id, to_entity_id, relationship_type,
                confidence, inferred, evidence, first_seen_run_id,
                last_seen_run_id, run_count, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (org_id, from_entity_id, to_entity_id, relationship_type)
            DO NOTHING
            """,
            (
                str(uuid.uuid4()), org_id, from_id, to_id, rel_type,
                0.6 if inferred else 0.9, bool(inferred), None, RUN, RUN, 1,
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
            cur.execute("DELETE FROM entity_relationships WHERE org_id = %s", (org,))
            cur.execute("DELETE FROM entities WHERE org_id = %s", (org,))
            cur.execute("DELETE FROM kv WHERE key = %s", (f"{eam.ALIAS_KV_KEY}:{org}",))
        con.commit()
    finally:
        con.close()


@pytest.fixture
def estate(monkeypatch):
    """A seeded two-source estate:

      referenced pair  — ServiceNow 'Payments Platform' cites the Jira record's
                         own id (explicit cross-reference → auto-merge);
      name-only pair   — ServiceNow 'Billing' and git 'billing' share a name and
                         a corroborating observed relationship (→ proposal only);
      decoy            — the same name in ANOTHER org (must never be matched).
    """
    monkeypatch.delenv(eam.ALIAS_ENV_VAR, raising=False)
    _cleanup()

    ids = {
        "sn_payments": _insert_entity(
            "Payments Platform",
            source_system="servicenow", source_record_id="sn-1",
            metadata={"cross_references": [
                {"system": "jira", "record_id": "PAY", "field": "correlation_id"}
            ]},
        ),
        "jira_payments": _insert_entity(
            "Payments", source_system="jira", source_record_id="PAY",
        ),
        "sn_billing": _insert_entity(
            "Billing", source_system="servicenow", source_record_id="sn-2",
        ),
        "git_billing": _insert_entity(
            "billing", source_system="git", source_record_id="repo-1",
        ),
        "team": _insert_entity(
            "Platform Team", source_system="servicenow", source_record_id="sn-team",
        ),
        "other_org": _insert_entity(
            "Billing", source_system="jira", source_record_id="sn-2", org_id=OTHER_ORG,
        ),
    }
    # The corroborating relationship behind the name-only proposal.
    _insert_edge(ids["sn_billing"], ids["team"], "depends_on")
    _insert_edge(ids["git_billing"], ids["team"], "depends_on")

    yield ids
    _cleanup()


def _by_subject(decisions):
    return {d.subject.entity_id: d for d in decisions}


# ─────────────────────────────────────────────────────────────────────────────
# AC1 end to end
# ─────────────────────────────────────────────────────────────────────────────


def test_ac1_explicit_reference_resolves_and_name_only_is_proposed(estate):
    decisions = _by_subject(csr.resolve_org_entity_type(ORG, "system"))

    referenced = decisions[estate["sn_payments"]]
    assert referenced.status == csr.STATUS_RESOLVED
    assert referenced.is_merge is True
    assert referenced.tier == csr.TIER_EXPLICIT_REFERENCE
    assert referenced.merge_target.entity_id == estate["jira_payments"]

    name_only = decisions[estate["sn_billing"]]
    assert name_only.status == csr.STATUS_PROPOSED
    assert name_only.is_merge is False, "a name match must never merge"
    assert name_only.merge_target is None
    assert [p.target.entity_id for p in name_only.proposals] == [estate["git_billing"]]
    assert name_only.proposals[0].action == csr.ACTION_PROPOSE


def test_the_engine_writes_nothing_to_the_graph(estate):
    """T1 decides; applying a merge is a later task. Nothing about the seeded
    rows may change as a result of resolving them."""
    def _snapshot():
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT id, canonical_name, resolution_status, resolution_confidence, "
                "updated_at FROM entities WHERE org_id = %s ORDER BY id",
                (ORG,),
            )
            return [tuple(r) for r in cur.fetchall()]
        finally:
            con.close()

    before = _snapshot()
    csr.resolve_org_entity_type(ORG, "system")
    assert _snapshot() == before


# ─────────────────────────────────────────────────────────────────────────────
# Loaders
# ─────────────────────────────────────────────────────────────────────────────


def test_the_candidate_loader_is_org_scoped_in_sql(estate):
    loaded = csr.load_resolution_entities(ORG, "system")
    ids = {e.entity_id for e in loaded}
    assert estate["sn_payments"] in ids
    assert estate["other_org"] not in ids, "another tenant's entity is not even loaded"
    assert all(e.org_id == ORG for e in loaded)


def test_the_loader_reads_cross_references_off_stored_metadata(estate):
    loaded = {e.entity_id: e for e in csr.load_resolution_entities(ORG, "system")}
    refs = loaded[estate["sn_payments"]].cross_references
    assert [(r.system, r.record_id) for r in refs] == [("jira", "PAY")]


def test_the_loader_excludes_ambiguous_rows_by_default(estate):
    unsure = _insert_entity(
        "Payments", source_system="aws", source_record_id="arn-1", status="ambiguous",
    )
    ids = {e.entity_id for e in csr.load_resolution_entities(ORG, "system")}
    assert unsure not in ids
    assert unsure in {
        e.entity_id for e in csr.load_resolution_entities(ORG, "system", resolved_only=False)
    }


def test_the_relationship_index_carries_observed_edges_only(estate):
    _insert_edge(estate["sn_payments"], estate["team"], "routes_to", inferred=True)

    index = csr.load_relationship_index(ORG)

    assert index.shared(estate["sn_billing"], estate["git_billing"]) == (
        ("depends_on", estate["team"]),
    )
    # The inferred edge is absent, so it cannot corroborate anything.
    assert ("routes_to", estate["team"]) not in index.for_entity(estate["sn_payments"])
    assert ("routes_to", estate["team"]) in csr.load_relationship_index(
        ORG, include_inferred=True
    ).for_entity(estate["sn_payments"])


def test_a_name_match_without_a_corroborating_edge_is_not_proposed(estate):
    """Same estate, corroboration removed — the pair drops out of the review
    queue rather than becoming a proposal."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "DELETE FROM entity_relationships WHERE org_id = %s AND from_entity_id = %s",
            (ORG, estate["git_billing"]),
        )
        con.commit()
    finally:
        con.close()

    decisions = _by_subject(csr.resolve_org_entity_type(ORG, "system"))
    billing = decisions[estate["sn_billing"]]
    assert billing.status == csr.STATUS_UNRESOLVED
    assert billing.considered["name_matches_not_proposed"][0]["reason"] == (
        csr.REASON_NO_CORROBORATION
    )


# ─────────────────────────────────────────────────────────────────────────────
# Alias table through the real kv layer
# ─────────────────────────────────────────────────────────────────────────────


def test_a_stored_alias_mapping_auto_merges_across_sources(estate):
    git_payments = _insert_entity(
        "payments-api", source_system="git", source_record_id="repo-2",
    )
    sn_payments_api = _insert_entity(
        "Payments API", source_system="servicenow", source_record_id="sn-3",
    )
    eam.put_alias_mappings(ORG, [
        {"entity_type": "system", "canonical": "payments-api",
         "aliases": ["Payments API"], "created_by": "owner@example.com"}
    ])

    decision = _by_subject(csr.resolve_org_entity_type(ORG, "system"))[sn_payments_api]

    assert decision.status == csr.STATUS_RESOLVED
    assert decision.tier == csr.TIER_ALIAS_MAPPING
    assert decision.merge_target.entity_id == git_payments
    assert decision.matches[0].evidence["created_by"] == "owner@example.com"


def test_one_orgs_alias_table_never_applies_to_another(estate):
    _insert_entity(
        "payments-api", source_system="git", source_record_id="repo-2", org_id=OTHER_ORG,
    )
    b_sn = _insert_entity(
        "Payments API", source_system="servicenow", source_record_id="sn-3",
        org_id=OTHER_ORG,
    )
    eam.put_alias_mappings(ORG, [
        {"entity_type": "system", "canonical": "payments-api", "aliases": ["Payments API"]}
    ])

    decisions = _by_subject(csr.resolve_org_entity_type(OTHER_ORG, "system"))

    assert decisions[b_sn].status != csr.STATUS_RESOLVED, (
        "org A's alias table must not merge org B's entities"
    )
    assert eam.get_alias_mappings(OTHER_ORG) == []


def test_two_org_isolation_holds_across_the_whole_pass(estate):
    """The decoy in the other org shares a name AND a source_record_id with an
    org-A entity — it must appear in neither pass's decisions."""
    a_ids = {d.subject.entity_id for d in csr.resolve_org_entity_type(ORG, "system")}
    b_ids = {d.subject.entity_id for d in csr.resolve_org_entity_type(OTHER_ORG, "system")}

    assert estate["other_org"] not in a_ids
    assert estate["sn_billing"] not in b_ids
    for decision in csr.resolve_org_entity_type(ORG, "system"):
        assert decision.considered["dropped"]["cross_org"] == 0, (
            "cross-org candidates are filtered in SQL, before the engine's gate"
        )
        targets = [m.target.entity_id for m in decision.matches]
        assert estate["other_org"] not in targets


def test_an_empty_org_resolves_to_no_decisions():
    assert csr.resolve_org_entity_type("csr-org-empty", "system") == []
