"""
R16-A2 / AT-416 (T1) — contract tests for the Slack change-based ingestor.

Covers the acceptance criteria assigned to this subtask:

  AC2 — SlackIngestor implements ChangeBasedIngestor. An incremental run returns
        only messages newer than the checkpoint; an unchanged workspace returns
        an empty delta.
  AC3 — A first run performs a resumable, checkpointed initial load: a failure
        mid-load resumes from the last fully-processed batch rather than
        restarting, with no skipped or duplicated records.

AC4 (public/invited channels only) is also asserted here at the source-read
boundary, since channel selection lives in this ingestor.

Tests run offline (the deterministic ``slack_sample.json`` fixture) and drive the
ingestor through the REAL runner (``change_runner.ingest_with_checkpoint``) via an
in-memory checkpoint store, so the checkpoint lifecycle is exercised end to end.
"""
from __future__ import annotations

import json

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint, DeltaBatch
from discovery.ingest.slack import (
    SlackIngestor,
    _decode_checkpoint,
    _encode_checkpoint,
)


# ─────────────────────────────────────────────────────────────────────────────
# In-memory checkpoint store wired through the runner's injectable seam.
# ─────────────────────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _drive(ingestor, org_id, store, **kw):
    return change_runner.ingest_with_checkpoint(
        ingestor, org_id, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )


# Fixture message identities for assertions (channel:ts == artifact_id).
_C001 = [
    "C001:1718000000.000100",
    "C001:1718000600.000200",
    "C001:1718003600.000300",
    "C001:1718090000.000400",
]
_C002 = [
    "C002:1718001000.000100",
    "C002:1718004000.000200",
]
_ALL = _C001 + _C002


# ─────────────────────────────────────────────────────────────────────────────
# Contract / shape
# ─────────────────────────────────────────────────────────────────────────────
def test_slack_implements_change_based_ingestor():
    ing = SlackIngestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "slack"
    # History polling cannot surface message deletions — declared explicitly.
    assert ing.reports_deletes is False


def test_records_carry_artifact_id_and_change_kind():
    """Records must carry artifact_id + change_kind so the runner can emit
    ingestion.artifact_changed events (AC7, handled downstream)."""
    batches = list(SlackIngestor().ingest_changes("org1", None))
    records = [r for b in batches for r in b.records]
    assert records
    for r in records:
        assert r["artifact_id"]
        assert r["change_kind"] in ("created", "updated")
        assert r["source_system"] == "slack"
    # The edited fixture message is surfaced as an update, not a create.
    edited = next(r for r in records if r["artifact_id"] == "C001:1718090000.000400")
    assert edited["change_kind"] == "updated"


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — only public channels AgentIQ is invited to are read
# ─────────────────────────────────────────────────────────────────────────────
def test_ac4_only_public_invited_channels_read():
    ing = SlackIngestor()
    accessible = {c["id"] for c in ing._accessible_channels("org1")}
    assert accessible == {"C001", "C002"}
    # Private, never-invited, and archived channels are excluded.
    assert "C900" not in accessible  # private
    assert "C901" not in accessible  # is_member == False
    assert "C902" not in accessible  # archived


def test_ac4_private_and_uninvited_messages_never_emitted():
    batches = list(SlackIngestor().ingest_changes("org1", None))
    channels_seen = {r["channel_id"] for b in batches for r in b.records}
    assert channels_seen == {"C001", "C002"}


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental returns only newer; unchanged returns empty delta
# ─────────────────────────────────────────────────────────────────────────────
def test_ac2_first_run_loads_all_accessible_messages():
    store = Store()
    seen: list = []
    res = _drive(
        SlackIngestor(),
        "org1",
        store,
        process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records),
    )
    assert res.ok and res.checkpoint_advanced
    assert sorted(seen) == sorted(_ALL)
    # Checkpoint is an opaque JSON cursor map covering both channels at their heads.
    cursors = _decode_checkpoint(store.read("org1", "slack").value)
    assert cursors == {
        "C001": "1718090000.000400",
        "C002": "1718004000.000200",
    }


def test_ac2_incremental_returns_only_newer_than_checkpoint():
    # Checkpoint mid-C001 (after msg 2) and pre-C002 entirely absent.
    since = Checkpoint.create(
        "slack", "org1", _encode_checkpoint({"C001": "1718000600.000200"})
    )
    batches = list(SlackIngestor().ingest_changes("org1", since))
    ids = [r["artifact_id"] for b in batches for r in b.records]
    # Only C001 msgs 3 & 4 (newer than cursor) + all of C002 (absent from map).
    assert sorted(ids) == sorted(_C001[2:] + _C002)
    assert "C001:1718000000.000100" not in ids  # older — not re-read
    assert "C001:1718000600.000200" not in ids  # equal to cursor — not re-read


def test_ac2_unchanged_workspace_returns_empty_delta():
    store = Store()
    # First run advances to head.
    _drive(SlackIngestor(), "org1", store)
    head_value = store.read("org1", "slack").value

    # Second run with nothing new → empty delta, position does not regress.
    res = _drive(SlackIngestor(), "org1", store)
    assert res.ok
    assert res.records == 0
    assert store.read("org1", "slack").value == head_value


def test_ac2_unchanged_delta_is_single_empty_batch_echoing_position():
    since = Checkpoint.create(
        "slack",
        "org1",
        _encode_checkpoint(
            {"C001": "1718090000.000400", "C002": "1718004000.000200"}
        ),
    )
    batches = list(SlackIngestor().ingest_changes("org1", since))
    assert len(batches) == 1
    assert batches[0].records == []
    assert batches[0].is_complete is True
    assert _decode_checkpoint(batches[0].next_checkpoint) == {
        "C001": "1718090000.000400",
        "C002": "1718004000.000200",
    }


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — resumable, checkpointed first load
# ─────────────────────────────────────────────────────────────────────────────
def test_ac3_first_load_streams_multiple_checkpointed_batches():
    store = Store()
    res = _drive(SlackIngestor(batch_size=1), "org1", store)
    assert res.first_run is True
    # 6 accessible messages → 6 single-record batches, each checkpointed.
    assert res.batches == 6
    assert res.batches_checkpointed == 6
    assert res.complete is True


def test_ac3_failure_midload_resumes_without_loss_or_duplication():
    store = Store()
    processed: list = []

    # Fail while processing the 3rd batch of the first load.
    def fail_on_third(batch: DeltaBatch):
        processed.extend(r["artifact_id"] for r in batch.records)
        if len(processed) == 3:
            raise RuntimeError("network dropped mid initial load")

    res1 = _drive(
        SlackIngestor(batch_size=1), "org1", store, process_batch=fail_on_third
    )
    assert res1.ok is False
    assert isinstance(res1.error, RuntimeError)
    # Batches 1 & 2 fully processed AND checkpointed; batch 3 raised before its
    # checkpoint was written, so the stored position marks the last good batch.
    assert res1.batches_checkpointed == 2
    cursors = _decode_checkpoint(store.read("org1", "slack").value)
    assert cursors == {"C001": "1718000600.000200"}  # after batch 2 only

    # Run 2: store has a checkpoint → incremental/resume mode. It must pick up
    # exactly where it left off — no re-processing of batches 1 & 2.
    resumed: list = []
    res2 = _drive(
        SlackIngestor(batch_size=1),
        "org1",
        store,
        process_batch=lambda b: resumed.extend(r["artifact_id"] for r in b.records),
    )
    assert res2.ok and res2.checkpoint_advanced

    # The two already-processed records are NOT re-read on resume.
    already = processed[:2]
    assert not set(already) & set(resumed)
    # Across both runs every record is processed exactly once (no loss, no dup).
    combined = processed[:2] + resumed
    assert sorted(combined) == sorted(_ALL)
    assert len(combined) == len(set(combined)) == 6
    # Final checkpoint is at the head of both channels.
    assert _decode_checkpoint(store.read("org1", "slack").value) == {
        "C001": "1718090000.000400",
        "C002": "1718004000.000200",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Opaque checkpoint encoding
# ─────────────────────────────────────────────────────────────────────────────
def test_checkpoint_value_is_opaque_but_decodable_by_owner():
    value = _encode_checkpoint({"C001": "1.0", "C002": "2.0"})
    # Deterministic JSON (sorted keys) — diff-friendly and reproducible.
    assert value == '{"channels":{"C001":"1.0","C002":"2.0"},"v":1}'
    assert _decode_checkpoint(value) == {"C001": "1.0", "C002": "2.0"}


def test_decode_checkpoint_is_tolerant_of_garbage():
    assert _decode_checkpoint(None) == {}
    assert _decode_checkpoint("") == {}
    assert _decode_checkpoint("not json") == {}
    assert _decode_checkpoint(json.dumps({"v": 1})) == {}  # no channels key
    assert _decode_checkpoint(json.dumps({"channels": []})) == {}  # wrong type


def test_round_trip_through_runner_then_back_yields_empty_delta():
    """The opaque value the runner persisted feeds straight back as an empty delta
    without the runner ever interpreting it (AC5-adjacent)."""
    store = Store()
    _drive(SlackIngestor(), "org1", store)
    cp = store.read("org1", "slack")
    again = list(SlackIngestor().ingest_changes("org1", cp))
    assert all(b.is_empty for b in again)
