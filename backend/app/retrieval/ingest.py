"""Producer contract — ``ingest_content(org_id, artifacts)`` (R18-B1 T5).

The ONE standard path by which content producers feed the retrieval substrate
(Section 3). Every present and future producer — R18-A1 document ingestion,
R18-A2 Git content, and the Sprint-2 deep-content stories (Slack/Teams
conversations, Confluence/SharePoint pages and documents) — hands extracted text
plus provenance to this entry point. Without this contract every ingestion story
would grow its own slightly different vector-writing path, and retrieval would be
impossible to trust.

The division of responsibility is absolute:

* **Producers provide** extracted clean text and source information: which source
  system, which artifact within it, what kind of content it is, when it changed,
  and any extra provenance metadata.
* **Producers never** decide chunk sizes, create embeddings, or write vectors —
  the :class:`ContentArtifact` shape has no field for any of those, and dict
  payloads carrying unknown fields (``embedding``, ``chunk_size``, …) are
  rejected outright.
* **The substrate owns** everything after handover: chunking per content-type
  policy (T2), content hashing, asynchronous embedding through the model gateway
  ONLY (T3, driven by ``jobs/embedding_worker.py``), indexing, and metadata
  storage in the org-partitioned pgvector store (T1). That is AC1 end to end.

Ingestion itself makes NO model call: chunks are written with ``embedding IS
NULL`` and picked up by the async embedding worker, so ingestion is fast, works
without a reachable embedding provider, and embedding lag never blocks anything
(AC7). This module therefore imports neither the model gateway nor the embedder.

Re-ingest semantics: handing over an artifact REPLACES that artifact's previous
chunks (``store.delete_by_artifact`` then write) — producers re-send changed
content and the substrate keeps the index consistent. Validation and chunking run
BEFORE the delete, so a rejected artifact never destroys its previously indexed
content. Hash-based change detection / re-embed avoidance is the freshness
story's concern (R18-B2), built on the content hash stamped here.

Failure isolation matches the substrate's operational posture: one bad artifact
in a batch is recorded as failed in the returned :class:`IngestResult` and never
sinks the rest of the batch. Only a malformed CALL (blank ``org_id``,
non-sequence ``artifacts``) raises. Artifact content is never logged — only
source identifiers and counts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, Iterable, List, Optional, Union

from app.retrieval import chunking, store
from database.models.retrieval import (
    CONTENT_TYPES,
    KNOWN_SOURCE_SYSTEMS,
    RetrievalChunkRecord,
)

logger = logging.getLogger(__name__)

# What a producer is allowed to say about the substrate's post-handover flow:
# nothing. Named here so the rejection message can teach the contract.
_SUBSTRATE_OWNED = (
    "chunking, hashing, embedding, indexing, and vector writes are owned by the "
    "retrieval substrate"
)


# ---------------------------------------------------------------------------
# The producer payload
# ---------------------------------------------------------------------------


@dataclass
class ContentArtifact:
    """One unit of producer-extracted content plus its provenance.

    This is the ENTIRE producer vocabulary (Section 3): text and source
    information. There is deliberately no field for chunk size, embedding,
    vector, or model — those decisions belong to the substrate (T2/T3/T1), and
    :meth:`from_dict` rejects any attempt to smuggle them in.

    ``source_system``    producing system, e.g. ``'document'``, ``'git'``,
                         ``'slack'``, ``'teams'``, ``'confluence'``,
                         ``'sharepoint'``. Unknown systems are accepted (new
                         producers must not need a code change here) but logged.
    ``source_artifact``  the artifact id within that system — page id, thread
                         id, file path.
    ``content``          the extracted clean text. Empty text is a valid
                         handover meaning "this artifact now has no content".
    ``content_type``     which chunking policy applies: ``'prose'``,
                         ``'conversation'``, or ``'code'`` (T2).
    ``source_timestamp`` when the source content was created/last changed —
                         ISO-8601 string or ``datetime``.
    ``provenance``       optional extra provenance metadata (URL, author,
                         space/repo, …), stored verbatim with every chunk.
    """

    source_system: str
    source_artifact: str
    content: Optional[str]
    content_type: str
    source_timestamp: Optional[Union[str, datetime]] = None
    provenance: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        for fname in ("source_system", "source_artifact", "content_type"):
            val = getattr(self, fname)
            if val is None or not str(val).strip():
                raise ValueError(f"{fname} is required")
        if self.content_type not in CONTENT_TYPES:
            raise ValueError(
                f"content_type must be one of {sorted(CONTENT_TYPES)}; "
                f"got {self.content_type!r}"
            )
        if self.content is not None and not isinstance(self.content, str):
            raise ValueError("content must be extracted text (str)")
        if self.provenance is not None and not isinstance(self.provenance, dict):
            raise ValueError("provenance must be a dict of metadata")
        if self.source_system not in KNOWN_SOURCE_SYSTEMS:
            # New producers are allowed without a code change — visible, not fatal.
            logger.debug(
                "ingest_content: unrecognised source_system %r (accepted)",
                self.source_system,
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContentArtifact":
        """Build an artifact from a plain dict, enforcing the contract strictly.

        Unknown keys are rejected rather than ignored: a producer passing
        ``embedding``, ``vector``, ``chunk_size`` (or anything else outside the
        producer vocabulary) is making a contract error that must surface loudly,
        not be silently dropped and half-work.
        """
        allowed = {f.name for f in fields(cls)}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(
                f"unknown artifact field(s) {sorted(unknown)}: producers supply "
                f"extracted text + provenance only — {_SUBSTRATE_OWNED} (R18-B1 T5)"
            )
        return cls(**data)


# ---------------------------------------------------------------------------
# The handover result
# ---------------------------------------------------------------------------


@dataclass
class ArtifactIngestResult:
    """Outcome for one handed-over artifact.

    ``status`` is ``'indexed'`` (chunks written, pending async embedding),
    ``'empty'`` (no content — any previous chunks for the artifact were
    removed), or ``'failed'`` (rejected/errored; the store was not touched for
    this artifact). ``chunks_replaced`` counts the artifact's previous chunks
    removed by the re-ingest path.
    """

    source_system: str
    source_artifact: str
    status: str
    chunks_indexed: int = 0
    chunks_replaced: int = 0
    error: Optional[str] = None


@dataclass
class IngestResult:
    """Aggregate outcome of one ``ingest_content`` call (plus per-artifact detail).

    Every chunk counted in ``chunks_indexed`` was written WITHOUT a vector and
    is pending asynchronous embedding (AC7) — producers get an accounting of the
    handover, never a promise about embedding latency.
    """

    org_id: str
    artifacts_received: int = 0
    artifacts_indexed: int = 0
    artifacts_empty: int = 0
    artifacts_failed: int = 0
    chunks_indexed: int = 0
    chunks_replaced: int = 0
    artifacts: List[ArtifactIngestResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def ingest_content(
    org_id: str,
    artifacts: Iterable[Union[ContentArtifact, dict]],
) -> IngestResult:
    """Hand producer-extracted content to the retrieval substrate (R18-B1 T5).

    The single standard entry point for ALL content producers (Section 3). Each
    artifact is chunked per its content-type policy, stamped with full provenance
    metadata and a content hash, and indexed into the org-partitioned store
    (AC1). Embedding happens asynchronously afterwards, through the model gateway
    only — this call never embeds, so it never blocks on a model provider (AC7).

    ``org_id``     the owning org — every chunk is written into this org's
                   partition and no other (AC3).
    ``artifacts``  a sequence of :class:`ContentArtifact` (or equivalent dicts,
                   validated strictly by :meth:`ContentArtifact.from_dict`).

    Re-handing an artifact the substrate has seen before replaces its previous
    chunks. Two artifacts with the same ``(source_system, source_artifact)`` in
    one call: the later one wins.

    Per-artifact failures are isolated — recorded on the returned
    :class:`IngestResult`, never raised, and never touching the store for the
    failed artifact. Raises ``ValueError`` only for a malformed call: blank
    ``org_id``, or ``artifacts`` that is not a sequence of artifacts (a str,
    bytes, a bare dict, or a non-iterable).
    """
    if org_id is None or not str(org_id).strip():
        raise ValueError("org_id is required")
    if artifacts is None or isinstance(artifacts, (str, bytes, dict, ContentArtifact)):
        raise ValueError(
            "artifacts must be a sequence of ContentArtifact (or equivalent dicts)"
        )
    try:
        items = list(artifacts)
    except TypeError as exc:
        raise ValueError(
            "artifacts must be a sequence of ContentArtifact (or equivalent dicts)"
        ) from exc

    result = IngestResult(org_id=org_id, artifacts_received=len(items))
    for item in items:
        outcome = _ingest_one(org_id, item)
        result.artifacts.append(outcome)
        result.chunks_indexed += outcome.chunks_indexed
        result.chunks_replaced += outcome.chunks_replaced
        if outcome.status == "indexed":
            result.artifacts_indexed += 1
        elif outcome.status == "empty":
            result.artifacts_empty += 1
        else:
            result.artifacts_failed += 1

    logger.info(
        "ingest_content: org=%s artifacts=%d indexed=%d empty=%d failed=%d "
        "chunks_indexed=%d chunks_replaced=%d (embedding is async)",
        org_id,
        result.artifacts_received,
        result.artifacts_indexed,
        result.artifacts_empty,
        result.artifacts_failed,
        result.chunks_indexed,
        result.chunks_replaced,
    )
    return result


# ---------------------------------------------------------------------------
# Per-artifact flow: validate -> chunk -> (replace) -> index. Never embeds.
# ---------------------------------------------------------------------------


def _ingest_one(
    org_id: str, item: Union[ContentArtifact, dict]
) -> ArtifactIngestResult:
    """Ingest a single artifact, isolating every failure to this artifact.

    Ordering is load-bearing: validation, chunking, and record construction all
    complete BEFORE the artifact's previous chunks are deleted, so a rejected or
    unchunkable handover leaves previously indexed content fully intact.
    """
    # Best-effort identifiers for the failure record, before validation.
    src_system = _peek(item, "source_system")
    src_artifact = _peek(item, "source_artifact")

    try:
        artifact = _normalise(item)
        src_system, src_artifact = artifact.source_system, artifact.source_artifact
        records = build_records(org_id, artifact)

        # Replace semantics (re-ingest path): only reached once this handover is
        # fully validated and built.
        replaced = store.delete_by_artifact(
            org_id, artifact.source_system, artifact.source_artifact
        )
        if not records:
            return ArtifactIngestResult(
                source_system=src_system,
                source_artifact=src_artifact,
                status="empty",
                chunks_replaced=replaced,
            )
        written = store.upsert_chunks(records)
        return ArtifactIngestResult(
            source_system=src_system,
            source_artifact=src_artifact,
            status="indexed",
            chunks_indexed=written,
            chunks_replaced=replaced,
        )
    except Exception as exc:  # noqa: BLE001 — one bad artifact never sinks a batch
        logger.warning(
            "ingest_content: artifact rejected (org=%s source_system=%r "
            "source_artifact=%r): %s",
            org_id,
            src_system,
            src_artifact,
            exc,
        )
        return ArtifactIngestResult(
            source_system=src_system or "",
            source_artifact=src_artifact or "",
            status="failed",
            error=str(exc),
        )


def _normalise(item: Union[ContentArtifact, dict]) -> ContentArtifact:
    """Coerce a handover item to a validated :class:`ContentArtifact`."""
    if isinstance(item, ContentArtifact):
        return item
    if isinstance(item, dict):
        return ContentArtifact.from_dict(item)
    raise ValueError(
        f"artifact must be a ContentArtifact or dict, got {type(item).__name__}"
    )


def build_records(org_id: str, artifact: ContentArtifact) -> list[RetrievalChunkRecord]:
    """Chunk one artifact per its content-type policy and build store records.

    The substrate-owned part of AC1: T2 chunking decides the units (and stamps
    position + provenance), the record derives the content hash, and every
    record is built WITHOUT an embedding — the async pipeline (T3) stamps
    vectors later. No caller input reaches the hash or the chunk boundaries.

    Public because the freshness refresh worker (R18-B2 T3) MUST re-chunk changed
    content through this exact path: hash-comparing re-extracted chunks against the
    stored ones only works if both go through the identical chunking + hashing
    logic. Sharing this one builder is what keeps ingest-time and refresh-time
    chunk boundaries (and therefore content hashes) in lock-step.
    """
    ts_text = _timestamp_text(artifact.source_timestamp)
    chunks = chunking.chunk_content(
        org_id=org_id,
        content=artifact.content or "",
        content_type=artifact.content_type,
        source_system=artifact.source_system,
        source_artifact=artifact.source_artifact,
        source_timestamp=ts_text,
        provenance=artifact.provenance,
    )
    ts_value = _timestamp_value(artifact.source_timestamp)
    return [
        RetrievalChunkRecord(
            org_id=chunk.org_id,
            content=chunk.content,
            content_type=chunk.content_type,
            source_system=chunk.source_system,
            source_artifact=chunk.source_artifact,
            chunk_position=chunk.position,
            source_timestamp=ts_value,
            provenance=chunk.provenance,
        )
        for chunk in chunks
    ]


def _peek(item: Any, key: str) -> str:
    """Best-effort read of an identifier off an unvalidated handover item."""
    if isinstance(item, ContentArtifact):
        return str(getattr(item, key, "") or "")
    if isinstance(item, dict):
        return str(item.get(key, "") or "")
    return ""


def _timestamp_text(value: Optional[Union[str, datetime]]) -> Optional[str]:
    """The string form of the source timestamp (what chunk metadata carries)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _timestamp_value(value: Optional[Union[str, datetime]]) -> Optional[datetime]:
    """The ``datetime`` form of the source timestamp (what the store persists).

    ISO-8601 strings are parsed (a trailing ``Z`` is accepted). An unparseable
    timestamp degrades to ``None`` with a warning — bad producer metadata must
    not block the content itself from being indexed.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(
            "ingest_content: unparseable source_timestamp %r (stored without one)",
            text,
        )
        return None
