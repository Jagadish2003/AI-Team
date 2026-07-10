"""Chunking policy — per-content-type retrieval units (R18-B1 T2).

Breaks large source content into smaller, retrieval-ready chunks so documents,
conversations, code files, and other text can be searched without sending the
whole source into the LLM. Policy is per content type (Section 1):

  * prose (documents, pages) splits on structure — headings / paragraphs — with
    overlap so a boundary never splits a fact in half;
  * conversations split on thread / time windows — consecutive turns grouped into
    size-bounded, chronologically-adjacent windows;
  * code splits on file / function boundaries where possible (falling back to a
    bounded window split for content with no detectable boundaries).

Chunk size is bounded (``MAX_CHUNK_CHARS``) so a retrieved set of up to the
assembler's ``max_evidence_chunks`` (R16-B2, default 10) stays within the LLM
input budget — retrieval provides useful evidence, never a flood of text.

Every chunk carries the full provenance needed to trace it back to its source:
org_id, source system, source artifact id, content type, position within the
artifact, source timestamp, and a content hash. The hash is what the freshness
story (R18-B2) uses to detect whether content changed and whether an old vector
is still valid.

Producers never create vectors: they hand extracted text + provenance to the
substrate, and the substrate owns chunking from here on (the ``ingest_content``
producer contract that calls this is T5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Re-exported so callers hash content through one definition shared with the
# storage schema (freshness parity, R18-B2).
from database.models.retrieval import CONTENT_TYPES, compute_content_hash

# Upper bound on a chunk's character length. Bounded so a retrieved set (up to the
# assembler's max_evidence_chunks — R16-B2) stays within the downstream LLM input
# budget (Section 1). ~2000 chars keeps a full retrieved set to a few thousand
# tokens.
MAX_CHUNK_CHARS = 2000
# Overlap carried between adjacent PROSE chunks so a boundary never splits a fact
# in half. Only prose overlaps (Section 1); conversation/code use 0 so turns and
# functions are not duplicated across chunks.
CHUNK_OVERLAP_CHARS = 200

# A paragraph counts as a heading (and is attached to the following body so a
# chunk keeps its section title) when it is a Markdown ATX heading, or a single
# short line with no terminal sentence punctuation.
_ATX_HEADING_RE = re.compile(r"^#{1,6}\s+\S")

# Best-effort, multi-language function/class boundary. Matches the start of a
# Python def/class, a JS/TS function/class, a Go func, and a Java/C#-style method
# signature. "where possible" (Section 1): content with no match falls back to a
# bounded window split.
_CODE_BOUNDARY_RE = re.compile(
    r"^\s*("
    r"(async\s+)?def\s+\w+"                         # Python def / async def
    r"|class\s+\w+"                                   # Python / JS / TS class
    r"|(export\s+)?(default\s+)?(async\s+)?function\b"  # JS / TS function
    r"|func\s+\w+"                                    # Go func
    r"|(public|private|protected|internal|static|final|abstract|virtual|override)"
    r"[\w\s<>\[\],.]*\s+\w+\s*\([^;{]*\)\s*\{?\s*$"   # Java / C# method signature
    r")"
)


@dataclass
class Chunk:
    """A single retrieval unit produced from a source artifact.

    Carries exactly the provenance Section 1 requires on every chunk: org_id,
    content type, position within the artifact, source system, source artifact id,
    an optional source timestamp, optional extra provenance, and the derived
    content hash. The content hash is computed from ``content`` (never supplied by
    a caller) so freshness (R18-B2) always compares like for like.
    """

    org_id: str
    content: str
    content_type: str
    source_system: str
    source_artifact: str
    position: int = 0
    source_timestamp: Optional[str] = None
    provenance: Optional[dict[str, Any]] = None
    content_hash: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.content_type not in CONTENT_TYPES:
            raise ValueError(f"content_type must be one of {sorted(CONTENT_TYPES)}")
        self.content_hash = compute_content_hash(self.content)


def chunk_content(
    org_id: str,
    content: str,
    content_type: str,
    source_system: str,
    source_artifact: str,
    source_timestamp: Optional[str] = None,
    provenance: Optional[dict[str, Any]] = None,
) -> list[Chunk]:
    """Split ``content`` into ``Chunk`` units per its content-type policy.

    Dispatches to the prose / conversation / code splitter for ``content_type``,
    then stamps each resulting piece with full provenance and its position within
    the artifact (0-based, in source order). Empty/whitespace content yields no
    chunks. Raises ``ValueError`` for an unknown content type.
    """
    if content_type not in CONTENT_TYPES:
        raise ValueError(f"content_type must be one of {sorted(CONTENT_TYPES)}")

    splitter: Callable[[str], list[str]] = _SPLITTERS[content_type]
    pieces = splitter(content or "")

    return [
        Chunk(
            org_id=org_id,
            content=piece,
            content_type=content_type,
            source_system=source_system,
            source_artifact=source_artifact,
            position=idx,
            source_timestamp=source_timestamp,
            provenance=provenance,
        )
        for idx, piece in enumerate(pieces)
    ]


# ---------------------------------------------------------------------------
# Per-content-type policies
# ---------------------------------------------------------------------------


def _split_prose(text: str) -> list[str]:
    """Prose: split on structure (headings / paragraphs) with overlap."""
    return _pack_units(_prose_units(text), "\n\n", CHUNK_OVERLAP_CHARS)


def _split_conversation(text: str) -> list[str]:
    """Conversations: group consecutive turns into size-bounded time windows.

    Turns are blank-line-separated blocks (a common message representation);
    when the content is a flat transcript with no blank lines, each non-empty line
    is a turn. Consecutive turns are packed into bounded windows — chronological
    adjacency is preserved, so each chunk is a contiguous slice of the thread. No
    overlap (turns are not duplicated across windows). Richer thread/time
    boundaries supplied by producers (Sprint 2) can refine this later.
    """
    blocks = _paragraphs(text)
    if len(blocks) <= 1:
        blocks = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return _pack_units(blocks, "\n", 0)


def _split_code(text: str) -> list[str]:
    """Code: split on file / function boundaries where possible.

    Groups lines into blocks that each start at a detected function/class
    boundary (leading lines before the first boundary — imports/module header —
    form their own block). Content with no detectable boundary falls back to a
    bounded window split. No overlap (a function is not duplicated across chunks).
    An oversized single function is window-split by ``_pack_units``.
    """
    lines = text.splitlines()
    if not any(_CODE_BOUNDARY_RE.match(ln) for ln in lines):
        return _window_split(text, 0)

    blocks: list[str] = []
    current: list[str] = []
    for ln in lines:
        if _CODE_BOUNDARY_RE.match(ln) and current:
            blocks.append("\n".join(current))
            current = [ln]
        else:
            current.append(ln)
    if current:
        blocks.append("\n".join(current))

    blocks = [b for b in blocks if b.strip()]
    return _pack_units(blocks, "\n", 0)


_SPLITTERS: dict[str, Callable[[str], list[str]]] = {
    "prose": _split_prose,
    "conversation": _split_conversation,
    "code": _split_code,
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _paragraphs(text: str) -> list[str]:
    """Split into non-empty, stripped, blank-line-separated paragraphs."""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _is_heading(paragraph: str) -> bool:
    """True if a paragraph is a section heading (attached to the following body)."""
    if "\n" in paragraph:
        return False
    if _ATX_HEADING_RE.match(paragraph):
        return True
    # A short single line with no terminal sentence punctuation reads as a title.
    return len(paragraph) <= 80 and not paragraph.rstrip().endswith(
        (".", "!", "?", ":", ";", ",")
    )


def _prose_units(text: str) -> list[str]:
    """Paragraph units with headings attached to the paragraph they introduce.

    Keeps a section's title in the same unit as its opening body so a chunk is
    self-describing. Consecutive headings accumulate onto the next body paragraph;
    a trailing heading with no body becomes its own unit.
    """
    units: list[str] = []
    pending: list[str] = []
    for para in _paragraphs(text):
        if _is_heading(para):
            pending.append(para)
            continue
        if pending:
            para = "\n".join(pending + [para])
            pending = []
        units.append(para)
    if pending:
        units.append("\n".join(pending))
    return units


def _pack_units(units: list[str], joiner: str, overlap_chars: int) -> list[str]:
    """Greedily pack units into chunks bounded by ``MAX_CHUNK_CHARS``.

    Units are combined in source order until the next unit would overflow the
    bound, then a chunk is emitted. For prose (``overlap_chars`` > 0) the new chunk
    starts with a tail of the previous chunk's units so context overlaps the
    boundary. A single unit larger than the bound is hard-split with
    ``_window_split`` (carrying the same overlap).
    """
    chunks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            chunks.append(joiner.join(current))

    for unit in units:
        if not unit:
            continue
        if len(unit) > MAX_CHUNK_CHARS:
            flush()
            current.clear()
            chunks.extend(_window_split(unit, overlap_chars))
            continue

        tentative = joiner.join(current + [unit]) if current else unit
        if current and len(tentative) > MAX_CHUNK_CHARS:
            flush()
            carry = _overlap_units(current, overlap_chars, joiner)
            # Never let the carried overlap plus the new unit exceed the bound.
            if carry and len(joiner.join(carry + [unit])) > MAX_CHUNK_CHARS:
                carry = []
            current = carry + [unit]
        else:
            current.append(unit)

    flush()
    return [c for c in chunks if c.strip()]


def _overlap_units(units: list[str], overlap_chars: int, joiner: str) -> list[str]:
    """Return the trailing units whose combined length is within ``overlap_chars``.

    At least the final unit is always carried when overlap is requested, so
    consecutive chunks share a boundary of context (bounded above by
    ``MAX_CHUNK_CHARS`` since every unit is).
    """
    if overlap_chars <= 0:
        return []
    tail: list[str] = []
    total = 0
    for unit in reversed(units):
        add = len(unit) + (len(joiner) if tail else 0)
        if tail and total + add > overlap_chars:
            break
        tail.insert(0, unit)
        total += add
    return tail


def _window_split(text: str, overlap_chars: int) -> list[str]:
    """Hard-split text into ``MAX_CHUNK_CHARS`` windows (overlapping if requested).

    The last-resort splitter for a single unit with no usable internal structure
    (an oversized paragraph/function, or code with no detectable boundaries).
    ``overlap_chars`` = 0 produces non-overlapping windows.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    step = max(1, MAX_CHUNK_CHARS - overlap_chars)
    return [text[i : i + MAX_CHUNK_CHARS] for i in range(0, len(text), step)]
