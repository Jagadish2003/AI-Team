"""
R17-A2 / AT-457 (T1) — contract tests for the Confluence change-based ingestor.

Covers the acceptance criteria assigned to this subtask:

  AC2 — ConfluenceIngestor implements ChangeBasedIngestor. An incremental run
        returns only content modified after the checkpoint; an unchanged source
        returns an empty delta.
  AC3 — A first run performs a resumable, checkpointed initial load: a failure
        mid-load resumes from the last fully-processed batch rather than
        restarting, with no skipped or duplicated content.

AC4 (only granted, non-archived spaces read) is also asserted here at the
source-read boundary, since space selection lives in this ingestor.

Tests run offline (the deterministic ``confluence_sample.json`` fixture) and
drive the ingestor through the REAL runner (``change_runner.ingest_with_checkpoint``)
via an in-memory checkpoint store, so the checkpoint lifecycle is exercised end
to end.
"""
from __future__ import annotations

import json

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint, DeltaBatch
from discovery.ingest.confluence import (
    ConfluenceIngestor,
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


# Fixture content identities for assertions (space:content_id == artifact_id).
_ENG = ["ENG:100", "ENG:200", "ENG:300", "ENG:400"]
_OPS = ["OPS:500", "OPS:600"]
_ALL = _ENG + _OPS


# ─────────────────────────────────────────────────────────────────────────────
# Contract / shape
# ─────────────────────────────────────────────────────────────────────────────
def test_confluence_implements_change_based_ingestor():
    ing = ConfluenceIngestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "confluence"
    # Last-modified-forward polling cannot surface deletions — declared explicitly.
    assert ing.reports_deletes is False


def test_records_carry_artifact_id_and_change_kind():
    """Records must carry artifact_id + change_kind so the runner can emit
    ingestion.artifact_changed events (AC6, handled downstream)."""
    batches = list(ConfluenceIngestor().ingest_changes("org1", None))
    records = [r for b in batches for r in b.records]
    assert records
    for r in records:
        assert r["artifact_id"]
        assert r["change_kind"] in ("created", "updated")
        assert r["source_system"] == "confluence"
    # A version>1 page surfaces as an update; a version-1 page as a creation.
    updated = next(r for r in records if r["artifact_id"] == "ENG:200")
    assert updated["change_kind"] == "updated"
    created = next(r for r in records if r["artifact_id"] == "ENG:100")
    assert created["change_kind"] == "created"


def test_records_carry_metadata_but_no_body():
    """Reach phase carries content METADATA only — never the page body (AC7)."""
    batches = list(ConfluenceIngestor().ingest_changes("org1", None))
    rec = next(
        r for b in batches for r in b.records if r["artifact_id"] == "ENG:200"
    )
    assert rec["space_key"] == "ENG"
    assert rec["content_type"] == "page"
    assert rec["title"] == "Incident INC-4821 postmortem"  # title is metadata
    assert rec["version_number"] == 3
    assert rec["modified_at"] == "2026-06-10T09:10:00Z"
    # No page/document body is ever read in the reach phase.
    assert "body" not in rec
    # T3 enriches each record with a reach-phase signals block (metadata only).
    assert "signals" in rec
    assert set(rec["signals"].keys()) == {"cross_references", "activity"}


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — only granted, non-archived spaces are read
# ─────────────────────────────────────────────────────────────────────────────
def test_ac4_only_granted_non_archived_spaces_read():
    ing = ConfluenceIngestor()
    accessible = {s["key"] for s in ing._accessible_spaces("org1")}
    assert accessible == {"ENG", "OPS"}
    assert "HR" not in accessible   # is_accessible == False (not granted)
    assert "OLD" not in accessible  # archived


def test_ac4_ungranted_and_archived_content_never_emitted():
    batches = list(ConfluenceIngestor().ingest_changes("org1", None))
    spaces_seen = {r["space_key"] for b in batches for r in b.records}
    assert spaces_seen == {"ENG", "OPS"}


def test_only_pages_and_blogposts_ingested_not_comments():
    """The reach phase ingests pages + blog posts; other content types (comments,
    attachments) are ignored — the fixture's comment must not appear."""
    batches = list(ConfluenceIngestor().ingest_changes("org1", None))
    ids = {r["artifact_id"] for b in batches for r in b.records}
    assert "ENG:450" not in ids  # a comment in the fixture
    types = {r["content_type"] for b in batches for r in b.records}
    assert types <= {"page", "blogpost"}


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental returns only newer; unchanged returns empty delta
# ─────────────────────────────────────────────────────────────────────────────
def test_ac2_first_run_loads_all_accessible_content():
    store = Store()
    seen: list = []
    res = _drive(
        ConfluenceIngestor(),
        "org1",
        store,
        process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records),
    )
    assert res.ok and res.checkpoint_advanced
    assert sorted(seen) == sorted(_ALL)
    # Checkpoint is an opaque JSON cursor map covering both spaces at their heads.
    cursors = _decode_checkpoint(store.read("org1", "confluence").value)
    assert cursors == {
        "ENG": "2026-06-11T08:05:00Z",   # page 400 (newest in ENG)
        "OPS": "2026-06-10T09:30:00Z",   # page 600 (newest in OPS)
    }


def test_ac2_incremental_returns_only_content_modified_after_checkpoint():
    # Cursor mid-ENG (after page 200) and OPS entirely absent from the map.
    since = Checkpoint.create(
        "confluence", "org1", _encode_checkpoint({"ENG": "2026-06-10T09:10:00Z"})
    )
    batches = list(ConfluenceIngestor().ingest_changes("org1", since))
    ids = [r["artifact_id"] for b in batches for r in b.records]
    # Only ENG 300 & 400 (modified after cursor) + all of OPS (absent from map).
    assert sorted(ids) == sorted(_ENG[2:] + _OPS)
    assert "ENG:100" not in ids  # older — not re-read
    assert "ENG:200" not in ids  # equal to cursor — not re-read


def test_ac2_unchanged_source_returns_empty_delta():
    store = Store()
    _drive(ConfluenceIngestor(), "org1", store)  # first run advances to head
    head_value = store.read("org1", "confluence").value

    res = _drive(ConfluenceIngestor(), "org1", store)  # nothing new
    assert res.ok
    assert res.records == 0
    assert store.read("org1", "confluence").value == head_value


def test_ac2_unchanged_delta_is_single_empty_batch_echoing_position():
    since = Checkpoint.create(
        "confluence",
        "org1",
        _encode_checkpoint(
            {"ENG": "2026-06-11T08:05:00Z", "OPS": "2026-06-10T09:30:00Z"}
        ),
    )
    batches = list(ConfluenceIngestor().ingest_changes("org1", since))
    assert len(batches) == 1
    assert batches[0].records == []
    assert batches[0].is_complete is True
    assert _decode_checkpoint(batches[0].next_checkpoint) == {
        "ENG": "2026-06-11T08:05:00Z",
        "OPS": "2026-06-10T09:30:00Z",
    }


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — resumable, checkpointed first load
# ─────────────────────────────────────────────────────────────────────────────
def test_ac3_first_load_streams_multiple_checkpointed_batches():
    store = Store()
    res = _drive(ConfluenceIngestor(batch_size=1), "org1", store)
    assert res.first_run is True
    # 6 accessible page/blogpost items → 6 single-record batches, each checkpointed.
    assert res.batches == 6
    assert res.batches_checkpointed == 6
    assert res.complete is True


def test_ac3_failure_midload_resumes_without_loss_or_duplication():
    store = Store()
    processed: list = []

    def fail_on_third(batch: DeltaBatch):
        processed.extend(r["artifact_id"] for r in batch.records)
        if len(processed) == 3:
            raise RuntimeError("network dropped mid initial load")

    res1 = _drive(
        ConfluenceIngestor(batch_size=1), "org1", store, process_batch=fail_on_third
    )
    assert res1.ok is False
    assert isinstance(res1.error, RuntimeError)
    # Batches 1 & 2 fully processed AND checkpointed; batch 3 raised before its
    # checkpoint was written, so the stored position marks the last good batch.
    assert res1.batches_checkpointed == 2
    cursors = _decode_checkpoint(store.read("org1", "confluence").value)
    assert cursors == {"ENG": "2026-06-10T09:10:00Z"}  # after page 200 only

    # Run 2: store has a checkpoint → incremental/resume mode. It must pick up
    # exactly where it left off — no re-processing of batches 1 & 2.
    resumed: list = []
    res2 = _drive(
        ConfluenceIngestor(batch_size=1),
        "org1",
        store,
        process_batch=lambda b: resumed.extend(r["artifact_id"] for r in b.records),
    )
    assert res2.ok and res2.checkpoint_advanced

    already = processed[:2]
    assert not set(already) & set(resumed)
    combined = processed[:2] + resumed
    assert sorted(combined) == sorted(_ALL)
    assert len(combined) == len(set(combined)) == 6
    assert _decode_checkpoint(store.read("org1", "confluence").value) == {
        "ENG": "2026-06-11T08:05:00Z",
        "OPS": "2026-06-10T09:30:00Z",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Opaque checkpoint encoding
# ─────────────────────────────────────────────────────────────────────────────
def test_checkpoint_value_is_opaque_but_decodable_by_owner():
    value = _encode_checkpoint({"ENG": "2026-06-10T09:00:00Z", "OPS": "2026-06-10T09:30:00Z"})
    assert value == (
        '{"spaces":{"ENG":"2026-06-10T09:00:00Z","OPS":"2026-06-10T09:30:00Z"},"v":1}'
    )
    assert _decode_checkpoint(value) == {
        "ENG": "2026-06-10T09:00:00Z",
        "OPS": "2026-06-10T09:30:00Z",
    }


def test_decode_checkpoint_is_tolerant_of_garbage():
    assert _decode_checkpoint(None) == {}
    assert _decode_checkpoint("") == {}
    assert _decode_checkpoint("not json") == {}
    assert _decode_checkpoint(json.dumps({"v": 1})) == {}  # no spaces key
    assert _decode_checkpoint(json.dumps({"spaces": []})) == {}  # wrong type


def test_round_trip_through_runner_then_back_yields_empty_delta():
    store = Store()
    _drive(ConfluenceIngestor(), "org1", store)
    cp = store.read("org1", "confluence")
    again = list(ConfluenceIngestor().ingest_changes("org1", cp))
    assert all(b.is_empty for b in again)
