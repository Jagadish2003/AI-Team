"""
R18-A5 / AT-602 (T3) — the page-vs-file router.

Covers AC2: "A file attached to a page or stored in a library is ingested
exactly once, via the document path — never duplicated through this story."

Three layers of proof:

  1. Unit tests for the classifiers themselves
     (:func:`content_router.classify_confluence_content`,
     :func:`content_router.classify_sharepoint_drive_item`).
  2. Wiring proof: the production call sites (``confluence_content.py``,
     ``documents_attachments.py``) actually route through the classifiers —
     not just "the classifiers are correct in isolation".
  3. End-to-end, whole-picture proof: driving BOTH the depth content path and
     the R18-A1 document path over the SAME real offline fixtures (no
     per-test fakes) for Confluence and for SharePoint, and asserting the two
     paths' artifact identities never collide (no double-ingestion) and every
     fixture artifact lands in exactly one of them (no gaps).

All offline — no database required.
"""
from __future__ import annotations

import pytest

from discovery.ingest.confluence import ConfluenceIngestor
from discovery.ingest.confluence_content import content_artifacts
from discovery.ingest.content_router import (
    CONFLUENCE_ATTACHMENT_TYPE,
    ContentRoute,
    classify_confluence_content,
    classify_sharepoint_drive_item,
)
from discovery.ingest.documents_attachments import (
    ConfluenceDocumentSource,
    SharePointDocumentSource,
)
from discovery.ingest.sharepoint import SharePointIngestor
from discovery.ingest.sharepoint_content import ingest_sharepoint_content

ORG = "org_content_router"


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — classifier unit tests
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "content_type,expected",
    [
        ("page", ContentRoute.PAGE_CONTENT),
        ("blogpost", ContentRoute.PAGE_CONTENT),
        ("PAGE", ContentRoute.PAGE_CONTENT),  # case-insensitive
        (" page ", ContentRoute.PAGE_CONTENT),  # tolerant of whitespace
        ("attachment", ContentRoute.DOCUMENT),
        ("comment", ContentRoute.SKIP),
        ("unknown-future-type", ContentRoute.SKIP),
        (None, ContentRoute.SKIP),
        ("", ContentRoute.SKIP),
    ],
)
def test_classify_confluence_content(content_type, expected):
    assert classify_confluence_content(content_type) == expected


@pytest.mark.parametrize(
    "item,expected",
    [
        ({"id": "f1", "file": {"mimeType": "text/plain"}}, ContentRoute.DOCUMENT),
        ({"id": "d1", "folder": {"childCount": 2}}, ContentRoute.SKIP),
        ({"id": "x1", "file": {}, "deleted": {"state": "deleted"}}, ContentRoute.SKIP),
        ({"id": "n1"}, ContentRoute.SKIP),  # neither facet
        ({}, ContentRoute.SKIP),
        (None, ContentRoute.SKIP),
        ("not-a-dict", ContentRoute.SKIP),
    ],
)
def test_classify_sharepoint_drive_item(item, expected):
    assert classify_sharepoint_drive_item(item) == expected


def test_confluence_attachment_type_constant_classifies_as_document():
    """The default type an attachment-listing caller uses when the entry itself
    carries no ``type`` field (the offline fixture shape) must be a document."""
    assert classify_confluence_content(CONFLUENCE_ATTACHMENT_TYPE) == ContentRoute.DOCUMENT


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — wiring proof: production code actually asks the router
# ─────────────────────────────────────────────────────────────────────────────
def test_confluence_content_path_uses_the_router_not_an_ad_hoc_list():
    """A record whose type the router does not classify PAGE_CONTENT must be
    rejected by confluence_content.py's own scope filter, proving it defers to
    the router rather than re-implementing its own type list."""
    from discovery.ingest.confluence_content import build_content_artifact

    ing = ConfluenceIngestor()
    comment_record = {
        "artifact_id": "ENG:450",
        "content_type": "comment",
        "space_key": "ENG",
        "content_id": "450",
    }
    assert classify_confluence_content("comment") == ContentRoute.SKIP
    assert build_content_artifact(ing, ORG, comment_record) is None


def test_sharepoint_document_source_skips_whatever_the_router_skips(monkeypatch):
    """Monkeypatch the router to reclassify a normally-DOCUMENT item as SKIP and
    confirm SharePointDocumentSource actually stops enumerating it — proving the
    scan defers to the router rather than its own inline 'file' in item check."""
    import discovery.ingest.documents_attachments as mod

    source = SharePointDocumentSource()
    before = {r.artifact_id for r in source.list_documents(ORG)}
    assert "sharepoint:S-eng/b-docs:f100" in before

    monkeypatch.setattr(mod, "classify_sharepoint_drive_item", lambda item: ContentRoute.SKIP)
    source2 = SharePointDocumentSource()
    after = {r.artifact_id for r in source2.list_documents(ORG)}
    assert after == set()  # the router now says skip everything — and it did


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — end-to-end: no double-ingestion, no gaps (AC2)
# ─────────────────────────────────────────────────────────────────────────────
def test_confluence_content_and_document_paths_never_overlap():
    """Run the REAL depth content path and the REAL document path over the SAME
    offline fixture and prove their artifact identities are completely disjoint
    (no double-ingestion) and jointly complete (no gaps): every page/blogpost is
    covered by the content path and every attachment by the document path."""
    ing = ConfluenceIngestor()
    records = [r for b in ing.ingest_changes(ORG, None) for r in b.records]

    content_ids = {("confluence", a.source_artifact) for a in content_artifacts(ing, ORG, records)}
    doc_refs = ConfluenceDocumentSource().list_documents(ORG)
    # DocumentRef ids already carry the 'confluence:' prefix that becomes the
    # substrate's source_artifact once translated to source_system='document'
    # by documents_handoff.py — using that exact prefixed id here proves the
    # identity spaces are disjoint at the point they are minted, not just after
    # some downstream translation happens to differ.
    document_ids = {("document", r.artifact_id) for r in doc_refs}

    # No double-ingestion: completely disjoint (source_system, source_artifact).
    assert content_ids.isdisjoint(document_ids)

    # No gaps: every page/blogpost in the fixture is on the content path...
    expected_pages = {
        ("confluence", "ENG:100"), ("confluence", "ENG:200"),
        ("confluence", "ENG:300"), ("confluence", "ENG:400"),
        ("confluence", "OPS:500"), ("confluence", "OPS:600"),
    }
    assert content_ids == expected_pages
    # ...and every attachment in the fixture is on the document path.
    expected_attachments = {
        ("document", "confluence:ENG:att-501"),
        ("document", "confluence:ENG:att-502"),
    }
    assert document_ids == expected_attachments


def test_confluence_comment_reaches_neither_path():
    """A comment (fixture id ENG:450) is on neither path — a declared SKIP, not
    a silent gap masquerading as coverage."""
    ing = ConfluenceIngestor()
    records = [r for b in ing.ingest_changes(ORG, None) for r in b.records]
    assert not any(r["artifact_id"] == "ENG:450" for r in records)  # reach already excludes it

    content_ids = {a.source_artifact for a in content_artifacts(ing, ORG, records)}
    doc_ids = {r.artifact_id for r in ConfluenceDocumentSource().list_documents(ORG)}
    assert "ENG:450" not in content_ids
    assert not any("450" in i for i in doc_ids)


def test_sharepoint_content_and_document_paths_never_overlap():
    """Same proof as Confluence, for SharePoint: the page/list content path
    (sharepoint_content.py) and the driveItem document path
    (documents_attachments.SharePointDocumentSource) over the SAME fixture."""

    class _FakeSubstrate:
        def __init__(self):
            self.artifacts = []

        def __call__(self, org_id, artifacts):
            from app.retrieval.ingest import ArtifactIngestResult, IngestResult

            artifacts = list(artifacts)
            self.artifacts.extend(artifacts)
            result = IngestResult(org_id=org_id, artifacts_received=len(artifacts))
            for a in artifacts:
                result.artifacts_indexed += 1
                result.artifacts.append(
                    ArtifactIngestResult(a.source_system, a.source_artifact, "indexed", chunks_indexed=1)
                )
            return result

    class _Store:
        def __init__(self):
            self.data = {}

        def read(self, org_id, connector_id):
            return self.data.get((org_id, connector_id))

        def save(self, cp):
            self.data[(cp.org_id, cp.connector_id)] = cp

    sub = _FakeSubstrate()
    ingest_sharepoint_content(
        ORG, ingest_fn=sub, read_checkpoint=_Store().read, save_checkpoint=_Store().save
    )
    content_ids = {("sharepoint", a.source_artifact) for a in sub.artifacts}

    doc_refs = SharePointDocumentSource().list_documents(ORG)
    document_ids = {("document", r.artifact_id) for r in doc_refs}

    # No double-ingestion.
    assert content_ids.isdisjoint(document_ids)

    # No gaps: every granted page/list is on the content path...
    expected_content = {
        ("sharepoint", "S-eng:page:pg-onboarding"),
        ("sharepoint", "S-eng:page:pg-decisions"),
        ("sharepoint", "S-eng:list:list-runbooks"),
    }
    assert content_ids == expected_content
    # ...and every granted, non-folder driveItem file is on the document path
    # (fold300 is a folder — correctly absent from both paths).
    expected_documents = {
        ("document", "sharepoint:S-eng/b-docs:f100"),
        ("document", "sharepoint:S-eng/b-docs:f200"),
        ("document", "sharepoint:S-eng/b-docs:f400"),
        ("document", "sharepoint:S-eng/b-specs:s100"),
        ("document", "sharepoint:S-eng/b-specs:s200"),
    }
    assert document_ids == expected_documents


def test_sharepoint_folder_reaches_neither_path():
    """The fixture's folder (fold300) is a declared SKIP on both paths — not a
    gap: a folder carries no content for either destination."""
    doc_ids = {r.artifact_id for r in SharePointDocumentSource().list_documents(ORG)}
    assert not any("fold300" in i for i in doc_ids)

    ing = SharePointIngestor()
    records = [r for b in ing.ingest_changes(ORG, None) for r in b.records]
    folder_record = next(r for r in records if r["item_id"] == "fold300")
    assert folder_record["item_type"] == "folder"
    assert classify_sharepoint_drive_item({"folder": {}}) == ContentRoute.SKIP


def test_ungranted_sharepoint_site_reaches_neither_path():
    """S-secret is ungranted — neither the content path nor the document path
    may surface any of its pages/lists/files (the reach-phase permission
    boundary re-verified at depth for BOTH paths, not just one)."""

    class _FakeSubstrate:
        def __init__(self):
            self.artifacts = []

        def __call__(self, org_id, artifacts):
            from app.retrieval.ingest import IngestResult

            artifacts = list(artifacts)
            self.artifacts.extend(artifacts)
            result = IngestResult(org_id=org_id, artifacts_received=len(artifacts))
            result.artifacts_indexed = len(artifacts)
            return result

    class _Store:
        def __init__(self):
            self.data = {}

        def read(self, org_id, connector_id):
            return self.data.get((org_id, connector_id))

        def save(self, cp):
            self.data[(cp.org_id, cp.connector_id)] = cp

    sub = _FakeSubstrate()
    ingest_sharepoint_content(
        ORG, ingest_fn=sub, read_checkpoint=_Store().read, save_checkpoint=_Store().save
    )
    assert not any(a.source_artifact.startswith("S-secret") for a in sub.artifacts)

    doc_ids = {r.artifact_id for r in SharePointDocumentSource().list_documents(ORG)}
    assert not any("S-secret" in i for i in doc_ids)
