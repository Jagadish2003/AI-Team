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
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.provenance import EvidencePointer, utc_now_iso
from app.retrieval.ingest import ContentArtifact, IngestResult, ingest_content, remove_content

from .base import ChangeKind
from .confluence import ConfluenceIngestor
from .content_router import ContentRoute, classify_confluence_content

logger = logging.getLogger(__name__)

#: The substrate's producer name for Confluence page content (in
#: ``database.models.retrieval.KNOWN_SOURCE_SYSTEMS``).
RETRIEVAL_SOURCE_SYSTEM = "confluence"

#: Page/blogpost bodies are prose — chunked on heading/paragraph structure (R18-B1 T2).
CONTENT_TYPE = "prose"

IngestFn = Callable[[str, List[ContentArtifact]], IngestResult]
#: The substrate's freshness-removal entry point, injectable for tests (R18-A5
#: / AT-603, T4) — mirrors ``git_content.py``'s AT-533 ``_remove_fn`` seam.
RemoveFn = Callable[[str, List[tuple]], Any]


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
        self._in_li = False

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
            self._in_li = True
            self._buf.append("- ")
            return
        if tag in ("td", "th"):
            if self._buf:
                self._buf.append(" | ")
            return
        if tag in self._BLOCK_TAGS:
            if tag == "p" and self._in_li:
                return
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TEXT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "li":
            self._flush()
            self._in_li = False
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
        missing = []
        if not space_key:
            missing.append("space_key")
        if not content_id:
            missing.append("content_id")
        logger.warning(
            "confluence_content: skipping page artifact with missing identity "
            "field(s) %s (org=%s artifact_id=%s)",
            missing,
            org_id,
            record.get("artifact_id"),
        )
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
        # The page's own Confluence id, declared explicitly. MSP-B5's runbook
        # library builds deterministic match keys from _PROVENANCE_ID_KEYS, which
        # includes 'page_id'; without it the only id-shaped key a Confluence page
        # offered was 'url', so a resolution note citing a bare page id could never
        # resolve and fell through to the semantic path. This is the id Confluence
        # already gave us — nothing is inferred from the title here.
        "page_id": content_id,
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


def accessible_space_keys(ingestor: ConfluenceIngestor, org_id: str) -> Optional[set]:
    """The CURRENT granted, non-archived space keys — or ``None`` if unresolvable.

    R18-A5 / AT-604 (T5, AC4): the depth path RE-VERIFIES the reach-phase grant
    boundary against this set before fetching any page body. Reading page bodies
    is more sensitive than reading activity, so the boundary is re-checked at
    depth rather than assumed inherited from the reach phase's earlier filter.
    Reuses the single source of truth — :meth:`ConfluenceIngestor._accessible_spaces`
    (the same ``is_accessible`` + non-archived gate the reach phase applies).

    Returns ``None`` (not an empty set) when the granted set cannot be resolved —
    e.g. a transient spaces-listing error — so the caller falls back to the
    reach-phase boundary that already gated the records, rather than blocking
    legitimate content. An empty set (nothing granted) is distinct and DOES refuse
    all content.
    """
    try:
        return {
            s.get("key")
            for s in ingestor._accessible_spaces(org_id)
            if s.get("key")
        }
    except Exception:  # noqa: BLE001 — never block content on a grant-listing error
        logger.warning(
            "confluence_content: could not resolve granted spaces for org=%s; "
            "falling back to the reach-phase boundary",
            org_id,
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Hand-off
# ---------------------------------------------------------------------------


@dataclass
class DeepContentResult:
    """Outcome of one :func:`ingest_confluence_content` call."""

    org_id: str
    pages_seen: int = 0
    # R18-A5 / AT-604 (T5, AC4) — page(s) refused at depth because their space is
    # not in the CURRENT granted/non-archived set (body never fetched).
    pages_ungranted_skipped: int = 0
    pages_identity_missing: int = 0
    pages_render_failed: int = 0
    artifacts_handed_off: int = 0
    artifacts_indexed: int = 0
    artifacts_empty: int = 0
    artifacts_failed: int = 0
    chunks_indexed: int = 0
    chunks_replaced: int = 0
    # R18-A5 / AT-603 (T4) — deletion/archival propagation.
    deletions_seen: int = 0
    artifacts_removed: int = 0
    artifacts_absent: int = 0
    artifacts_removal_failed: int = 0
    chunks_removed: int = 0
    #: ServiceNow/Jira/GitHub markers found in page BODIES (COR-11's supply).
    #: The reach path extracts these from page TITLES only, which misses the common
    #: case by a wide margin: a postmortem cites "INC-4821" in its text, not in its
    #: heading. Collected here because this is the only path that holds the rendered
    #: body. The rule that reads them still requires an exact id match against a
    #: record already linked to the detector, so a passing mention corroborates
    #: nothing on its own.
    cross_references: List[Dict[str, Any]] = field(default_factory=list)


def extract_body_cross_references(
    artifacts: List[ContentArtifact],
) -> List[Dict[str, Any]]:
    """Pull ServiceNow/Jira/GitHub markers out of rendered page text.

    Uses the SAME source-agnostic extractor the reach paths use
    (``slack_signals.extract_cross_reference_markers``) so a reference means the
    same thing wherever it was found. Deduplicated on ``(system, ref)``.
    """
    from .slack_signals import extract_cross_reference_markers

    seen = set()
    out: List[Dict[str, Any]] = []
    for artifact in artifacts or []:
        content = getattr(artifact, "content", "") or ""
        if not content:
            continue
        try:
            markers = extract_cross_reference_markers(content)
        except Exception:  # noqa: BLE001 — a marker scan never sinks ingestion
            continue
        for marker in markers or ():
            key = (marker.get("system"), str(marker.get("ref", "")).upper())
            if key in seen:
                continue
            seen.add(key)
            out.append(marker)
    return out


def ingest_confluence_content(
    org_id: str,
    records: List[Dict[str, Any]],
    *,
    ingestor: Optional[ConfluenceIngestor] = None,
    ingest_fn: IngestFn = ingest_content,
    remove_fn: RemoveFn = remove_content,
) -> DeepContentResult:
    """Render + hand off page/blogpost bodies for one run's CHANGED records.

    ``records`` is the same changed-record set the reach-phase corroboration path
    already collected off the ConfluenceIngestor's checkpoint (R17-A2) — this
    function rides that delta rather than driving a second checkpointed pass
    (see module docstring). Scope is decided by the page-vs-file router
    (AT-602 / T3, ``content_router.classify_confluence_content``): only records
    it classifies PAGE_CONTENT are considered — comments and attachment-shaped
    records are ignored here exactly as they are on the reach phase. The
    granted-space boundary (AC4) is RE-VERIFIED here at depth, not assumed
    inherited (R18-A5 / AT-604, T5): before any page body is fetched, each
    record's ``space_key`` is re-checked against the CURRENT accessible set
    (:func:`accessible_space_keys` → :meth:`ConfluenceIngestor._accessible_spaces`)
    and any ungranted/archived space is refused with its body never fetched —
    reading a page body is more sensitive than reading activity, so the boundary
    is re-checked rather than trusted from the reach-phase filter alone. (In the
    normal flow the reach phase already excluded those spaces, so this is
    defence-in-depth; it becomes load-bearing if a record for an ungranted space
    is ever handed in directly.)

    Deletion / archival (R18-A5 / AT-603, T4, AC3): records with
    ``change_kind='deleted'`` (:mod:`confluence.py`'s known-id diff — see its
    module docstring) carry no body — the router already excludes them from
    ``scoped``/rendering — and are instead routed straight to
    ``retrieval.ingest.remove_content`` in THIS SAME call, synchronously. This is
    belt-and-braces on top of the standard ``ingestion.artifact_changed`` ->
    retrieval-freshness event pipeline (which ALSO purges the same artifact,
    idempotently, off the ``change_runner`` emission the reach phase already
    drives) — deletion does not depend solely on that fire-and-forget path
    (mirrors ``git_content.py``'s AT-533 pattern).

    Never raises: a body-fetch/render failure is isolated per page, and a
    substrate hand-off (or removal) failure is isolated per artifact by
    ``ingest_content``/``remove_content`` themselves (or caught here
    defensively) — deep content is additive and must never block the
    reach-phase corroboration signal that already rides this checkpoint.
    """
    result = DeepContentResult(org_id=org_id)
    ing = ingestor if ingestor is not None else ConfluenceIngestor()
    valid_records = [r for r in (records or []) if isinstance(r, dict)]

    deletions = [r for r in valid_records if r.get("change_kind") == ChangeKind.DELETED]
    result.deletions_seen = len(deletions)
    if deletions:
        removals = [("confluence", r.get("artifact_id")) for r in deletions if r.get("artifact_id")]
        try:
            removal = remove_fn(org_id, removals)
        except Exception:  # noqa: BLE001 — deletion must never block the run
            logger.warning(
                "confluence_content: retrieval removal failed for org=%s "
                "(non-blocking)",
                org_id,
                exc_info=True,
            )
        else:
            result.artifacts_removed = removal.artifacts_removed
            result.artifacts_absent = removal.artifacts_absent
            result.artifacts_removal_failed = removal.artifacts_failed
            result.chunks_removed = removal.chunks_removed
            logger.info(
                "confluence_content: org=%s removed=%d absent=%d failed=%d "
                "chunks_removed=%d",
                org_id,
                result.artifacts_removed,
                result.artifacts_absent,
                result.artifacts_removal_failed,
                result.chunks_removed,
            )

    scoped = [
        r for r in valid_records
        if classify_confluence_content(r.get("content_type")) == ContentRoute.PAGE_CONTENT
    ]

    identity_ready = []
    for r in scoped:
        if r.get("space_key") and r.get("content_id"):
            identity_ready.append(r)
        else:
            result.pages_identity_missing += 1
            missing = []
            if not r.get("space_key"):
                missing.append("space_key")
            if not r.get("content_id"):
                missing.append("content_id")
            logger.warning(
                "confluence_content: skipping page at depth with missing identity "
                "field(s) %s (org=%s artifact_id=%s)",
                missing,
                org_id,
                r.get("artifact_id"),
            )
    scoped = identity_ready

    # AC4 — re-verify the granted-space boundary AT DEPTH, before any body fetch.
    # We do not merely trust that each record came from a granted space (the
    # reach-phase filter); a page body is more sensitive than page activity, so we
    # re-check every record's space against the CURRENT accessible set and refuse
    # any that is not granted/non-archived — its body is never fetched. This
    # mirrors the SharePoint content path's own ``_accessible_sites`` gate.
    granted = accessible_space_keys(ing, org_id)
    if granted is not None:
        in_scope = [r for r in scoped if r.get("space_key") in granted]
        refused = [r for r in scoped if r.get("space_key") not in granted]
        if refused:
            result.pages_ungranted_skipped = len(refused)
            logger.warning(
                "confluence_content: refused %d page(s) at depth in ungranted/"
                "archived space(s) %s (org=%s) — body never fetched (AC4)",
                len(refused),
                sorted({str(r.get("space_key")) for r in refused}),
                org_id,
            )
        scoped = in_scope

    result.pages_seen = len(scoped)
    if not scoped:
        return result

    artifacts = content_artifacts(ing, org_id, scoped)
    result.pages_render_failed = result.pages_seen - len(artifacts)
    # COR-11's supply: markers in the page BODY, which only this path can see.
    result.cross_references = extract_body_cross_references(artifacts)
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
