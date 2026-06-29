"""
R17-A1 / AT-430 (T1) — contract tests for the Teams change-based ingestor.

Covers the acceptance criteria assigned to this subtask:

  AC2 — TeamsIngestor implements ChangeBasedIngestor. An incremental run uses a
        Graph delta query and returns only messages newer than the stored delta
        token; an unchanged workspace returns an empty delta.
  AC3 — A first run performs a resumable, checkpointed initial load: a failure
        mid-load resumes from the last fully-processed batch rather than
        restarting, with no skipped or duplicated records.

AC4 (only granted standard channels read; private chats / DMs never accessed) is
also asserted here at the source-read boundary, since channel selection lives in
this ingestor.

Tests run offline (the deterministic ``teams_sample.json`` fixture) and drive the
ingestor through the REAL runner (``change_runner.ingest_with_checkpoint``) via an
in-memory checkpoint store, so the checkpoint lifecycle is exercised end to end.
"""
from __future__ import annotations

import json

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint, DeltaBatch
from discovery.ingest.teams import (
    TeamsIngestor,
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


# Fixture message identities for assertions (team/channel:message_id == artifact_id).
_OPS = [
    "T-eng/19:ops:m100",
    "T-eng/19:ops:m200",
    "T-eng/19:ops:m300",
    "T-eng/19:ops:m400",
]
_DEPLOYS = [
    "T-eng/19:deploys:d100",
    "T-eng/19:deploys:d200",
]
_ALL = _OPS + _DEPLOYS


# ─────────────────────────────────────────────────────────────────────────────
# Contract / shape
# ─────────────────────────────────────────────────────────────────────────────
def test_teams_implements_change_based_ingestor():
    ing = TeamsIngestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "teams"
    # Graph delta @removed handling is out of reach-phase scope — declared.
    assert ing.reports_deletes is False


def test_records_carry_artifact_id_and_change_kind():
    """Records must carry artifact_id + change_kind so the runner can emit
    ingestion.artifact_changed events (AC7, handled downstream)."""
    batches = list(TeamsIngestor().ingest_changes("org1", None))
    records = [r for b in batches for r in b.records]
    assert records
    for r in records:
        assert r["artifact_id"]
        assert r["change_kind"] in ("created", "updated")
        assert r["source_system"] == "teams"
    # The edited fixture message is surfaced as an update, not a create.
    edited = next(r for r in records if r["artifact_id"] == "T-eng/19:ops:m400")
    assert edited["change_kind"] == "updated"


def test_records_carry_raw_signal_fields_for_downstream_passes():
    """T1 carries the raw message fields the T2 signal pass consumes (body text,
    team/channel identity, author, engagement counts) — without extracting
    signal here (AC8: structured signal only, no deep-content NLP)."""
    batches = list(TeamsIngestor().ingest_changes("org1", None))
    rec = next(
        r for b in batches for r in b.records
        if r["artifact_id"] == "T-eng/19:ops:m200"
    )
    assert rec["team_id"] == "T-eng"
    assert rec["channel_id"] == "19:ops"
    assert rec["channel_name"] == "ops-incidents"
    assert rec["user"] == "U101"
    assert "INC-4821" in rec["text"]  # raw text passed through, not interpreted
    assert "signals" not in rec  # signal extraction is the separate T2 subtask


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — only granted standard channels read; private chats / DMs never accessed
# ─────────────────────────────────────────────────────────────────────────────
def test_ac4_only_granted_standard_channels_read():
    ing = TeamsIngestor()
    accessible = {c["id"] for c in ing._accessible_channels("org1")}
    assert accessible == {"19:ops", "19:deploys"}
    # Private, never-granted, and archived channels are excluded.
    assert "19:leads-private" not in accessible  # private channel
    assert "19:not-granted" not in accessible    # is_accessible == False
    assert "19:archived-ops" not in accessible   # archived


def test_ac4_private_and_ungranted_messages_never_emitted():
    batches = list(TeamsIngestor().ingest_changes("org1", None))
    channels_seen = {r["channel_id"] for b in batches for r in b.records}
    assert channels_seen == {"19:ops", "19:deploys"}


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental delta query returns only newer; unchanged returns empty delta
# ─────────────────────────────────────────────────────────────────────────────
def test_ac2_first_run_loads_all_accessible_messages():
    store = Store()
    seen: list = []
    res = _drive(
        TeamsIngestor(),
        "org1",
        store,
        process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records),
    )
    assert res.ok and res.checkpoint_advanced
    assert sorted(seen) == sorted(_ALL)
    # Checkpoint is an opaque JSON delta-token map covering both channels at their heads.
    tokens = _decode_checkpoint(store.read("org1", "teams").value)
    assert tokens == {
        "T-eng/19:ops": "400",
        "T-eng/19:deploys": "200",
    }


def test_ac2_incremental_returns_only_newer_than_delta_token():
    # Delta token mid-ops (after m200) and deploys entirely absent from the map.
    since = Checkpoint.create(
        "teams", "org1", _encode_checkpoint({"T-eng/19:ops": "200"})
    )
    batches = list(TeamsIngestor().ingest_changes("org1", since))
    ids = [r["artifact_id"] for b in batches for r in b.records]
    # Only ops m300 & m400 (newer than token) + all of deploys (absent from map).
    assert sorted(ids) == sorted(_OPS[2:] + _DEPLOYS)
    assert "T-eng/19:ops:m100" not in ids  # older — not re-read
    assert "T-eng/19:ops:m200" not in ids  # equal to token — not re-read


def test_ac2_unchanged_workspace_returns_empty_delta():
    store = Store()
    # First run advances to head.
    _drive(TeamsIngestor(), "org1", store)
    head_value = store.read("org1", "teams").value

    # Second run with nothing new → empty delta, position does not regress.
    res = _drive(TeamsIngestor(), "org1", store)
    assert res.ok
    assert res.records == 0
    assert store.read("org1", "teams").value == head_value


def test_ac2_unchanged_delta_is_single_empty_batch_echoing_position():
    since = Checkpoint.create(
        "teams",
        "org1",
        _encode_checkpoint({"T-eng/19:ops": "400", "T-eng/19:deploys": "200"}),
    )
    batches = list(TeamsIngestor().ingest_changes("org1", since))
    assert len(batches) == 1
    assert batches[0].records == []
    assert batches[0].is_complete is True
    assert _decode_checkpoint(batches[0].next_checkpoint) == {
        "T-eng/19:ops": "400",
        "T-eng/19:deploys": "200",
    }


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — resumable, checkpointed first load
# ─────────────────────────────────────────────────────────────────────────────
def test_ac3_first_load_streams_multiple_checkpointed_batches():
    store = Store()
    res = _drive(TeamsIngestor(batch_size=1), "org1", store)
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
        TeamsIngestor(batch_size=1), "org1", store, process_batch=fail_on_third
    )
    assert res1.ok is False
    assert isinstance(res1.error, RuntimeError)
    # Batches 1 & 2 fully processed AND checkpointed; batch 3 raised before its
    # checkpoint was written, so the stored position marks the last good batch.
    assert res1.batches_checkpointed == 2
    tokens = _decode_checkpoint(store.read("org1", "teams").value)
    assert tokens == {"T-eng/19:ops": "200"}  # after batch 2 only

    # Run 2: store has a checkpoint → incremental/resume mode. It must pick up
    # exactly where it left off — no re-processing of batches 1 & 2.
    resumed: list = []
    res2 = _drive(
        TeamsIngestor(batch_size=1),
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
    assert _decode_checkpoint(store.read("org1", "teams").value) == {
        "T-eng/19:ops": "400",
        "T-eng/19:deploys": "200",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Opaque checkpoint encoding
# ─────────────────────────────────────────────────────────────────────────────
def test_checkpoint_value_is_opaque_but_decodable_by_owner():
    value = _encode_checkpoint({"T-eng/19:ops": "100", "T-eng/19:deploys": "200"})
    # Deterministic JSON (sorted keys) — diff-friendly and reproducible.
    assert value == (
        '{"channels":{"T-eng/19:deploys":"200","T-eng/19:ops":"100"},"v":1}'
    )
    assert _decode_checkpoint(value) == {
        "T-eng/19:ops": "100",
        "T-eng/19:deploys": "200",
    }


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
    _drive(TeamsIngestor(), "org1", store)
    cp = store.read("org1", "teams")
    again = list(TeamsIngestor().ingest_changes("org1", cp))
    assert all(b.is_empty for b in again)
