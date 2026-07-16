"""
R18-A4 / AT-597 (T4) — conversation author → entity resolution.

Message authors are resolved to knowledge-graph entities WHERE THE ENTITY LAYER
ALREADY KNOWS THEM, so conversation participants link into the graph. Resolution
is conservative: only confident matches link (recorded under
``provenance['participant_entities']``); unresolved authors stay plain references
in ``provenance['participants']`` and nothing is ever created or merged.

These tests exercise the shared resolution seam with an injected resolver (no DB),
so the linking behaviour and the conservative discipline are provable in isolation.
The end-to-end proof against the real entity layer lives in
``backend/tests/contract/test_conversation_author_resolution_contract.py``.
"""
from __future__ import annotations

import app.db as app_db
from discovery.ingest.conversation_content import (
    ConversationMessage,
    ConversationThread,
    _participant_identity,
    build_author_resolver,
    thread_to_artifact,
)
from discovery.ingest.slack import SlackIngestor


def _teams_msg(author, user_id, text, msg_id, thread_key):
    return ConversationMessage(
        container_id="T-eng/19:ops",
        container_name="ops",
        msg_id=msg_id,
        thread_key=thread_key,
        sort_key=float(msg_id[1:]) if msg_id[1:].isdigit() else 0.0,
        iso_ts="2026-06-10T09:00:00Z",
        author=author,
        text=text,
        extra={"team_id": "T-eng", "channel_id": "19:ops", "user_id": user_id},
    )


def _thread(messages):
    return ConversationThread(
        source_system="teams",
        container_id="T-eng/19:ops",
        container_name="ops",
        key=messages[0].msg_id,
        messages=messages,
        is_window=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# thread_to_artifact — participant_entities
# ─────────────────────────────────────────────────────────────────────────────
def test_known_participants_link_unknown_stay_plain_refs():
    thread = _thread([
        _teams_msg("Ada Lovelace", "U1", "pager went off", "m100", None),
        _teams_msg("Unknown Person", "U9", "on it", "m200", "m100"),
    ])

    def resolver(display_name, source_record_id):
        if display_name == "Ada Lovelace":
            return {
                "entity_id": "ent-ada",
                "canonical_name": "ada lovelace",
                "display_name": "Ada Lovelace",
                "resolution_confidence": 0.8,
                "resolution_status": "resolved",
            }
        return None

    art = thread_to_artifact(thread, author_resolver=resolver)

    # Confident match linked into the graph; keyed by the raw author ref.
    assert art.provenance["participant_entities"] == [
        {
            "ref": "Ada Lovelace",
            "entity_id": "ent-ada",
            "canonical_name": "ada lovelace",
            "display_name": "Ada Lovelace",
            "resolution_confidence": 0.8,
            "resolution_status": "resolved",
        }
    ]
    # The unresolved author remains a plain reference — never dropped, never merged.
    assert art.provenance["participants"] == ["Ada Lovelace", "Unknown Person"]


def test_no_resolver_yields_empty_participant_entities():
    thread = _thread([_teams_msg("Ada", "U1", "hi", "m100", None)])
    art = thread_to_artifact(thread)  # no resolver
    assert art.provenance["participant_entities"] == []
    assert art.provenance["participants"] == ["Ada"]


def test_all_unresolved_stay_plain_refs():
    thread = _thread([
        _teams_msg("Nobody One", "U1", "a", "m100", None),
        _teams_msg("Nobody Two", "U2", "b", "m200", "m100"),
    ])
    art = thread_to_artifact(thread, author_resolver=lambda d, s: None)
    assert art.provenance["participant_entities"] == []
    assert art.provenance["participants"] == ["Nobody One", "Nobody Two"]


def test_resolver_exception_isolated_to_one_author():
    thread = _thread([
        _teams_msg("Boom", "U1", "a", "m100", None),
        _teams_msg("Ada", "U2", "b", "m200", "m100"),
    ])

    def resolver(display_name, source_record_id):
        if display_name == "Boom":
            raise RuntimeError("resolver blew up")
        return {"entity_id": "ent-ada", "canonical_name": "ada",
                "display_name": "Ada", "resolution_confidence": 1.0,
                "resolution_status": "resolved"}

    art = thread_to_artifact(thread, author_resolver=resolver)
    refs = [p["ref"] for p in art.provenance["participant_entities"]]
    assert refs == ["Ada"]  # the exploding author is simply not linked


def test_duplicate_author_resolved_once():
    thread = _thread([
        _teams_msg("Ada", "U1", "a", "m100", None),
        _teams_msg("Ada", "U1", "b", "m200", "m100"),
    ])
    calls = []

    def resolver(display_name, source_record_id):
        calls.append(display_name)
        return {"entity_id": "ent-ada", "canonical_name": "ada",
                "display_name": "Ada", "resolution_confidence": 1.0,
                "resolution_status": "resolved"}

    art = thread_to_artifact(thread, author_resolver=resolver)
    assert calls == ["Ada"]  # distinct participants resolved once
    assert len(art.provenance["participant_entities"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# _participant_identity — platform-uniform (display_name, source_record_id)
# ─────────────────────────────────────────────────────────────────────────────
def test_participant_identity_teams_uses_user_id():
    m = _teams_msg("Ada Lovelace", "U-ada", "hi", "m100", None)
    assert _participant_identity(m) == ("Ada Lovelace", "U-ada")


def test_participant_identity_slack_falls_back_to_author():
    # Slack messages carry no user_id in extra — the author IS the user id.
    m = ConversationMessage(
        container_id="C001", container_name="ops", msg_id="1.0", thread_key=None,
        sort_key=1.0, iso_ts=None, author="U100", text="hi", extra={"channel_id": "C001"},
    )
    assert _participant_identity(m) == ("U100", "U100")


# ─────────────────────────────────────────────────────────────────────────────
# build_author_resolver — no DB → no-op (offline safe)
# ─────────────────────────────────────────────────────────────────────────────
def test_build_author_resolver_is_noop_without_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert build_author_resolver("org", "slack") is None


# ─────────────────────────────────────────────────────────────────────────────
# ingest_deep_content threads the injected resolver into provenance
# ─────────────────────────────────────────────────────────────────────────────
def test_ingest_deep_content_populates_participant_entities(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")

    def fake_get(org_id, connector_id):
        return {"id": "slack", "status": "connected"} if connector_id == "slack" else None

    monkeypatch.setattr(app_db, "org_connector_get", fake_get)

    captured = []

    def substrate(org_id, artifacts):
        from app.retrieval.ingest import ArtifactIngestResult, IngestResult
        artifacts = list(artifacts)
        captured.extend(artifacts)
        res = IngestResult(org_id=org_id, artifacts_received=len(artifacts))
        for a in artifacts:
            res.artifacts_indexed += 1
            res.chunks_indexed += 1
            res.artifacts.append(ArtifactIngestResult(a.source_system, a.source_artifact, "indexed", chunks_indexed=1))
        return res

    def resolver(display_name, source_record_id):
        if display_name == "U100":
            return {"entity_id": "ent-1", "canonical_name": "u100",
                    "display_name": "U100", "resolution_confidence": 1.0,
                    "resolution_status": "resolved"}
        return None

    rec = {
        "channel_id": "C001", "channel_name": "ops", "ts": "1000.0000",
        "user": "U100", "text": "alpha", "reply_count": 0, "change_kind": "created",
    }
    SlackIngestor().ingest_deep_content(
        "org_a", [rec], ingest_fn=substrate, freshness_fn=lambda e: None, author_resolver=resolver,
    )
    assert captured
    assert captured[0].provenance["participant_entities"] == [
        {"ref": "U100", "entity_id": "ent-1", "canonical_name": "u100",
         "display_name": "U100", "resolution_confidence": 1.0, "resolution_status": "resolved"}
    ]
