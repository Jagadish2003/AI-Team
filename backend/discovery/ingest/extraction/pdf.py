"""
R18-A1 / T2 — PDF text extraction handler (text-based PDFs only).

Extracts the text layer of a text-based PDF with ``pypdf`` (pure-python, no system
dependency). The two loud-skip cases the story calls out are handled explicitly
(AC3):

  * **Encrypted** — a PDF that will not open with an empty password is recorded as
    :data:`~discovery.ingest.extraction.ENCRYPTED`, not force-read.
  * **Scanned image** — a PDF whose pages carry NO extractable text (the pages are
    images) is recorded as :data:`~discovery.ingest.extraction.SCANNED_IMAGE`
    rather than indexed as empty "content". OCR of those images is out of scope.

A genuinely corrupt/unparseable PDF raises
:class:`~discovery.ingest.extraction.ExtractionError` so the ingestor isolates it
per-file (AC5). If ``pypdf`` is not installed the handler degrades to a loud
``NO_HANDLER`` skip rather than breaking the run.
"""
from __future__ import annotations

import io
import logging

from . import (
    ENCRYPTED,
    NO_HANDLER,
    SCANNED_IMAGE,
    ExtractedText,
    ExtractionError,
    ExtractionSkipped,
    ExtractionOutcome,
    register_handler,
)

logger = logging.getLogger(__name__)


def extract_pdf(raw: bytes, fmt: str) -> ExtractionOutcome:
    """Extract text from a PDF, or record a loud skip (encrypted/scanned)."""
    try:
        import pypdf
    except ImportError:  # pragma: no cover - pypdf ships in requirements
        return ExtractionSkipped(NO_HANDLER, "pypdf is not installed")

    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001 — a parse failure is a corrupt file (AC5)
        raise ExtractionError(f"could not parse PDF: {type(exc).__name__}") from exc

    if reader.is_encrypted:
        # An owner-only-restricted PDF still opens with an empty user password; a
        # truly password-protected one does not → loud ENCRYPTED skip (AC3).
        try:
            opened = bool(reader.decrypt(""))
        except Exception:  # noqa: BLE001 — any decrypt failure means we can't read it
            opened = False
        if not opened:
            return ExtractionSkipped(ENCRYPTED, "PDF is password-protected")

    pages = reader.pages
    parts = []
    for page in pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — one unreadable page must not lose the rest
            page_text = ""
        if page_text.strip():
            parts.append(page_text.strip())

    content = "\n\n".join(parts).strip()
    page_count = len(pages)
    if not content:
        # Pages exist but no text layer → scanned images (loud skip, AC3), never
        # silently indexed as empty content.
        return ExtractionSkipped(
            SCANNED_IMAGE,
            f"no extractable text in {page_count}-page PDF (likely scanned images)",
        )

    return ExtractedText(
        content=content,
        chunk_content_type="prose",
        structure_hints={"format": "pdf", "pages": page_count, "chars": len(content)},
        provenance=_pdf_provenance(reader),
    )


def _pdf_provenance(reader) -> dict:
    """Best-effort title/author from the PDF metadata (never fatal)."""
    prov: dict = {}
    try:
        meta = reader.metadata or {}
        if meta.get("/Title"):
            prov["title"] = str(meta.get("/Title"))
        if meta.get("/Author"):
            prov["author"] = str(meta.get("/Author"))
    except Exception:  # noqa: BLE001 — metadata is advisory only
        pass
    return prov


register_handler("pdf", extract_pdf)
