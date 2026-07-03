"""
R17-A2 / AT-459 (T2) — contract tests for the SharePoint change-based ingestor.

Covers the acceptance criteria assigned to this subtask:

  AC2 — SharePointIngestor implements ChangeBasedIngestor. An incremental run uses
        a Graph delta query and returns only driveItems newer than the stored delta
        token; an unchanged document estate returns an empty delta.
  AC3 — A first run performs a resumable, checkpointed initial load: a failure
        mid-load resumes from the last fully-processed batch rather than
        restarting, with no skipped or duplicated records.

The granted-only access boundary (only granted sites/libraries read) is also
asserted here at the source-read boundary, since library selection lives in this
ingestor.

Tests run offline (the deterministic ``sharepoint_sample.json`` fixture) and drive
the ingestor through the REAL runner (``change_runner.ingest_with_checkpoint``) via
an in-memory checkpoint store, so the checkpoint lifecycle is exercised end to end.
"""
from __future__ import annotations

import json

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint, DeltaBatch
from discovery.ingest.sharepoint import (
    SharePointIngestor,
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


# Fixture item identities for assertions (site/drive:item_id == artifact_id).
_DOCS = [
    "S-eng/b-docs:f100",
    "S-eng/b-docs:f200",
    "S-eng/b-docs:fold300",
    "S-eng/b-docs:f400",
]
_SPECS = [
    "S-eng/b-specs:s100",
    "S-eng/b-specs:s200",
]
_ALL = _DOCS + _SPECS


# ─────────────────────────────────────────────────────────────────────────────
# Contract / shape
# ─────────────────────────────────────────────────────────────────────────────
def test_sharepoint_implements_change_based_ingestor():
    ing = SharePointIngestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "sharepoint"
    # Graph drive-delta `deleted`-facet handling is out of reach-phase scope — declared.
    assert ing.reports_deletes is False


def test_records_carry_artifact_id_and_change_kind():
    """Records must carry artifact_id + change_kind so the runner can emit
    ingestion.artifact_changed events (handled downstream by the shared runner)."""
    batches = list(SharePointIngestor().ingest_changes("org1", None))
    records = [r for b in batches for r in b.records]
    assert records
    for r in records:
        assert r["artifact_id"]
        assert r["change_kind"] in ("created", "updated")
        assert r["source_system"] == "sharepoint"
    # The edited fixture document is surfaced as an update, not a create.
    edited = next(r for r in records if r["artifact_id"] == "S-eng/b-docs:f400")
    assert edited["change_kind"] == "updated"


def test_records_carry_core_metadata_and_evidence_pointer():
    """Records carry structured document metadata signal (no body content) plus a
    fully-populated OBSERVED evidence pointer."""
    batches = list(SharePointIngestor().ingest_changes("org1", None))
    rec = next(
        r for b in batches for r in b.records
        if r["artifact_id"] == "S-eng/b-docs:fold300"
    )
    assert rec["site_id"] == "S-eng"
    assert rec["site_name"] == "Engineering"
    assert rec["drive_id"] == "b-docs"
    assert rec["library_name"] == "Documents"
    assert rec["item_name"] == "Archive"
    assert rec["item_type"] == "folder"
    assert rec["created_by"] == "U102"
    # No document body content is present — reach phase reads metadata signal only.
    assert "content" not in rec
    ep = rec["evidence_pointer"]
    assert ep["source_system"] == "sharepoint"
    assert ep["source_artifact"] == "S-eng/b-docs:fold300"
    assert ep["origin"] == "observed"
    assert ep["extraction_job_id"] is None  # observed needs no extraction job


def test_file_vs_folder_item_type():
    batches = list(SharePointIngestor().ingest_changes("org1", None))
    by_id = {r["artifact_id"]: r for b in batches for r in b.records}
    assert by_id["S-eng/b-docs:f100"]["item_type"] == "file"
    assert by_id["S-eng/b-docs:fold300"]["item_type"] == "folder"


# ─────────────────────────────────────────────────────────────────────────────
# Granted-only access boundary — only granted sites/libraries are read
# ─────────────────────────────────────────────────────────────────────────────
def test_only_granted_libraries_read():
    ing = SharePointIngestor()
    accessible = {lib["id"] for lib in ing._accessible_libraries("org1")}
    assert accessible == {"b-docs", "b-specs"}
    assert "b-private" not in accessible   # is_accessible == False
    assert "b-hidden" not in accessible    # hidden / system library
    # A library on an ungranted site is never reached.
    assert "x-docs" not in accessible


def test_ungranted_and_hidden_items_never_emitted():
    batches = list(SharePointIngestor().ingest_changes("org1", None))
    drives_seen = {r["drive_id"] for b in batches for r in b.records}
    assert drives_seen == {"b-docs", "b-specs"}
    ids = {r["artifact_id"] for b in batches for r in b.records}
    assert "S-eng/b-private:p100" not in ids
    assert "S-eng/b-hidden:h100" not in ids
    assert "S-secret/x-docs:x100" not in ids


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental delta query returns only newer; unchanged returns empty delta
# ─────────────────────────────────────────────────────────────────────────────
def test_ac2_first_run_loads_all_accessible_items():
    store = Store()
    seen: list = []
    res = _drive(
        SharePointIngestor(),
        "org1",
        store,
        process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records),
    )
    assert res.ok and res.checkpoint_advanced
    assert sorted(seen) == sorted(_ALL)
    # Checkpoint is an opaque JSON delta-token map covering both libraries at their
    # heads (each token is the library head's high-water change marker).
    tokens = _decode_checkpoint(store.read("org1", "sharepoint").value)
    assert tokens == {
        "S-eng/b-docs": "2026-06-11T08:05:00Z",   # f400 lastModifiedDateTime
        "S-eng/b-specs": "2026-06-10T09:30:00Z",  # s200 createdDateTime
    }


def test_ac2_incremental_returns_only_newer_than_delta_token():
    # Delta token mid-docs (after f200) and specs entirely absent from the map.
    since = Checkpoint.create(
        "sharepoint",
        "org1",
        _encode_checkpoint({"S-eng/b-docs": "2026-06-10T09:10:00Z"}),
    )
    batches = list(SharePointIngestor().ingest_changes("org1", since))
    ids = [r["artifact_id"] for b in batches for r in b.records]
    # Only docs fold300 & f400 (newer than token) + all of specs (absent from map).
    assert sorted(ids) == sorted(["S-eng/b-docs:fold300", "S-eng/b-docs:f400"] + _SPECS)
    assert "S-eng/b-docs:f100" not in ids  # older — not re-read
    assert "S-eng/b-docs:f200" not in ids  # equal to token — not re-read


def test_ac2_unchanged_estate_returns_empty_delta():
    store = Store()
    # First run advances to head.
    _drive(SharePointIngestor(), "org1", store)
    head_value = store.read("org1", "sharepoint").value

    # Second run with nothing new → empty delta, position does not regress.
    res = _drive(SharePointIngestor(), "org1", store)
    assert res.ok
    assert res.records == 0
    assert store.read("org1", "sharepoint").value == head_value


def test_ac2_unchanged_delta_is_single_empty_batch_echoing_position():
    since = Checkpoint.create(
        "sharepoint",
        "org1",
        _encode_checkpoint(
            {
                "S-eng/b-docs": "2026-06-11T08:05:00Z",
                "S-eng/b-specs": "2026-06-10T09:30:00Z",
            }
        ),
    )
    batches = list(SharePointIngestor().ingest_changes("org1", since))
    assert len(batches) == 1
    assert batches[0].records == []
    assert batches[0].is_complete is True
    assert _decode_checkpoint(batches[0].next_checkpoint) == {
        "S-eng/b-docs": "2026-06-11T08:05:00Z",
        "S-eng/b-specs": "2026-06-10T09:30:00Z",
    }


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — resumable, checkpointed first load
# ─────────────────────────────────────────────────────────────────────────────
def test_ac3_first_load_streams_multiple_checkpointed_batches():
    store = Store()
    res = _drive(SharePointIngestor(batch_size=1), "org1", store)
    assert res.first_run is True
    # 6 accessible items → 6 single-record batches, each checkpointed.
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
        SharePointIngestor(batch_size=1), "org1", store, process_batch=fail_on_third
    )
    assert res1.ok is False
    assert isinstance(res1.error, RuntimeError)
    # Batches 1 & 2 fully processed AND checkpointed; batch 3 raised before its
    # checkpoint was written, so the stored position marks the last good batch.
    assert res1.batches_checkpointed == 2
    tokens = _decode_checkpoint(store.read("org1", "sharepoint").value)
    assert tokens == {"S-eng/b-docs": "2026-06-10T09:10:00Z"}  # after batch 2 (f200) only

    # Run 2: store has a checkpoint → incremental/resume mode. It must pick up
    # exactly where it left off — no re-processing of batches 1 & 2.
    resumed: list = []
    res2 = _drive(
        SharePointIngestor(batch_size=1),
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
    # Final checkpoint is at the head of both libraries.
    assert _decode_checkpoint(store.read("org1", "sharepoint").value) == {
        "S-eng/b-docs": "2026-06-11T08:05:00Z",
        "S-eng/b-specs": "2026-06-10T09:30:00Z",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Opaque checkpoint encoding
# ─────────────────────────────────────────────────────────────────────────────
def test_checkpoint_value_is_opaque_but_decodable_by_owner():
    value = _encode_checkpoint({"S-eng/b-docs": "100", "S-eng/b-specs": "200"})
    # Deterministic JSON (sorted keys) — diff-friendly and reproducible.
    assert value == (
        '{"drives":{"S-eng/b-docs":"100","S-eng/b-specs":"200"},"v":1}'
    )
    assert _decode_checkpoint(value) == {
        "S-eng/b-docs": "100",
        "S-eng/b-specs": "200",
    }


def test_decode_checkpoint_is_tolerant_of_garbage():
    assert _decode_checkpoint(None) == {}
    assert _decode_checkpoint("") == {}
    assert _decode_checkpoint("not json") == {}
    assert _decode_checkpoint(json.dumps({"v": 1})) == {}  # no drives key
    assert _decode_checkpoint(json.dumps({"drives": []})) == {}  # wrong type


def test_round_trip_through_runner_then_back_yields_empty_delta():
    """The opaque value the runner persisted feeds straight back as an empty delta
    without the runner ever interpreting it (AC5-adjacent)."""
    store = Store()
    _drive(SharePointIngestor(), "org1", store)
    cp = store.read("org1", "sharepoint")
    again = list(SharePointIngestor().ingest_changes("org1", cp))
    assert all(b.is_empty for b in again)


def test_batch_size_must_be_positive():
    with pytest.raises(ValueError):
        SharePointIngestor(batch_size=0)
