"""
R18-A4 / AT-594 (T1) — Slack deep-content path (thread assembly, scope check,
substrate hand-off).

The depth phase adds a CONTENT path beside the unchanged reach signal path: the
same change-based delta records are assembled into threads (or time-bounded
windows where no thread structure exists), scope-checked against the P5 channel
selection, rendered as author-attributed text, and handed to the R18-B1 retrieval
substrate via ``ingest_content`` as ``ContentArtifact``s carrying an
``origin='observed'`` thread-level evidence pointer.

These tests exercise the path WITHOUT a database by injecting a fake substrate
(``ingest_fn``) that captures what would be indexed, so thread assembly, rendering,
thread-level provenance, and the scope boundary are all provable in isolation. The
end-to-end "delivered and subsequently retrievable" proof against the real
pgvector substrate (AC1) lives in
``backend/tests/contract/test_slack_retrieval_handoff.py``.

Covered here:
  * AC1 (delivery) — selected-channel conversation is assembled into thread/window
    artifacts and handed to ``ingest_content`` with content_type='conversation'
    and thread-level provenance (origin='observed', evidence pointer).
  * AC2 (scope) — content from unselected channels, private channels, DMs, and
    non-member/archived channels is never assembled or handed off.
  * Thread assembly — replies group with their parent; standalone messages window
    by time; author-attributed rendering.
  * The reach signal path is untouched (records still carry their signal block).
  * Incremental: the depth path rides the shared ``(org, 'slack')`` checkpoint and
    re-hands nothing on an unchanged second run.
"""
from __future__ import annotations

from typing import List, Optional

import pytest

import app.db as app_db
from app.retrieval.ingest import ArtifactIngestResult, ContentArtifact, IngestResult
from discovery.ingest import change_runner
from discovery.ingest.slack import (
    CONTENT_TYPE,
    RETRIEVAL_SOURCE_SYSTEM,
    THREAD_WINDOW_SECONDS,
    SlackDeepContentError,
    SlackIngestor,
)

ORG = "org_deep"


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

    def by_id(self, source_artifact: str) -> ContentArtifact:
        return next(a for a in self.artifacts if a.source_artifact == source_artifact)


class Store:
    """In-memory checkpoint store for change_runner-driven tests."""

    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _msg(channel_id, ts, user, text, *, thread_ts=None, reply_count=0, channel_name="chan"):
    rec = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "ts": ts,
        "user": user,
        "text": text,
        "reply_count": reply_count,
        "reply_users_count": 0,
        "reactions": [],
    }
    if thread_ts is not None:
        rec["thread_ts"] = thread_ts
    return rec


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # Robust against the known cross-file INGEST_MODE leak: the deep path reads
    # channels via the offline fixture for its scope check.
    monkeypatch.setenv("INGEST_MODE", "offline")


def _set_selection(monkeypatch, value):
    """Patch the P5 selection the ingestor reads (None = no selection saved)."""
    def fake_get(org_id, connector_id):
        if connector_id != "slack":
            return None
        record = {"id": "slack", "status": "connected"}
        if value is not None:
            record["channels"] = value
        return record

    monkeypatch.setattr(app_db, "org_connector_get", fake_get)


# ─────────────────────────────────────────────────────────────────────────────
# Thread assembly
# ─────────────────────────────────────────────────────────────────────────────
def test_replies_group_with_their_parent_into_one_thread():
    ing = SlackIngestor()
    parent = _msg("C001", "1000.0001", "U1", "payments API is down", reply_count=2)
    r1 = _msg("C001", "1001.0001", "U2", "looking now", thread_ts="1000.0001")
    r2 = _msg("C001", "1002.0001", "U3", "rolled back", thread_ts="1000.0001")

    threads = ing.assemble_threads([r2, parent, r1])  # unordered input
    assert len(threads) == 1
    t = threads[0]
    assert t.is_window is False
    assert t.key == "1000.0001"
    # Oldest-first, all three messages present.
    assert [m["ts"] for m in t.messages] == ["1000.0001", "1001.0001", "1002.0001"]
    assert t.source_artifact() == "C001:1000.0001"


def test_standalone_messages_window_by_time():
    ing = SlackIngestor()
    base = 1_000_000.0
    within = _msg("C001", f"{base:.4f}", "U1", "first")
    close = _msg("C001", f"{base + 100:.4f}", "U2", "still same window")
    far = _msg("C001", f"{base + THREAD_WINDOW_SECONDS + 10:.4f}", "U3", "new window")

    threads = ing.assemble_threads([within, close, far])
    assert all(t.is_window for t in threads)
    assert len(threads) == 2  # two time-bounded windows
    sizes = sorted(len(t.messages) for t in threads)
    assert sizes == [1, 2]


def test_threads_and_windows_coexist_per_channel():
    ing = SlackIngestor()
    parent = _msg("C001", "2000.0000", "U1", "thread root", reply_count=1)
    reply = _msg("C001", "2001.0000", "U2", "reply", thread_ts="2000.0000")
    lone = _msg("C001", "9000.0000", "U3", "unrelated standalone")

    threads = ing.assemble_threads([parent, reply, lone])
    units = sorted((t.is_window, len(t.messages)) for t in threads)
    assert units == [(False, 2), (True, 1)]


# ─────────────────────────────────────────────────────────────────────────────
# Rendering + provenance (AC1, AC5)
# ─────────────────────────────────────────────────────────────────────────────
def test_thread_text_is_author_attributed():
    ing = SlackIngestor()
    parent = _msg("C001", "1000.0001", "U1", "payments API is down", reply_count=1)
    reply = _msg("C001", "1001.0001", "U2", "on it", thread_ts="1000.0001")
    art = ing._thread_to_artifact(ing.assemble_threads([parent, reply])[0])
    assert art.content == "U1: payments API is down\nU2: on it"


def test_artifact_carries_thread_level_observed_provenance():
    ing = SlackIngestor()
    parent = _msg("C001", "1718000000.000100", "U1", "root", reply_count=1, channel_name="ops")
    reply = _msg("C001", "1718000600.000200", "U2", "reply", thread_ts="1718000000.000100")
    art = ing._thread_to_artifact(ing.assemble_threads([parent, reply])[0])

    assert art.source_system == RETRIEVAL_SOURCE_SYSTEM == "slack"
    assert art.content_type == CONTENT_TYPE == "conversation"
    assert art.source_artifact == "C001:1718000000.000100"

    prov = art.provenance
    assert prov["origin"] == "observed"
    assert prov["channel_id"] == "C001"
    assert prov["channel_name"] == "ops"
    assert prov["unit"] == "thread"
    assert prov["thread_ts"] == "1718000000.000100"
    assert prov["message_count"] == 2
    assert prov["participants"] == ["U1", "U2"]

    # Thread-level evidence pointer, observed, pointing at the exact thread (AC5).
    ep = prov["evidence_pointer"]
    assert ep["origin"] == "observed"
    assert ep["source_system"] == "slack"
    assert ep["source_artifact"] == "C001:1718000000.000100"
    assert ep["source_artifact_type"] == "record_id"
    assert ep["source_timestamp"]  # populated spine


def test_window_provenance_marks_unit_window_with_null_thread_ts():
    ing = SlackIngestor()
    lone = _msg("C001", "1718000000.000100", "U1", "solo message")
    art = ing._thread_to_artifact(ing.assemble_threads([lone])[0])
    assert art.provenance["unit"] == "window"
    assert art.provenance["thread_ts"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Hand-off + scope (AC1, AC2)
# ─────────────────────────────────────────────────────────────────────────────
def test_selected_channel_content_is_handed_off(monkeypatch):
    _set_selection(monkeypatch, None)  # no selection → all accessible (C001, C002)
    ing = SlackIngestor()
    sub = FakeSubstrate()
    records = [
        _msg("C001", "1000.0000", "U1", "hello ops"),
        _msg("C002", "2000.0000", "U2", "deploying"),
    ]
    result = ing.ingest_deep_content(ORG, records, ingest_fn=sub)

    assert result.artifacts_handed_off == 2
    assert result.artifacts_indexed == 2
    assert result.artifacts_failed == 0
    assert sub.artifact_ids == {"C001:1000.0000", "C002:2000.0000"}
    assert all(a.source_system == "slack" for a in sub.artifacts)


def test_only_selected_channels_are_read(monkeypatch):
    # AC2: C001 selected → C002 content must NOT be ingested even though accessible.
    _set_selection(monkeypatch, ["C001"])
    ing = SlackIngestor()
    sub = FakeSubstrate()
    records = [
        _msg("C001", "1000.0000", "U1", "in scope"),
        _msg("C002", "2000.0000", "U2", "out of scope"),
    ]
    ing.ingest_deep_content(ORG, records, ingest_fn=sub)
    assert sub.artifact_ids == {"C001:1000.0000"}
    assert not any(a.source_artifact.startswith("C002") for a in sub.artifacts)


def test_private_dm_and_unauthorised_channels_never_ingested(monkeypatch):
    # AC2: seed private (C900), not-member (C901), archived (C902), a DM (D001),
    # and an unselected accessible channel — only the selected accessible one is
    # handed off.
    _set_selection(monkeypatch, ["C001"])
    ing = SlackIngestor()
    sub = FakeSubstrate()
    records = [
        _msg("C900", "1.0", "U9", "private channel — never read"),
        _msg("C901", "2.0", "U9", "channel AgentIQ was never invited to"),
        _msg("C902", "3.0", "U9", "archived channel"),
        _msg("D001", "4.0", "U9", "direct message — never read"),
        _msg("C002", "5.0", "U2", "accessible but not selected"),
        _msg("C001", "6.0", "U1", "the one selected channel"),
    ]
    ing.ingest_deep_content(ORG, records, ingest_fn=sub)
    assert sub.artifact_ids == {"C001:6.0"}


def test_in_selected_scope_predicate(monkeypatch):
    _set_selection(monkeypatch, ["C001"])
    ing = SlackIngestor()
    assert ing._in_selected_scope(ORG, "C001") is True
    assert ing._in_selected_scope(ORG, "C002") is False  # accessible, not selected
    assert ing._in_selected_scope(ORG, "C900") is False  # private
    assert ing._in_selected_scope(ORG, "D001") is False  # DM / unknown
    assert ing._in_selected_scope(ORG, None) is False


def test_empty_records_hand_off_nothing(monkeypatch):
    _set_selection(monkeypatch, None)
    ing = SlackIngestor()
    sub = FakeSubstrate()
    result = ing.ingest_deep_content(ORG, [], ingest_fn=sub)
    assert result.artifacts_handed_off == 0
    assert sub.calls == []


def test_substrate_failure_raises_for_at_least_once(monkeypatch):
    _set_selection(monkeypatch, None)
    ing = SlackIngestor()
    failing = FakeSubstrate(fail={"C001:1000.0000"})
    with pytest.raises(SlackDeepContentError):
        ing.ingest_deep_content(
            ORG, [_msg("C001", "1000.0000", "U1", "boom")], ingest_fn=failing
        )


# ─────────────────────────────────────────────────────────────────────────────
# Rides the shared checkpoint beside the reach path (incremental)
# ─────────────────────────────────────────────────────────────────────────────
def test_deep_path_rides_shared_checkpoint_and_does_not_re_hand(monkeypatch):
    # Drive the real SlackIngestor through the change runner (offline fixture),
    # handing each fully-processed batch to the deep path — exactly as the runner
    # does. A second unchanged run re-hands nothing (AC4 rides the checkpoint).
    _set_selection(monkeypatch, None)
    store = Store()
    ing = SlackIngestor()
    first = FakeSubstrate()

    def process(batch, sub):
        # Reach signal is untouched: records still carry their signals block.
        for r in batch.records:
            assert "signals" in r
        ing.ingest_deep_content(ORG, batch.records, ingest_fn=sub)

    r1 = change_runner.ingest_with_checkpoint(
        ing, ORG, process_batch=lambda b: process(b, first),
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    assert r1.ok
    assert first.artifacts, "first run should hand off the fixture's conversation"
    # Only accessible channels C001/C002 produced content.
    assert all(a.source_artifact.split(":")[0] in {"C001", "C002"} for a in first.artifacts)

    second = FakeSubstrate()
    r2 = change_runner.ingest_with_checkpoint(
        ing, ORG, process_batch=lambda b: process(b, second),
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    assert r2.ok
    assert second.artifacts == []  # unchanged workspace → nothing re-handed
