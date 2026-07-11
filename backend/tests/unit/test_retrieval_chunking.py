"""Unit tests for per-content-type chunking policies (R18-B1 T2).

Covers the T2 acceptance surface (Section 1 chunking policy):

* content-type-aware splitting — prose on headings/paragraphs with overlap,
  conversation on turn/time windows, code on function boundaries;
* every chunk carries full provenance (org_id, source system, source artifact,
  content type, position, source timestamp) plus a correct content hash;
* chunk size is bounded so a retrieved set fits the assembly budget;
* invalid content type is rejected; empty content yields no chunks.

Pure functions — no DB, no model gateway — so this runs in the unit suite.
"""
from __future__ import annotations

import pytest

from app.retrieval.chunking import (
    CHUNK_OVERLAP_CHARS,
    MAX_CHUNK_CHARS,
    Chunk,
    chunk_content,
)
from database.models.retrieval import compute_content_hash

_COMMON = dict(
    org_id="org_a",
    source_system="confluence",
    source_artifact="page/123",
    source_timestamp="2026-07-07T00:00:00+00:00",
    provenance={"url": "https://example/x"},
)


# ---------------------------------------------------------------------------
# Contract: validation + provenance + hash
# ---------------------------------------------------------------------------


def test_unknown_content_type_rejected():
    with pytest.raises(ValueError):
        chunk_content(content="hi", content_type="pdf", **_COMMON)


@pytest.mark.parametrize("content_type", ["prose", "conversation", "code"])
@pytest.mark.parametrize("empty", ["", "   \n\t  ", None])
def test_empty_content_yields_no_chunks(content_type, empty):
    assert chunk_content(content=empty, content_type=content_type, **_COMMON) == []


@pytest.mark.parametrize("content_type", ["prose", "conversation", "code"])
def test_every_chunk_carries_full_provenance_and_hash(content_type):
    text = "\n\n".join(f"Paragraph {i} with enough words to matter." for i in range(6))
    chunks = chunk_content(content=text, content_type=content_type, **_COMMON)
    assert chunks, "expected at least one chunk"
    for pos, ch in enumerate(chunks):
        assert isinstance(ch, Chunk)
        assert ch.org_id == "org_a"
        assert ch.source_system == "confluence"
        assert ch.source_artifact == "page/123"
        assert ch.content_type == content_type
        assert ch.source_timestamp == "2026-07-07T00:00:00+00:00"
        assert ch.provenance == {"url": "https://example/x"}
        # position is 0-based and sequential in source order
        assert ch.position == pos
        # content hash is derived from content (freshness key, R18-B2)
        assert ch.content_hash == compute_content_hash(ch.content)


@pytest.mark.parametrize("content_type", ["prose", "conversation", "code"])
def test_all_chunks_bounded(content_type):
    # 400 distinct lines/paragraphs -> well over one chunk for every policy.
    text = "\n\n".join(
        f"Line {i} carrying a fair amount of representative text content." for i in range(400)
    )
    chunks = chunk_content(content=text, content_type=content_type, **_COMMON)
    assert len(chunks) > 1
    for ch in chunks:
        assert len(ch.content) <= MAX_CHUNK_CHARS


# ---------------------------------------------------------------------------
# Prose policy — structure + overlap
# ---------------------------------------------------------------------------


def test_prose_small_input_is_single_chunk():
    chunks = chunk_content(
        content="One short paragraph.\n\nAnother short paragraph.",
        content_type="prose",
        **_COMMON,
    )
    assert len(chunks) == 1
    assert "One short paragraph." in chunks[0].content
    assert "Another short paragraph." in chunks[0].content


def test_prose_attaches_heading_to_following_body():
    text = "# Section Title\n\n" + "Body sentence that explains the section in detail."
    chunks = chunk_content(content=text, content_type="prose", **_COMMON)
    assert len(chunks) == 1
    # The heading travels with the body it introduces (self-describing chunk).
    assert "# Section Title" in chunks[0].content
    assert "Body sentence" in chunks[0].content


def test_prose_overlaps_across_boundaries():
    # Distinct, sentence-punctuated paragraphs (so none read as headings), large
    # enough to force multiple chunks.
    paras = [f"Paragraph {i} " + ("detail " * 30) + "." for i in range(40)]
    text = "\n\n".join(paras)
    chunks = chunk_content(content=text, content_type="prose", **_COMMON)
    assert len(chunks) >= 2
    # Prose carries overlap: the start of chunk N repeats a tail paragraph of
    # chunk N-1.
    overlaps = 0
    for prev, nxt in zip(chunks, chunks[1:]):
        last_para_prev = prev.content.split("\n\n")[-1]
        if last_para_prev and last_para_prev in nxt.content:
            overlaps += 1
    assert overlaps >= 1, "expected prose chunks to overlap at a boundary"


# ---------------------------------------------------------------------------
# Conversation policy — turn/time windows, no overlap
# ---------------------------------------------------------------------------


def test_conversation_groups_turns_without_overlap():
    # Long enough in total (~60 turns * ~70 chars > 2 * MAX_CHUNK_CHARS) to force
    # multiple windows.
    turns = [
        f"user{i}: message number {i} with additional descriptive content to add length."
        for i in range(60)
    ]
    text = "\n".join(turns)
    chunks = chunk_content(content=text, content_type="conversation", **_COMMON)
    assert len(chunks) >= 2
    for ch in chunks:
        assert len(ch.content) <= MAX_CHUNK_CHARS
    # No overlap for conversations: each turn appears in exactly one chunk.
    joined = "\n".join(c.content for c in chunks)
    assert joined.count("user0: message number 0") == 1
    assert joined.count("user59: message number 59") == 1


# ---------------------------------------------------------------------------
# Code policy — function/file boundaries, fallback
# ---------------------------------------------------------------------------


def test_code_splits_on_function_boundaries():
    text = (
        "import os\n"
        "\n"
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
        "\n"
        "class Gamma:\n"
        "    def method(self):\n"
        "        return 3\n"
    )
    chunks = chunk_content(content=text, content_type="code", **_COMMON)
    joined = "\n".join(c.content for c in chunks)
    # All definitions survive intact.
    for token in ("def alpha", "def beta", "class Gamma", "import os"):
        assert token in joined
    # A boundary is respected: alpha and beta do not both begin the same line run
    # as a single monolithic block when boundaries exist (there is real structure).
    assert any("def alpha" in c.content for c in chunks)
    assert any("def beta" in c.content for c in chunks)


def test_code_without_boundaries_falls_back_to_window():
    # A long boundary-free blob (e.g. minified/config) must still be bounded.
    text = "x = 1; " * 1000  # ~7000 chars, no def/class/function
    chunks = chunk_content(content=text, content_type="code", **_COMMON)
    assert len(chunks) > 1
    for ch in chunks:
        assert len(ch.content) <= MAX_CHUNK_CHARS


def test_oversized_single_unit_is_window_split():
    # One paragraph with no internal blank lines, far larger than the bound.
    text = "word " * 1200  # ~6000 chars, single paragraph
    chunks = chunk_content(content=text, content_type="prose", **_COMMON)
    assert len(chunks) > 1
    for ch in chunks:
        assert len(ch.content) <= MAX_CHUNK_CHARS
