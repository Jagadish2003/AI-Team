"""
R18-A4 / AT-595 (T2) — Teams deep-content path over Microsoft Graph messages.

Mirrors the Slack deep-content path (T1/AT-594) using the SHARED conversation
model (:mod:`discovery.ingest.conversation_content`): the two platforms diverge
ONLY at the collection edge (turning a Graph delta record into a neutral
``ConversationMessage`` + resolving the granted-channel scope). Everything after —
thread assembly, author-attributed rendering, thread-level provenance, substrate
hand-off — is the shared model.

These tests exercise the path WITHOUT a database by injecting a fake substrate
(``ingest_fn``). The end-to-end "delivered and subsequently retrievable" proof
against the real pgvector substrate (AC1) lives in
``backend/tests/contract/test_teams_retrieval_handoff.py``.

Covered here:
  * AC1 (delivery) — granted-channel conversation is assembled into thread/window
    artifacts and handed to ``ingest_content`` with content_type='conversation'
    and thread-level provenance (origin='observed', evidence pointer, team/channel).
  * AC2 (scope) — content from ungranted, private, archived channels (and any
    non-enumerated DM) is never assembled or handed off.
  * Thread assembly — replies group with their parent via reply_to_id; standalone
    messages window by time; author-attributed rendering (display name).
  * The reach signal path is untouched (records still carry their signal block).
  * Incremental: the depth path rides the shared ``(org, 'teams')`` checkpoint and
    re-hands nothing on an unchanged second run.
"""
from __future__ import annotations

from typing import List, Optional

import pytest

from app.retrieval.ingest import ArtifactIngestResult, ContentArtifact, IngestResult
from discovery.ingest import change_runner
from discovery.ingest.conversation_content import CONTENT_TYPE
from discovery.ingest.teams import (
    RETRIEVAL_SOURCE_SYSTEM,
    TeamsDeepContentError,
    TeamsIngestor,
)

ORG = "org_teams_deep"


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles
# ─────────────────────────────────────────────────────────────────────────────
class FakeSubstrate:
    """Stands in for ``retrieval.ingest_content`` — records every hand-off."""

    def __init__(self, *, fail: Optional[set] = None):
        self.calls: List[tuple] = []
        self.artifacts: List[ContentArtifact] = []
        self._fail = set(fail or ())

    def __call__(self, org_id: str, artifacts) -> IngestResult:
        artifacts = list(artifacts)
        self.calls.append((org_id, artifacts))
        self.artifacts.extend(artifacts)
        result = IngestResult(org_id=org_id, artifacts_received=len(artifacts))
        for a in artifacts:
            if a.source_artifact in self._fail:
                result.artifacts_failed += 1
                result.artifacts.append(
                    ArtifactIngestResult(a.source_system, a.source_artifact, "failed", error="boom")
                )
            else:
                result.artifacts_indexed += 1
                result.chunks_indexed += 1
                result.artifacts.append(
                    ArtifactIngestResult(a.source_system, a.source_artifact, "indexed", chunks_indexed=1)
                )
        return result

    @property
    def artifact_ids(self) -> set:
        return {a.source_artifact for a in self.artifacts}


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _rec(
    channel_id,
    message_id,
    user,
    text,
    *,
    team_id="T-eng",
    reply_to_id=None,
    reply_count=0,
    channel_name="chan",
    created="2026-06-10T09:00:00Z",
    modified=None,
    display=None,
):
    return {
        "source_system": "teams",
        "team_id": team_id,
        "team_name": "Engineering",
        "channel_id": channel_id,
        "channel_name": channel_name,
        "message_id": message_id,
        "reply_to_id": reply_to_id,
        "reply_count": reply_count,
        "created_at": created,
        "last_modified_at": modified,
        "user": user,
        "user_display_name": display or user,
        "text": text,
        "signals": {"cross_references": [], "escalation": {}},
    }


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # The deep path reads granted channels via the offline fixture for its scope check.
    monkeypatch.setenv("INGEST_MODE", "offline")


# ─────────────────────────────────────────────────────────────────────────────
# Thread assembly
# ─────────────────────────────────────────────────────────────────────────────
def test_replies_group_with_their_parent_via_reply_to_id():
    ing = TeamsIngestor()
    parent = _rec("19:ops", "m1", "U100", "payments API is down", reply_count=2,
                  created="2026-06-10T09:00:00Z")
    r1 = _rec("19:ops", "m2", "U101", "looking now", reply_to_id="m1",
              created="2026-06-10T09:01:00Z")
    r2 = _rec("19:ops", "m3", "U102", "rolled back", reply_to_id="m1",
              created="2026-06-10T09:02:00Z")

    threads = ing.assemble_threads([r2, parent, r1])  # unordered input
    assert len(threads) == 1
    t = threads[0]
    assert t.is_window is False
    assert t.key == "m1"
    assert [m.msg_id for m in t.messages] == ["m1", "m2", "m3"]  # oldest-first
    assert t.source_artifact() == "T-eng/19:ops:m1"


def test_standalone_messages_window_by_time():
    ing = TeamsIngestor()
    within = _rec("19:ops", "m1", "U1", "first", created="2026-06-10T09:00:00Z")
    close = _rec("19:ops", "m2", "U2", "same window", created="2026-06-10T09:30:00Z")
    far = _rec("19:ops", "m3", "U3", "new window", created="2026-06-10T11:00:00Z")

    threads = ing.assemble_threads([within, close, far])
    assert all(t.is_window for t in threads)
    assert len(threads) == 2  # 09:00+09:30 within 3600s; 11:00 is a new window
    assert sorted(len(t.messages) for t in threads) == [1, 2]


# ─────────────────────────────────────────────────────────────────────────────
# Rendering + provenance (AC1, AC5)
# ─────────────────────────────────────────────────────────────────────────────
def test_thread_text_is_author_attributed_by_display_name():
    ing = TeamsIngestor()
    parent = _rec("19:ops", "m1", "U100", "payments API is down", reply_count=1,
                  display="Ada", created="2026-06-10T09:00:00Z")
    reply = _rec("19:ops", "m2", "U101", "on it", reply_to_id="m1",
                 display="Lin", created="2026-06-10T09:01:00Z")
    art = ing._thread_to_artifact(ing.assemble_threads([parent, reply])[0])
    assert art.content == "Ada: payments API is down\nLin: on it"


def test_artifact_carries_thread_level_observed_provenance():
    ing = TeamsIngestor()
    parent = _rec("19:ops", "m1", "U100", "root", reply_count=1, display="Ada",
                  channel_name="ops-incidents", created="2026-06-10T09:00:00Z")
    reply = _rec("19:ops", "m2", "U101", "reply", reply_to_id="m1", display="Lin",
                 channel_name="ops-incidents", created="2026-06-10T09:10:00Z")
    art = ing._thread_to_artifact(ing.assemble_threads([parent, reply])[0])

    assert art.source_system == RETRIEVAL_SOURCE_SYSTEM == "teams"
    assert art.content_type == CONTENT_TYPE == "conversation"
    assert art.source_artifact == "T-eng/19:ops:m1"

    prov = art.provenance
    assert prov["origin"] == "observed"
    assert prov["channel_name"] == "ops-incidents"
    assert prov["unit"] == "thread"
    assert prov["thread_id"] == "m1"
    assert prov["message_count"] == 2
    assert prov["participants"] == ["Ada", "Lin"]
    # Teams-specific provenance from the collection edge.
    assert prov["team_id"] == "T-eng"
    assert prov["channel_id"] == "19:ops"
    assert prov["user_id"] == "U100"

    ep = prov["evidence_pointer"]
    assert ep["origin"] == "observed"
    assert ep["source_system"] == "teams"
    assert ep["source_artifact"] == "T-eng/19:ops:m1"
    assert ep["source_artifact_type"] == "record_id"
    assert ep["source_timestamp"]


# ─────────────────────────────────────────────────────────────────────────────
# Hand-off + scope (AC1, AC2)
# ─────────────────────────────────────────────────────────────────────────────
def test_granted_channel_content_is_handed_off():
    ing = TeamsIngestor()
    sub = FakeSubstrate()
    records = [
        _rec("19:ops", "m1", "U1", "hello ops", channel_name="ops-incidents"),
        _rec("19:deploys", "d1", "U2", "deploying", channel_name="deploys"),
    ]
    result = ing.ingest_deep_content(ORG, records, ingest_fn=sub)

    assert result.artifacts_handed_off == 2
    assert result.artifacts_indexed == 2
    assert result.artifacts_failed == 0
    assert sub.artifact_ids == {"T-eng/19:ops:m1", "T-eng/19:deploys:d1"}
    assert all(a.source_system == "teams" for a in sub.artifacts)


def test_ungranted_private_archived_and_dm_never_ingested():
    # AC2: seed private (leads-private), ungranted (not-granted), archived
    # (archived-ops), a DM-like unknown channel, and a granted channel — only the
    # granted channel is handed off.
    ing = TeamsIngestor()
    sub = FakeSubstrate()
    records = [
        _rec("19:leads-private", "p1", "U9", "private — never read"),
        _rec("19:not-granted", "n1", "U9", "never granted"),
        _rec("19:archived-ops", "a1", "U9", "archived channel"),
        _rec("19:dm-xyz", "x1", "U9", "direct message — never enumerated"),
        _rec("19:ops", "m1", "U1", "the one granted channel"),
    ]
    ing.ingest_deep_content(ORG, records, ingest_fn=sub)
    assert sub.artifact_ids == {"T-eng/19:ops:m1"}


def test_in_granted_scope_predicate():
    ing = TeamsIngestor()
    assert ing._in_granted_scope(ORG, "T-eng/19:ops") is True
    assert ing._in_granted_scope(ORG, "T-eng/19:deploys") is True
    assert ing._in_granted_scope(ORG, "T-eng/19:leads-private") is False  # private
    assert ing._in_granted_scope(ORG, "T-eng/19:not-granted") is False    # ungranted
    assert ing._in_granted_scope(ORG, "T-eng/19:archived-ops") is False   # archived
    assert ing._in_granted_scope(ORG, None) is False


def test_empty_records_hand_off_nothing():
    ing = TeamsIngestor()
    sub = FakeSubstrate()
    result = ing.ingest_deep_content(ORG, [], ingest_fn=sub)
    assert result.artifacts_handed_off == 0
    assert sub.calls == []


def test_substrate_failure_raises_for_at_least_once():
    ing = TeamsIngestor()
    failing = FakeSubstrate(fail={"T-eng/19:ops:m1"})
    with pytest.raises(TeamsDeepContentError):
        ing.ingest_deep_content(
            ORG, [_rec("19:ops", "m1", "U1", "boom")], ingest_fn=failing
        )


# ─────────────────────────────────────────────────────────────────────────────
# Rides the shared checkpoint beside the reach path (incremental)
# ─────────────────────────────────────────────────────────────────────────────
def test_deep_path_rides_shared_checkpoint_and_does_not_re_hand():
    # Drive the real TeamsIngestor through the change runner (offline fixture),
    # handing each fully-processed batch to the deep path — exactly as the runner
    # does. A second unchanged run re-hands nothing (AC4 rides the checkpoint).
    store = Store()
    ing = TeamsIngestor()
    first = FakeSubstrate()

    def process(batch, sub):
        for r in batch.records:
            assert "signals" in r  # reach signal untouched
        ing.ingest_deep_content(ORG, batch.records, ingest_fn=sub)

    r1 = change_runner.ingest_with_checkpoint(
        ing, ORG, process_batch=lambda b: process(b, first),
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    assert r1.ok
    assert first.artifacts, "first run should hand off the fixture's conversation"
    # Only the granted channels ops/deploys produced content.
    assert all(
        a.source_artifact.startswith("T-eng/19:ops:")
        or a.source_artifact.startswith("T-eng/19:deploys:")
        for a in first.artifacts
    )
    # Never a private / ungranted / archived channel.
    assert not any(
        any(bad in a.source_artifact for bad in ("leads-private", "not-granted", "archived"))
        for a in first.artifacts
    )

    second = FakeSubstrate()
    r2 = change_runner.ingest_with_checkpoint(
        ing, ORG, process_batch=lambda b: process(b, second),
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    assert r2.ok
    assert second.artifacts == []  # unchanged workspace → nothing re-handed
