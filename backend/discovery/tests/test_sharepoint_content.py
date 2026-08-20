"""
R18-A5 / AT-601 (T2) — tests for the SharePoint page-native content path.

T2 adds the SharePoint site-page + list-text CONTENT path: it renders modern site
pages and list text (page-native content only) to structure-preserving prose and
hands them to the R18-B1 retrieval substrate via ``ingest_content``. Binary library
files are OUT of scope here (they route to the R18-A1 document path).

These tests exercise the path offline (the deterministic fixture) WITHOUT a database
by injecting a fake substrate (``ingest_fn``) that captures what would be indexed, so
the rendering, granted-scope boundary, provenance, and page-vs-file separation are
all provable in isolation. They satisfy AC1 (site page ingested as structured text,
chunked on headings, retrievable with site/page-level provenance).

Run:
  python -m pytest discovery/tests/test_sharepoint_content.py
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import List, Optional

from app.provenance import OBSERVED
from app.retrieval import chunking
from app.retrieval.ingest import ArtifactIngestResult, ContentArtifact, IngestResult
from discovery.ingest.base import Checkpoint
from discovery.ingest.sharepoint_content import (
    CONNECTOR_ID,
    SOURCE_SYSTEM,
    SharePointContentIngestor,
    ingest_sharepoint_content,
    render_list_text,
    render_page_text,
)

ORG = "org_sp_content"

# Fixture identities (backend/discovery/ingest/fixtures/sharepoint_sample.json).
_ONBOARDING = "S-eng:page:pg-onboarding"
_DECISIONS = "S-eng:page:pg-decisions"
_RUNBOOKS_LIST = "S-eng:list:list-runbooks"
_UNGRANTED_PAGE = "S-secret:page:pg-board"
_UNGRANTED_LIST = "S-secret:list:list-exec"


# ─────────────────────────────────────────────────────────────────────────────
# In-memory checkpoint store + a capturing fake substrate (no DB)
# ─────────────────────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


class FakeSubstrate:
    """Stands in for ``retrieval.ingest_content`` — records every hand-off."""

    def __init__(self, *, fail: Optional[set] = None):
        self.artifacts: List[ContentArtifact] = []
        self._fail = set(fail or ())

    def __call__(self, org_id: str, artifacts) -> IngestResult:
        artifacts = list(artifacts)
        self.artifacts.extend(artifacts)
        result = IngestResult(org_id=org_id, artifacts_received=len(artifacts))
        for a in artifacts:
            if a.source_artifact in self._fail:
                result.artifacts_failed += 1
                result.artifacts.append(
                    ArtifactIngestResult(a.source_system, a.source_artifact, "failed", error="boom")
                )
            else:
                result.artifacts_indexed += 1
                result.chunks_indexed += 1
                result.artifacts.append(
                    ArtifactIngestResult(a.source_system, a.source_artifact, "indexed", chunks_indexed=1)
                )
        return result

    @property
    def by_id(self) -> dict:
        return {a.source_artifact: a for a in self.artifacts}

    @property
    def artifact_ids(self) -> set:
        return {a.source_artifact for a in self.artifacts}


def _run(store: Store, substrate: FakeSubstrate, **kw):
    return ingest_sharepoint_content(
        ORG,
        ingest_fn=substrate,
        read_checkpoint=store.read,
        save_checkpoint=store.save,
        **kw,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rendering: structure-preserving text (AC1)
# ─────────────────────────────────────────────────────────────────────────────
def test_render_page_text_preserves_heading_structure():
    page = {
        "id": "p1",
        "title": "Runbook",
        "canvasLayout": {
            "horizontalSections": [
                {"columns": [{"webparts": [
                    {"innerHtml": "<h2>Rollback</h2><p>Roll back with the deploy tool.</p>"},
                ]}]},
                {"columns": [{"webparts": [
                    {"innerHtml": "<h2>On-call</h2><p>Page the on-call engineer.</p>"},
                    {"innerHtml": ""},  # image/empty webpart → skipped
                ]}]},
            ]
        },
    }
    text = render_page_text(page)
    # Title leads as an H1; each section heading is preserved as Markdown ATX.
    assert text.startswith("# Runbook")
    assert "## Rollback" in text
    assert "## On-call" in text
    # No HTML survives the render.
    assert "<" not in text and ">" not in text
    assert "Roll back with the deploy tool." in text


def test_render_list_text_titles_are_headings_and_system_fields_dropped():
    list_obj = {
        "id": "l1",
        "displayName": "Runbooks",
        "items": [
            {"fields": {"Title": "Failover", "Description": "Promote the replica.",
                        "Modified": "2026-06-09T14:00:00Z", "Editor": "sys"}},
        ],
    }
    text = render_list_text(list_obj)
    assert text.startswith("# Runbooks")
    assert "## Failover" in text
    assert "Promote the replica." in text
    # System columns are not human-authored page-native text.
    assert "2026-06-09" not in text
    assert "sys" not in text


# ─────────────────────────────────────────────────────────────────────────────
# Hand-off: granted page/list content → the substrate (AC1)
# ─────────────────────────────────────────────────────────────────────────────
def test_granted_pages_and_lists_are_handed_to_the_substrate():
    store, sub = Store(), FakeSubstrate()
    result = _run(store, sub)

    assert result.ok
    assert result.first_run is True
    assert result.checkpoint_advanced is True
    # Every granted page + list is handed over — and nothing from the ungranted site.
    assert {_ONBOARDING, _DECISIONS, _RUNBOOKS_LIST} <= sub.artifact_ids


def test_every_artifact_is_sharepoint_prose_with_observed_deep_link_provenance():
    store, sub = Store(), FakeSubstrate()
    _run(store, sub)

    art = sub.by_id[_ONBOARDING]
    assert art.source_system == SOURCE_SYSTEM  # 'sharepoint'
    assert art.content_type == "prose"
    assert art.content and "# Engineering Onboarding" in art.content
    # Site/page-level provenance with a working deep link + observed spine (AC1/AC6).
    prov = art.provenance
    assert prov["origin"] == OBSERVED
    assert prov["site_id"] == "S-eng"
    assert prov["page_id"] == "pg-onboarding"
    assert prov["web_url"].endswith("engineering-onboarding.aspx")
    ep = prov["evidence_pointer"]
    assert ep["origin"] == OBSERVED
    assert ep["source_system"] == SOURCE_SYSTEM
    assert ep["source_artifact"] == _ONBOARDING
    # The page's own last-modified drives the timestamp (freshness/recency signal).
    assert art.source_timestamp == "2026-06-11T08:05:00Z"


def test_page_content_chunks_on_headings():
    """AC1 'chunked on headings': the rendered prose splits on its section headings.

    Runs the handed-over content through the SAME prose policy the substrate uses.
    The onboarding page is multi-section and long enough to split into more than one
    chunk, and every resulting chunk keeps a section heading — the boundary is the
    heading, not an arbitrary character offset.
    """
    store, sub = Store(), FakeSubstrate()
    _run(store, sub)
    content = sub.by_id[_ONBOARDING].content

    chunks = chunking.chunk_content(
        org_id=ORG,
        content=content,
        content_type="prose",
        source_system=SOURCE_SYSTEM,
        source_artifact=_ONBOARDING,
    )
    assert len(chunks) >= 2  # a multi-section page splits, it is not one blob
    # Each chunk carries at least one heading line — chunk boundaries are headings.
    for chunk in chunks:
        assert any(line.lstrip().startswith("#") for line in chunk.content.splitlines())


# ─────────────────────────────────────────────────────────────────────────────
# Granted-scope boundary (only granted sites read) + page-vs-file separation
# ─────────────────────────────────────────────────────────────────────────────
def test_ungranted_site_content_is_never_ingested():
    store, sub = Store(), FakeSubstrate()
    _run(store, sub)
    assert _UNGRANTED_PAGE not in sub.artifact_ids
    assert _UNGRANTED_LIST not in sub.artifact_ids
    # No artifact belongs to the ungranted site at all.
    assert not any(a.source_artifact.startswith("S-secret") for a in sub.artifacts)


def test_binary_library_files_are_not_ingested_by_the_content_path():
    """Library binary FILES are the R18-A1 document path — never this one.

    The reach fixture seeds driveItem files (roadmap.docx, budget.xlsx, …). The
    content path enumerates only pages + lists, so no driveItem file id (which use a
    '{site}/{drive}:{item}' shape, never ':page:'/':list:') is ever handed over.
    """
    store, sub = Store(), FakeSubstrate()
    _run(store, sub)
    for artifact_id in sub.artifact_ids:
        assert ":page:" in artifact_id or ":list:" in artifact_id
    # Concretely: a known library file id never appears.
    assert "S-eng/b-docs:f100" not in sub.artifact_ids


# ─────────────────────────────────────────────────────────────────────────────
# Incremental by checkpoint + at-least-once hand-off
# ─────────────────────────────────────────────────────────────────────────────
def test_unchanged_estate_hands_off_nothing_on_the_second_run():
    store = Store()
    first = _run(store, FakeSubstrate())
    assert first.artifacts_handed_off > 0

    second_sub = FakeSubstrate()
    second = _run(store, second_sub)
    assert second.ok
    assert second.first_run is False
    assert second_sub.artifacts == []  # nothing changed → nothing re-handed


def test_edited_page_is_re_ingested_on_the_next_run():
    store = Store()
    _run(store, FakeSubstrate())  # first load establishes the checkpoint

    # A stub source whose onboarding page was edited after the last run (newer
    # lastModifiedDateTime) — only that page should re-surface.
    class EditedIngestor(SharePointContentIngestor):
        def _raw_pages(self, org_id, site_id):
            out = []
            for page in super()._raw_pages(org_id, site_id):
                if page.get("id") == "pg-onboarding":
                    page = {**page, "lastModifiedDateTime": "2026-07-01T10:00:00Z"}
                out.append(page)
            return out

    sub = FakeSubstrate()
    result = ingest_sharepoint_content(
        ORG,
        ingestor=EditedIngestor(),
        ingest_fn=sub,
        read_checkpoint=store.read,
        save_checkpoint=store.save,
    )
    assert result.ok
    assert sub.artifact_ids == {_ONBOARDING}  # only the edited page re-handed


# ─────────────────────────────────────────────────────────────────────────────
# Deletion / archival propagation (R18-A5 AC3, second half)
# ─────────────────────────────────────────────────────────────────────────────
class FakeRemover:
    """Stands in for ``retrieval.remove_content`` — records every removal."""

    def __init__(self):
        self.removed: List[tuple] = []

    def __call__(self, org_id: str, pairs):
        pairs = list(pairs)
        self.removed.extend(pairs)
        return SimpleNamespace(
            artifacts_removed=len(pairs), chunks_removed=len(pairs)
        )


def _run_with_remover(store, substrate, remover, *, ingestor=None):
    return ingest_sharepoint_content(
        ORG,
        ingestor=ingestor,
        ingest_fn=substrate,
        remove_fn=remover,
        read_checkpoint=store.read,
        save_checkpoint=store.save,
    )


def test_deleted_page_is_tombstoned_and_its_chunks_removed():
    """A page that disappears from a site we can still read leaves retrieval."""
    store = Store()
    _run(store, FakeSubstrate())  # first load establishes the known-id set

    class MissingPageIngestor(SharePointContentIngestor):
        def _raw_pages(self, org_id, site_id):
            return [
                p for p in super()._raw_pages(org_id, site_id)
                if p.get("id") != "pg-onboarding"
            ]

    remover = FakeRemover()
    result = _run_with_remover(
        store, FakeSubstrate(), remover, ingestor=MissingPageIngestor()
    )

    assert result.ok
    assert remover.removed == [(SOURCE_SYSTEM, _ONBOARDING)]
    assert result.artifacts_removed == 1


def test_a_tombstoned_page_is_not_re_tombstoned_on_the_next_run():
    """The id is dropped from the checkpoint, so the deletion is reported once."""
    store = Store()
    _run(store, FakeSubstrate())

    class MissingPageIngestor(SharePointContentIngestor):
        def _raw_pages(self, org_id, site_id):
            return [
                p for p in super()._raw_pages(org_id, site_id)
                if p.get("id") != "pg-onboarding"
            ]

    _run_with_remover(store, FakeSubstrate(), FakeRemover(), ingestor=MissingPageIngestor())

    second = FakeRemover()
    _run_with_remover(store, FakeSubstrate(), second, ingestor=MissingPageIngestor())
    assert second.removed == []


def test_losing_a_sites_grant_tombstones_its_content_without_reading_it():
    """AC4 still holds on the deletion path: the ungranted site is never read."""
    store = Store()
    first = FakeSubstrate()
    _run(store, first)
    known = set(first.artifact_ids)
    assert known

    read_sites: List[str] = []

    class NoGrantIngestor(SharePointContentIngestor):
        def _accessible_sites(self, org_id):
            return []          # every grant withdrawn

        def _raw_pages(self, org_id, site_id):  # pragma: no cover — must not run
            read_sites.append(site_id)
            return super()._raw_pages(org_id, site_id)

    remover = FakeRemover()
    result = _run_with_remover(
        store, FakeSubstrate(), remover, ingestor=NoGrantIngestor()
    )

    assert result.ok
    assert {a for _s, a in remover.removed} == known
    assert read_sites == []    # the ungranted site's content was never fetched


def test_unchanged_estate_tombstones_nothing():
    """The negative control: deletion detection must not fire on a quiet run, or
    every stable page would be dropped from retrieval on every run."""
    store = Store()
    _run(store, FakeSubstrate())

    remover = FakeRemover()
    result = _run_with_remover(store, FakeSubstrate(), remover)

    assert result.ok
    assert remover.removed == []
    assert result.artifacts_removed == 0


def test_connector_declares_that_it_reports_deletes():
    """``reports_deletes`` is the contract's honesty flag — it must match reality."""
    assert SharePointContentIngestor.reports_deletes is True


def test_substrate_failure_does_not_advance_the_checkpoint():
    store = Store()
    # Fail the onboarding page hand-off — the run must not advance the checkpoint,
    # so a later clean run re-hands every granted artifact (idempotent replace).
    failing = FakeSubstrate(fail={_ONBOARDING})
    result = _run(store, failing)
    assert not result.ok
    assert result.checkpoint_advanced is False
    assert store.read(ORG, CONNECTOR_ID) is None

    retry = FakeSubstrate()
    _run(store, retry)
    assert {_ONBOARDING, _DECISIONS, _RUNBOOKS_LIST} <= retry.artifact_ids
