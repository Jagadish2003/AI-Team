"""
R18-A2 / AT-529 (T1) — contract tests for the Git content change-based ingestor.

Covers the acceptance criteria assigned to this subtask:

  AC1 — GitContentIngestor implements ChangeBasedIngestor. A first run streams
        HEAD content as checkpointed, RESUMABLE batches; the checkpoint is the
        head SHA per repo. A failure mid-load resumes from the last fully-
        processed batch rather than restarting, with no skipped/duplicated files.
  AC2 — An incremental run processes ONLY the files the commits since..HEAD
        touched (verified by commit-count vs files-processed); an unchanged
        source returns an empty delta.

Tests run offline (the deterministic ``git_content_sample.json`` fixture) and
drive the ingestor through the REAL runner (``change_runner.ingest_with_checkpoint``)
via an in-memory checkpoint store, so the checkpoint lifecycle is exercised end
to end. Content handover to the retrieval substrate is captured with an injected
``ingest_fn`` so the tests need no database.
"""
from __future__ import annotations

import json

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from discovery.ingest.git_content import (
    FIXTURE_PATH,
    GitContentIngestor,
    _decode_checkpoint,
    _encode_checkpoint,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles
# ─────────────────────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


class FakeIngest:
    """Captures every ContentArtifact handed to the substrate (no DB needed)."""

    def __init__(self):
        self.artifacts: list = []
        self.calls: int = 0

    def __call__(self, org_id, artifacts):
        self.calls += 1
        self.artifacts.extend(artifacts)


def _ingestor(batch_size: int = 100):
    fake = FakeIngest()
    return GitContentIngestor(batch_size=batch_size, ingest_fn=fake), fake


def _drive(ingestor, org_id, store, **kw):
    return change_runner.ingest_with_checkpoint(
        ingestor, org_id, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )


def _complete(sha: str) -> dict:
    return {"sha": sha, "offset": None}


# Fixture identities (artifact_id == "{repo_id}:{path}").
_WEB_TREE = [
    "web-app:README.md",
    "web-app:src/api.py",
    "web-app:src/main.py",
    "web-app:src/new_feature.py",
    "web-app:src/utils.py",
]  # assets/logo.png is binary → skipped
_DATA_TREE = ["data-pipeline:etl/extract.py", "data-pipeline:etl/load.sql"]
_ALL_TREE = _WEB_TREE + _DATA_TREE


# ─────────────────────────────────────────────────────────────────────────────
# Contract / shape
# ─────────────────────────────────────────────────────────────────────────────
def test_implements_change_based_ingestor():
    ing, _ = _ingestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "git_content"
    # git diff natively reports deletions.
    assert ing.reports_deletes is True


def test_records_carry_artifact_id_change_kind_and_source_system():
    ing, _ = _ingestor()
    batches = list(ing.ingest_changes("org1", None))
    records = [r for b in batches for r in b.records]
    assert records
    for r in records:
        assert r["artifact_id"]
        assert r["change_kind"] in ("created", "updated", "deleted")
        assert r["source_system"] == "git"
        assert r["connector_id"] == "git_content"
        assert r["content_type"] == "code"
        # OBSERVED provenance pointer back to the exact file.
        ev = r["evidence_pointer"]
        assert ev["source_system"] == "git"
        assert ev["origin"] == "observed"
        assert ev["source_artifact"] == r["artifact_id"]


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — first run streams HEAD content; checkpoint is the head SHA per repo
# ─────────────────────────────────────────────────────────────────────────────
def test_ac1_first_run_loads_full_head_tree_skipping_binary():
    store = Store()
    ing, fake = _ingestor()
    seen: list = []
    res = _drive(
        ing,
        "org1",
        store,
        process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records),
    )
    assert res.ok and res.checkpoint_advanced and res.first_run
    assert sorted(seen) == sorted(_ALL_TREE)
    # Binary file is skipped-with-reason, never emitted or indexed.
    assert "web-app:assets/logo.png" not in seen

    # Every non-binary file was handed to the substrate as observed code content.
    assert len(fake.artifacts) == len(_ALL_TREE)
    art = next(a for a in fake.artifacts if a.source_artifact == "web-app:src/main.py")
    assert art.source_system == "git"
    assert art.content_type == "code"
    assert art.content  # real file text
    assert art.provenance["repo"] == "web-app"
    assert art.provenance["path"] == "src/main.py"
    assert art.provenance["commit_sha"] == "c3c3c3c3"
    assert art.provenance["origin"] == "observed"


def test_ac1_checkpoint_is_head_sha_per_repo():
    store = Store()
    ing, _ = _ingestor()
    _drive(ing, "org1", store)
    value = store.read("org1", "git_content").value
    # After a full first load each repo's cursor collapses to a PLAIN head SHA.
    assert json.loads(value)["repos"] == {
        "web-app": "c3c3c3c3",
        "data-pipeline": "d2d2d2d2",
    }
    assert _decode_checkpoint(value) == {
        "web-app": _complete("c3c3c3c3"),
        "data-pipeline": _complete("d2d2d2d2"),
    }


def test_ac1_first_load_streams_multiple_checkpointed_batches():
    store = Store()
    ing, _ = _ingestor(batch_size=1)
    res = _drive(ing, "org1", store)
    assert res.first_run is True
    # 7 non-binary files → 7 single-file batches, each checkpointed (resumable).
    assert res.batches == 7
    assert res.batches_checkpointed == 7
    assert res.complete is True


def test_ac1_failure_midload_resumes_without_loss_or_duplication():
    store = Store()
    processed: list = []

    def fail_on_third(batch: DeltaBatch):
        processed.extend(r["artifact_id"] for r in batch.records)
        if len(processed) == 3:
            raise RuntimeError("network dropped mid initial load")

    ing1, _ = _ingestor(batch_size=1)
    res1 = _drive(ing1, "org1", store, process_batch=fail_on_third)
    assert res1.ok is False
    assert isinstance(res1.error, RuntimeError)
    # Two batches fully processed AND checkpointed; the third raised before its
    # checkpoint was written, so the stored position marks the last good batch.
    assert res1.batches_checkpointed == 2

    # Run 2: a checkpoint now exists → incremental/resume mode. It picks up exactly
    # where it left off — no re-processing of the batches already done.
    resumed: list = []
    ing2, _ = _ingestor(batch_size=1)
    res2 = _drive(
        ing2,
        "org1",
        store,
        process_batch=lambda b: resumed.extend(r["artifact_id"] for r in b.records),
    )
    assert res2.ok and res2.checkpoint_advanced

    already = processed[:2]  # only the two fully-processed batches
    assert not set(already) & set(resumed)
    combined = already + resumed
    assert sorted(combined) == sorted(_ALL_TREE)
    assert len(combined) == len(set(combined)) == len(_ALL_TREE)
    # Load completed across runs → checkpoint at both head SHAs.
    assert _decode_checkpoint(store.read("org1", "git_content").value) == {
        "web-app": _complete("c3c3c3c3"),
        "data-pipeline": _complete("d2d2d2d2"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental processes only touched files; unchanged → empty delta
# ─────────────────────────────────────────────────────────────────────────────
def _incremental_since() -> Checkpoint:
    # web-app synced at an OLD commit (c1), data-pipeline already at HEAD (d2).
    return Checkpoint.create(
        "git_content",
        "org1",
        _encode_checkpoint(
            {"web-app": _complete("c1c1c1c1"), "data-pipeline": _complete("d2d2d2d2")}
        ),
    )


def test_ac2_incremental_processes_only_touched_files():
    ing, fake = _ingestor()
    batches = list(ing.ingest_changes("org1", _incremental_since()))
    records = [r for b in batches for r in b.records]
    ids = sorted(r["artifact_id"] for r in records)
    # Only the 3 files the 2 commits (c1..c3) touched — NOT the whole 6-file tree.
    assert ids == sorted(
        ["web-app:src/main.py", "web-app:src/new_feature.py", "web-app:src/legacy.py"]
    )
    # commit-count vs files-processed (AC2): 2 commits touched 3 files, and 3 is
    # strictly fewer than the full HEAD tree — the source was not re-read in full.
    assert len(records) == 3
    assert len(records) < len(_WEB_TREE) + 1  # +1 for the skipped binary in the tree

    by_id = {r["artifact_id"]: r for r in records}
    assert by_id["web-app:src/main.py"]["change_kind"] == "updated"
    assert by_id["web-app:src/new_feature.py"]["change_kind"] == "created"
    assert by_id["web-app:src/legacy.py"]["change_kind"] == "deleted"

    # A deletion carries no content and is not indexed; only the 2 live files are.
    handed = {a.source_artifact for a in fake.artifacts}
    assert handed == {"web-app:src/main.py", "web-app:src/new_feature.py"}


def test_ac2_commit_count_matches_fixture():
    """The fixture records how many commits produced the diff — the other half of
    the commit-count vs files-processed check."""
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        fixture = json.load(fh)
    web = next(r for r in fixture["repos"] if r["repo_id"] == "web-app")
    diff = web["diffs"]["c1c1c1c1"]
    assert diff["commit_count"] == 2
    assert len(diff["changes"]) == 3  # matches files-processed above


def test_ac2_incremental_advances_only_the_changed_repo():
    store = Store()
    store.save(_incremental_since())
    ing, _ = _ingestor()
    res = _drive(ing, "org1", store)
    assert res.ok and res.checkpoint_advanced
    assert _decode_checkpoint(store.read("org1", "git_content").value) == {
        "web-app": _complete("c3c3c3c3"),      # advanced to HEAD
        "data-pipeline": _complete("d2d2d2d2"),  # untouched
    }


def test_ac2_unchanged_source_returns_empty_delta():
    since = Checkpoint.create(
        "git_content",
        "org1",
        _encode_checkpoint(
            {"web-app": _complete("c3c3c3c3"), "data-pipeline": _complete("d2d2d2d2")}
        ),
    )
    ing, fake = _ingestor()
    batches = list(ing.ingest_changes("org1", since))
    assert len(batches) == 1
    assert batches[0].records == []
    assert batches[0].is_complete is True
    assert fake.artifacts == []  # nothing handed to the substrate
    assert _decode_checkpoint(batches[0].next_checkpoint) == {
        "web-app": _complete("c3c3c3c3"),
        "data-pipeline": _complete("d2d2d2d2"),
    }


def test_ac2_unchanged_run_leaves_checkpoint_untouched():
    store = Store()
    ing1, _ = _ingestor()
    _drive(ing1, "org1", store)  # first run advances to head
    head_value = store.read("org1", "git_content").value

    ing2, _ = _ingestor()
    res = _drive(ing2, "org1", store)  # nothing new
    assert res.ok
    assert res.records == 0
    assert store.read("org1", "git_content").value == head_value


# ─────────────────────────────────────────────────────────────────────────────
# Opaque checkpoint encoding
# ─────────────────────────────────────────────────────────────────────────────
def test_checkpoint_encodes_complete_as_plain_sha_and_inprogress_as_object():
    value = _encode_checkpoint(
        {"a": _complete("sha_a"), "b": {"sha": "sha_b", "offset": 3}}
    )
    assert value == '{"repos":{"a":"sha_a","b":{"offset":3,"sha":"sha_b"}},"v":1}'
    assert _decode_checkpoint(value) == {
        "a": {"sha": "sha_a", "offset": None},
        "b": {"sha": "sha_b", "offset": 3},
    }


def test_decode_checkpoint_is_tolerant_of_garbage():
    assert _decode_checkpoint(None) == {}
    assert _decode_checkpoint("") == {}
    assert _decode_checkpoint("not json") == {}
    assert _decode_checkpoint(json.dumps({"v": 1})) == {}  # no repos key
    assert _decode_checkpoint(json.dumps({"repos": []})) == {}  # wrong type


def test_round_trip_through_runner_then_back_yields_empty_delta():
    store = Store()
    ing1, _ = _ingestor()
    _drive(ing1, "org1", store)
    cp = store.read("org1", "git_content")
    ing2, fake = _ingestor()
    again = list(ing2.ingest_changes("org1", cp))
    assert all(b.is_empty for b in again)
    assert fake.artifacts == []
