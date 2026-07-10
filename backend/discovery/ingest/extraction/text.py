"""
R18-A1 / T2 — plain-text / markdown / CSV extraction handler.

The trivial end of the format range: these formats ARE their text. The handler
decodes the bytes as UTF-8 (accepting a BOM) and hands the text straight through.
A byte stream that is not valid UTF-8 text is a corrupt or mislabelled file and
raises :class:`~discovery.ingest.extraction.ExtractionError`, so the ingestor
isolates it per-file (AC5) rather than indexing mojibake. Empty content is a
truthful empty extraction, never a skip — the file genuinely had no text.
"""
from __future__ import annotations

from . import ExtractedText, ExtractionError, _TEXT_FORMATS, register_handler


def extract_text(raw: bytes, fmt: str) -> ExtractedText:
    """Decode a text-family document to :class:`ExtractedText` (prose policy)."""
    if isinstance(raw, str):
        text = raw
    else:
        text = None
        for encoding in ("utf-8", "utf-8-sig"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ExtractionError(f"{fmt} content is not valid UTF-8 text")
    return ExtractedText(
        content=text,
        chunk_content_type="prose",
        structure_hints={
            "format": fmt,
            "chars": len(text),
            "lines": text.count("\n") + 1 if text else 0,
        },
    )


for _fmt in _TEXT_FORMATS:
    register_handler(_fmt, extract_text)
