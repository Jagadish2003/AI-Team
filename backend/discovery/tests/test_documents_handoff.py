"""
R18-A1 / T3 (AT-525) — contract tests for the retrieval hand-off.

T3 wires the :class:`~discovery.ingest.documents.DocumentIngestor` to the R18-B1
retrieval substrate: it maps each successfully-extracted document record to a
:class:`~app.retrieval.ingest.ContentArtifact` and hands it to
``retrieval.ingest_content(org_id, extracted_artifacts)``. This story stops at the
hand-off — chunking/embedding/indexing belong to the substrate.

These tests exercise the hand-off WITHOUT a database by injecting a fake substrate
(``ingest_fn``) that captures what would be indexed, so the mapping, filtering,
vocabulary translation, and at-least-once semantics are all provable in isolation.
The end-to-end "delivered and subsequently retrievable" proof against the real
pgvector substrate (AC1) lives in
``backend/tests/contract/test_documents_retrieval_handoff.py``.

Covered here:
  * AC1 (delivery) — extracted PDF/docx/xlsx/pptx/text records are mapped and
    handed to ``ingest_content`` with the right content + content_type.
  * AC6 — every handed artifact carries ``origin='observed'``, the full
    EvidencePointer spine, and the source file id/name (correct source).
  * Only extracted records are handed over — skips, per-file errors, and delete
    tombstones (no text) are never sent to the substrate.
  * The ``'documents'`` → ``'document'`` source_system vocabulary translation.
  * Incremental: an unchanged estate hands off nothing (AC2 is the ingestor's
    checkpoint; here we prove the hand-off rides it and does not re-send).
  * At-least-once: a substrate-reported failure does NOT advance the checkpoint,
    and the batch is re-handed on the next run (idempotent replace).
"""
from __future__ import annotations

from typing import List, Optional

import pytest

from app.retrieval.ingest import (
    ArtifactIngestResult,
    ContentArtifact,
    IngestResult,
)
from discovery.ingest.base import Checkpoint
from discovery.ingest.documents_handoff import (
    RETRIEVAL_SOURCE_SYSTEM,
    DocumentHandoffResult,
    extracted_artifacts,
    ingest_documents,
    record_to_artifact,
)
from discovery.ingest.documents_source import DocumentRef, DocumentSource, FixtureDocumentSource

ORG = "org_handoff"

# Fixture artifact ids (the offline documents_sample.json).
_TEXT_IDS = {"handbook/onboarding.md", "handbook/expenses.csv", "policies/security.txt"}
_SKIPPED = "diagrams/architecture.vsdx"   # unsupported → ExtractionSkipped
_CORRUPT = "reports/corrupt.txt"          # raise_on_read → extraction error


# ─────────────────────────────────────────────────────────────────────────────
# In-memory checkpoint store + a capturing fake substrate
# ─────────────────────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


class FakeSubstrate:
    """Stands in for ``retrieval.ingest_content`` — records every hand-off.

    ``fail`` is a set of source_artifact ids to report as failed, so the
    at-least-once (don't-advance-on-failure) path is testable without a real store.
    """

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


def _run(store: Store, substrate: FakeSubstrate, **kw) -> DocumentHandoffResult:
    return ingest_documents(
        ORG,
        source=FixtureDocumentSource(),
        ingest_fn=substrate,
        read_checkpoint=store.read,
        save_checkpoint=store.save,
        **kw,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pure mapping: record → ContentArtifact
# ─────────────────────────────────────────────────────────────────────────────
def _extracted_record(**over):
    rec = {
        "artifact_id": "lib/report.pdf",
        "filename": "report.pdf",
        "location": "lib",
        "document_format": "pdf",
        "source_timestamp": "2026-07-09T00:00:00+00:00",
        "chunk_content_type": "prose",
        "content": "quarterly numbers",
        "extraction": {"status": "extracted"},
        "provenance": {"author": "Finance"},
        "evidence_pointer": {
            "source_system": "documents",
            "source_artifact": "lib/report.pdf",
            "source_timestamp": "2026-07-09T00:00:00+00:00",
            "origin": "observed",
            "source_artifact_type": "record_id",
        },
    }
    rec.update(over)
    return rec


def test_maps_extracted_record_to_content_artifact():
    art = record_to_artifact(_extracted_record())
    assert isinstance(art, ContentArtifact)
    # Vocabulary translation: connector 'documents' → substrate 'document' (AC1).
    assert art.source_system == RETRIEVAL_SOURCE_SYSTEM == "document"
    assert art.source_artifact == "lib/report.pdf"
    assert art.content == "quarterly numbers"
    assert art.content_type == "prose"
    assert art.source_timestamp == "2026-07-09T00:00:00+00:00"


def test_artifact_provenance_carries_observed_spine():
    """AC6: retrieval of this content must show the correct source file and prove
    it was observed — provenance carries origin='observed', the EvidencePointer
    spine, and the human-facing file identity."""
    art = record_to_artifact(_extracted_record())
    prov = art.provenance
    assert prov["origin"] == "observed"
    assert prov["filename"] == "report.pdf"
    assert prov["location"] == "lib"
    assert prov["document_format"] == "pdf"
    # Full R16-B1 spine preserved verbatim, origin observed.
    ep = prov["evidence_pointer"]
    assert ep["origin"] == "observed"
    assert ep["source_artifact"] == "lib/report.pdf"
    # Handler/source provenance is preserved, not clobbered.
    assert prov["author"] == "Finance"


def test_extracted_record_with_empty_text_is_still_handed_off():
    """Empty text is a truthful 'this file now has no content' hand-off (distinct
    from a loud skip) — it must still map so the substrate can drop stale chunks."""
    art = record_to_artifact(_extracted_record(content=""))
    assert isinstance(art, ContentArtifact)
    assert art.content == ""


@pytest.mark.parametrize(
    "record",
    [
        {"artifact_id": "x", "extraction": {"status": "skipped", "reason": "scanned_image"}},
        {"artifact_id": "x", "extraction": {"status": "error", "reason": "ValueError"}},
        {"artifact_id": "x", "change_kind": "deleted"},  # tombstone: no extraction/content
        {"artifact_id": "x"},                             # nothing extracted
    ],
)
def test_non_extracted_records_are_not_handed_off(record):
    assert record_to_artifact(record) is None


def test_extracted_artifacts_filters_a_mixed_batch():
    records = [
        _extracted_record(artifact_id="a", content="one"),
        {"artifact_id": "b", "extraction": {"status": "skipped", "reason": "encrypted"}},
        _extracted_record(artifact_id="c", content="two"),
        {"artifact_id": "d", "change_kind": "deleted"},
    ]
    arts = extracted_artifacts(records)
    assert [a.source_artifact for a in arts] == ["a", "c"]


# ─────────────────────────────────────────────────────────────────────────────
# Driven end-to-end through the change runner (fake substrate)
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_hands_off_only_extracted_files():
    store, substrate = Store(), FakeSubstrate()
    result = _run(store, substrate)

    # Only the three text-family files carry extracted text; the unsupported and
    # corrupt files are recorded by the ingestor but never handed to the substrate.
    assert substrate.artifact_ids == _TEXT_IDS
    assert _SKIPPED not in substrate.artifact_ids
    assert _CORRUPT not in substrate.artifact_ids

    assert result.artifacts_handed_off == 3
    assert result.artifacts_indexed == 3
    assert result.artifacts_failed == 0
    assert result.ok
    # Every handed artifact speaks the substrate vocabulary.
    assert all(a.source_system == "document" for a in substrate.artifacts)


def test_incremental_run_hands_off_nothing_when_unchanged():
    store, first = Store(), FakeSubstrate()
    _run(store, first)
    assert first.artifact_ids == _TEXT_IDS  # first run handed the estate over

    # Second run over the same unchanged fixture: the checkpoint means nothing is
    # re-extracted, so nothing is re-handed to the substrate (AC2 rides through).
    second = FakeSubstrate()
    result = _run(store, second)
    assert second.artifacts == []
    assert result.artifacts_handed_off == 0


def test_substrate_failure_does_not_advance_checkpoint_then_rehands():
    # First run: the substrate reports one artifact failed → the run must NOT
    # advance the checkpoint (at-least-once: the batch is retried next run).
    store = Store()
    failing = FakeSubstrate(fail={"policies/security.txt"})
    result = _run(store, failing)
    assert result.artifacts_failed == 1
    assert result.checkpoint_advanced is False
    assert result.error is not None  # surfaced, not raised

    # Next run with a healthy substrate re-hands the SAME files (idempotent) and
    # now advances — no content is lost because the checkpoint never moved past it.
    healthy = FakeSubstrate()
    result2 = _run(store, healthy)
    assert healthy.artifact_ids == _TEXT_IDS
    assert result2.artifacts_failed == 0
    assert result2.checkpoint_advanced is True


def test_substrate_failure_is_logged_loudly(caplog):
    """Issue #5: even though ingest_documents never raises, a hand-off failure must
    be logged at ERROR so a total substrate outage can't look like a clean run in the
    logs when a caller ignores the returned summary."""
    import logging

    store = Store()
    failing = FakeSubstrate(fail={"policies/security.txt"})
    with caplog.at_level(logging.ERROR, logger="discovery.ingest.documents_handoff"):
        result = _run(store, failing)
    assert not result.ok
    assert any(
        rec.levelno == logging.ERROR and "did NOT complete" in rec.getMessage()
        for rec in caplog.records
    )


def test_changed_file_is_rehanded_next_run():
    store, first = Store(), FakeSubstrate()
    _run(store, first)

    # A source whose one file has a NEW signature (content changed) must re-hand
    # exactly that file — the substrate replaces its prior chunks by artifact id.
    changed_ref = DocumentRef(
        artifact_id="handbook/onboarding.md",
        filename="onboarding.md",
        location="handbook",
        signature="sig-onboarding-2",  # advanced signature
        source_timestamp="2026-07-09T12:00:00Z",
        content_type="text/markdown",
    )

    class OneChangedSource(DocumentSource):
        reports_deletes = False  # partial set: don't infer deletes for the rest

        def list_documents(self, org_id):
            return [changed_ref]

        def read(self, org_id, ref):
            return b"# Onboarding v2\n\nUpdated instructions.\n"

    second = FakeSubstrate()
    result = ingest_documents(
        ORG,
        source=OneChangedSource(),
        ingest_fn=second,
        read_checkpoint=store.read,
        save_checkpoint=store.save,
    )
    assert second.artifact_ids == {"handbook/onboarding.md"}
    assert result.artifacts_handed_off == 1
