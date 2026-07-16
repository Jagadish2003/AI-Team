"""
R18-A4 / AT-597 (T4) — conversation author → entity resolution, end to end (AC5).

Proves against the REAL entity layer and the REAL retrieval store that conversation
participants link into the knowledge graph where the entity layer already knows
them, following the conservative-resolution discipline:

  * A message author that matches a single resolved ``person`` entity is linked —
    the indexed thread chunk's provenance carries the graph ``entity_id`` alongside
    the thread-level evidence pointer, so a finding citing the thread traces the
    participant into the graph (AC5).
  * An author with no confident match stays a plain reference (never linked).
  * ``lookup_resolved_entity`` is conservative: an ambiguous canonical (several
    resolved rows) or an ``ambiguous``-status row never links, and nothing is ever
    created or merged by the read.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app import db
from app.entity_resolution import lookup_resolved_entity, resolve_or_create_entity
from app.entity_resolution import _insert_entity  # direct insert for ambiguous seeding
from database.models.entities import Entity
from discovery.ingest.teams import TeamsIngestor


def _retrieval_store_available() -> bool:
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT to_regclass('public.retrieval_chunks')")
            has_chunks = cur.fetchone()[0] is not None
            cur.execute("SELECT to_regclass('public.entities')")
            has_entities = cur.fetchone()[0] is not None
            return has_chunks and has_entities
        finally:
            con.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _retrieval_store_available(),
    reason="retrieval_chunks / entities store not present in this environment",
)


def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        cur.execute("DELETE FROM entities WHERE org_id = %s", (org_id,))
        con.commit()
    finally:
        con.close()


def _insert_person(org_id, canonical, display, *, status="resolved", source_system="jira", source_record_id=None):
    con = db.connect()
    try:
        _insert_entity(
            con,
            Entity(
                org_id=org_id,
                entity_type="person",
                canonical_name=canonical,
                display_name=display,
                source_system=source_system,
                source_record_id=source_record_id,
                resolution_confidence=0.8,
                resolution_status=status,
                first_seen_run_id="r_seed",
                last_seen_run_id="r_seed",
                run_count=1,
            ),
        )
        con.commit()
    finally:
        con.close()


def _thread_provenance(org_id, source_artifact):
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT provenance FROM retrieval_chunks "
            "WHERE org_id = %s AND source_artifact = %s LIMIT 1",
            (org_id, source_artifact),
        )
        row = cur.fetchone()
        if not row:
            return None
        prov = row[0]
        return json.loads(prov) if isinstance(prov, str) else prov
    finally:
        con.close()


@pytest.fixture
def org(request, monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")
    name = f"conv_authors_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


def _trec(msg_id, text, display, user_id, *, reply_to_id=None, reply_count=0):
    return {
        "team_id": "T-eng", "team_name": "Engineering",
        "channel_id": "19:ops", "channel_name": "ops-incidents",
        "message_id": msg_id, "reply_to_id": reply_to_id,
        "created_at": "2026-06-10T09:00:00Z", "last_modified_at": None,
        "user": user_id, "user_display_name": display, "text": text,
        "reply_count": reply_count, "change_kind": "created",
    }


# ===========================================================================
# AC5 — a known participant links into the graph; an unknown one stays plain
# ===========================================================================
def test_ac5_known_participant_links_into_graph(org):
    # The entity layer already knows "Ada Lovelace" (a resolved person from Jira).
    seeded = resolve_or_create_entity(
        org_id=org, entity_type="person", display_name="Ada Lovelace",
        source_system="jira", source_record_id=None, run_id="r1",
    )
    assert seeded.resolution_status == "resolved"

    # A Teams thread: Ada (known) starts it, an unknown person replies.
    records = [
        _trec("m100", "pager went off for payments", "Ada Lovelace", "U-ada", reply_count=1),
        _trec("m200", "on it", "Unknown Responder", "U-unknown", reply_to_id="m100"),
    ]
    result = TeamsIngestor().ingest_deep_content(org, records, freshness_fn=lambda e: None)
    assert result.artifacts_handed_off == 1

    prov = _thread_provenance(org, "T-eng/19:ops:m100")
    assert prov is not None, "thread was not indexed"

    # Both participants are retained as plain references (never dropped).
    assert prov["participants"] == ["Ada Lovelace", "Unknown Responder"]

    # Only the confidently-known author is linked into the graph (AC5).
    links = prov["participant_entities"]
    assert [l["ref"] for l in links] == ["Ada Lovelace"]
    assert links[0]["entity_id"] == str(seeded.id)
    assert links[0]["canonical_name"] == "ada lovelace"
    assert links[0]["resolution_status"] == "resolved"

    # The thread-level evidence pointer still traces to the exact thread (AC5 spine).
    assert prov["evidence_pointer"]["source_artifact"] == "T-eng/19:ops:m100"
    assert prov["evidence_pointer"]["origin"] == "observed"


def test_ac5_unknown_author_never_links(org):
    records = [_trec("m900", "nobody knows me", "Complete Stranger", "U-x")]
    TeamsIngestor().ingest_deep_content(org, records, freshness_fn=lambda e: None)

    prov = _thread_provenance(org, "T-eng/19:ops:m900")
    assert prov is not None
    assert prov["participants"] == ["Complete Stranger"]
    assert prov["participant_entities"] == []  # conservative: no confident match


# ===========================================================================
# lookup_resolved_entity — conservative discipline (read-only)
# ===========================================================================
def test_lookup_returns_single_resolved_match(org):
    seeded = resolve_or_create_entity(
        org_id=org, entity_type="person", display_name="Grace Hopper",
        source_system="jira", source_record_id=None, run_id="r1",
    )
    found = lookup_resolved_entity(org_id=org, entity_type="person", display_name="grace hopper")
    assert found is not None and str(found.id) == str(seeded.id)


def test_lookup_ambiguous_canonical_does_not_link(org):
    # Two DISTINCT resolved rows share a canonical → ambiguous → never link.
    _insert_person(org, "sam carter", "Sam Carter", source_system="jira")
    _insert_person(org, "sam carter", "Sam Carter", source_system="servicenow")
    assert lookup_resolved_entity(org_id=org, entity_type="person", display_name="Sam Carter") is None


def test_lookup_ambiguous_status_row_does_not_link(org):
    _insert_person(org, "pat jones", "Pat Jones", status="ambiguous")
    assert lookup_resolved_entity(org_id=org, entity_type="person", display_name="Pat Jones") is None


def test_lookup_unknown_returns_none(org):
    assert lookup_resolved_entity(org_id=org, entity_type="person", display_name="No Such Person") is None


def test_lookup_matches_by_source_record_id(org):
    seeded = resolve_or_create_entity(
        org_id=org, entity_type="person", display_name="Teams Native User",
        source_system="teams", source_record_id="U-native", run_id="r1",
    )
    # A different display name, but the same source id → still a confident match.
    found = lookup_resolved_entity(
        org_id=org, entity_type="person", display_name="different label",
        source_system="teams", source_record_id="U-native",
    )
    assert found is not None and str(found.id) == str(seeded.id)


def test_lookup_never_creates_rows(org):
    lookup_resolved_entity(org_id=org, entity_type="person", display_name="Phantom")
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM entities WHERE org_id = %s", (org,))
        assert cur.fetchone()[0] == 0  # read-only: nothing created
    finally:
        con.close()
