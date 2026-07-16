"""
R18-A5 / AT-602 (T3) — the page-vs-file router.

SharePoint (and, in its own way, Confluence) is BOTH a page platform and a file
store. This module is the single, explicit, tested place that classifies an
artifact a knowledge-source connector encounters into exactly one destination
(Section 4, "one artifact, one owner"):

  * :attr:`ContentRoute.PAGE_CONTENT` — page-native content (Confluence pages/
    blogposts, SharePoint site pages, list text) — routes to the R18-A5 depth
    content path (``confluence_content.py`` / ``sharepoint_content.py``).
  * :attr:`ContentRoute.DOCUMENT` — binary files in libraries and page
    attachments (PDF, docx, xlsx, images, …) — routes to the R18-A1 document
    path (``documents_attachments.py`` → ``DocumentIngestor``).
  * :attr:`ContentRoute.SKIP` — neither (e.g. a Confluence comment, a
    SharePoint folder) — carries no content for either path.

Why a router module exists even though the two paths already read from
DISJOINT surfaces
------------------------------------------------------------------------------
Today, each connector's depth content path and document path already read from
structurally separate discovery mechanisms — Confluence's page/blogpost delta
listing (``confluence.py::_changed_content``) versus its per-page attachment
listing (``content/{id}/child/attachment``); SharePoint's ``/sites/{id}/pages``
+ ``/sites/{id}/lists`` surface (``sharepoint_content.py``) versus its
``/drives/{id}/root/delta`` driveItem surface (``sharepoint.py``). That
separation is what makes double-ingestion structurally hard today. But "hard"
is not "impossible", and relying on two independently-evolving listing
functions to never overlap is exactly the kind of implicit invariant that
silently breaks when one of them changes. This module makes the invariant
EXPLICIT and TESTED (Section 4: "small but load-bearing... gets its own
tests") rather than leaving it as an emergent property of unrelated code:
every one of the four call sites below asks this module before deciding an
artifact's destination, so a future change that widens either listing's scope
degrades to a defensive skip/log here instead of silently double-ingesting or
dropping content.

One artifact, one owner
------------------------
Every classification result maps to EXACTLY one destination. A caller must
never treat a :attr:`ContentRoute.SKIP` result as "route it anyway" — skip
means the artifact carries no content for either path (a comment has no body
either path wants; a folder has no bytes at all).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Optional


class ContentRoute(str, Enum):
    """Where one artifact's content belongs — exactly one destination."""

    #: Page-native content — the R18-A5 depth content path.
    PAGE_CONTENT = "page_content"
    #: A binary file / attachment — the R18-A1 document path.
    DOCUMENT = "document"
    #: Neither path (e.g. a comment, a folder, an unrecognised kind).
    SKIP = "skip"


# ---------------------------------------------------------------------------
# Confluence — content-item `type` classification
# ---------------------------------------------------------------------------

#: Confluence content-item `type` values that are page-native (Section 1):
#: pages and blog posts render to structure-preserving prose via
#: ``confluence_content.py``.
_CONFLUENCE_PAGE_TYPES = frozenset({"page", "blogpost"})

#: Confluence content-item `type` values that are files. Confluence's REST
#: content model tags an attachment object with ``type='attachment'`` when it
#: is returned through the generic ``/content`` shape; this codebase's
#: per-page attachment listing (``content/{id}/child/attachment``) does not
#: repeat that field on every entry (see ``ConfluenceClient.list_attachments``),
#: so callers on that path pass this constant explicitly (see
#: :data:`CONFLUENCE_ATTACHMENT_TYPE` below) rather than relying on the field
#: being present.
_CONFLUENCE_FILE_TYPES = frozenset({"attachment"})

#: The type token an attachment-listing caller should classify with when the
#: attachment object itself carries no ``type`` field — see module docstring.
CONFLUENCE_ATTACHMENT_TYPE = "attachment"


def classify_confluence_content(content_type: Optional[str]) -> ContentRoute:
    """Classify one Confluence content item by its ``type`` field.

    ``page`` / ``blogpost`` -> :attr:`ContentRoute.PAGE_CONTENT` (this story's
    depth content path, ``confluence_content.py``). ``attachment`` ->
    :attr:`ContentRoute.DOCUMENT` (the R18-A1 document path). Anything else —
    ``comment``, an unrecognised/future content type, or a missing/blank
    value — -> :attr:`ContentRoute.SKIP`: neither path wants it, and a caller
    must not guess.
    """
    kind = (content_type or "").strip().lower()
    if kind in _CONFLUENCE_PAGE_TYPES:
        return ContentRoute.PAGE_CONTENT
    if kind in _CONFLUENCE_FILE_TYPES:
        return ContentRoute.DOCUMENT
    return ContentRoute.SKIP


# ---------------------------------------------------------------------------
# SharePoint — driveItem classification
# ---------------------------------------------------------------------------


def classify_sharepoint_drive_item(item: Mapping[str, Any]) -> ContentRoute:
    """Classify one SharePoint driveItem (a ``/drives/{id}/root/delta`` result).

    A driveItem is a document-library FILE or FOLDER (Graph's ``file`` /
    ``folder`` facets) — SharePoint site pages and list text are a DIFFERENT
    Graph surface entirely (``/sites/{id}/pages`` / ``/sites/{id}/lists``,
    read by ``sharepoint_content.py``) and never appear as driveItems, so this
    classifier only ever needs to distinguish file vs folder vs deleted:

      * a folder, a deleted item, or anything with neither facet ->
        :attr:`ContentRoute.SKIP` (no bytes to route anywhere);
      * a file -> :attr:`ContentRoute.DOCUMENT` (the R18-A1 document path).

    No driveItem is ever classified :attr:`ContentRoute.PAGE_CONTENT` — that
    destination is reached only through the separate pages/lists surface,
    never through this classifier. Defensive by construction: an unrecognised
    or malformed item degrades to ``SKIP`` rather than being guessed into a
    path it may not belong to.
    """
    if not isinstance(item, Mapping):
        return ContentRoute.SKIP
    if item.get("deleted"):
        return ContentRoute.SKIP
    if "folder" in item:
        return ContentRoute.SKIP
    if "file" in item:
        return ContentRoute.DOCUMENT
    return ContentRoute.SKIP
