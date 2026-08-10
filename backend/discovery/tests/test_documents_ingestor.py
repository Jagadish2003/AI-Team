"""
R18-A1 / T1 (AT-523) — contract tests for the document change-based ingestor.

Covers the acceptance criteria assigned to this subtask:

  AC2 — DocumentIngestor implements ChangeBasedIngestor. An incremental run reads
        and extracts only files whose content signature is new or changed;
        unchanged files are never re-read; an unchanged estate returns an empty
        delta echoing the position.
  AC5 — A single corrupt/unreadable file fails its OWN extraction only: it is
        recorded as an error on its record, the run and every other file proceed,
        and the failed file's checkpoint position does NOT advance (so it is
        retried, never silently lost).

AC6-adjacent (observed EvidencePointer spine on every record) and the loud-skip /
delete-detection behaviours are asserted here too, at the boundaries this ingestor
owns.

Tests run offline (the deterministic ``documents_sample.json`` fixture) and drive
the ingestor through the REAL runner (``change_runner.ingest_with_checkpoint``) via
an in-memory checkpoint store, so the checkpoint lifecycle is exercised end to end.
"""
from __future__ import annotations

import json
from typing import Dict, List

import pytest

from discovery.ingest import change_runner, extraction
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint, DeltaBatch
from discovery.ingest.documents import (
    DocumentIngestor,
    _decode_checkpoint,
    _encode_checkpoint,
)
from discovery.ingest.documents_source import (
    DocumentRef,
    DocumentSource,
    FixtureDocumentSource,
)
from discovery.ingest.extraction import ExtractedText, ExtractionSkipped

ORG = "org1"

# Fixture artifact ids / signatures for assertions.
_ONBOARDING = "handbook/onboarding.md"
_EXPENSES = "handbook/expenses.csv"
_SECURITY = "policies/security.txt"
_UNSUPPORTED = "diagrams/architecture.vsdx"
_CORRUPT = "reports/corrupt.txt"

_TEXT_IDS = {_ONBOARDING, _EXPENSES, _SECURITY}
_ALL_IDS = _TEXT_IDS | {_UNSUPPORTED, _CORRUPT}

_CURRENT_SIGS = {
    _ONBOARDING: "sig-onboarding-1",
    _EXPENSES: "sig-expenses-1",
    _SECURITY: "sig-security-1",
    _UNSUPPORTED: "sig-arch-1",
    _CORRUPT: "sig-corrupt-1",
}


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


def _drive(ingestor, store, **kw):
    return change_runner.ingest_with_checkpoint(
        ingestor, ORG, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )


def _records(batches) -> List[dict]:
    return [r for b in batches for r in b.records]


def _by_id(batches) -> Dict[str, dict]:
    return {r["artifact_id"]: r for r in _records(batches)}


class CountingSource(DocumentSource):
    """Wraps the fixture source and records which artifacts had bytes READ."""

    def __init__(self):
        self._inner = FixtureDocumentSource()
        self.reads: List[str] = []

    def list_documents(self, org_id):
        return self._inner.list_documents(org_id)

    def read(self, org_id, ref):
        self.reads.append(ref.artifact_id)
        return self._inner.read(org_id, ref)


class StubSource(DocumentSource):
    """A source with an explicit ref set and a configurable ``reports_deletes``."""

    def __init__(self, refs: List[DocumentRef], reports_deletes: bool = True):
        self._refs = refs
        self.reports_deletes = reports_deletes

    def list_documents(self, org_id):
        return list(self._refs)

    def read(self, org_id, ref):
        return b"stub content"


# ─────────────────────────────────────────────────────────────────────────────
# Contract / shape
# ─────────────────────────────────────────────────────────────────────────────
def test_implements_change_based_ingestor():
    ing = DocumentIngestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "documents"
    # The default fixture source lists a full inventory → deletions are detectable.
    assert ing.reports_deletes is True


def test_records_carry_spine_and_observed_evidence_pointer():
    """Every record carries artifact_id + change_kind + observed EvidencePointer
    so the runner can emit artifact_changed events and any finding is traceable to
    its exact source file (AC6)."""
    recs = _by_id(DocumentIngestor().ingest_changes(ORG, None))
    assert _TEXT_IDS <= set(recs)
    for r in recs.values():
        assert r["artifact_id"]
        assert r["change_kind"] in ("created", "updated", "deleted")
        assert r["source_system"] == "documents"
        ep = r["evidence_pointer"]
        assert ep["origin"] == "observed"
        assert ep["source_system"] == "documents"
        assert ep["source_artifact"] == r["artifact_id"]
        assert ep["source_timestamp"]
        assert ep["extraction_job_id"] is None  # observed needs no job id


def test_extracted_text_carries_content_and_policy():
    """A text/markdown/CSV file yields extracted text with the prose chunk policy
    (the payload the T3 hand-off will pass to the substrate)."""
    recs = _by_id(DocumentIngestor().ingest_changes(ORG, None))
    onb = recs[_ONBOARDING]
    assert onb["extraction"]["status"] == "extracted"
    assert "Welcome to the team" in onb["content"]
    assert onb["chunk_content_type"] == "prose"
    assert onb["document_format"] == "markdown"
    # Provenance from the source is preserved on the record.
    assert onb["provenance"]["author"] == "People Ops"


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental reads only new/changed; unchanged files are never re-read
# ─────────────────────────────────────────────────────────────────────────────
def test_ac2_first_run_extracts_all_current_files():
    store = Store()
    res = _drive(DocumentIngestor(), store)
    assert res.ok and res.checkpoint_advanced
    # Checkpoint is an opaque signature map of every successfully handled file
    # (extracted text + the deliberately-skipped PDF); the errored file is absent.
    files = _decode_checkpoint(store.read(ORG, "documents").value)
    assert set(files) == {_ONBOARDING, _EXPENSES, _SECURITY, _UNSUPPORTED}
    assert _CORRUPT not in files


def test_ac2_incremental_reads_only_changed_files():
    """Only files whose signature changed are READ + extracted; unchanged files
    are never even read (the whole point of the change-based contract)."""
    # Prior checkpoint: everything at its current signature EXCEPT onboarding, which
    # has an older signature (i.e. its content changed since last run).
    prev = dict(_CURRENT_SIGS)
    prev[_ONBOARDING] = "sig-onboarding-OLD"
    since = Checkpoint.create("documents", ORG, _encode_checkpoint(prev))

    source = CountingSource()
    batches = list(DocumentIngestor(source=source).ingest_changes(ORG, since))
    recs = _by_id(batches)

    # Only the changed file is emitted, and it is an UPDATE (seen before).
    assert set(recs) == {_ONBOARDING}
    assert recs[_ONBOARDING]["change_kind"] == "updated"
    # Unchanged files were never read from the source.
    assert source.reads == [_ONBOARDING]
    assert _EXPENSES not in source.reads
    assert _SECURITY not in source.reads


def test_ac2_unchanged_estate_returns_single_empty_delta():
    since = Checkpoint.create("documents", ORG, _encode_checkpoint(dict(_CURRENT_SIGS)))
    source = CountingSource()
    batches = list(DocumentIngestor(source=source).ingest_changes(ORG, since))
    assert len(batches) == 1
    assert batches[0].records == []
    assert batches[0].is_complete is True
    # Nothing read at all when nothing changed.
    assert source.reads == []
    # The position is echoed back unchanged.
    assert _decode_checkpoint(batches[0].next_checkpoint) == _CURRENT_SIGS


def test_ac2_incremental_unchanged_leaves_checkpoint_untouched():
    store = Store()
    _drive(DocumentIngestor(), store)  # first run
    head = store.read(ORG, "documents").value
    # A second run over the same fixture re-reads ONLY the errored file (retry);
    # every extracted/skipped file is unchanged and left alone.
    source = CountingSource()
    _drive(DocumentIngestor(source=source), store)
    assert source.reads == [_CORRUPT]  # only the failed file is retried
    assert _ONBOARDING not in source.reads


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — per-file failure isolation
# ─────────────────────────────────────────────────────────────────────────────
def test_ac5_corrupt_file_fails_alone_run_and_others_proceed():
    store = Store()
    collected: List[dict] = []
    res = _drive(
        DocumentIngestor(),
        store,
        process_batch=lambda b: collected.extend(b.records),
    )
    # The run completed successfully despite one unreadable file.
    assert res.ok and res.complete
    recs = {r["artifact_id"]: r for r in collected}
    # Every file produced a record (the corrupt one included).
    assert _ALL_IDS <= set(recs)
    # The corrupt file is recorded as an ERROR; the others still extracted.
    assert recs[_CORRUPT]["extraction"]["status"] == "error"
    for good in _TEXT_IDS:
        assert recs[good]["extraction"]["status"] == "extracted"


def test_ac5_errored_file_signature_not_advanced():
    store = Store()
    _drive(DocumentIngestor(), store)
    files = _decode_checkpoint(store.read(ORG, "documents").value)
    # The corrupt file's position never advanced, so the next run retries it.
    assert _CORRUPT not in files


def test_ac5_extractor_exception_is_isolated_per_file():
    """An exception raised by the EXTRACTOR (not just the reader) is isolated too."""

    def flaky_extractor(raw, *, filename, content_type=None):
        if filename == "expenses.csv":
            raise RuntimeError("parser blew up")
        return extraction.extract(raw, filename=filename, content_type=content_type)

    # Fixture source (so bytes read fine); only the extractor fails for one file.
    batches = list(
        DocumentIngestor(
            source=FixtureDocumentSource(), extractor=flaky_extractor
        ).ingest_changes(ORG, None)
    )
    recs = _by_id(batches)
    assert recs[_EXPENSES]["extraction"]["status"] == "error"
    assert recs[_EXPENSES]["extraction"]["reason"] == "RuntimeError"
    # A different text file still extracted normally.
    assert recs[_ONBOARDING]["extraction"]["status"] == "extracted"


# ─────────────────────────────────────────────────────────────────────────────
# Loud skips (never silent emptiness)
# ─────────────────────────────────────────────────────────────────────────────
def test_unsupported_format_is_a_loud_skip_and_advances():
    """An unsupported format is a recorded skip — never empty text — and DOES
    advance the checkpoint (re-reading an unsupported file will never help)."""
    store = Store()
    _drive(DocumentIngestor(), store)
    rec = _fetch_record(_UNSUPPORTED)
    assert rec["extraction"]["status"] == "skipped"
    assert rec["extraction"]["reason"] == extraction.UNSUPPORTED_FORMAT
    assert "content" not in rec  # nothing was fabricated
    files = _decode_checkpoint(store.read(ORG, "documents").value)
    assert _UNSUPPORTED in files  # deliberate skip advances the checkpoint


# ─────────────────────────────────────────────────────────────────────────────
# Deletes / tombstones (R16-A1 §5)
# ─────────────────────────────────────────────────────────────────────────────
def test_deleted_file_emits_tombstone_and_drops_from_checkpoint():
    # Prior checkpoint knows every current file plus one that has since vanished.
    prev = dict(_CURRENT_SIGS)
    prev["handbook/removed.md"] = "sig-removed-1"
    since = Checkpoint.create("documents", ORG, _encode_checkpoint(prev))

    batches = list(DocumentIngestor().ingest_changes(ORG, since))
    recs = _by_id(batches)
    assert set(recs) == {"handbook/removed.md"}
    tomb = recs["handbook/removed.md"]
    assert tomb["change_kind"] == "deleted"
    assert tomb["source_system"] == "documents"
    # The vanished file is dropped from the advanced checkpoint.
    files = _decode_checkpoint(batches[-1].next_checkpoint)
    assert "handbook/removed.md" not in files


def test_partial_source_does_not_infer_deletes():
    ref = DocumentRef(
        artifact_id="a.txt", filename="a.txt", location="", signature="s1"
    )
    since = Checkpoint.create(
        "documents", ORG, _encode_checkpoint({"a.txt": "s1", "gone.txt": "s0"})
    )
    ing = DocumentIngestor(source=StubSource([ref], reports_deletes=False))
    assert ing.reports_deletes is False
    batches = list(ing.ingest_changes(ORG, since))
    # Nothing changed and deletes are not inferred → empty delta, gone.txt retained.
    assert _records(batches) == []
    assert "gone.txt" in _decode_checkpoint(batches[-1].next_checkpoint)


# ─────────────────────────────────────────────────────────────────────────────
# Resumable, checkpointed first load
# ─────────────────────────────────────────────────────────────────────────────
def test_first_load_streams_checkpointed_batches():
    store = Store()
    res = _drive(DocumentIngestor(batch_size=1), store)
    assert res.first_run is True
    # 5 files → 5 single-record batches, each persisting the running signature map.
    assert res.batches == 5
    assert res.batches_checkpointed == 5
    assert res.complete is True
    # The corrupt file never entered the map, so the final position excludes it.
    files = _decode_checkpoint(store.read(ORG, "documents").value)
    assert set(files) == {_ONBOARDING, _EXPENSES, _SECURITY, _UNSUPPORTED}


# ─────────────────────────────────────────────────────────────────────────────
# Opaque checkpoint encoding
# ─────────────────────────────────────────────────────────────────────────────
def test_checkpoint_is_opaque_but_decodable_by_owner():
    value = _encode_checkpoint({"b.txt": "s2", "a.txt": "s1"})
    assert value == '{"files":{"a.txt":"s1","b.txt":"s2"},"v":1}'
    assert _decode_checkpoint(value) == {"a.txt": "s1", "b.txt": "s2"}


def test_decode_checkpoint_is_tolerant_of_garbage():
    assert _decode_checkpoint(None) == {}
    assert _decode_checkpoint("") == {}
    assert _decode_checkpoint("not json") == {}
    assert _decode_checkpoint(json.dumps({"v": 1})) == {}  # no files key
    assert _decode_checkpoint(json.dumps({"files": []})) == {}  # wrong type


# ─────────────────────────────────────────────────────────────────────────────
# Helpers that re-run a throwaway ingest to fetch a single record by id.
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_record(artifact_id: str) -> dict:
    """Extract one record from a fresh first-run ingest (no checkpoint)."""
    recs = _by_id(DocumentIngestor().ingest_changes(ORG, None))
    return recs[artifact_id]


# ─────────────────────────────────────────────────────────────────────────────
# R18-A5 AC2 — the document path is REACHED from a discovery run
#
# The router always guaranteed "never twice"; the "at least once" half was missing
# because nothing called ingest_documents, so library files, page attachments and
# configured locations were ingested ZERO times and no finding could cite a PDF.
# ─────────────────────────────────────────────────────────────────────────────
def test_runner_has_a_document_ingest_step():
    from discovery import runner

    assert hasattr(runner, "_ingest_documents")


def test_runner_document_step_drives_the_handoff(monkeypatch):
    from discovery import runner
    from discovery.ingest import documents_handoff

    calls = []

    class _Result:
        org_id = "org_1"
        batches = 1
        records = 3
        artifacts_handed_off = 2
        artifacts_indexed = 2
        artifacts_empty = 0
        artifacts_failed = 0
        chunks_indexed = 5
        chunks_replaced = 0
        checkpoint_advanced = True
        first_run = True
        error = None

    monkeypatch.setattr(
        documents_handoff,
        "ingest_documents",
        lambda org_id, **kw: (calls.append(org_id), _Result())[1],
    )

    runner._ingest_documents("org_1", "run_1")
    assert calls == ["org_1"]


def test_runner_document_step_never_breaks_the_run(monkeypatch):
    """Non-blocking, like every other deep-content hand-off: a document failure
    must not abort a discovery run that has real findings to report."""
    from discovery import runner
    from discovery.ingest import documents_handoff

    def _boom(org_id, **kw):
        raise RuntimeError("extraction exploded")

    monkeypatch.setattr(documents_handoff, "ingest_documents", _boom)

    runner._ingest_documents("org_1", "run_1")  # must not raise
