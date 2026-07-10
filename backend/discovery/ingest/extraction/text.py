"""
R18-A1 / T2 — plain-text / markdown / CSV extraction handler.

The trivial end of the format range: these formats ARE their text. The handler
decodes the bytes with a tolerant encoding chain — UTF-8 (accepting a BOM) first,
then the legacy single-byte encodings common in enterprise documents (Windows-1252,
then ISO-8859-1 / Latin-1). Latin-1 maps every possible byte, so decoding a
text-family file NEVER fails: a legacy-encoded file is read correctly instead of
erroring on every discovery run forever (the checkpoint only advances on a
successful extraction or a deliberate skip, so a hard decode error would be retried
indefinitely with no cap). Empty content is a truthful empty extraction, never a
skip — the file genuinely had no text.

The encoding that succeeded is recorded in ``structure_hints['encoding']`` so run
health can surface when a non-UTF-8 fallback was needed. This handler is only
reached for text-typed formats (txt/markdown/csv); binary formats route to their
own handlers, so tolerant decoding here does not mask a mis-detected binary file.
"""
from __future__ import annotations

from . import ExtractedText, ExtractionError, _TEXT_FORMATS, register_handler

#: Decode order: UTF-8 (+BOM) first for the modern common case, then the legacy
#: single-byte encodings. ``latin-1`` is last and decodes ANY byte sequence, so it
#: is the guaranteed terminal fallback — no text-family file is ever un-decodable.
_ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1")


def extract_text(raw: bytes, fmt: str) -> ExtractedText:
    """Decode a text-family document to :class:`ExtractedText` (prose policy)."""
    if isinstance(raw, str):
        text, used_encoding = raw, "str"
    else:
        text = None
        used_encoding = None
        for encoding in _ENCODINGS:
            try:
                text = raw.decode(encoding)
                used_encoding = encoding
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            # Unreachable in practice (latin-1 decodes every byte); kept as a
            # defensive guard so a future change to _ENCODINGS can never silently
            # index nothing.
            raise ExtractionError(f"{fmt} content could not be decoded as text")
    return ExtractedText(
        content=text,
        chunk_content_type="prose",
        structure_hints={
            "format": fmt,
            "encoding": used_encoding,
            "chars": len(text),
            "lines": text.count("\n") + 1 if text else 0,
        },
    )


for _fmt in _TEXT_FORMATS:
    register_handler(_fmt, extract_text)
