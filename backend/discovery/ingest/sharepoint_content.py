"""
R18-A5 / AT-601 (T2) — SharePoint site-page & list-text CONTENT path via Graph.

The depth-phase counterpart to the R17-A2 reach connector (``sharepoint.py``): the
reach connector reads document-library *activity/metadata signal* (what exists, how
active, who touches it) and deliberately never reads bodies; this module reads the
PAGE-NATIVE CONTENT — modern site pages and list text — and feeds it into the
retrieval substrate so a finding can draw on and cite the institutional 'why' that
lives on those pages.

Boundary with R18-A1 (one artifact, one owner)
----------------------------------------------
SharePoint is both a page platform AND a file store. The rule (R18-A5 §desc):

  * **page-native content** — modern *site pages* (their canvas/webpart text) and
    *list* text — is THIS story, ingested here as structure-preserving prose; and
  * **binary library files** (PDF/docx/xlsx driveItems) are NOT touched here — they
    route to the R18-A1 document path (``documents_attachments.SharePointDocumentSource``
    → ``DocumentIngestor``). Site pages / lists are a *different Graph surface*
    (``/sites/{id}/pages``, ``/sites/{id}/lists``) from driveItems, so the two paths
    never overlap and never double-ingest. The page-vs-file router is AT-602 (T3);
    this module simply stays on the page-native surface.

Depth rides the reach rails (no new connector, granted-scope reused)
--------------------------------------------------------------------
This is a content path beside the reach connector's signal path — it REUSES the
R17-A2 :class:`~discovery.ingest.sharepoint.SharePointIngestor` access layer (its
Graph client, granted-site resolution, fixture plumbing, offline/live switch) rather
than re-authenticating or re-discovering. Only sites AgentIQ is granted are read:
ungranted sites (``is_accessible == False``) are filtered out exactly as in reach,
and in live mode Microsoft Graph only returns granted resources anyway — the same
least-privilege boundary, re-verified for content (R18-A5 §4 "Permissions are
re-verified at depth").

Structure-preserving text + heading-based chunking (AC1)
--------------------------------------------------------
Site-page canvas webparts carry HTML; :func:`render_page_text` renders them to clean
text that PRESERVES heading structure as Markdown ATX (``##``), and list text renders
each item's Title as a heading with its text below. The substrate's *prose* chunk
policy (``retrieval.chunking``) then splits on those headings with overlap, so a
retrieved chunk keeps its section title and reads cleanly to a human reviewing a
finding — AC1's "chunked on headings".

Provenance (AC1 / AC6, deep-linkable)
-------------------------------------
Every handed-over artifact carries the R16-B1 OBSERVED provenance spine
(``source_system='sharepoint'``, ``origin='observed'``, an ``EvidencePointer`` back
to the exact page/list) plus the page/list ``web_url`` — so a finding citing a
runbook resolves to the exact page and the UI can deep-link it.

Incremental by checkpoint (rides the reach model)
-------------------------------------------------
Position is an opaque per-artifact change-marker MAP (the page/list
``lastModifiedDateTime``), mirroring the reach connector's delta-token map. On an
incremental run only pages/lists whose marker moved past the stored one are
re-emitted — an edited page re-surfaces and ``ingest_content`` replaces its chunks;
unchanged pages are never re-read (the foundation edit/delete propagation, AT-603,
builds on). ``reports_deletes = False``: like the reach connector, deletion of a
page is deferred to the freshness story (R18-B2) rather than fabricated here.

Offline vs live
---------------
Offline (default): the deterministic ``fixtures/sharepoint_sample.json`` ``pages`` /
``lists`` sections. Live: ``SharePointGraphClient.list_site_pages`` /
``list_site_lists`` / ``list_item_fields`` (added alongside), through the reach
connector's authenticated client and per-run credential context.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

from app.provenance import EvidencePointer, utc_now_iso
from app.retrieval.ingest import ContentArtifact, IngestResult, ingest_content

from . import change_runner
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .sharepoint import (
    SharePointIngestor,
    _change_marker,
    _marker_epoch,
    _marker_gt,
)

logger = logging.getLogger(__name__)

#: The retrieval source-system tag for SharePoint content (R18-B1
#: ``KNOWN_SOURCE_SYSTEMS``). The change-based ``connector_id`` below is the
#: checkpoint key namespace — the two live in different namespaces, exactly like
#: ``git_content`` (connector) vs ``git`` (source system).
SOURCE_SYSTEM = "sharepoint"

#: Change-checkpoint key / connector id for the CONTENT path. Distinct from the
#: reach connector's ``'sharepoint'`` so the two paths keep independent checkpoints.
CONNECTOR_ID = "sharepoint_content"

#: Site pages and list text are prose — they chunk under the substrate's prose
#: policy (heading/paragraph split with overlap), which is what makes AC1's
#: "chunked on headings" work.
CONTENT_TYPE = "prose"

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default number of content artifacts emitted per :class:`DeltaBatch`.
_DEFAULT_BATCH_SIZE = 50

#: List item ``fields`` keys that are SharePoint system columns, not page-native
#: text — skipped by :func:`render_list_text` so only human-authored text is
#: rendered. ``Title`` is handled specially (rendered as the item heading).
_LIST_SYSTEM_FIELDS = frozenset(
    {
        "Title",
        "id",
        "ID",
        "ContentType",
        "Modified",
        "Created",
        "Author",
        "Editor",
        "Attachments",
        "Edit",
        "LinkTitle",
        "LinkTitleNoMenu",
        "DocIcon",
        "ItemChildCount",
        "FolderChildCount",
        "_UIVersionString",
        "AppAuthor",
        "AppEditor",
    }
)


# ---------------------------------------------------------------------------
# HTML → structure-preserving text (site-page canvas rendering)
# ---------------------------------------------------------------------------

_H_RE = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(
    r"</(p|div|ul|ol|section|table|tr|h[1-6])>", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _html_to_text(raw: Optional[str]) -> str:
    """Render a webpart's HTML to clean text that PRESERVES heading structure.

    Headings (``<h1>``…``<h6>``) become Markdown ATX (``#``…``######``) on their own
    blank-line-separated paragraph, so the substrate's prose policy keeps a section
    title attached to the body it introduces and can split on it (AC1). List items
    become ``-`` bullets, ``<br>`` and block-closers become line/paragraph breaks,
    every other tag is stripped, and HTML entities are unescaped. Best-effort and
    dependency-free — SharePoint canvas HTML is simple, structured markup, not
    arbitrary documents.
    """
    if not raw:
        return ""
    text = str(raw)

    def _heading(match: "re.Match[str]") -> str:
        level = int(match.group(1))
        inner = _TAG_RE.sub("", match.group(2)).strip()
        return f"\n\n{'#' * level} {inner}\n\n" if inner else "\n\n"

    text = _H_RE.sub(_heading, text)
    text = _LI_RE.sub(lambda m: f"\n- {_TAG_RE.sub('', m.group(1)).strip()}", text)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_CLOSE_RE.sub("\n\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    lines = [ln.strip() for ln in text.split("\n")]
    text = _BLANK_LINES_RE.sub("\n\n", "\n".join(lines))
    return text.strip()


def _iter_text_webparts(page: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Yield the text webparts of a site page's canvas, in reading order.

    Walks ``canvasLayout.horizontalSections[].columns[].webparts[]`` (plus an
    optional ``verticalSection``). Only text-bearing webparts are yielded — an
    ``innerHtml`` string identifies one; image/embed webparts carry no page-native
    text and are skipped.
    """
    layout = page.get("canvasLayout") or {}
    sections = list(layout.get("horizontalSections") or [])
    vertical = layout.get("verticalSection")
    if isinstance(vertical, dict):
        sections.append(vertical)
    for section in sections:
        for column in section.get("columns") or []:
            for webpart in column.get("webparts") or column.get("webParts") or []:
                if not isinstance(webpart, dict):
                    continue
                inner = webpart.get("innerHtml") or webpart.get("innerHTML")
                if isinstance(inner, str) and inner.strip():
                    yield webpart


def render_page_text(page: Dict[str, Any]) -> str:
    """Render a site page to structure-preserving prose text (AC1).

    The page title leads as an ``#`` heading, then each text webpart's HTML is
    rendered in reading order (headings preserved as ATX). Returns clean text ready
    for the substrate's prose chunker — no HTML, no webpart chrome. A page with a
    title but no text webparts still renders its title (a truthful, near-empty page);
    a page with neither renders empty (the substrate records it as empty).
    """
    parts: List[str] = []
    title = str(page.get("title") or page.get("name") or "").strip()
    if title:
        parts.append(f"# {title}")
    for webpart in _iter_text_webparts(page):
        body = _html_to_text(webpart.get("innerHtml") or webpart.get("innerHTML"))
        if body:
            parts.append(body)
    return "\n\n".join(parts).strip()


def render_list_text(list_obj: Dict[str, Any]) -> str:
    """Render a list's item text to structure-preserving prose (AC1).

    The list name leads as an ``#`` heading; each item renders its ``Title`` as a
    ``##`` heading with its remaining text columns as paragraphs beneath. SharePoint
    system columns (:data:`_LIST_SYSTEM_FIELDS`, and any ``@``/``_``-prefixed key)
    are skipped so only human-authored text is ingested. Deterministic order: items
    as given, fields sorted by name.
    """
    parts: List[str] = []
    name = str(list_obj.get("displayName") or list_obj.get("name") or "").strip()
    if name:
        parts.append(f"# {name}")
    for item in list_obj.get("items") or []:
        fields = item.get("fields") if isinstance(item, dict) else None
        if not isinstance(fields, dict):
            continue
        title = str(fields.get("Title") or "").strip()
        if title:
            parts.append(f"## {title}")
        for key in sorted(fields):
            if key in _LIST_SYSTEM_FIELDS or key.startswith(("@", "_")):
                continue
            value = fields.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Opaque per-artifact change-marker checkpoint (owned here; opaque to the runner)
# ---------------------------------------------------------------------------


def _encode_checkpoint(markers: Dict[str, str]) -> str:
    """Encode the per-artifact change-marker map as the opaque checkpoint value.

    ``sort_keys`` keeps the encoding deterministic so two runs over identical state
    produce byte-identical checkpoints (diff-friendly, testable).
    """
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "artifacts": markers},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, str]:
    """Decode an opaque checkpoint value back into the per-artifact marker map.

    Tolerant by design: a missing, empty, or unparseable value yields an empty map
    (every page/list read as a first load) rather than raising — a degenerate
    checkpoint must degrade to a safe full re-read, never crash the run.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning(
            "sharepoint_content: could not decode checkpoint value; treating as "
            "first run (full re-read). value=%r",
            value,
        )
        return {}
    artifacts = data.get("artifacts") if isinstance(data, dict) else None
    if not isinstance(artifacts, dict):
        return {}
    return {str(k): str(v) for k, v in artifacts.items() if v is not None}


def _page_artifact_id(site_id: str, page_id: str) -> str:
    """Stable substrate identity for one site page — ``"{site_id}:page:{page_id}"``.

    A ``:page:`` / ``:list:`` namespace keeps page-native content ids disjoint from
    the reach connector's driveItem ids (``"{site}/{drive}:{item}"``), so a page can
    never collide with a file in the store's ``(source_system, source_artifact)`` key.
    """
    return f"{site_id}:page:{page_id}"


def _list_artifact_id(site_id: str, list_id: str) -> str:
    """Stable substrate identity for one list — ``"{site_id}:list:{list_id}"``."""
    return f"{site_id}:list:{list_id}"


def _build_evidence_pointer(artifact_id: str, timestamp: Optional[str]) -> Dict[str, Any]:
    """Build the R16-B1 OBSERVED EvidencePointer for one page/list content artifact.

    Mirrors the reach connector's pointer: ``source_system='sharepoint'``, the stable
    page/list identity as ``source_artifact`` (``source_artifact_type='record_id'``),
    the content's own last-modified timestamp (falling back to now so the mandatory
    spine is always populated), and ``origin='observed'`` — read directly from
    SharePoint via Microsoft Graph, so no ``extraction_job_id`` is required.
    """
    return EvidencePointer.observed(
        source_system=SOURCE_SYSTEM,
        source_artifact=artifact_id,
        source_timestamp=timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


# ---------------------------------------------------------------------------
# The unit of content work
# ---------------------------------------------------------------------------


@dataclass
class _ContentWork:
    """One page/list's rendered content plus the identity + provenance it carries."""

    artifact_id: str
    kind: str  # "page" | "list"
    site_id: str
    site_name: str
    native_id: str  # the page id or list id within the site
    title: str
    web_url: Optional[str]
    marker: str  # change marker (lastModified/created) — the checkpoint position
    content: str


# ---------------------------------------------------------------------------
# The content ingestor
# ---------------------------------------------------------------------------


class SharePointContentIngestor(ChangeBasedIngestor):
    """Change-based SharePoint page-native CONTENT ingestor (R18-A5 / AT-601).

    Reads modern site pages and list text for GRANTED sites, renders them to
    structure-preserving prose, and yields them as delta records carrying the text
    (the hand-off in :func:`ingest_sharepoint_content` maps each to a substrate
    ``ContentArtifact``). Position is a per-artifact change-marker map (opaque to the
    runner); a first run emits all granted content, an incremental run only the
    pages/lists whose marker moved. Binary library files are never touched here —
    they are the R18-A1 document path.
    """

    connector_id = CONNECTOR_ID
    reports_deletes = False

    def __init__(
        self,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        *,
        ingestor: Optional[SharePointIngestor] = None,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size
        # Reuse the reach connector's access layer (client / granted-site resolution
        # / fixture plumbing / offline-live switch) rather than re-authenticating.
        self._reach = ingestor or SharePointIngestor()

    # ── ChangeBasedIngestor contract ────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of changed SharePoint page/list content since ``since``.

        First run (``since is None``): every granted page/list, streamed as
        checkpointed batches. Incremental: only content whose change marker is newer
        than the stored one (an edited page re-surfaces). An unchanged estate yields
        a single empty :class:`DeltaBatch` echoing the incoming position.
        """
        tokens = _decode_checkpoint(since.value if since else None)
        running = dict(tokens)

        items = self._content_items(org_id)
        fresh = [it for it in items if _marker_gt(it.marker, tokens.get(it.artifact_id))]
        # Oldest-first so the checkpoint advances monotonically as batches emit;
        # artifact_id breaks ties for a deterministic order.
        fresh.sort(
            key=lambda it: (_marker_epoch(it.marker) or float("-inf"), it.artifact_id)
        )
        logger.info(
            "sharepoint_content: org=%s %s — %d granted content artifact(s), %d changed",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(items),
            len(fresh),
        )

        if not fresh:
            yield DeltaBatch(
                records=[],
                next_checkpoint=_encode_checkpoint(running),
                is_complete=True,
            )
            return

        total_batches = (len(fresh) + self.batch_size - 1) // self.batch_size
        emitted = 0
        for start in range(0, len(fresh), self.batch_size):
            page = fresh[start : start + self.batch_size]
            records = [self._to_record(it) for it in page]
            for it in page:
                running[it.artifact_id] = it.marker
            emitted += 1
            yield DeltaBatch(
                records=records,
                next_checkpoint=_encode_checkpoint(running),
                is_complete=(emitted == total_batches),
            )

    # ── Granted-scope enumeration (R18-A5 §4 — permissions re-verified at depth) ─
    def _accessible_sites(self, org_id: str) -> List[Dict[str, Any]]:
        """Return only the sites AgentIQ is granted (``is_accessible != False``).

        The same least-privilege boundary the reach connector applies to sites; in
        live mode Graph only returns granted sites anyway, so this is the offline
        equivalent of that boundary — an ungranted site's pages/lists are never read.
        When the org has saved a site selection (Integration Hub), the granted set
        is further narrowed to it — the SAME selection the reach path applies (via
        the shared ``SharePointIngestor._selected_site_ids``), so reach and depth
        stay consistent.
        """
        selected = self._reach._selected_site_ids(org_id)
        sites: List[Dict[str, Any]] = []
        for site in self._reach._raw_sites(org_id):
            if not site.get("is_accessible", True):
                continue
            if selected is not None and str(site.get("id", "")) not in selected:
                continue
            sites.append(site)
        return sites

    def _content_items(self, org_id: str) -> List[_ContentWork]:
        """Render every granted site's pages + list text into content work units."""
        work: List[_ContentWork] = []
        for site in self._accessible_sites(org_id):
            site_id = str(site.get("id") or "")
            if not site_id:
                continue
            site_name = str(site.get("displayName") or site.get("name") or "")
            for page in self._raw_pages(org_id, site_id):
                item = self._page_work(site_id, site_name, page)
                if item is not None:
                    work.append(item)
            for list_obj in self._raw_lists(org_id, site_id):
                item = self._list_work(site_id, site_name, list_obj)
                if item is not None:
                    work.append(item)
        return work

    def _page_work(
        self, site_id: str, site_name: str, page: Dict[str, Any]
    ) -> Optional[_ContentWork]:
        page_id = str(page.get("id") or "").strip()
        if not page_id:
            return None
        content = render_page_text(page)
        return _ContentWork(
            artifact_id=_page_artifact_id(site_id, page_id),
            kind="page",
            site_id=site_id,
            site_name=site_name,
            native_id=page_id,
            title=str(page.get("title") or page.get("name") or ""),
            web_url=page.get("webUrl"),
            marker=_change_marker(page),
            content=content,
        )

    def _list_work(
        self, site_id: str, site_name: str, list_obj: Dict[str, Any]
    ) -> Optional[_ContentWork]:
        list_id = str(list_obj.get("id") or "").strip()
        if not list_id:
            return None
        content = render_list_text(list_obj)
        return _ContentWork(
            artifact_id=_list_artifact_id(site_id, list_id),
            kind="list",
            site_id=site_id,
            site_name=site_name,
            native_id=list_id,
            title=str(list_obj.get("displayName") or list_obj.get("name") or ""),
            web_url=list_obj.get("webUrl"),
            marker=_change_marker(list_obj),
            content=content,
        )

    def _to_record(self, item: _ContentWork) -> Dict[str, Any]:
        """Shape one content work unit into a delta record.

        Carries the identity the shared runner needs (``artifact_id`` +
        ``change_kind`` for ``ingestion.artifact_changed`` events), the site/page
        provenance + an OBSERVED evidence pointer (R16-B1), and the RENDERED prose
        ``content`` the hand-off feeds to the substrate.
        """
        return {
            "artifact_id": item.artifact_id,
            "change_kind": ChangeKind.CREATED,
            "source_system": SOURCE_SYSTEM,
            "connector_id": self.connector_id,
            "content_kind": item.kind,
            "site_id": item.site_id,
            "site_name": item.site_name,
            "page_id": item.native_id if item.kind == "page" else None,
            "list_id": item.native_id if item.kind == "list" else None,
            "title": item.title,
            "web_url": item.web_url,
            "content": item.content,
            "content_type": CONTENT_TYPE,
            "source_timestamp": item.marker or None,
            "evidence_pointer": _build_evidence_pointer(item.artifact_id, item.marker),
        }

    # ── Source access: offline fixture vs live Microsoft Graph API ───────────
    def _raw_pages(self, org_id: str, site_id: str) -> List[Dict[str, Any]]:
        from . import is_live

        if not is_live():
            return list(self._reach._fixture().get("pages", {}).get(site_id, []))
        return self._reach._client(org_id).list_site_pages(site_id)

    def _raw_lists(self, org_id: str, site_id: str) -> List[Dict[str, Any]]:
        """Return a granted site's lists, each carrying its items' field text.

        Offline: the fixture's ``lists`` section (items inline). Live: enumerate the
        site's lists, then attach each list's item fields (``$expand=fields``) so the
        renderer sees the same shape as offline.
        """
        from . import is_live

        if not is_live():
            return list(self._reach._fixture().get("lists", {}).get(site_id, []))
        client = self._reach._client(org_id)
        lists: List[Dict[str, Any]] = []
        for list_obj in client.list_site_lists(site_id):
            list_id = str(list_obj.get("id") or "").strip()
            if not list_id:
                continue
            items = client.list_item_fields(site_id, list_id)
            lists.append({**list_obj, "items": items})
        return lists


# ---------------------------------------------------------------------------
# The retrieval hand-off (mirrors documents_handoff.py)
# ---------------------------------------------------------------------------

#: The producer→substrate hand-off callable, injectable for tests.
IngestFn = Callable[[str, List[ContentArtifact]], IngestResult]


class SharePointContentHandoffError(RuntimeError):
    """Raised by the batch hand-off when the substrate reports failed artifacts.

    Propagated into the change runner so the checkpoint is NOT advanced past content
    that never reached retrieval — the batch is re-read and re-handed next run
    (idempotent via ``ingest_content``'s per-artifact replace).
    """


def _build_provenance(record: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the provenance a retrieval hit shows for this page/list (AC1/AC6).

    Carries ``origin='observed'`` (surfaced at the top level so a consumer need not
    unwrap the spine), the site/page identity and human-facing title, the
    deep-linkable ``web_url``, and the full R16-B1 EvidencePointer spine verbatim. No
    page content ever goes into provenance — only source identifiers.
    """
    evidence_pointer = record.get("evidence_pointer") or {}
    return {
        "origin": evidence_pointer.get("origin", "observed"),
        "content_kind": record.get("content_kind"),
        "site_id": record.get("site_id"),
        "site_name": record.get("site_name"),
        "page_id": record.get("page_id"),
        "list_id": record.get("list_id"),
        "title": record.get("title"),
        "web_url": record.get("web_url"),
        "evidence_pointer": evidence_pointer,
    }


def record_to_artifact(record: Dict[str, Any]) -> Optional[ContentArtifact]:
    """Map one ingestor record to a substrate :class:`ContentArtifact`, or ``None``.

    Deletions (``change_kind='deleted'``) carry no content and are never handed to
    the substrate (deletion is the R18-B2 freshness story). Everything else maps to a
    prose artifact under ``source_system='sharepoint'`` with the record's rendered
    text and full observed provenance.
    """
    if not isinstance(record, dict):
        return None
    if record.get("change_kind") == ChangeKind.DELETED:
        return None
    artifact_id = record.get("artifact_id")
    if not artifact_id:
        return None
    return ContentArtifact(
        source_system=SOURCE_SYSTEM,
        source_artifact=str(artifact_id),
        content=record.get("content") or "",
        content_type=record.get("content_type") or CONTENT_TYPE,
        source_timestamp=record.get("source_timestamp"),
        provenance=_build_provenance(record),
    )


def content_artifacts(records: List[Dict[str, Any]]) -> List[ContentArtifact]:
    """Map a batch's records to the substrate artifacts worth handing over."""
    artifacts: List[ContentArtifact] = []
    for record in records or []:
        artifact = record_to_artifact(record)
        if artifact is not None:
            artifacts.append(artifact)
    return artifacts


def build_content_artifact(
    ingestor: "SharePointContentIngestor",
    org_id: str,
    site_id: str,
    kind: str,
    native_id: str,
) -> Optional[ContentArtifact]:
    """Re-read ONE page/list and build its substrate artifact, or ``None``.

    The single public seam the retrieval-freshness resolver
    (``app.retrieval.default_resolvers._resolve_sharepoint``) uses to turn a queued
    ``"{site_id}:page:{page_id}"`` / ``"{site_id}:list:{list_id}"`` artifact id back
    into current content. It reuses the EXACT render + record + provenance chain the
    direct hand-off (:func:`ingest_sharepoint_content`) uses — ``_page_work`` /
    ``_list_work`` → ``_to_record`` → :func:`record_to_artifact` — so an
    async refresh produces the same structure-preserving prose as the synchronous
    path, never a metadata-only stub. Mirrors
    ``confluence_content.build_content_artifact`` (R18-A5 / AT-603).

    Without this the resolver could not parse the ``:page:`` / ``:list:`` namespace
    at all: it returned ``None`` for every one, the worker recorded ``no_content``
    and parked the row ``failed`` after its retry budget, and the chunks stayed
    ``is_stale = TRUE`` — excluded from default retrieval — permanently. Because
    ``change_runner`` emits ``artifact_changed`` AFTER the hand-off already wrote
    fresh chunks, that applied to a page's FIRST indexing, not just to edits.

    Scope is re-verified here (R18-A5 §4 "permissions re-verified at depth"):
    the site must still be granted AND still inside the org's saved site selection,
    resolved through :meth:`SharePointContentIngestor._accessible_sites` — which is
    also where ``site_name`` comes from, so provenance matches the hand-off's.
    A site that has since lost its grant resolves to ``None`` (the artifact keeps
    its existing state; removal is ``remove_artifact``'s job, not a resolver's).
    """
    if kind not in ("page", "list"):
        return None
    site_id = str(site_id or "").strip()
    native_id = str(native_id or "").strip()
    if not site_id or not native_id:
        return None

    site = next(
        (
            candidate
            for candidate in ingestor._accessible_sites(org_id)
            if str(candidate.get("id") or "") == site_id
        ),
        None,
    )
    if site is None:
        return None
    site_name = str(site.get("displayName") or site.get("name") or "")

    if kind == "page":
        source = ingestor._raw_pages(org_id, site_id)
        build_work = ingestor._page_work
    else:
        source = ingestor._raw_lists(org_id, site_id)
        build_work = ingestor._list_work

    for item in source:
        if str(item.get("id") or "").strip() != native_id:
            continue
        work = build_work(site_id, site_name, item)
        if work is None:
            return None
        return record_to_artifact(ingestor._to_record(work))
    return None


@dataclass
class SharePointContentResult:
    """Outcome of one :func:`ingest_sharepoint_content` run — read + hand-off totals."""

    org_id: str
    batches: int = 0
    records: int = 0
    artifacts_handed_off: int = 0
    artifacts_indexed: int = 0
    artifacts_empty: int = 0
    artifacts_failed: int = 0
    chunks_indexed: int = 0
    chunks_replaced: int = 0
    checkpoint_advanced: bool = False
    first_run: bool = False
    error: Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def ingest_sharepoint_content(
    org_id: str,
    *,
    ingestor: Optional[SharePointContentIngestor] = None,
    batch_size: Optional[int] = None,
    ingest_fn: IngestFn = ingest_content,
    **runner_kwargs: Any,
) -> SharePointContentResult:
    """Run SharePoint content ingestion and hand every artifact to the substrate.

    Drives the :class:`SharePointContentIngestor` through the shared change runner
    (so incremental reads, the resumable first load, and the
    ``ingestion.artifact_changed`` events are all unchanged) and, per fully-read
    batch, hands its rendered page/list content to
    ``retrieval.ingest_content(org_id, artifacts)`` (AC1). Only granted sites are
    read; binary library files are never touched (R18-A1 owns those).

    ``ingest_fn`` is the substrate entry point, injectable so tests can capture
    hand-offs without a database; it defaults to the real ``ingest_content``. Extra
    ``runner_kwargs`` (e.g. ``read_checkpoint`` / ``save_checkpoint``) pass straight
    through to the change runner.

    Never raises for a runtime failure: like the change runner, a substrate hand-off
    failure is captured on ``result.error`` and leaves the checkpoint un-advanced so
    the batch is re-handed next run (idempotent replace).
    """
    kwargs: Dict[str, Any] = {}
    if batch_size is not None:
        kwargs["batch_size"] = batch_size
    ing = ingestor or SharePointContentIngestor(**kwargs)

    summary = SharePointContentResult(org_id=org_id)

    def _process_batch(batch: change_runner.DeltaBatch) -> None:
        artifacts = content_artifacts(batch.records)
        if not artifacts:
            return
        result = ingest_fn(org_id, artifacts)
        summary.artifacts_handed_off += len(artifacts)
        summary.artifacts_indexed += result.artifacts_indexed
        summary.artifacts_empty += result.artifacts_empty
        summary.artifacts_failed += result.artifacts_failed
        summary.chunks_indexed += result.chunks_indexed
        summary.chunks_replaced += result.chunks_replaced
        logger.info(
            "sharepoint_content: handed off org=%s artifacts=%d indexed=%d empty=%d "
            "failed=%d chunks_indexed=%d (embedding is async)",
            org_id,
            len(artifacts),
            result.artifacts_indexed,
            result.artifacts_empty,
            result.artifacts_failed,
            result.chunks_indexed,
        )
        if result.artifacts_failed:
            raise SharePointContentHandoffError(
                f"{result.artifacts_failed} artifact(s) failed retrieval hand-off "
                f"for org {org_id}; checkpoint not advanced (will retry)"
            )

    run = change_runner.ingest_with_checkpoint(
        ing, org_id, process_batch=_process_batch, **runner_kwargs
    )
    summary.batches = run.batches
    summary.records = run.records
    summary.checkpoint_advanced = run.checkpoint_advanced
    summary.first_run = run.first_run
    summary.error = run.error

    if summary.error is not None:
        logger.error(
            "sharepoint_content: retrieval hand-off did NOT complete for org=%s "
            "(%d artifact(s) failed, checkpoint not advanced, will retry): %s",
            org_id,
            summary.artifacts_failed,
            type(summary.error).__name__,
        )
    return summary
