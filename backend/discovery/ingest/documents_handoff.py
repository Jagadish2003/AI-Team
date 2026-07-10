"""
R18-A1 / T3 (AT-525) — hand extracted document text to the retrieval substrate.

The join between R18-A1 (this extraction story) and the R18-B1 retrieval
substrate. The :class:`~discovery.ingest.documents.DocumentIngestor` (T1) reads
only new/changed files and extracts their text (T2 handlers) but — by strict
division of labour — never calls the substrate itself. This module is that call:
it maps each successfully-extracted document record to the substrate's producer
payload and hands it over through the ONE standard contract,

    retrieval.ingest_content(org_id, extracted_artifacts)

and nothing more. Chunking, embedding, and indexing all belong to the substrate
(R18-B1); this story stops at extraction + hand-off. Because the substrate owns
everything after the hand-off, a new document format (or OCR later) reaches
retrieval with zero change here — the plug point is the extractor, and the
substrate boundary is this one call.

What is handed over (AC1, AC6)
------------------------------
Only records the ingestor marked ``extraction.status == 'extracted'`` carry text
to index, so they are the only ones handed over. A deliberate skip
(scanned/encrypted/unsupported), a per-file extraction error, and a delete
tombstone all carry NO extracted text and are therefore never handed to the
substrate — freshness/deletion is the R18-B2 concern, driven off the
``ingestion.artifact_changed`` events the change runner already emits.

Every handed-over :class:`~app.retrieval.ingest.ContentArtifact` carries the full
provenance a retrieval hit needs to show the correct source file (AC6): the
stable file id as ``source_artifact``, and a provenance dict carrying
``origin='observed'``, the R16-B1 EvidencePointer spine, and the human-facing
filename/location. An extracted file with genuinely empty text is still handed
over (a truthful "this file now has no content"): the substrate records it as
empty and drops any previous chunks — distinct from a loud skip.

source_system vocabulary
------------------------
The discovery-side connector id is ``'documents'`` (plural — the connector
catalog convention, used by the change checkpoint and the artifact_changed
events). The retrieval substrate's canonical producer name is ``'document'``
(singular — the value in ``KNOWN_SOURCE_SYSTEMS`` that every substrate test and
query uses). This module is the single translation point between the two
vocabularies: it stamps ``source_system='document'`` on every hand-off so the
content is indexed under, and retrievable by, the name the substrate expects.

At-least-once delivery
----------------------
The hand-off is driven through the shared change runner, so the checkpoint
lifecycle (incremental read, resumable first load, write-only-on-full-success) is
unchanged. If the substrate reports that any artifact FAILED (a transient store
problem), the batch's ``process_batch`` raises so the runner does NOT advance the
checkpoint past that content — the file is re-read and re-handed next run.
``ingest_content`` replaces an artifact's chunks by ``(source_system,
source_artifact)``, so re-handing an already-indexed file is idempotent and a
batch-level retry never duplicates content.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from app.retrieval.ingest import ContentArtifact, IngestResult, ingest_content

from . import change_runner
from .documents import DocumentIngestor, Extractor
from .documents_source import DocumentSource

logger = logging.getLogger(__name__)

#: The substrate's canonical producer name for document content (the value in
#: ``database.models.retrieval.KNOWN_SOURCE_SYSTEMS``). Deliberately singular —
#: the discovery connector id is ``'documents'`` (plural); this module is the one
#: place the two vocabularies are reconciled (see module docstring).
RETRIEVAL_SOURCE_SYSTEM = "document"

#: The chunking policy for document text. Every document format is prose; used
#: only as a fallback when a record somehow lacks the extractor's own hint.
_DEFAULT_CONTENT_TYPE = "prose"

#: Record marker set by the ingestor for a file whose text was extracted.
_EXTRACTED = "extracted"

#: The producer→substrate hand-off callable, injectable for tests.
IngestFn = Callable[[str, List[ContentArtifact]], IngestResult]


class DocumentHandoffError(RuntimeError):
    """Raised by the batch hand-off when the substrate reports failed artifacts.

    Propagated into the change runner so the checkpoint is NOT advanced past
    content that never reached retrieval — the batch is re-read and re-handed on
    the next run (idempotent via ``ingest_content``'s per-artifact replace).
    """


def _build_provenance(record: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the provenance a retrieval hit shows for this file (AC6).

    Starts from the record's own provenance (the source ref's provenance merged
    with any a handler discovered), then stamps the fields that let a retrieval
    result name the exact source file and prove it was observed, not inferred:

      * ``origin`` = ``'observed'`` — from the record's EvidencePointer (documents
        are read directly); defaulted defensively so it is always present.
      * ``filename`` / ``location`` — the human-facing source location.
      * ``document_format`` — the resolved format token (pdf/docx/…).
      * ``evidence_pointer`` — the full R16-B1 spine, stored verbatim.

    No document content ever goes into provenance — only source identifiers.
    """
    prov: Dict[str, Any] = dict(record.get("provenance") or {})
    evidence_pointer = record.get("evidence_pointer") or {}
    # origin='observed' surfaced at the top level so a retrieval consumer (and
    # AC6) can read it without unwrapping the nested spine.
    prov.setdefault("origin", evidence_pointer.get("origin", "observed"))
    prov["filename"] = record.get("filename")
    prov["location"] = record.get("location")
    prov["document_format"] = record.get("document_format")
    prov["evidence_pointer"] = evidence_pointer
    return prov


def record_to_artifact(record: Dict[str, Any]) -> Optional[ContentArtifact]:
    """Map one ingestor record to a substrate :class:`ContentArtifact`, or ``None``.

    Returns ``None`` for any record that carries no extracted text — a deliberate
    skip, a per-file error, or a delete tombstone — so only genuinely extracted
    content is handed to the substrate. An extracted record with empty text IS
    mapped (a truthful empty hand-off), because that is a real state the substrate
    should record, distinct from a loud skip.
    """
    if not isinstance(record, dict):
        return None
    extraction = record.get("extraction") or {}
    if extraction.get("status") != _EXTRACTED:
        return None
    if "content" not in record:
        # Defensive: an 'extracted' record always carries content; if one does
        # not, treat it as nothing to hand off rather than fabricating text.
        return None

    artifact_id = record.get("artifact_id")
    if not artifact_id:
        return None

    return ContentArtifact(
        source_system=RETRIEVAL_SOURCE_SYSTEM,
        source_artifact=str(artifact_id),
        content=record.get("content"),
        content_type=record.get("chunk_content_type") or _DEFAULT_CONTENT_TYPE,
        source_timestamp=record.get("source_timestamp"),
        provenance=_build_provenance(record),
    )


def extracted_artifacts(records: List[Dict[str, Any]]) -> List[ContentArtifact]:
    """Map a batch's records to the substrate artifacts worth handing over.

    Filters out every non-extracted record (skips, errors, tombstones) and maps
    the rest to :class:`ContentArtifact`. The result is exactly the ``extracted
    artifacts`` handed to ``retrieval.ingest_content(org_id, extracted_artifacts)``.
    """
    artifacts: List[ContentArtifact] = []
    for record in records or []:
        artifact = record_to_artifact(record)
        if artifact is not None:
            artifacts.append(artifact)
    return artifacts


@dataclass
class DocumentHandoffResult:
    """Outcome of one :func:`ingest_documents` run — extraction + hand-off totals.

    Combines the change-run accounting (batches/records/checkpoint) with the
    substrate's per-artifact result totals, so a caller sees both how much was
    read and how much was indexed. ``error`` mirrors the change runner: it is set
    (and ``checkpoint_advanced`` is left as reported) when a batch hand-off failed,
    so the caller can surface it without the run having raised.
    """

    org_id: str
    batches: int = 0
    records: int = 0
    artifacts_handed_off: int = 0
    artifacts_indexed: int = 0
    artifacts_empty: int = 0
    artifacts_failed: int = 0
    chunks_indexed: int = 0
    chunks_replaced: int = 0
    checkpoint_advanced: bool = False
    first_run: bool = False
    error: Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def ingest_documents(
    org_id: str,
    *,
    source: Optional[DocumentSource] = None,
    extractor: Optional[Extractor] = None,
    batch_size: Optional[int] = None,
    ingest_fn: IngestFn = ingest_content,
    **runner_kwargs: Any,
) -> DocumentHandoffResult:
    """Run document ingestion and hand every extracted artifact to the substrate.

    Drives the :class:`~discovery.ingest.documents.DocumentIngestor` through the
    shared change runner (so incremental reads, the resumable first load, and the
    ``ingestion.artifact_changed`` events are all unchanged) and, for each
    fully-read batch, hands its extracted artifacts to
    ``retrieval.ingest_content(org_id, extracted_artifacts)`` (AC1). Unchanged
    files are never re-extracted or re-handed (AC2, owned by the ingestor's
    checkpoint); skips/errors/tombstones carry no text and are not handed over.

    ``source`` / ``extractor`` / ``batch_size`` are forwarded to the ingestor
    (all optional — the offline fixture source and the real extractor are the
    defaults). ``ingest_fn`` is the substrate entry point, injectable so tests can
    capture hand-offs without a database; it defaults to the real
    ``ingest_content``. Extra ``runner_kwargs`` (e.g. ``read_checkpoint`` /
    ``save_checkpoint``) pass straight through to the change runner.

    Never raises for a runtime failure: like the change runner, a substrate
    hand-off failure is captured on ``result.error`` and leaves the checkpoint
    un-advanced so the batch is re-handed next run (idempotent replace).
    """
    ingestor_kwargs: Dict[str, Any] = {"source": source, "extractor": extractor}
    if batch_size is not None:
        ingestor_kwargs["batch_size"] = batch_size
    ingestor = DocumentIngestor(**ingestor_kwargs)

    summary = DocumentHandoffResult(org_id=org_id)

    def _process_batch(batch: change_runner.DeltaBatch) -> None:
        artifacts = extracted_artifacts(batch.records)
        if not artifacts:
            return
        result = ingest_fn(org_id, artifacts)
        summary.artifacts_handed_off += len(artifacts)
        summary.artifacts_indexed += result.artifacts_indexed
        summary.artifacts_empty += result.artifacts_empty
        summary.artifacts_failed += result.artifacts_failed
        summary.chunks_indexed += result.chunks_indexed
        summary.chunks_replaced += result.chunks_replaced
        logger.info(
            "documents: handed off org=%s artifacts=%d indexed=%d empty=%d "
            "failed=%d chunks_indexed=%d (embedding is async)",
            org_id,
            len(artifacts),
            result.artifacts_indexed,
            result.artifacts_empty,
            result.artifacts_failed,
            result.chunks_indexed,
        )
        if result.artifacts_failed:
            # Do not let the checkpoint advance past content the substrate did not
            # accept — raise so the runner leaves the position for a re-hand next
            # run. Re-handing is idempotent (ingest_content replaces by artifact).
            raise DocumentHandoffError(
                f"{result.artifacts_failed} artifact(s) failed retrieval hand-off "
                f"for org {org_id}; checkpoint not advanced (will retry)"
            )

    run = change_runner.ingest_with_checkpoint(
        ingestor, org_id, process_batch=_process_batch, **runner_kwargs
    )
    summary.batches = run.batches
    summary.records = run.records
    summary.checkpoint_advanced = run.checkpoint_advanced
    summary.first_run = run.first_run
    summary.error = run.error

    # Surface a hand-off failure LOUDLY even if the caller ignores the returned
    # summary: like the change runner, this function never raises for a runtime
    # failure (the batch hand-off error is captured on ``summary.error`` and the
    # checkpoint is left for a retry), but a total substrate outage must not look
    # like a clean run in the logs. A pipeline caller should still check
    # ``summary.ok`` / ``summary.error`` and raise it as a run warning, but this
    # error-level line guarantees the failure is visible regardless.
    if summary.error is not None:
        logger.error(
            "documents: retrieval hand-off did NOT complete for org=%s "
            "(%d artifact(s) failed, checkpoint not advanced, will retry): %s",
            org_id,
            summary.artifacts_failed,
            type(summary.error).__name__,
        )
    return summary
