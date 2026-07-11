"""
R18-A1 / T1 — unit tests for the document extraction plug point (the format
boundary). Covers format detection, the built-in text/markdown/CSV handler, the
loud-skip outcomes, and per-file corrupt-content signalling.
"""
from __future__ import annotations

import pytest

from discovery.ingest import extraction
from discovery.ingest.extraction import (
    ExtractedText,
    ExtractionSkipped,
    detect_format,
    extract,
    register_handler,
)


@pytest.mark.parametrize(
    "filename,mime,expected",
    [
        ("a.pdf", None, "pdf"),
        ("a.docx", None, "docx"),
        ("a.xlsx", None, "xlsx"),
        ("a.pptx", None, "pptx"),
        ("a.txt", None, "text"),
        ("a.md", None, "markdown"),
        ("a.csv", None, "csv"),
        ("noext", "application/pdf", "pdf"),
        ("noext", "text/markdown", "markdown"),
        ("clip.unknown", "video/mp4", "media"),
        ("mystery", None, "unknown"),
    ],
)
def test_detect_format(filename, mime, expected):
    assert detect_format(filename, mime) == expected


def test_extract_text_markdown_csv_yield_prose():
    for name in ("notes.txt", "readme.md", "data.csv"):
        out = extract(b"hello, world", filename=name)
        assert isinstance(out, ExtractedText)
        assert out.content == "hello, world"
        assert out.chunk_content_type == "prose"


def test_extract_empty_text_is_truthful_empty_not_a_skip():
    out = extract(b"", filename="empty.txt")
    assert isinstance(out, ExtractedText)
    assert out.content == ""


def test_extract_unknown_format_is_unsupported_skip():
    out = extract(b"...", filename="thing.xyz")
    assert isinstance(out, ExtractionSkipped)
    assert out.reason == extraction.UNSUPPORTED_FORMAT


def test_extract_media_is_non_text_skip():
    out = extract(b"...", filename="clip", content_type="audio/mpeg")
    assert isinstance(out, ExtractionSkipped)
    assert out.reason == extraction.NON_TEXT_MEDIA


def test_extract_recognised_format_without_handler_is_no_handler_skip():
    # A recognised format with no registered handler → loud NO_HANDLER skip. All
    # phase-one formats now have handlers, so simulate a recognised-but-unhandled
    # one by mapping an extension without registering a handler for it.
    extraction._EXT_FORMAT[".fake"] = "fakefmt"
    try:
        out = extract(b"anything", filename="doc.fake")
        assert isinstance(out, ExtractionSkipped)
        assert out.reason == extraction.NO_HANDLER
    finally:
        extraction._EXT_FORMAT.pop(".fake", None)


def test_extract_legacy_encoded_text_decodes_via_fallback():
    """Non-UTF-8 text (legacy Windows-1252 / Latin-1) decodes via the fallback chain
    instead of raising — so a legacy-encoded enterprise file is read once, not
    retried on every run forever. The fallback encoding is recorded for run health.
    """
    # Windows-1252: 0x93/0x94 are smart quotes, 0xe9 is 'é' — invalid UTF-8, valid cp1252.
    out = extract(b"caf\xe9 \x93quoted\x94", filename="legacy.txt")
    assert isinstance(out, ExtractedText)
    assert out.structure_hints["encoding"] == "cp1252"
    assert "café" in out.content

    # 0x81 is undefined in cp1252 but valid in Latin-1, which decodes ANY byte — the
    # guaranteed terminal fallback, so no text-family file is ever un-decodable.
    out = extract(b"raw \x81 byte", filename="broken.txt")
    assert isinstance(out, ExtractedText)
    assert out.structure_hints["encoding"] == "latin-1"


def test_register_handler_is_additive_plug_point():
    """A new format handler slots in without touching the ingestor (T2 pattern)."""
    sentinel = "xyz"
    extraction._EXT_FORMAT[".xyz"] = sentinel  # pretend the format is recognised
    try:
        register_handler(sentinel, lambda raw, fmt: ExtractedText(content="parsed!"))
        out = extract(b"anything", filename="thing.xyz")
        assert isinstance(out, ExtractedText)
        assert out.content == "parsed!"
    finally:
        extraction._EXT_FORMAT.pop(".xyz", None)
        extraction._HANDLERS.pop(sentinel, None)
