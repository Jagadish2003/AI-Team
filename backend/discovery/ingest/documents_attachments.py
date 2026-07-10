"""
R18-A1 / T5 (AT-527) — SharePoint & Confluence attachments into the document path.

The 1.7 SharePoint (R17-A2) and Confluence (R17-A2) connectors surface the
*existence and metadata* of document-library files and page attachments, but by
the reach/depth boundary they deliberately do NOT read file BYTES — every one of
their docstrings defers "reading document/page bodies into the retrieval layer" to
"the separate 1.8 deep-content story." That story is R18-A1, and this module is
the wire: it exposes each connector's file-bearing artifacts as
:class:`~discovery.ingest.documents_source.DocumentSource`\\ s so their bytes flow
through the :class:`~discovery.ingest.documents.DocumentIngestor` — detected,
extracted (T2 handlers), and handed to the retrieval substrate (T3) exactly like
any other document, with NO new extraction or hand-off code.

Reuse, not reinvention
----------------------
Each adapter WRAPS the existing connector and reuses its access layer — the OAuth
client, the granted-site/space filtering, the fixture plumbing — so credentials,
least-privilege scoping, and offline determinism are inherited, not duplicated:

  * :class:`SharePointDocumentSource` reuses ``SharePointIngestor._accessible_libraries``
    + ``_raw_items`` to enumerate the driveItems that are FILES.
  * :class:`ConfluenceDocumentSource` reuses ``ConfluenceIngestor._accessible_spaces``
    and lists each page's ATTACHMENTS.

The only genuinely new capability is fetching the bytes — the connectors never
did (content was 1.8). Live byte fetches use the connector's authenticated client
(:meth:`SharePointGraphClient.download_item_content`,
:meth:`ConfluenceClient.download_attachment`, added alongside); offline reads take
inline bytes from the connector fixtures.

Incremental behaviour (AC2)
---------------------------
These adapters do not re-implement delta. Each file/attachment is listed with a
cheap per-artifact change ``signature`` (SharePoint: the driveItem eTag / change
marker; Confluence: the attachment version), and the DocumentIngestor's per-file
signature checkpoint does the rest: on an incremental run only files whose
signature is new or changed are read + extracted, and unchanged ones are never
re-fetched or re-processed (AC2). Because these sources return only the artifacts
the connector currently surfaces (not a guaranteed full estate), they declare
``reports_deletes = False`` so an absent artifact is never mistaken for a deletion
— attachment/file deletion belongs to the R18-B2 freshness story.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional, Tuple

from .documents_source import DocumentRef, DocumentSource, DocumentSourceError

logger = logging.getLogger(__name__)


def _inline_bytes(entry: Dict[str, Any]) -> bytes:
    """Materialise an offline fixture artifact's bytes from ``content_b64``/``text``.

    Offline parity with the other connectors: a fixture file/attachment carries its
    bytes inline (base64 ``content_b64`` or plain ``text``). A fixture entry with
    neither is a metadata-only stub — reading it raises, so a test that expects
    bytes fails loudly rather than silently extracting empty content.
    """
    if entry.get("content_b64") is not None:
        return base64.b64decode(entry["content_b64"])
    text = entry.get("text")
    if text is None:
        raise DocumentSourceError(
            f"fixture artifact {entry.get('id')!r} carries no inline bytes "
            "(content_b64/text) to read"
        )
    return str(text).encode("utf-8")


# ---------------------------------------------------------------------------
# SharePoint document libraries → the document path
# ---------------------------------------------------------------------------
class SharePointDocumentSource(DocumentSource):
    """Surface SharePoint document-library FILES for the DocumentIngestor.

    Reuses the R17-A2 :class:`~discovery.ingest.sharepoint.SharePointIngestor`
    access layer (granted sites → granted libraries → driveItems) and yields the
    items that are files (folders are skipped) as :class:`DocumentRef`\\ s. The
    driveItem eTag (else its last-modified change marker) is the change signature,
    so the DocumentIngestor re-reads a file only after it actually changes (AC2).
    """

    reports_deletes = False  # delta-oriented source; never infer deletes here

    def __init__(self, ingestor: Optional[Any] = None):
        # Imported lazily so importing this module never forces the SharePoint
        # connector (and its deps) in when only the Confluence adapter is used.
        if ingestor is None:
            from .sharepoint import SharePointIngestor

            ingestor = SharePointIngestor()
        self._ing = ingestor
        self._raw_cache: Dict[str, Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]] = {}

    def _scan(self, org_id: str) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Enumerate accessible library FILE driveItems, keyed by artifact id.

        Cached per org on this (per-run) instance so ``list_documents`` and the
        subsequent ``read`` calls share one enumeration instead of re-scanning.
        """
        cache: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        for library in self._ing._accessible_libraries(org_id):
            for item in self._ing._raw_items(org_id, library):
                if item.get("deleted"):
                    continue
                if "file" not in item:  # folders / non-file items carry no bytes
                    continue
                item_id = str(item.get("id") or "").strip()
                if not item_id:
                    continue
                artifact_id = f"sharepoint:{library['site_id']}/{library['id']}:{item_id}"
                cache[artifact_id] = (library, item)
        self._raw_cache[org_id] = cache
        return cache

    def list_documents(self, org_id: str) -> List[DocumentRef]:
        from .sharepoint import _change_marker

        refs: List[DocumentRef] = []
        for artifact_id, (library, item) in self._scan(org_id).items():
            file_facet = item.get("file") or {}
            marker = _change_marker(item)
            modified_by = (item.get("lastModifiedBy") or {}).get("user") or {}
            refs.append(
                DocumentRef(
                    artifact_id=artifact_id,
                    filename=str(item.get("name") or item_id_from(artifact_id)),
                    location=library.get("name") or library.get("site_name") or "",
                    # eTag changes on every edit; the change marker (last-modified)
                    # is the fallback. Either advances only when the file changes.
                    signature=str(item.get("eTag") or marker or item.get("id")),
                    source_timestamp=item.get("lastModifiedDateTime")
                    or item.get("createdDateTime"),
                    content_type=file_facet.get("mimeType"),
                    provenance={
                        # True origin (the record's own source_system stays
                        # 'documents'); this names the file's real home for AC6.
                        "source_system": "sharepoint",
                        "web_url": item.get("webUrl"),
                        "site_name": library.get("site_name", ""),
                        "library_name": library.get("name", ""),
                        "last_modified_by": modified_by.get("displayName"),
                    },
                )
            )
        return refs

    def read(self, org_id: str, ref: DocumentRef) -> bytes:
        cache = self._raw_cache.get(org_id) or self._scan(org_id)
        entry = cache.get(ref.artifact_id)
        if entry is None:
            raise DocumentSourceError(
                f"SharePoint document {ref.artifact_id!r} not found for read"
            )
        library, item = entry
        from . import is_live

        if not is_live():
            return _inline_bytes(item)
        # Live: fetch the bytes through the connector's authenticated Graph client
        # (the /content endpoint the 1.7 connector deliberately never called).
        return self._ing._client(org_id).download_item_content(library["id"], item["id"])


def item_id_from(artifact_id: str) -> str:
    """Best-effort filename fallback: the item id tail of a composite artifact id."""
    return artifact_id.rsplit(":", 1)[-1]


# ---------------------------------------------------------------------------
# Confluence page attachments → the document path
# ---------------------------------------------------------------------------
class ConfluenceDocumentSource(DocumentSource):
    """Surface Confluence page ATTACHMENTS for the DocumentIngestor.

    Reuses the R17-A2 :class:`~discovery.ingest.confluence.ConfluenceIngestor`
    granted-space access and lists each page's attachments as
    :class:`DocumentRef`\\ s. The attachment version (Confluence bumps it on every
    re-upload) is the change signature, so the DocumentIngestor re-reads an
    attachment only after it is replaced (AC2).
    """

    reports_deletes = False  # per-page attachment listing; never infer deletes here

    def __init__(self, ingestor: Optional[Any] = None):
        if ingestor is None:
            from .confluence import ConfluenceIngestor

            ingestor = ConfluenceIngestor()
        self._ing = ingestor
        self._raw_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _raw_attachments(self, org_id: str, space: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return a space's attachments (offline fixture, or live per-page listing)."""
        from . import is_live

        if not is_live():
            fixture = self._ing._fixture()
            return list(fixture.get("attachments", {}).get(space["key"], []))
        # Live: attachments hang off pages, so list them per accessible page.
        client = self._ing._client(org_id)
        attachments: List[Dict[str, Any]] = []
        for content in self._ing._raw_content(org_id, space):
            if content.get("type") != "page":
                continue
            page_id = str(content.get("id") or "")
            if not page_id:
                continue
            for att in client.list_attachments(page_id):
                attachments.append({**att, "page_id": page_id})
        return attachments

    def _scan(self, org_id: str) -> Dict[str, Dict[str, Any]]:
        cache: Dict[str, Dict[str, Any]] = {}
        for space in self._ing._accessible_spaces(org_id):
            for att in self._raw_attachments(org_id, space):
                att_id = str(att.get("id") or "").strip()
                if not att_id:
                    continue
                artifact_id = f"confluence:{space['key']}:{att_id}"
                cache[artifact_id] = {**att, "_space_key": space["key"], "_space_name": space.get("name", "")}
        self._raw_cache[org_id] = cache
        return cache

    def list_documents(self, org_id: str) -> List[DocumentRef]:
        refs: List[DocumentRef] = []
        for artifact_id, att in self._scan(org_id).items():
            version = att.get("version") or {}
            # Version bumps on every re-upload; 'when' is the fallback. Either
            # advances only when the attachment changes (AC2).
            signature = str(version.get("number") or version.get("when") or att.get("id"))
            refs.append(
                DocumentRef(
                    artifact_id=artifact_id,
                    filename=str(att.get("title") or att.get("fileName") or att_id_from(artifact_id)),
                    location=att.get("_space_name") or att.get("_space_key") or "",
                    signature=signature,
                    source_timestamp=version.get("when"),
                    content_type=att.get("mediaType") or att.get("content_type"),
                    provenance={
                        "source_system": "confluence",
                        "space_key": att.get("_space_key"),
                        "page_id": att.get("page_id"),
                        "web_url": (att.get("_links") or {}).get("webui")
                        or (att.get("_links") or {}).get("download"),
                    },
                )
            )
        return refs

    def read(self, org_id: str, ref: DocumentRef) -> bytes:
        cache = self._raw_cache.get(org_id) or self._scan(org_id)
        att = cache.get(ref.artifact_id)
        if att is None:
            raise DocumentSourceError(
                f"Confluence attachment {ref.artifact_id!r} not found for read"
            )
        from . import is_live

        if not is_live():
            return _inline_bytes(att)
        download_path = (att.get("_links") or {}).get("download")
        if not download_path:
            # Some Confluence versions / attachment types omit the download link.
            # Fail with a clear, per-file DocumentSourceError (the ingestor isolates
            # it, AC5) instead of passing None into the HTTP client and crashing.
            raise DocumentSourceError(
                f"Confluence attachment {ref.artifact_id!r} has no download link"
            )
        return self._ing._client(org_id).download_attachment(download_path)


def att_id_from(artifact_id: str) -> str:
    """Best-effort filename fallback: the attachment id tail of an artifact id."""
    return artifact_id.rsplit(":", 1)[-1]


# ---------------------------------------------------------------------------
# Composite — several document sources behind one, for the document path
# ---------------------------------------------------------------------------
class CompositeDocumentSource(DocumentSource):
    """Present several :class:`DocumentSource`\\ s as one.

    ``list_documents`` concatenates every child's inventory; ``read`` routes to the
    child that listed a given ref (matched by artifact-id ownership). One bad child
    (auth/IO failure) is logged and skipped rather than sinking the others — the
    project's "degrade, don't crash" ingestion rule — so a run still ingests the
    document locations and connectors that ARE reachable.

    ``reports_deletes`` is True only if EVERY child reports a full inventory;
    otherwise a partial child could make another child's files look deleted.
    """

    def __init__(self, sources: List[DocumentSource]):
        self._sources = list(sources)
        self.reports_deletes = bool(sources) and all(
            getattr(s, "reports_deletes", True) for s in sources
        )
        self._owner: Dict[str, DocumentSource] = {}

    def list_documents(self, org_id: str) -> List[DocumentRef]:
        refs: List[DocumentRef] = []
        self._owner = {}
        for source in self._sources:
            try:
                listed = source.list_documents(org_id)
            except Exception as exc:  # noqa: BLE001 — one bad source never sinks the run
                logger.warning(
                    "documents: source %s failed to list (skipping): %s",
                    type(source).__name__,
                    type(exc).__name__,
                )
                continue
            for ref in listed:
                self._owner[ref.artifact_id] = source
                refs.append(ref)
        return refs

    def read(self, org_id: str, ref: DocumentRef) -> bytes:
        source = self._owner.get(ref.artifact_id)
        if source is None:
            raise DocumentSourceError(
                f"no source owns document {ref.artifact_id!r} (list before read)"
            )
        return source.read(org_id, ref)
