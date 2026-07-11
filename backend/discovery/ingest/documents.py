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
import os
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from app.provenance import EvidencePointer, utc_now_iso

from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch, tombstone
from .documents_source import DocumentRef, DocumentSource, default_source
from . import extraction
from .extraction import (
    BUDGET_EXCEEDED,
    SIZE_CAPPED,
    ExtractedText,
    ExtractionSkipped,
    ExtractionOutcome,
)

logger = logging.getLogger(__name__)

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default number of files emitted per :class:`DeltaBatch`. Kept modest so a large
#: initial load streams as many small, individually-checkpointed batches
#: (resumability) rather than one monolithic read.
_DEFAULT_BATCH_SIZE = 100

#: Default per-file size cap (bytes) — a single file larger than this is skipped
#: with reason rather than read/parsed (R18-A1 T4 / AC4). Overridable per-deployment
#: via ``DOCUMENT_MAX_FILE_BYTES``; 0 disables the cap.
_DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MiB

#: Default per-run extraction budget (bytes) — once this much file content has been
#: read in one run, remaining changed files are skipped-with-reason (and retried
#: next run) so one bulk-upload event cannot starve a run. Overridable via
#: ``DOCUMENT_EXTRACTION_BUDGET_BYTES``; 0 disables the budget.
_DEFAULT_EXTRACTION_BUDGET_BYTES = 256 * 1024 * 1024  # 256 MiB


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int env var, falling back to ``default`` if unset/invalid.

    A negative or unparseable value degrades to the default rather than raising —
    a misconfigured cap must not break ingestion.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("documents: %s=%r is not an integer; using default %d", name, raw, default)
        return default
    return value if value >= 0 else default

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

    Size cap & extraction budget (R18-A1 T4 / AC4): a per-file ``max_file_bytes``
    cap skips (with reason ``size_capped``) any single file too large to read, and a
    per-run ``extraction_budget_bytes`` bound skips remaining files (reason
    ``budget_exceeded``) once a run has EXTRACTED that much content — so one enormous
    archive or bulk upload cannot starve a run. Both are configurable (constructor
    args override the ``DOCUMENT_MAX_FILE_BYTES`` / ``DOCUMENT_EXTRACTION_BUDGET_BYTES``
    env defaults; ``0`` disables a limit). A ``size_capped`` skip advances the
    checkpoint (deterministic for that file's current signature); a
    ``budget_exceeded`` skip does NOT (it is transient, so the file is retried next
    run when budget is available).

    The budget counts only SUCCESSFULLY-EXTRACTED bytes — a file that is read but
    then discarded (over the post-read size cap, an extraction error, or a
    deliberate skip) charges 0, exactly like the pre-read size cap. Discarded
    content therefore never consumes the budget and can never starve legitimate
    files later in the same run (e.g. an attachment of unknown size that turns out
    oversized does not eat into what remains for the files after it).
    """

    connector_id = "documents"
    reports_deletes = True

    def __init__(
        self,
        *,
        source: Optional[DocumentSource] = None,
        extractor: Optional[Extractor] = None,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        max_file_bytes: Optional[int] = None,
        extraction_budget_bytes: Optional[int] = None,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size
        self._source = source
        self._extractor = extractor or extraction.extract
        # Limits: explicit arg wins; otherwise the env default (0 = unlimited).
        self.max_file_bytes = (
            _env_int("DOCUMENT_MAX_FILE_BYTES", _DEFAULT_MAX_FILE_BYTES)
            if max_file_bytes is None
            else max(0, int(max_file_bytes))
        )
        self.extraction_budget_bytes = (
            _env_int("DOCUMENT_EXTRACTION_BUDGET_BYTES", _DEFAULT_EXTRACTION_BUDGET_BYTES)
            if extraction_budget_bytes is None
            else max(0, int(extraction_budget_bytes))
        )
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
        budget_used = 0  # bytes of file content successfully extracted this run (AC4 budget)
        skipped_size = skipped_budget = 0
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

                # Per-file size cap, applied BEFORE the read when the source knows
                # the size — an enormous file is never downloaded (AC4).
                if self._exceeds_cap(ref.size_bytes):
                    records.append(self._skip_record(
                        ref, previous, SIZE_CAPPED,
                        f"file is {ref.size_bytes} bytes (cap {self.max_file_bytes})",
                        size_bytes=ref.size_bytes,
                    ))
                    running[ref.artifact_id] = ref.signature  # deterministic → advance
                    skipped_size += 1
                    continue

                # Per-run extraction budget: once exhausted, skip the rest of this
                # run's changed files (retried next run — transient, no advance).
                if self.extraction_budget_bytes and budget_used >= self.extraction_budget_bytes:
                    records.append(self._skip_record(
                        ref, previous, BUDGET_EXCEEDED,
                        f"per-run extraction budget {self.extraction_budget_bytes} bytes "
                        "exhausted; retried next run",
                    ))
                    skipped_budget += 1
                    continue

                record, advanced, budget_bytes = self._extract_record(
                    org_id, source, ref, previous
                )
                budget_used += budget_bytes
                if record["extraction"].get("reason") == SIZE_CAPPED:
                    skipped_size += 1
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

        if skipped_size or skipped_budget:
            # Surfaced in run health: how much coverage the caps/budget cost (AC4).
            logger.warning(
                "documents: org=%s — %d file(s) skipped over size cap, %d over the "
                "per-run extraction budget (%d bytes extracted).",
                org_id, skipped_size, skipped_budget, budget_used,
            )

    # ── Size-cap / budget helpers (R18-A1 T4 / AC4) ──────────────────────────
    def _exceeds_cap(self, size_bytes: Optional[int]) -> bool:
        """True when a KNOWN file size exceeds the configured per-file cap.

        Returns False when the cap is disabled (0) or the size is unknown — the
        unknown case is re-checked against the actual byte length after the read.
        """
        return bool(self.max_file_bytes) and size_bytes is not None and size_bytes > self.max_file_bytes

    def _skip_record(
        self,
        ref: DocumentRef,
        previous: Dict[str, str],
        reason: str,
        detail: str,
        *,
        size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Build a skipped-with-reason record (loud, never silent emptiness — AC4)."""
        change_kind = (
            ChangeKind.CREATED if ref.artifact_id not in previous else ChangeKind.UPDATED
        )
        document_format = extraction.detect_format(ref.filename, ref.content_type)
        record = self._base_record(ref, change_kind, document_format)
        block: Dict[str, Any] = {"status": "skipped", "reason": reason, "detail": detail}
        if size_bytes is not None:
            block["size_bytes"] = size_bytes
        record["extraction"] = block
        logger.warning(
            "documents: artifact %s skipped (%s)", ref.artifact_id, reason
        )
        return record

    # ── Per-file extraction (isolated) ───────────────────────────────────────
    def _extract_record(
        self, org_id: str, source: DocumentSource, ref: DocumentRef, previous: Dict[str, str]
    ) -> Tuple[Dict[str, Any], bool, int]:
        """Read + extract one file into a record, isolating any failure to it (AC5).

        Returns ``(record, advanced, budget_bytes)``. ``advanced`` is True when the
        checkpoint signature should move forward for this file (a successful
        extraction or a DELIBERATE skip — including a post-read size cap) and False
        when it must not (an unexpected extraction error, so the file is retried
        next run). ``budget_bytes`` is what this file charges against the per-run
        extraction budget: the file's size ONLY when its content was successfully
        extracted, and 0 otherwise (read failure, post-read size cap, extraction
        error, or deliberate skip) — read-but-discarded content never consumes the
        budget, matching the pre-read size cap. No document content is ever logged.
        """
        change_kind = (
            ChangeKind.CREATED if ref.artifact_id not in previous else ChangeKind.UPDATED
        )
        document_format = extraction.detect_format(ref.filename, ref.content_type)

        try:
            raw = source.read(org_id, ref)
        except Exception as exc:  # noqa: BLE001 — one unreadable file never sinks the run
            logger.warning(
                "documents: read FAILED (org=%s artifact=%s): %s",
                org_id, ref.artifact_id, type(exc).__name__,
            )
            record = self._base_record(ref, change_kind, document_format)
            record["extraction"] = {
                "status": "error",
                "reason": type(exc).__name__,
                "detail": str(exc),
            }
            return record, False, 0

        size = len(raw)
        # Post-read size cap: the source did not know the size up front, so it is
        # enforced now against the actual bytes (AC4). The file is discarded (never
        # indexed), so it charges 0 to the budget — identical to the pre-read cap,
        # so an unknown-size oversized file cannot starve legitimate files.
        if self._exceeds_cap(size):
            return (
                self._skip_record(
                    ref, previous, SIZE_CAPPED,
                    f"file is {size} bytes (cap {self.max_file_bytes})",
                    size_bytes=size,
                ),
                True,
                0,
            )

        try:
            outcome: ExtractionOutcome = self._extractor(
                raw, filename=ref.filename, content_type=ref.content_type
            )
        except Exception as exc:  # noqa: BLE001 — one bad file never sinks the run
            logger.warning(
                "documents: extraction FAILED (org=%s artifact=%s format=%s): %s",
                org_id, ref.artifact_id, document_format, type(exc).__name__,
            )
            record = self._base_record(ref, change_kind, document_format)
            record["extraction"] = {
                "status": "error",
                "reason": type(exc).__name__,
                "detail": str(exc),
            }
            # Nothing was indexed — charge 0 (the file is retried next run anyway).
            return record, False, 0

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
            # Nothing was indexed (deliberate skip) — charge 0 to the budget.
            return record, True, 0

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
        # Content was extracted and will be indexed — this is the only path that
        # charges the per-run extraction budget.
        return record, True, size

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
