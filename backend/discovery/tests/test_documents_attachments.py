"""
R18-A1 / T5 (AT-527) — SharePoint & Confluence attachments in the document path.

T5 wires the 1.7 SharePoint document-library and Confluence attachment connectors
into document ingestion, so their files flow through the DocumentIngestor and are
extracted like any other document — with the incremental guarantee (AC2): only
new/changed files are read + extracted; unchanged ones are never re-fetched.

These tests inject FAKE connectors (reusing the adapters' real code paths against
the connectors' access layer) with inline-text files, so the flow-through and the
AC2 incremental behaviour are provable offline and deterministically, driving the
REAL DocumentIngestor through the REAL change runner via an in-memory checkpoint.

Covered:
  * SharePoint document-library FILES flow through the DocumentIngestor and are
    extracted (folders are skipped — only files carry bytes).
  * Confluence page ATTACHMENTS flow through the DocumentIngestor and are extracted.
  * AC2 — an unchanged estate yields an empty delta (nothing re-read); changing one
    file's / attachment's change signature re-reads ONLY that one.
  * The composite source lists across children and routes each read to its owner.
"""
from __future__ import annotations

from typing import Dict, List

from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.documents import DocumentIngestor
from discovery.ingest.documents_attachments import (
    CompositeDocumentSource,
    ConfluenceDocumentSource,
    SharePointDocumentSource,
)

ORG = "org_t5"


# ─────────────────────────────────────────────────────────────────────────────
# In-memory checkpoint store + record collection through the real runner
# ─────────────────────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _drive(source, store: Store) -> List[dict]:
    """Drive the DocumentIngestor over ``source`` once; return emitted records."""
    collected: List[dict] = []
    change_runner.ingest_with_checkpoint(
        DocumentIngestor(source=source),
        ORG,
        process_batch=lambda batch: collected.extend(batch.records),
        read_checkpoint=store.read,
        save_checkpoint=store.save,
    )
    return collected


def _extracted_ids(records: List[dict]) -> set:
    return {
        r["artifact_id"]
        for r in records
        if (r.get("extraction") or {}).get("status") == "extracted"
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fake connectors — reused by the real adapters against controlled data
# ─────────────────────────────────────────────────────────────────────────────
class FakeSharePointIngestor:
    """Stands in for SharePointIngestor's access layer with inline-text driveItems."""

    def __init__(self, items: List[dict]):
        self._lib = {"id": "b-docs", "site_id": "S-eng", "site_name": "Engineering", "name": "Documents"}
        self._items = items

    def _accessible_libraries(self, org_id):
        return [self._lib]

    def _raw_items(self, org_id, library):
        return list(self._items)


class FakeConfluenceIngestor:
    """Stands in for ConfluenceIngestor's access layer with inline-text attachments."""

    def __init__(self, attachments: List[dict]):
        self._space = {"key": "ENG", "name": "Engineering", "is_accessible": True}
        self._attachments = attachments

    def _accessible_spaces(self, org_id):
        return [self._space]

    def _fixture(self):
        return {"attachments": {"ENG": list(self._attachments)}}


def _sp_file(item_id, name, text, *, marker="2026-06-10T09:00:00Z", etag=None):
    item = {
        "id": item_id,
        "name": name,
        "file": {"mimeType": "text/plain"},
        "createdDateTime": "2026-06-10T09:00:00Z",
        "lastModifiedDateTime": marker,
        "text": text,  # inline bytes for offline read
    }
    if etag is not None:
        item["eTag"] = etag
    return item


def _sp_folder(item_id, name):
    return {"id": item_id, "name": name, "folder": {"childCount": 1}}


def _cf_attachment(att_id, title, text, *, version=1, when="2026-06-10T09:00:00Z"):
    return {
        "id": att_id,
        "title": title,
        "mediaType": "text/plain",
        "version": {"number": version, "when": when},
        "page_id": "100",
        "text": text,  # inline bytes for offline read
    }


# ─────────────────────────────────────────────────────────────────────────────
# SharePoint document-library files flow through the DocumentIngestor
# ─────────────────────────────────────────────────────────────────────────────
def test_sharepoint_files_flow_through_and_folders_are_skipped():
    source = SharePointDocumentSource(
        ingestor=FakeSharePointIngestor(
            [
                _sp_file("f1", "roadmap.md", "# Roadmap\n\nShip the thing.", etag="v1"),
                _sp_file("f2", "notes.txt", "meeting notes", etag="v1"),
                _sp_folder("d1", "Archive"),  # must be skipped (no bytes)
            ]
        )
    )
    records = _drive(source, Store())
    extracted = _extracted_ids(records)
    assert extracted == {"sharepoint:S-eng/b-docs:f1", "sharepoint:S-eng/b-docs:f2"}
    # Folder never became a record.
    assert not any(r["artifact_id"].endswith(":d1") for r in records)
    # Provenance names the true source system + web home (AC6-style traceability).
    rec = next(r for r in records if r["artifact_id"].endswith(":f1"))
    assert rec["provenance"]["source_system"] == "sharepoint"
    assert rec["content"].startswith("# Roadmap")


def test_sharepoint_incremental_only_changed_file_reread():
    """AC2 for SharePoint: unchanged files are not re-extracted; a changed eTag
    re-reads only that file."""
    store = Store()
    items = [
        _sp_file("f1", "roadmap.md", "v1 body", etag="etag-1"),
        _sp_file("f2", "notes.txt", "notes v1", etag="etag-1"),
    ]
    first = _drive(SharePointDocumentSource(ingestor=FakeSharePointIngestor(items)), store)
    assert _extracted_ids(first) == {"sharepoint:S-eng/b-docs:f1", "sharepoint:S-eng/b-docs:f2"}

    # Unchanged estate → empty delta, nothing re-extracted.
    second = _drive(SharePointDocumentSource(ingestor=FakeSharePointIngestor(items)), store)
    assert _extracted_ids(second) == set()

    # One file's eTag advances (content changed) → only that file is re-read.
    changed = [
        _sp_file("f1", "roadmap.md", "v2 body", etag="etag-2"),
        _sp_file("f2", "notes.txt", "notes v1", etag="etag-1"),
    ]
    third = _drive(SharePointDocumentSource(ingestor=FakeSharePointIngestor(changed)), store)
    assert _extracted_ids(third) == {"sharepoint:S-eng/b-docs:f1"}


# ─────────────────────────────────────────────────────────────────────────────
# Confluence page attachments flow through the DocumentIngestor
# ─────────────────────────────────────────────────────────────────────────────
def test_confluence_attachments_flow_through():
    source = ConfluenceDocumentSource(
        ingestor=FakeConfluenceIngestor(
            [
                _cf_attachment("att1", "spec.md", "# Spec\n\nDetails."),
                _cf_attachment("att2", "budget.csv", "a,b\n1,2\n"),
            ]
        )
    )
    records = _drive(source, Store())
    assert _extracted_ids(records) == {"confluence:ENG:att1", "confluence:ENG:att2"}
    rec = next(r for r in records if r["artifact_id"].endswith(":att1"))
    assert rec["provenance"]["source_system"] == "confluence"
    assert rec["provenance"]["space_key"] == "ENG"
    assert rec["content"].startswith("# Spec")


def test_confluence_incremental_only_changed_attachment_reread():
    """AC2 for Confluence: an unchanged attachment is not re-extracted; a bumped
    version re-reads only that attachment."""
    store = Store()
    atts = [
        _cf_attachment("att1", "spec.md", "spec v1", version=1),
        _cf_attachment("att2", "budget.csv", "x,y\n1,2\n", version=1),
    ]
    first = _drive(ConfluenceDocumentSource(ingestor=FakeConfluenceIngestor(atts)), store)
    assert _extracted_ids(first) == {"confluence:ENG:att1", "confluence:ENG:att2"}

    second = _drive(ConfluenceDocumentSource(ingestor=FakeConfluenceIngestor(atts)), store)
    assert _extracted_ids(second) == set()

    bumped = [
        _cf_attachment("att1", "spec.md", "spec v2", version=2),  # re-uploaded
        _cf_attachment("att2", "budget.csv", "x,y\n1,2\n", version=1),
    ]
    third = _drive(ConfluenceDocumentSource(ingestor=FakeConfluenceIngestor(bumped)), store)
    assert _extracted_ids(third) == {"confluence:ENG:att1"}


# ─────────────────────────────────────────────────────────────────────────────
# Composite source: lists across children, routes reads to the owner
# ─────────────────────────────────────────────────────────────────────────────
def test_composite_lists_and_routes_reads_across_sources():
    sp = SharePointDocumentSource(
        ingestor=FakeSharePointIngestor([_sp_file("f1", "sp.md", "sharepoint body", etag="v1")])
    )
    cf = ConfluenceDocumentSource(
        ingestor=FakeConfluenceIngestor([_cf_attachment("att1", "cf.md", "confluence body")])
    )
    composite = CompositeDocumentSource([sp, cf])

    records = _drive(composite, Store())
    assert _extracted_ids(records) == {"sharepoint:S-eng/b-docs:f1", "confluence:ENG:att1"}
    # Each file's content came from the correct child source (routing worked).
    by_id = {r["artifact_id"]: r for r in records}
    assert by_id["sharepoint:S-eng/b-docs:f1"]["content"] == "sharepoint body"
    assert by_id["confluence:ENG:att1"]["content"] == "confluence body"
    # A composite of delta-oriented sources never infers deletes.
    assert composite.reports_deletes is False


def test_composite_isolates_a_failing_source():
    """One source raising on list must not sink the others (degrade, don't crash)."""

    class Boom(SharePointDocumentSource):
        def list_documents(self, org_id):
            raise RuntimeError("connector down")

    ok = ConfluenceDocumentSource(
        ingestor=FakeConfluenceIngestor([_cf_attachment("att1", "cf.md", "still here")])
    )
    composite = CompositeDocumentSource([Boom(ingestor=FakeSharePointIngestor([])), ok])
    records = _drive(composite, Store())
    assert _extracted_ids(records) == {"confluence:ENG:att1"}
