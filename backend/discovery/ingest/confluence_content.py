"""
R18-A5 / AT-600 (T1) — Confluence page/blogpost DEEP CONTENT path.

Extends the R17-A2 :class:`~discovery.ingest.confluence.ConfluenceIngestor`
(``confluence.py``) with a content path: render page/blogpost bodies to
structure-preserving text and hand them to
``retrieval.ingest_content(org_id, artifacts)`` (the R18-B1 producer contract).

Reach vs depth (unchanged boundary)
------------------------------------
``confluence.py``'s reach-phase listing (``content_for_space``) deliberately
never expands ``body.*`` — that boundary is untouched by this module. This file
is the ONE place that crosses it: :meth:`ConfluenceIngestor._raw_page_body`
(added alongside, offline fixture / live ``ConfluenceClient.page_body``) fetches
a single page's body + labels, and only for a page already known changed.

No second checkpoint, no second space scan
-------------------------------------------
This module does not drive its own ``change_runner.ingest_with_checkpoint`` pass.
It rides the SAME changed-record set the reach-phase corroboration path already
collects off the ConfluenceIngestor's ``(org_id, 'confluence')`` checkpoint
(``discovery/runner.py::_ingest_confluence_corroboration``) — a second pass over
the same connector_id would race the checkpoint the reach phase already owns.
:func:`ingest_confluence_content` is called with that batch's records directly
(mirrors the story's own pseudocode: ``_ingest_deep_content(org_id, changed_pages)``).

Only PAGE-NATIVE content is in scope here — attachments/files on a page route
to the document path (``documents_attachments.py`` / R18-A1), which lists
attachment ids, an entirely disjoint identifier space from the page ids this
module writes under (so no double-ingestion is structurally possible between
the two paths).

Never blocks reach-phase corroboration
---------------------------------------
Deep content is additive: a body-fetch/render failure is isolated per page (this
module never raises out of :func:`ingest_confluence_content`), and a substrate
hand-off failure is isolated per artifact by ``ingest_content`` itself. The
caller (``runner.py``) treats the whole Confluence path as non-blocking already;
this module upholds that same posture for its own depth work.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.provenance import EvidencePointer, utc_now_iso
from app.retrieval.ingest import ContentArtifact, IngestResult, ingest_content

from .confluence import ConfluenceIngestor
from .content_router import ContentRoute, classify_confluence_content

logger = logging.getLogger(__name__)

#: The substrate's producer name for Confluence page content (in
#: ``database.models.retrieval.KNOWN_SOURCE_SYSTEMS``).
RETRIEVAL_SOURCE_SYSTEM = "confluence"

#: Page/blogpost bodies are prose — chunked on heading/paragraph structure (R18-B1 T2).
CONTENT_TYPE = "prose"

IngestFn = Callable[[str, List[ContentArtifact]], IngestResult]


# ---------------------------------------------------------------------------
# T1: structure-preserving storage-format -> text rendering
# ---------------------------------------------------------------------------


class _StorageTextRenderer(HTMLParser):
    """Render Confluence storage-format XHTML to clean, structure-preserving text.

    Headings (``h1``-``h6``) become ATX-style ``#``/``##``/... prefixed lines so
    the substrate's prose chunker (``app.retrieval.chunking._is_heading``) detects
    them as section boundaries — this is what "chunked on headings" (AC1) actually
    rides on downstream; no special-casing is needed in the chunker. List items
    become ``"- "``-prefixed lines; table cells are joined with ``" | "``. Confluence
    macro chrome (``ac:parameter`` — layout/config, not body content) is skipped;
    macro BODY content (``ac:rich-text-body``, ``ac:plain-text-body`` — including a
    literal ``<![CDATA[...]]>`` plain-text macro body) is kept, since that is the
    macro's actual visible content (an info panel's text, a code snippet's body).
    """

    _HEADING_LEVELS: Dict[str, int] = {f"h{i}": i for i in range(1, 7)}
    #: Macro parameters are structured config (e.g. a panel's title/layout key),
    #: not body text — their text is dropped so it never pollutes the rendered page.
    _SKIP_TEXT_TAGS = {"ac:parameter"}
    #: Tags that start a new text unit (flushed as their own block/heading).
    _BLOCK_TAGS = {
        "p", "div", "tr", "table", "ul", "ol", "blockquote",
        "ac:rich-text-body", "ac:structured-macro",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[str] = []
        self.headings: List[Dict[str, Any]] = []
        self._buf: List[str] = []
        self._heading_level: Optional[int] = None
        self._skip_depth = 0

    def _flush(self) -> None:
        text = re.sub(r"[ \t]+", " ", "".join(self._buf)).strip()
        self._buf = []
        heading_level, self._heading_level = self._heading_level, None
        if not text:
            return
        if heading_level is not None:
            self.blocks.append(f"{'#' * heading_level} {text}")
            self.headings.append(
                {"level": heading_level, "text": text, "position": len(self.blocks) - 1}
            )
        else:
            self.blocks.append(text)

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001 - stdlib signature
        tag = tag.lower()
        if tag in self._SKIP_TEXT_TAGS:
            self._skip_depth += 1
            return
        if tag in self._HEADING_LEVELS:
            self._flush()
            self._heading_level = self._HEADING_LEVELS[tag]
            return
        if tag == "br":
            self._buf.append("\n")
            return
        if tag == "li":
            self._flush()
            self._buf.append("- ")
            return
        if tag in ("td", "th"):
            if self._buf:
                self._buf.append(" | ")
            return
        if tag in self._BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TEXT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in self._HEADING_LEVELS or tag in self._BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        self._buf.append(data)

    def unknown_decl(self, data: str) -> None:
        # Confluence code/plain-text macro bodies are literal <![CDATA[ ... ]]>.
        if self._skip_depth > 0 or not data.startswith("CDATA["):
            return
        text = data[len("CDATA["):]
        if text.endswith("]]"):
            text = text[:-2]
        self._buf.append(text)

    def close(self) -> None:
        super().close()
        self._flush()


def render_page_text(storage_html: Optional[str]) -> Tuple[str, List[Dict[str, Any]]]:
    """Render one page/blogpost's ``body.storage`` value to structure-preserving text.

    Returns ``(text, heading_map)`` — ``text`` is clean prose with ATX-style
    headings preserved (the substrate's prose chunker splits on exactly this),
    and ``heading_map`` is the ``[{level, text, position}]`` structure hint carried
    on the artifact's provenance (the ticket's "structure_hints"). Empty/missing
    input yields ``("", [])`` — a truthful empty page, not an error. A malformed
    fragment degrades to tag-stripped plain text rather than raising, so one
    unparseable page never blocks the batch.
    """
    if not storage_html or not str(storage_html).strip():
        return "", []
    renderer = _StorageTextRenderer()
    try:
        renderer.feed(storage_html)
        renderer.close()
    except Exception:  # noqa: BLE001 — a malformed fragment must degrade, not raise
        logger.warning(
            "confluence_content: storage-format render failed; falling back to "
            "tag-stripped text",
            exc_info=True,
        )
        text = re.sub(r"<[^>]+>", " ", storage_html)
        text = re.sub(r"\s+", " ", text).strip()
        return text, []
    return "\n\n".join(renderer.blocks), renderer.headings


# ---------------------------------------------------------------------------
# Body + labels -> ContentArtifact
# ---------------------------------------------------------------------------


def _labels(body: Dict[str, Any]) -> List[str]:
    results = (((body.get("metadata") or {}).get("labels") or {}).get("results")) or []
    return [r["name"] for r in results if isinstance(r, dict) and r.get("name")]


def _storage_value(body: Dict[str, Any]) -> str:
    return ((body.get("body") or {}).get("storage") or {}).get("value", "") or ""


def build_content_artifact(
    ingestor: ConfluenceIngestor, org_id: str, record: Dict[str, Any]
) -> Optional[ContentArtifact]:
    """Build one page/blogpost's :class:`ContentArtifact`, or ``None`` if out of scope.

    ``record`` is one changed-content record as already shaped by
    :meth:`ConfluenceIngestor._to_record` (the reach-phase delta) — only its
    ``content_type``/``space_key``/``content_id`` identity is needed to fetch the
    body via the depth-only :meth:`ConfluenceIngestor._raw_page_body` seam.
    Scope is decided by the page-vs-file router (AT-602 / T3): only records the
    router classifies :attr:`~discovery.ingest.content_router.ContentRoute.
    PAGE_CONTENT` are handled — anything else (a comment, an attachment-shaped
    record, an unrecognised type) returns ``None``.

    Provenance carries the full R16-B1 observed spine plus space/label context and
    the heading map (AC1, AC6): a retrieved chunk's ``evidence_pointer`` /
    ``url`` resolve back to the exact source page.
    """
    if classify_confluence_content(record.get("content_type")) != ContentRoute.PAGE_CONTENT:
        return None
    space_key = record.get("space_key")
    content_id = record.get("content_id")
    if not space_key or not content_id:
        return None

    body = ingestor._raw_page_body(org_id, space_key, content_id) or {}
    text, heading_map = render_page_text(_storage_value(body))
    labels = _labels(body)

    source_artifact = f"{space_key}:{content_id}"
    source_timestamp = record.get("modified_at") or utc_now_iso()
    evidence_pointer = EvidencePointer.observed(
        source_system=RETRIEVAL_SOURCE_SYSTEM,
        source_artifact=source_artifact,
        source_timestamp=source_timestamp,
        source_artifact_type="record_id",
    ).to_dict()

    provenance: Dict[str, Any] = {
        "origin": "observed",
        "space_key": space_key,
        "space_name": record.get("space_name"),
        "content_type": record.get("content_type"),
        "title": record.get("title"),
        "labels": labels,
        "url": record.get("url"),
        "heading_map": heading_map,
        "evidence_pointer": evidence_pointer,
    }

    return ContentArtifact(
        source_system=RETRIEVAL_SOURCE_SYSTEM,
        source_artifact=source_artifact,
        content=text,
        content_type=CONTENT_TYPE,
        source_timestamp=source_timestamp,
        provenance=provenance,
    )


def content_artifacts(
    ingestor: ConfluenceIngestor, org_id: str, records: List[Dict[str, Any]]
) -> List[ContentArtifact]:
    """Map a batch of changed records to the artifacts worth handing to retrieval.

    One bad record (missing ids, a body-fetch/render exception) is logged and
    skipped — isolated to that page, never sinking the rest of the batch.
    """
    artifacts: List[ContentArtifact] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        try:
            artifact = build_content_artifact(ingestor, org_id, record)
        except Exception:  # noqa: BLE001 — one bad page never sinks the batch
            logger.warning(
                "confluence_content: failed to build content artifact for %s "
                "(skipping)",
                record.get("artifact_id"),
                exc_info=True,
            )
            continue
        if artifact is not None:
            artifacts.append(artifact)
    return artifacts


# ---------------------------------------------------------------------------
# Hand-off
# ---------------------------------------------------------------------------


@dataclass
class DeepContentResult:
    """Outcome of one :func:`ingest_confluence_content` call."""

    org_id: str
    pages_seen: int = 0
    pages_render_failed: int = 0
    artifacts_handed_off: int = 0
    artifacts_indexed: int = 0
    artifacts_empty: int = 0
    artifacts_failed: int = 0
    chunks_indexed: int = 0
    chunks_replaced: int = 0


def ingest_confluence_content(
    org_id: str,
    records: List[Dict[str, Any]],
    *,
    ingestor: Optional[ConfluenceIngestor] = None,
    ingest_fn: IngestFn = ingest_content,
) -> DeepContentResult:
    """Render + hand off page/blogpost bodies for one run's CHANGED records.

    ``records`` is the same changed-record set the reach-phase corroboration path
    already collected off the ConfluenceIngestor's checkpoint (R17-A2) — this
    function rides that delta rather than driving a second checkpointed pass
    (see module docstring). Scope is decided by the page-vs-file router
    (AT-602 / T3, ``content_router.classify_confluence_content``): only records
    it classifies PAGE_CONTENT are considered — comments and attachment-shaped
    records are ignored here exactly as they are on the reach phase. Records
    already reflect the granted-space boundary (AC4) — the ConfluenceIngestor's
    ``_accessible_spaces`` filter excludes ungranted/archived spaces before any
    record is ever yielded, so this depth path inherits that boundary
    automatically rather than re-implementing it.

    Never raises: a body-fetch/render failure is isolated per page, and a
    substrate hand-off failure is isolated per artifact by ``ingest_content``
    itself (or caught here defensively) — deep content is additive and must never
    block the reach-phase corroboration signal that already rides this checkpoint.
    """
    result = DeepContentResult(org_id=org_id)
    ing = ingestor if ingestor is not None else ConfluenceIngestor()
    scoped = [
        r for r in (records or [])
        if isinstance(r, dict)
        and classify_confluence_content(r.get("content_type")) == ContentRoute.PAGE_CONTENT
    ]
    result.pages_seen = len(scoped)
    if not scoped:
        return result

    artifacts = content_artifacts(ing, org_id, scoped)
    result.pages_render_failed = result.pages_seen - len(artifacts)
    if not artifacts:
        return result

    try:
        outcome = ingest_fn(org_id, artifacts)
    except Exception:  # noqa: BLE001 — deep content must never block the run
        logger.warning(
            "confluence_content: retrieval hand-off failed for org=%s (non-blocking)",
            org_id,
            exc_info=True,
        )
        return result

    result.artifacts_handed_off = len(artifacts)
    result.artifacts_indexed = outcome.artifacts_indexed
    result.artifacts_empty = outcome.artifacts_empty
    result.artifacts_failed = outcome.artifacts_failed
    result.chunks_indexed = outcome.chunks_indexed
    result.chunks_replaced = outcome.chunks_replaced
    logger.info(
        "confluence_content: org=%s pages=%d handed_off=%d indexed=%d empty=%d "
        "failed=%d chunks_indexed=%d (embedding is async)",
        org_id,
        result.pages_seen,
        result.artifacts_handed_off,
        result.artifacts_indexed,
        result.artifacts_empty,
        result.artifacts_failed,
        result.chunks_indexed,
    )
    return result
