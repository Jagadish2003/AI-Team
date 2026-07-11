"""
R18-A1 — Document text-extraction plug point (the format boundary).

Document ingestion has a strict division of labour (R18-A1, Architectural Note
"Extraction is the plug point"):

  * **This layer** turns a file's raw bytes into either :class:`ExtractedText`
    (clean text + light structure hints + provenance) or an
    :class:`ExtractionSkipped` reason. New formats — and OCR later — are new
    handlers behind this contract; NOTHING downstream changes when one is added.
  * The **substrate** (R18-B1) owns everything after the text is produced:
    chunking, hashing, embedding, indexing. Extraction never touches it.

T1 vs T2 boundary
-----------------
This module is the CONTRACT plus the trivial text formats. T1 (the
:class:`~discovery.ingest.documents.DocumentIngestor`) owns the plug point — the
result types, format detection, and the handler registry — and ships the
plain-text / markdown / CSV handler, which needs no third-party parser. T2
(the binary format handlers: PDF, .docx, .xlsx, .pptx) registers its handlers
into :func:`register_handler` without touching the ingestor or the substrate. A
format with no handler yet resolves to a LOUD :class:`ExtractionSkipped`
(``NO_HANDLER``) — never silent emptiness (R18-A1 Architectural Note "Loud skips,
never silent emptiness").

Loud skips, never silent emptiness
----------------------------------
A scanned-image PDF that extracts to nothing, indexed as "no content", would make
the document invisible while appearing ingested. So every non-extraction is an
explicit :class:`ExtractionSkipped` carrying a reason the run health can show
(AC3), and a genuinely corrupt/undecodable file raises :class:`ExtractionError`
which the ingestor isolates per file (AC5) — neither is ever an empty
:class:`ExtractedText`. An empty :class:`ExtractedText` means the file really had
no text, which is a different, truthful fact.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Callable, Dict, Optional, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types — the extraction contract (produced by every handler)
# ---------------------------------------------------------------------------


@dataclass
class ExtractedText:
    """Text a handler successfully extracted from one document.

    ``content``            the extracted clean text. Empty string is valid and
                           TRUTHFUL — the file genuinely had no text — and is
                           distinct from a skip (unsupported/scanned) or an error
                           (corrupt).
    ``chunk_content_type`` which retrieval chunking policy the text belongs to
                           (``'prose'`` | ``'conversation'`` | ``'code'``). Every
                           document format is prose; the field exists so the T3
                           hand-off can name the substrate's policy without a
                           second lookup.
    ``structure_hints``    light, non-secret structural metadata a handler chose
                           to surface (page count, sheet names, char count …).
                           Advisory only; never load-bearing.
    ``provenance``         optional extra provenance a handler discovered (title,
                           author embedded in the file …), merged with the
                           ingestor's source provenance.
    """

    content: str
    chunk_content_type: str = "prose"
    structure_hints: Optional[Dict[str, Any]] = None
    provenance: Optional[Dict[str, Any]] = None


@dataclass
class ExtractionSkipped:
    """A DELIBERATE, recorded non-extraction — loud, never silent emptiness.

    A handler returns this when the file is a recognised format it cannot turn
    into text (a scanned-image PDF with no text layer, an encrypted file, a
    format with no handler installed yet). ``reason`` is a stable machine token
    (one of the ``*`` constants below); ``detail`` is a short human string safe to
    show in run health — it must never contain document content.
    """

    reason: str
    detail: Optional[str] = None


#: Result of an extraction attempt: text, or a recorded skip.
ExtractionOutcome = Union[ExtractedText, ExtractionSkipped]


class ExtractionError(Exception):
    """A file was recognised but its content could not be parsed (corrupt/undecodable).

    Distinct from :class:`ExtractionSkipped` (a deliberate, expected non-extraction):
    this is an unexpected failure. Handlers raise it — or any exception — and the
    ingestor isolates the failure to that one file (AC5), keeping the run and the
    other files going. Its message must never carry document content.
    """


# ---------------------------------------------------------------------------
# Skip-reason vocabulary (stable tokens surfaced in run health)
# ---------------------------------------------------------------------------

#: A PDF (or image) with no extractable text layer — needs OCR (out of scope).
SCANNED_IMAGE = "scanned_image"
#: The file is password-protected / encrypted and cannot be opened.
ENCRYPTED = "encrypted"
#: The file's type is not a supported document format at all.
UNSUPPORTED_FORMAT = "unsupported_format"
#: A recognised format whose handler is not installed yet (e.g. before T2 lands).
NO_HANDLER = "no_handler"
#: Audio / video / other binary that carries no document text.
NON_TEXT_MEDIA = "non_text_media"
#: The file exceeds the configured per-file size cap (R18-A1 T4 / AC4). Recorded
#: by the ingestor, not a format handler — the file is never read/parsed.
SIZE_CAPPED = "size_capped"
#: The per-run extraction budget was exhausted before this file (R18-A1 T4 / AC4).
#: A TRANSIENT, run-level skip: the file is retried on the next run.
BUDGET_EXCEEDED = "budget_exceeded"


# ---------------------------------------------------------------------------
# Format detection (extension first, MIME as a fallback)
# ---------------------------------------------------------------------------

#: Recognised document formats. The trivial text family is handled here in T1;
#: the binary family (pdf/docx/xlsx/pptx) is handled by T2's registered handlers.
_EXT_FORMAT: Dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".txt": "text",
    ".text": "text",
    ".log": "text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdown": "markdown",
    ".csv": "csv",
    ".tsv": "csv",
}

_MIME_FORMAT: Dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "text/plain": "text",
    "text/markdown": "markdown",
    "text/csv": "csv",
    "text/tab-separated-values": "csv",
}

#: MIME prefixes that are non-document media — flagged, never parsed as text.
_MEDIA_MIME_PREFIXES = ("audio/", "video/", "image/")

#: Formats this module handles directly (no third-party parser needed).
_TEXT_FORMATS = frozenset({"text", "markdown", "csv"})


def detect_format(filename: str, content_type: Optional[str] = None) -> str:
    """Resolve a document's format token from its filename (then MIME as fallback).

    Extension wins because it is the most reliable signal for the on-disk /
    attachment cases; a MIME/``content_type`` hint from the source resolves the
    extension-less case. Returns ``'unknown'`` when neither identifies a supported
    format, and ``'media'`` for audio/video/image MIME types (flagged out of scope,
    not treated as text — R18-A1 §1).
    """
    ext = PurePath(filename or "").suffix.lower()
    if ext in _EXT_FORMAT:
        return _EXT_FORMAT[ext]

    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime:
        if mime in _MIME_FORMAT:
            return _MIME_FORMAT[mime]
        if any(mime.startswith(p) for p in _MEDIA_MIME_PREFIXES):
            return "media"
    return "unknown"


# ---------------------------------------------------------------------------
# Handler registry — the additive plug point
# ---------------------------------------------------------------------------

#: A handler takes raw bytes + the resolved format token and returns an outcome.
Handler = Callable[[bytes, str], ExtractionOutcome]

_HANDLERS: Dict[str, Handler] = {}


def register_handler(fmt: str, handler: Handler) -> None:
    """Register (or replace) the extraction handler for a format token.

    The additive seam for new formats (R18-A1 Architectural Note "Extraction is
    the plug point"). T2 registers ``pdf``/``docx``/``xlsx``/``pptx`` here; a
    future OCR pass would register an image handler — neither touches the
    ingestor or the substrate.
    """
    if not fmt or not isinstance(fmt, str):
        raise ValueError("format token must be a non-empty string")
    if not callable(handler):
        raise ValueError("handler must be callable")
    _HANDLERS[fmt] = handler


def extract(
    raw: bytes,
    *,
    filename: str,
    content_type: Optional[str] = None,
    fmt: Optional[str] = None,
) -> ExtractionOutcome:
    """Extract text from one document's raw bytes, dispatching on its format.

    The single entry point the ingestor calls per file. Resolves the format (unless
    the caller pre-computed it), then:

      * a supported text format          → :class:`ExtractedText`;
      * a recognised binary format with
        no handler installed yet          → :class:`ExtractionSkipped` (``NO_HANDLER``);
      * audio/video/image media           → :class:`ExtractionSkipped` (``NON_TEXT_MEDIA``);
      * anything unrecognised             → :class:`ExtractionSkipped` (``UNSUPPORTED_FORMAT``).

    A handler may still raise (corrupt file) — this function does NOT swallow it;
    the ingestor owns per-file isolation (AC5) so the failure is recorded against
    the right artifact.
    """
    resolved = fmt or detect_format(filename, content_type)
    if resolved == "unknown":
        return ExtractionSkipped(
            UNSUPPORTED_FORMAT, f"unrecognised document type for {filename!r}"
        )
    if resolved == "media":
        return ExtractionSkipped(
            NON_TEXT_MEDIA, f"audio/video/image is out of scope for {filename!r}"
        )
    handler = _HANDLERS.get(resolved)
    if handler is None:
        return ExtractionSkipped(
            NO_HANDLER,
            f"no extraction handler installed for {resolved!r} yet (R18-A1 T2)",
        )
    return handler(raw, resolved)


# ---------------------------------------------------------------------------
# Handler registration (R18-A1 T2)
# ---------------------------------------------------------------------------
# Importing the format-handler modules is what registers them: each calls
# register_handler() at import time. They live in their own modules (text.py,
# pdf.py, docx.py, xlsx.py, pptx.py) so a new format is a new file — the additive
# plug point. Imported LAST so register_handler / the result types are already
# defined when each submodule imports them back from this package.
from . import text as _text  # noqa: E402,F401
from . import pdf as _pdf  # noqa: E402,F401
from . import docx as _docx  # noqa: E402,F401
from . import xlsx as _xlsx  # noqa: E402,F401
from . import pptx as _pptx  # noqa: E402,F401
