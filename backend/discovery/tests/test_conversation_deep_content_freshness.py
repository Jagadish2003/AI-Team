"""
R18-A4 / AT-596 (T3) — edit/delete propagation for Slack & Teams deep content.

The deep-content path wires message edits and deletions into R18-B2 freshness at the
THREAD level (conversation chunks are stored per thread, so freshness must act per
thread, not per message). These tests drive the SHARED conversation model through
each platform's collection edge with an injected freshness recorder (no DB), proving:

  * AC3 (edit) — an ``updated`` message emits a thread-level ``updated`` freshness
    event so the WHOLE thread is re-chunked; the partial batch is NOT handed off.
  * AC3 (delete) — a ``deleted`` standalone message emits a thread-level ``deleted``
    event (immediate purge); a ``deleted`` reply inside a larger thread emits an
    ``updated`` event (re-read drops it) rather than purging the whole thread.
  * A ``created`` reply to a thread whose root is NOT in the batch is re-read as a
    whole thread (``updated``) instead of the partial batch clobbering it.
  * A fully-present created thread is still handed off directly (T1/T2 unchanged),
    emitting no freshness event.
  * AC2 boundary — changes in unselected/ungranted containers emit nothing.
  * The events are keyed at the THREAD level (not per message).
"""
from __future__ import annotations

from typing import List

import pytest

import app.db as app_db
from discovery.ingest.slack import SlackIngestor
from discovery.ingest.teams import TeamsIngestor


ORG = "org_fresh"


class FreshnessRecorder:
    """Stands in for ``retrieval.freshness.on_artifact_changed`` — records events."""

    def __init__(self) -> None:
        self.events: List[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)


class Substrate:
    """Stands in for ``retrieval.ingest_content`` — records direct hand-offs."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.artifacts: List = []

    def __call__(self, org_id, artifacts):
        from app.retrieval.ingest import ArtifactIngestResult, IngestResult

        artifacts = list(artifacts)
        self.calls.append((org_id, artifacts))
        self.artifacts.extend(artifacts)
        result = IngestResult(org_id=org_id, artifacts_received=len(artifacts))
        for a in artifacts:
            result.artifacts_indexed += 1
            result.chunks_indexed += 1
            result.artifacts.append(
                ArtifactIngestResult(a.source_system, a.source_artifact, "indexed", chunks_indexed=1)
            )
        return result

    @property
    def artifact_ids(self) -> set:
        return {a.source_artifact for a in self.artifacts}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


# ─────────────────────────────────────────────────────────────────────────────
# Slack
# ─────────────────────────────────────────────────────────────────────────────
def _slack_selection(monkeypatch, value):
    def fake_get(org_id, connector_id):
        if connector_id != "slack":
            return None
        record = {"id": "slack", "status": "connected"}
        if value is not None:
            record["channels"] = value
        return record

    monkeypatch.setattr(app_db, "org_connector_get", fake_get)


def _srec(channel_id, ts, user, text, *, kind="created", thread_ts=None, reply_count=0):
    rec = {
        "channel_id": channel_id,
        "channel_name": "ops",
        "ts": ts,
        "user": user,
        "text": text,
        "reply_count": reply_count,
        "change_kind": kind,
    }
    if thread_ts is not None:
        rec["thread_ts"] = thread_ts
    return rec


def test_slack_edited_reply_refreshes_whole_thread(monkeypatch):
    _slack_selection(monkeypatch, None)
    ing, sub, fresh = SlackIngestor(), Substrate(), FreshnessRecorder()

    # An edit to a reply of thread 1000.0000 (its parent) — only the reply is in the
    # incremental batch.
    records = [_srec("C001", "1001.0000", "U2", "edited reply", kind="updated", thread_ts="1000.0000")]
    res = ing.ingest_deep_content(ORG, records, ingest_fn=sub, freshness_fn=fresh)

    # Whole-thread refresh event keyed to the PARENT thread, no partial hand-off.
    assert fresh.events == [
        {"org_id": ORG, "connector_id": "slack", "artifact_id": "C001:1000.0000", "change_kind": "updated"}
    ]
    assert sub.calls == []
    assert res.threads_refreshed == 1
    assert res.threads_removed == 0
    assert res.artifacts_handed_off == 0


def test_slack_deleted_standalone_message_purges_immediately(monkeypatch):
    _slack_selection(monkeypatch, None)
    ing, sub, fresh = SlackIngestor(), Substrate(), FreshnessRecorder()

    records = [_srec("C001", "5000.0000", "U1", "oops", kind="deleted")]  # standalone
    res = ing.ingest_deep_content(ORG, records, ingest_fn=sub, freshness_fn=fresh)

    assert fresh.events == [
        {"org_id": ORG, "connector_id": "slack", "artifact_id": "C001:5000.0000", "change_kind": "deleted"}
    ]
    assert res.threads_removed == 1
    assert res.threads_refreshed == 0
    assert sub.calls == []


def test_slack_deleted_reply_refreshes_thread_not_purge(monkeypatch):
    _slack_selection(monkeypatch, None)
    ing, sub, fresh = SlackIngestor(), Substrate(), FreshnessRecorder()

    # Deleting ONE reply must not purge the whole thread — re-read drops it.
    records = [_srec("C001", "1002.0000", "U3", "", kind="deleted", thread_ts="1000.0000")]
    res = ing.ingest_deep_content(ORG, records, ingest_fn=sub, freshness_fn=fresh)

    assert fresh.events == [
        {"org_id": ORG, "connector_id": "slack", "artifact_id": "C001:1000.0000", "change_kind": "updated"}
    ]
    assert res.threads_refreshed == 1
    assert res.threads_removed == 0


def test_slack_created_reply_to_existing_thread_is_refreshed(monkeypatch):
    _slack_selection(monkeypatch, None)
    ing, sub, fresh = SlackIngestor(), Substrate(), FreshnessRecorder()

    # A brand-new reply whose thread root is NOT in this batch → re-read the whole
    # thread instead of clobbering it with a partial hand-off.
    records = [_srec("C001", "1003.0000", "U4", "late reply", kind="created", thread_ts="1000.0000")]
    ing.ingest_deep_content(ORG, records, ingest_fn=sub, freshness_fn=fresh)

    assert sub.calls == []
    assert fresh.events == [
        {"org_id": ORG, "connector_id": "slack", "artifact_id": "C001:1000.0000", "change_kind": "updated"}
    ]


def test_slack_created_full_thread_handed_off_no_freshness(monkeypatch):
    _slack_selection(monkeypatch, None)
    ing, sub, fresh = SlackIngestor(), Substrate(), FreshnessRecorder()

    # Root + reply both present → direct hand-off (T1/T2), no freshness event.
    records = [
        _srec("C001", "1000.0000", "U1", "root", kind="created", reply_count=1),
        _srec("C001", "1001.0000", "U2", "reply", kind="created", thread_ts="1000.0000"),
    ]
    res = ing.ingest_deep_content(ORG, records, ingest_fn=sub, freshness_fn=fresh)

    assert fresh.events == []
    assert sub.artifact_ids == {"C001:1000.0000"}
    assert res.artifacts_handed_off == 1


def test_slack_out_of_scope_change_emits_nothing(monkeypatch):
    _slack_selection(monkeypatch, ["C001"])  # C002 accessible but NOT selected
    ing, sub, fresh = SlackIngestor(), Substrate(), FreshnessRecorder()

    records = [_srec("C002", "2000.0000", "U9", "edit", kind="updated", thread_ts="1999.0000")]
    res = ing.ingest_deep_content(ORG, records, ingest_fn=sub, freshness_fn=fresh)

    assert fresh.events == []
    assert sub.calls == []
    assert res.freshness_events == 0


def test_slack_mixed_batch_hands_off_new_and_refreshes_edit(monkeypatch):
    _slack_selection(monkeypatch, None)
    ing, sub, fresh = SlackIngestor(), Substrate(), FreshnessRecorder()

    records = [
        # A fully-present new thread.
        _srec("C001", "3000.0000", "U1", "new root", kind="created", reply_count=1),
        _srec("C001", "3001.0000", "U2", "new reply", kind="created", thread_ts="3000.0000"),
        # An edit to a different, pre-existing thread.
        _srec("C002", "2500.0000", "U3", "edited", kind="updated", thread_ts="2000.0000"),
    ]
    res = ing.ingest_deep_content(ORG, records, ingest_fn=sub, freshness_fn=fresh)

    assert sub.artifact_ids == {"C001:3000.0000"}
    assert fresh.events == [
        {"org_id": ORG, "connector_id": "slack", "artifact_id": "C002:2000.0000", "change_kind": "updated"}
    ]
    assert res.artifacts_handed_off == 1
    assert res.threads_refreshed == 1


# ─────────────────────────────────────────────────────────────────────────────
# Teams
# ─────────────────────────────────────────────────────────────────────────────
_TEAMS_CONTAINER = "T-eng/19:ops"


def _trec(msg_id, text, user, *, kind="created", reply_to_id=None, reply_count=0, channel="19:ops"):
    return {
        "team_id": "T-eng",
        "team_name": "Engineering",
        "channel_id": channel,
        "channel_name": "ops-incidents",
        "message_id": msg_id,
        "reply_to_id": reply_to_id,
        "created_at": "2026-06-10T09:00:00Z",
        "last_modified_at": None,
        "user": user,
        "user_display_name": user,
        "text": text,
        "reply_count": reply_count,
        "change_kind": kind,
    }


def test_teams_edited_reply_refreshes_whole_thread():
    ing, sub, fresh = TeamsIngestor(), Substrate(), FreshnessRecorder()

    records = [_trec("m200", "edited reply", "Lin", kind="updated", reply_to_id="m100")]
    res = ing.ingest_deep_content(ORG, records, ingest_fn=sub, freshness_fn=fresh)

    assert fresh.events == [
        {
            "org_id": ORG,
            "connector_id": "teams",
            "artifact_id": f"{_TEAMS_CONTAINER}:m100",
            "change_kind": "updated",
        }
    ]
    assert sub.calls == []
    assert res.threads_refreshed == 1


def test_teams_deleted_standalone_message_purges_immediately():
    ing, sub, fresh = TeamsIngestor(), Substrate(), FreshnessRecorder()

    records = [_trec("m500", "", "Ada", kind="deleted")]  # standalone (no reply_to_id)
    res = ing.ingest_deep_content(ORG, records, ingest_fn=sub, freshness_fn=fresh)

    assert fresh.events == [
        {
            "org_id": ORG,
            "connector_id": "teams",
            "artifact_id": f"{_TEAMS_CONTAINER}:m500",
            "change_kind": "deleted",
        }
    ]
    assert res.threads_removed == 1


def test_teams_out_of_scope_change_emits_nothing():
    ing, sub, fresh = TeamsIngestor(), Substrate(), FreshnessRecorder()

    # 19:leads-private is a private channel — never in the granted scope.
    records = [_trec("p200", "edit", "Exec", kind="updated", reply_to_id="p100", channel="19:leads-private")]
    res = ing.ingest_deep_content(ORG, records, ingest_fn=sub, freshness_fn=fresh)

    assert fresh.events == []
    assert sub.calls == []
    assert res.freshness_events == 0


# ─────────────────────────────────────────────────────────────────────────────
# Connector-level change-kind detection (AC4: surfaced natively from the delta)
# ─────────────────────────────────────────────────────────────────────────────
def test_teams_record_marks_removed_message_as_deleted():
    """Microsoft Graph reports a deleted message as an ``@removed`` annotation — the
    connector must surface it as a delete so freshness removes its content."""
    channel = {"team_id": "T-eng", "id": "19:ops", "team_name": "Engineering", "displayName": "ops"}
    removed = {"id": "m900", "@removed": {"reason": "deleted"}, "replyToId": None}
    rec = TeamsIngestor()._to_record(channel, removed)
    assert rec["change_kind"] == "deleted"
    assert rec["artifact_id"] == "T-eng/19:ops:m900"


def test_teams_record_marks_edited_message_as_updated():
    channel = {"team_id": "T-eng", "id": "19:ops", "team_name": "Engineering", "displayName": "ops"}
    edited = {
        "id": "m100",
        "createdDateTime": "2026-06-10T09:00:00Z",
        "lastModifiedDateTime": "2026-06-10T10:00:00Z",
        "from": {"user": {"id": "U1", "displayName": "Ada"}},
        "body": {"contentType": "text", "content": "edited"},
    }
    rec = TeamsIngestor()._to_record(channel, edited)
    assert rec["change_kind"] == "updated"


def test_slack_record_marks_tombstone_as_deleted():
    """Slack history polling does not normally surface deletions, but when a
    tombstone / message_deleted record IS produced the connector marks it deleted."""
    channel = {"id": "C001", "name": "ops"}
    tombstone = {"ts": "1000.0000", "subtype": "tombstone", "user": "U1", "text": ""}
    rec = SlackIngestor()._to_record(channel, tombstone)
    assert rec["change_kind"] == "deleted"
    assert rec["artifact_id"] == "C001:1000.0000"


# ─────────────────────────────────────────────────────────────────────────────
# The change runner must not double-drive freshness for conversation connectors
# ─────────────────────────────────────────────────────────────────────────────
def test_slack_and_teams_manage_their_own_freshness():
    assert SlackIngestor.manages_retrieval_freshness is True
    assert TeamsIngestor.manages_retrieval_freshness is True


def test_change_runner_skips_message_level_freshness_but_still_emits_telemetry(monkeypatch):
    """Per-message freshness would stomp thread-level chunks, so it is skipped for
    self-managing connectors — while the ingestion.artifact_changed telemetry event
    (AC7 of the reach stories) is still emitted."""
    import app.telemetry as telemetry
    from discovery.ingest import change_runner

    calls = {"telemetry": 0, "freshness": 0}
    monkeypatch.setattr(
        telemetry, "record_event", lambda t, p: calls.__setitem__("telemetry", calls["telemetry"] + 1)
    )
    monkeypatch.setattr(
        change_runner, "_notify_freshness", lambda e: calls.__setitem__("freshness", calls["freshness"] + 1)
    )

    records = [{"artifact_id": "C001:1.0", "change_kind": "updated"}]

    # Conversation connector: telemetry yes, freshness no.
    change_runner._emit_artifact_changed("org", "slack", records, notify_freshness=False)
    assert calls == {"telemetry": 1, "freshness": 0}

    # A 1:1-mapped connector still drives freshness generically.
    change_runner._emit_artifact_changed("org", "confluence", records, notify_freshness=True)
    assert calls == {"telemetry": 2, "freshness": 1}
