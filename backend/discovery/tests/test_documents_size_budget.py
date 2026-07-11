"""
R18-A1 / T4 (AT-526) — contract tests for the per-file size cap and per-run
extraction budget.

Covers the acceptance criterion assigned to this subtask:

  AC4 — A file exceeding the size cap is skipped-with-reason and visible in run
        health; the run continues. The per-run extraction budget behaves the same
        way once exhausted, so one enormous archive cannot starve a run.

Also pins the retry semantics that make the two skips different: a ``size_capped``
skip is deterministic and advances the checkpoint (not retried while unchanged),
while a ``budget_exceeded`` skip is transient and does NOT advance (retried next
run when budget is available).
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pytest

from discovery.ingest import change_runner, extraction
from discovery.ingest.base import Checkpoint
from discovery.ingest.documents import DocumentIngestor, _decode_checkpoint
from discovery.ingest.documents_source import DocumentRef, DocumentSource

ORG = "org1"


class MemSource(DocumentSource):
    """In-memory source with controllable size_bytes + read tracking."""

    def __init__(self, docs: List[dict]):
        self._docs = docs
        self.reads: List[str] = []

    def list_documents(self, org_id):
        refs = []
        for d in self._docs:
            refs.append(
                DocumentRef(
                    artifact_id=d["id"],
                    filename=d.get("filename", d["id"] + ".txt"),
                    location="loc",
                    signature=d.get("signature", "v1"),
                    source_timestamp=d.get("ts"),
                    size_bytes=d.get("size_bytes"),
                    content_type=d.get("content_type"),
                )
            )
        return refs

    def read(self, org_id, ref):
        self.reads.append(ref.artifact_id)
        for d in self._docs:
            if d["id"] == ref.artifact_id:
                return d["content"]
        raise KeyError(ref.artifact_id)


def _records(batches) -> List[dict]:
    return [r for b in batches for r in b.records]


def _by_id(batches) -> Dict[str, dict]:
    return {r["artifact_id"]: r for r in _records(batches)}


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — per-file size cap
# ─────────────────────────────────────────────────────────────────────────────
def test_ac4_oversized_known_file_skipped_without_reading():
    src = MemSource([
        {"id": "big", "size_bytes": 100, "content": b"x" * 100, "ts": "2026-01-01T00:00:00Z"},
        {"id": "small", "size_bytes": 10, "content": b"hello ok", "ts": "2026-01-02T00:00:00Z"},
    ])
    recs = _by_id(DocumentIngestor(source=src, max_file_bytes=50).ingest_changes(ORG, None))

    assert recs["big"]["extraction"]["status"] == "skipped"
    assert recs["big"]["extraction"]["reason"] == extraction.SIZE_CAPPED
    assert recs["big"]["extraction"]["size_bytes"] == 100
    assert "content" not in recs["big"]  # nothing fabricated
    # The oversized file was never downloaded (cap applied on known size).
    assert "big" not in src.reads
    # The run continued: the small file extracted normally.
    assert recs["small"]["extraction"]["status"] == "extracted"
    assert "hello ok" in recs["small"]["content"]


def test_ac4_oversized_unknown_size_skipped_after_read():
    # size_bytes unknown → cap enforced on the actual bytes after the read.
    src = MemSource([{"id": "big", "size_bytes": None, "content": b"y" * 200}])
    recs = _by_id(DocumentIngestor(source=src, max_file_bytes=50).ingest_changes(ORG, None))
    assert recs["big"]["extraction"]["reason"] == extraction.SIZE_CAPPED
    assert recs["big"]["extraction"]["size_bytes"] == 200
    assert src.reads == ["big"]  # had to read it to learn the size


def test_ac4_size_capped_skip_advances_and_is_not_retried():
    src = MemSource([{"id": "big", "size_bytes": 100, "content": b"x" * 100, "signature": "v1"}])
    store = Store()
    change_runner.ingest_with_checkpoint(
        DocumentIngestor(source=src, max_file_bytes=50), ORG,
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    files = _decode_checkpoint(store.read(ORG, "documents").value)
    assert "big" in files  # deterministic skip advanced the checkpoint

    # Second run, same unchanged file → nothing to do (not re-read).
    src2 = MemSource([{"id": "big", "size_bytes": 100, "content": b"x" * 100, "signature": "v1"}])
    change_runner.ingest_with_checkpoint(
        DocumentIngestor(source=src2, max_file_bytes=50), ORG,
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    assert src2.reads == []


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — per-run extraction budget
# ─────────────────────────────────────────────────────────────────────────────
def test_ac4_budget_exhausted_skips_rest_run_continues():
    src = MemSource([
        {"id": "a", "content": b"a" * 100, "ts": "2026-01-01T00:00:00Z"},
        {"id": "b", "content": b"b" * 100, "ts": "2026-01-02T00:00:00Z"},
        {"id": "c", "content": b"c" * 100, "ts": "2026-01-03T00:00:00Z"},
    ])
    # Budget fits only the first file; the rest are skipped but the run continues.
    recs = _by_id(DocumentIngestor(source=src, extraction_budget_bytes=100).ingest_changes(ORG, None))
    assert recs["a"]["extraction"]["status"] == "extracted"
    assert recs["b"]["extraction"]["reason"] == extraction.BUDGET_EXCEEDED
    assert recs["c"]["extraction"]["reason"] == extraction.BUDGET_EXCEEDED
    # Budget-skipped files were not read.
    assert src.reads == ["a"]


def test_ac4_budget_skip_does_not_advance_and_is_retried_next_run():
    docs = [
        {"id": "a", "content": b"a" * 100, "signature": "sa", "ts": "2026-01-01T00:00:00Z"},
        {"id": "b", "content": b"b" * 100, "signature": "sb", "ts": "2026-01-02T00:00:00Z"},
    ]
    store = Store()
    # Run 1: tight budget → only "a" extracts; "b" is budget-skipped (not advanced).
    change_runner.ingest_with_checkpoint(
        DocumentIngestor(source=MemSource(docs), extraction_budget_bytes=100), ORG,
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    files = _decode_checkpoint(store.read(ORG, "documents").value)
    assert "a" in files and "b" not in files  # b retried next run

    # Run 2: generous budget → "b" is picked up (checkpoint exists → incremental).
    src2 = MemSource(docs)
    change_runner.ingest_with_checkpoint(
        DocumentIngestor(source=src2, extraction_budget_bytes=0), ORG,
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    assert src2.reads == ["b"]  # only the previously-skipped file
    files = _decode_checkpoint(store.read(ORG, "documents").value)
    assert "b" in files


def test_discarded_oversized_file_does_not_consume_budget():
    """Regression (review finding): a post-read oversized file (source omitted its
    size) is read then DISCARDED by the size cap, so it must charge 0 to the budget
    — exactly like the pre-read cap. A legitimate file after it then still extracts
    within the same run instead of being starved by BUDGET_EXCEEDED."""
    docs = [
        # Unknown size, 200 bytes → post-read size cap discards it (charges 0).
        {"id": "big", "size_bytes": None, "content": b"y" * 200, "ts": "2026-01-01T00:00:00Z"},
        # 40 bytes → must still extract; the budget (100) was NOT eaten by "big".
        {"id": "small", "content": b"z" * 40, "ts": "2026-01-02T00:00:00Z"},
    ]
    recs = _by_id(
        DocumentIngestor(
            source=MemSource(docs), max_file_bytes=50, extraction_budget_bytes=100
        ).ingest_changes(ORG, None)
    )
    assert recs["big"]["extraction"]["reason"] == extraction.SIZE_CAPPED
    # Would be BUDGET_EXCEEDED under the old (charge-read-bytes) behaviour.
    assert recs["small"]["extraction"]["status"] == "extracted"


def test_read_but_skipped_file_does_not_consume_budget():
    """A file that is read but deliberately skipped (e.g. unsupported/scanned) also
    charges 0 — only successfully-extracted content counts against the budget."""
    docs = [
        # A recognised-but-unsupported type → ExtractionSkipped after the read.
        {"id": "diagram", "filename": "arch.vsdx", "content": b"x" * 90, "ts": "2026-01-01T00:00:00Z"},
        {"id": "note", "filename": "note.txt", "content": b"z" * 40, "ts": "2026-01-02T00:00:00Z"},
    ]
    recs = _by_id(
        DocumentIngestor(source=MemSource(docs), extraction_budget_bytes=100).ingest_changes(ORG, None)
    )
    assert recs["diagram"]["extraction"]["status"] == "skipped"
    assert recs["note"]["extraction"]["status"] == "extracted"  # budget intact


# ─────────────────────────────────────────────────────────────────────────────
# Disabling the limits
# ─────────────────────────────────────────────────────────────────────────────
def test_cap_zero_disables_the_size_cap():
    src = MemSource([{"id": "big", "size_bytes": 10_000, "content": b"z" * 10_000}])
    recs = _by_id(DocumentIngestor(source=src, max_file_bytes=0).ingest_changes(ORG, None))
    assert recs["big"]["extraction"]["status"] == "extracted"


def test_budget_zero_disables_the_budget():
    src = MemSource([
        {"id": "a", "content": b"a" * 1000, "ts": "2026-01-01T00:00:00Z"},
        {"id": "b", "content": b"b" * 1000, "ts": "2026-01-02T00:00:00Z"},
    ])
    recs = _by_id(DocumentIngestor(source=src, extraction_budget_bytes=0).ingest_changes(ORG, None))
    assert recs["a"]["extraction"]["status"] == "extracted"
    assert recs["b"]["extraction"]["status"] == "extracted"


# ─────────────────────────────────────────────────────────────────────────────
# Configurability (env defaults)
# ─────────────────────────────────────────────────────────────────────────────
def test_limits_default_from_env(monkeypatch):
    monkeypatch.setenv("DOCUMENT_MAX_FILE_BYTES", "12345")
    monkeypatch.setenv("DOCUMENT_EXTRACTION_BUDGET_BYTES", "67890")
    ing = DocumentIngestor()
    assert ing.max_file_bytes == 12345
    assert ing.extraction_budget_bytes == 67890


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DOCUMENT_MAX_FILE_BYTES", "not-a-number")
    ing = DocumentIngestor()
    assert ing.max_file_bytes == 25 * 1024 * 1024  # built-in default


def test_explicit_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("DOCUMENT_MAX_FILE_BYTES", "12345")
    ing = DocumentIngestor(max_file_bytes=999)
    assert ing.max_file_bytes == 999
