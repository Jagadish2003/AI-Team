"""
R18-A1 / T1 (AT-523) — Document change-based ingestor.

The extraction layer between file-bearing sources and the R18-B1 retrieval
substrate. It reads the actual CONTENT of documents — PDFs, Office files,
attachments, plain text — so the knowledge inside files becomes discoverable, not
just their existence. This is a new capability: no document-content extraction
existed in the codebase before R18-A1.

Change-based by construction (R16-A1)
-------------------------------------
Implements the :class:`~discovery.ingest.base.ChangeBasedIngestor` contract, so a
large document estate is NOT re-read on every run. The connector's change signal
is a per-file content ``signature`` supplied by the source
(:mod:`discovery.ingest.documents_source`); it encodes a map of
``{artifact_id: signature}`` as its opaque checkpoint and, each run, reads bytes
and extracts text ONLY for files whose signature is new or changed (AC2). A file
whose signature is unchanged is never re-read or re-extracted.

Division of labour is strict (R18-A1)
-------------------------------------
This story EXTRACTS text and hands it, with provenance, onward. It does NOT chunk,
embed, or index — R18-B1 owns that. This subtask (T1) is the ingestor itself: the
change-based contract, the checkpoint, per-file extraction orchestration, and
per-file failure isolation. The format handlers are :mod:`extraction` (the plug
point; T1 ships text/markdown/CSV, T2 adds the binary formats), and the hand-off
to ``retrieval.ingest_content`` is T3 — every record already carries the extracted
``content`` + provenance the hand-off needs, but this file does not call the
substrate.

Per-file failure isolation (AC5)
--------------------------------
Extraction of each file is wrapped independently: a corrupt/unreadable file fails
its OWN extraction only, is recorded on its record as an error, and never takes
down the run or the other files. A file that errors does NOT advance its
checkpoint signature, so a transient failure is retried on the next run rather
than silently skipped forever; a DELIBERATE skip (unsupported/scanned/encrypted —
:class:`extraction.ExtractionSkipped`) is recorded and DOES advance, because
re-reading an unsupported file will never help.

Provenance & change events
--------------------------
Every record carries a fully-populated OBSERVED ``evidence_pointer`` (R16-B1,
``source_system='documents'``, ``origin='observed'`` — AC6) plus ``artifact_id`` +
``change_kind`` so the shared runner (``change_runner.py``) emits one
``ingestion.artifact_changed`` event per changed file. Document content is NEVER
logged — only artifact ids and counts.

Deletes / tombstones (R16-A1 §5)
--------------------------------
When the source reports a full inventory (``source.reports_deletes``), a file
present in the checkpoint but absent from the current listing is a real deletion
and is emitted as a :func:`~discovery.ingest.base.tombstone` record so downstream
freshness (R18-B2) can drop it. A source that returns only a partial/changed set
declares ``reports_deletes = False`` and no deletions are inferred.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from app.provenance import EvidencePointer, utc_now_iso

from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch, tombstone
from .documents_source import DocumentRef, DocumentSource, default_source
from . import extraction
from .extraction import ExtractedText, ExtractionSkipped, ExtractionOutcome

logger = logging.getLogger(__name__)

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default number of files emitted per :class:`DeltaBatch`. Kept modest so a large
#: initial load streams as many small, individually-checkpointed batches
#: (resumability) rather than one monolithic read.
_DEFAULT_BATCH_SIZE = 100

#: The extractor callable signature the ingestor depends on. Defaults to
#: :func:`extraction.extract`; injectable so tests (and future variants) can plug a
#: different extractor without touching the ingestor.
Extractor = Callable[..., ExtractionOutcome]


def _encode_checkpoint(signatures: Dict[str, str]) -> str:
    """Encode the per-file signature map as the opaque checkpoint value.

    ``sort_keys`` keeps the encoding deterministic so two runs over identical state
    produce byte-identical checkpoints (testable, diff-friendly).
    """
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "files": signatures},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, str]:
    """Decode an opaque checkpoint value back into the per-file signature map.

    Tolerant by design: a missing, empty, or unparseable value yields an empty map
    (read every file as new) rather than raising — a degenerate checkpoint must
    degrade to a safe full re-read, never crash the run.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning(
            "documents: could not decode checkpoint value; treating as first run "
            "(full re-read)."
        )
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(k): str(v) for k, v in files.items() if v is not None}


def _build_evidence_pointer(ref: DocumentRef) -> Dict[str, Any]:
    """Build the R16-B1 OBSERVED EvidencePointer for one document (AC6).

    Every extracted unit must be traceable to its exact source file, so each
    record carries a fully-populated, OBSERVED provenance pointer:

      * ``source_system`` = ``'documents'``
      * ``source_artifact`` = the file's stable ``artifact_id`` (so
        ``source_artifact_type`` is ``'record_id'``), identical to the record's
        ``artifact_id``.
      * ``source_timestamp`` = the file's own last-changed UTC ISO-8601 time; falls
        back to now only when missing, so the mandatory spine is always populated.
      * ``origin`` = ``'observed'`` — the file's bytes were read directly, never
        inferred, so no ``extraction_job_id`` is required.
    """
    return EvidencePointer.observed(
        source_system=DocumentIngestor.connector_id,
        source_artifact=ref.artifact_id,
        source_timestamp=ref.source_timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


class DocumentIngestor(ChangeBasedIngestor):
    """Change-based document ingestor (R18-A1 / T1, connector_id ``'documents'``).

    Encodes its position as a per-file content-signature map (opaque to the runner)
    and extracts text only for files whose signature is new or changed (AC2). A
    first run (``since is None``) extracts every current file, streamed as
    resumable, individually-checkpointed batches.

    The document source and the text extractor are both injectable so the ingestor
    can be driven offline, in tests, and (later) against the SharePoint/Confluence
    attachment sources (T5) and the T2 binary format handlers — without any change
    here.

    Deletes / tombstones (R16-A1 §5): ``reports_deletes`` mirrors the source. A
    full-inventory (scan) source lets a file removed from the location be emitted
    as a tombstone; a partial/changed-set source declares False and no deletions
    are inferred.
    """

    connector_id = "documents"
    reports_deletes = True

    def __init__(
        self,
        *,
        source: Optional[DocumentSource] = None,
        extractor: Optional[Extractor] = None,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size
        self._source = source
        self._extractor = extractor or extraction.extract
        # reports_deletes reflects whether the source lists a full inventory.
        if source is not None:
            self.reports_deletes = bool(getattr(source, "reports_deletes", True))

    def _resolve_source(self, org_id: str) -> DocumentSource:
        """Return the injected source, or the mode default (fixture/scan)."""
        if self._source is not None:
            return self._source
        src = default_source(org_id)
        self.reports_deletes = bool(getattr(src, "reports_deletes", True))
        return src

    # ── ChangeBasedIngestor contract ────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of changed documents since ``since``.

        First run (``since is None``): extract every current file, streamed as
        checkpointed batches (resumable). Incremental run: read + extract only
        files whose signature is new or changed, plus tombstones for files that
        disappeared from a full-inventory source (AC2). An unchanged estate yields a
        single empty :class:`DeltaBatch` whose ``next_checkpoint`` echoes the
        incoming position.
        """
        previous = _decode_checkpoint(since.value if since else None)
        # Working signature map advanced as batches are emitted; each yielded
        # next_checkpoint encodes it so any single batch is a valid resume point.
        running = dict(previous)

        source = self._resolve_source(org_id)
        refs = source.list_documents(org_id)
        current_ids = {r.artifact_id for r in refs}

        # New/changed files: signature absent or different from the checkpoint.
        changed = [r for r in refs if previous.get(r.artifact_id) != r.signature]
        # Oldest-first so the checkpoint advances monotonically as batches emit.
        changed.sort(key=lambda r: str(r.source_timestamp or ""))

        # Deletions: known before but no longer present (full-inventory sources).
        deleted_ids = (
            [aid for aid in previous if aid not in current_ids]
            if self.reports_deletes
            else []
        )

        logger.info(
            "documents: org=%s %s — %d file(s) in scope, %d changed, %d deleted",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(refs),
            len(changed),
            len(deleted_ids),
        )

        # One ordered work list: changed files first (extraction), then tombstones.
        work: List[Tuple[str, Any]] = [("change", r) for r in changed]
        work += [("delete", aid) for aid in deleted_ids]

        if not work:
            # Unchanged estate → empty delta echoing the incoming position.
            yield DeltaBatch(
                records=[],
                next_checkpoint=_encode_checkpoint(running),
                is_complete=True,
            )
            return

        total_batches = (len(work) + self.batch_size - 1) // self.batch_size
        emitted = 0
        for start in range(0, len(work), self.batch_size):
            page = work[start : start + self.batch_size]
            records: List[Dict[str, Any]] = []
            for kind, item in page:
                if kind == "delete":
                    records.append(
                        tombstone(item, source_system=self.connector_id)
                    )
                    running.pop(item, None)
                    continue
                ref: DocumentRef = item
                record, advanced = self._extract_record(org_id, source, ref, previous)
                records.append(record)
                if advanced:
                    running[ref.artifact_id] = ref.signature
                # On a NON-advance (extraction error) the file keeps its prior
                # signature (or stays absent), so the next run re-reads it.
            emitted += 1
            yield DeltaBatch(
                records=records,
                next_checkpoint=_encode_checkpoint(running),
                is_complete=(emitted == total_batches),
            )

    # ── Per-file extraction (isolated) ───────────────────────────────────────
    def _extract_record(
        self, org_id: str, source: DocumentSource, ref: DocumentRef, previous: Dict[str, str]
    ) -> Tuple[Dict[str, Any], bool]:
        """Read + extract one file into a record, isolating any failure to it (AC5).

        Returns ``(record, advanced)``: ``advanced`` is True when the checkpoint
        signature should move forward for this file (a successful extraction or a
        DELIBERATE skip) and False when it must not (an unexpected extraction error,
        so the file is retried next run). No document content is ever logged.
        """
        change_kind = (
            ChangeKind.CREATED if ref.artifact_id not in previous else ChangeKind.UPDATED
        )
        document_format = extraction.detect_format(ref.filename, ref.content_type)

        try:
            raw = source.read(org_id, ref)
            outcome: ExtractionOutcome = self._extractor(
                raw, filename=ref.filename, content_type=ref.content_type
            )
        except Exception as exc:  # noqa: BLE001 — one bad file never sinks the run
            logger.warning(
                "documents: extraction FAILED (org=%s artifact=%s format=%s): %s",
                org_id,
                ref.artifact_id,
                document_format,
                type(exc).__name__,
            )
            record = self._base_record(ref, change_kind, document_format)
            record["extraction"] = {
                "status": "error",
                "reason": type(exc).__name__,
                "detail": str(exc),
            }
            return record, False

        record = self._base_record(ref, change_kind, document_format)
        if isinstance(outcome, ExtractionSkipped):
            # A deliberate, recorded non-extraction — loud, never silent emptiness.
            record["extraction"] = {
                "status": "skipped",
                "reason": outcome.reason,
                "detail": outcome.detail,
            }
            logger.info(
                "documents: artifact %s skipped (%s)", ref.artifact_id, outcome.reason
            )
            return record, True

        # Successful extraction — carry the text + policy for the T3 hand-off.
        extracted: ExtractedText = outcome
        record["extraction"] = {"status": "extracted"}
        record["content"] = extracted.content
        record["chunk_content_type"] = extracted.chunk_content_type
        record["structure_hints"] = extracted.structure_hints or {}
        if extracted.provenance:
            merged = dict(record.get("provenance") or {})
            merged.update(extracted.provenance)
            record["provenance"] = merged
        return record, True

    def _base_record(
        self, ref: DocumentRef, change_kind: str, document_format: str
    ) -> Dict[str, Any]:
        """Build the provenance/metadata spine common to every document record.

        ``artifact_id`` + ``change_kind`` let the shared runner emit
        ``ingestion.artifact_changed`` events; the OBSERVED ``evidence_pointer``
        (R16-B1) makes every record traceable to its exact source file (AC6). The
        extracted text (or skip/error) is attached by the caller.
        """
        return {
            "artifact_id": ref.artifact_id,
            "change_kind": change_kind,
            "source_system": self.connector_id,
            "filename": ref.filename,
            "location": ref.location,
            "content_type": ref.content_type,
            "document_format": document_format,
            "signature": ref.signature,
            "source_timestamp": ref.source_timestamp,
            "provenance": dict(ref.provenance or {}),
            "evidence_pointer": _build_evidence_pointer(ref),
        }
