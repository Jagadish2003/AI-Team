"""Chunking policy — per-content-type retrieval units (R18-B1).

Splits source content into retrieval units. Policy is per content type
(Section 1):

  * prose (documents, pages) splits on structure — headings / paragraphs — with
    overlap;
  * conversations split on thread / time windows;
  * code splits on file / function boundaries.

Chunk size is bounded so any retrieved set fits assembly budgets. Every chunk
records its source artifact id, org_id, content-type, position, and a content
hash (the hash is what freshness — R18-B2 — uses to detect change).

Scope (T1): this module defines the chunk contract (the ``Chunk`` dataclass, the
``chunk_content()`` dispatch entry point, and the content-hash helper) so the
package skeleton is complete and importable. The real per-content-type splitting
policies are implemented in T2; the dispatcher currently routes every type
through a single conservative paragraph/window splitter and is marked accordingly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Re-exported so callers hash content through one definition shared with the
# storage schema (freshness parity, R18-B2).
from database.models.retrieval import CONTENT_TYPES, compute_content_hash

# Upper bound on a chunk's character length. Bounded so any retrieved set fits the
# assembler's budgets (Section 1). The precise per-type sizing is tuned in T2.
MAX_CHUNK_CHARS = 2000
# Overlap between adjacent prose chunks so a boundary never splits a fact in half.
CHUNK_OVERLAP_CHARS = 200


@dataclass
class Chunk:
    """A single retrieval unit produced from a source artifact.

    Carries exactly the metadata Section 1 requires on every chunk: org_id,
    content type, position within the artifact, and the content hash, plus the
    source-system / source-artifact provenance and an optional source timestamp.
    ``to_record_kwargs()`` hands these straight to a
    ``RetrievalChunkRecord``/``store.upsert_chunks()`` without re-deriving
    anything.
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

    T1 provides the dispatch surface and a single conservative splitter so the
    substrate is end-to-end wireable. T2 replaces ``_split_default`` with the
    per-content-type policies (prose on structure with overlap; conversations on
    thread/time windows; code on file/function boundaries).
    """
    if content_type not in CONTENT_TYPES:
        raise ValueError(f"content_type must be one of {sorted(CONTENT_TYPES)}")

    # TODO(R18-B1 T2): dispatch to prose / conversation / code specific policies.
    pieces = _split_default(content)
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


def _split_default(content: str) -> list[str]:
    """Conservative bounded splitter with overlap (placeholder for T2 policies).

    Splits on a fixed character window with overlap so no chunk exceeds
    ``MAX_CHUNK_CHARS``. Deterministic and structure-agnostic — good enough to
    wire the pipeline in T1, replaced by structure-aware policies in T2.
    """
    text = content.strip()
    if not text:
        return []
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]

    step = max(1, MAX_CHUNK_CHARS - CHUNK_OVERLAP_CHARS)
    return [text[i : i + MAX_CHUNK_CHARS] for i in range(0, len(text), step)]
