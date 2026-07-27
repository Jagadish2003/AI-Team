"""
R18-A5 / AT-600 (T1) — Confluence page/blogpost DEEP CONTENT path.

Covers the acceptance criterion this subtask satisfies plus the boundaries it
must uphold (Section 3 of R18-A5, scoped to Confluence — SharePoint is T2):

  AC1 — A Confluence page is ingested as structured text, chunked on headings,
        and retrievable with page-level provenance.
  AC4 — Pages in ungranted/archived spaces are not readable at depth (the
        reach-phase permission boundary — R17-A2 — holds for content too).
  AC5 — Incremental runs ingest only pages changed since the checkpoint.
  AC6 — A retrieved page chunk's provenance resolves to a working deep link
        (the page's ``url``) plus the full R16-B1 EvidencePointer spine.

These tests exercise the render + hand-off WITHOUT a database by injecting a
fake substrate (``ingest_fn``) that captures what would be indexed — mirrors
``test_documents_handoff.py``'s pattern. Real end-to-end chunking through the
actual substrate chunker (proving AC1's "chunked on headings" claim) uses the
real ``app.retrieval.ingest.build_records`` (pure, no DB).
"""
from __future__ import annotations

from typing import List, Optional

import pytest

from app.provenance import EvidencePointer
from app.retrieval.ingest import (
    ArtifactIngestResult,
    ContentArtifact,
    IngestResult,
    build_records,
)
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.confluence import ConfluenceIngestor
from discovery.ingest.content_router import ContentRoute, classify_confluence_content
from discovery.ingest.confluence_content import (
    RETRIEVAL_SOURCE_SYSTEM,
    build_content_artifact,
    content_artifacts,
    ingest_confluence_content,
    render_page_text,
)

ORG = "org_confluence_content"


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


# ─────────────────────────────────────────────────────────────────────────────
# In-memory checkpoint store + a capturing fake substrate (mirrors
# test_documents_handoff.py's pattern).
# ─────────────────────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


class FakeSubstrate:
    def __init__(self, *, fail: Optional[set] = None):
        self.calls: List[tuple] = []
        self.artifacts: List[ContentArtifact] = []
        self._fail = set(fail or ())

    def __call__(self, org_id: str, artifacts) -> IngestResult:
        artifacts = list(artifacts)
        self.calls.append((org_id, artifacts))
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
    def artifact_ids(self) -> set:
        return {a.source_artifact for a in self.artifacts}


def _all_records(ingestor: Optional[ConfluenceIngestor] = None) -> List[dict]:
    ing = ingestor or ConfluenceIngestor()
    return [r for b in ing.ingest_changes(ORG, None) for r in b.records]


# ─────────────────────────────────────────────────────────────────────────────
# render_page_text — structure-preserving rendering
# ─────────────────────────────────────────────────────────────────────────────
def test_render_preserves_heading_structure():
    html = (
        "<h1>Payments API Runbook</h1>"
        "<p>This runbook covers on-call response.</p>"
        "<h2>Rollback Steps</h2>"
        "<ol><li>Disable canary.</li><li>Revert traffic.</li></ol>"
    )
    text, headings = render_page_text(html)
    assert text.startswith("# Payments API Runbook")
    assert "## Rollback Steps" in text
    assert "Disable canary." in text
    assert headings == [
        {"level": 1, "text": "Payments API Runbook", "position": 0},
        {"level": 2, "text": "Rollback Steps", "position": 2},
    ]


def test_render_list_item_with_paragraph_keeps_bullet_text_together():
    html = "<ul><li><p>Disable canary.</p></li><li><p>Revert traffic.</p></li></ul>"
    text, _ = render_page_text(html)
    assert "- Disable canary." in text
    assert "- Revert traffic." in text
    assert "\n\n-\n\nDisable canary." not in text


def test_render_tables_join_cells():
    html = "<h1>Overview</h1><table><tr><th>Service</th><th>Owner</th></tr><tr><td>ingest</td><td>Ada</td></tr></table>"
    text, _ = render_page_text(html)
    assert "Service | Owner" in text
    assert "ingest | Ada" in text


def test_render_macro_body_kept_parameters_dropped():
    html = (
        '<ac:structured-macro ac:name="info">'
        '<ac:parameter ac:name="title">Heads up</ac:parameter>'
        "<ac:rich-text-body><p>Read this before deploying.</p></ac:rich-text-body>"
        "</ac:structured-macro>"
    )
    text, _ = render_page_text(html)
    assert "Read this before deploying." in text
    assert "Heads up" not in text  # macro parameter/config, not body content


def test_render_empty_and_none_is_truthful_empty():
    assert render_page_text(None) == ("", [])
    assert render_page_text("") == ("", [])
    assert render_page_text("   ") == ("", [])


def test_render_malformed_fragment_never_raises():
    # Unbalanced/garbage markup must never raise — HTMLParser tolerates it (or,
    # for input it cannot parse at all, render_page_text falls back to
    # tag-stripped text) — either way the caller gets text back, never a crash.
    text, _headings = render_page_text("<h1>Oops<p>unterminated tags <div")
    assert "Oops" in text


def test_render_falls_back_to_tag_stripped_text_on_parser_failure(monkeypatch):
    """A genuinely unparseable fragment degrades to tag-stripped plain text
    rather than raising out of the hand-off (one bad page must never sink a
    batch)."""
    import discovery.ingest.confluence_content as mod

    def _boom(self, data):
        raise ValueError("simulated parser failure")

    monkeypatch.setattr(mod._StorageTextRenderer, "feed", _boom)
    text, headings = render_page_text("<h1>Title</h1><p>Body text</p>")
    assert "Title" in text
    assert "Body text" in text
    assert headings == []


# ─────────────────────────────────────────────────────────────────────────────
# build_content_artifact / content_artifacts — record -> ContentArtifact
# ─────────────────────────────────────────────────────────────────────────────
def test_build_content_artifact_carries_prose_and_provenance():
    ing = ConfluenceIngestor()
    records = _all_records(ing)
    rec = next(r for r in records if r["artifact_id"] == "ENG:200")

    art = build_content_artifact(ing, ORG, rec)
    assert art is not None
    assert art.source_system == RETRIEVAL_SOURCE_SYSTEM == "confluence"
    assert art.source_artifact == "ENG:200"
    assert art.content_type == "prose"
    assert art.content.startswith("# Incident INC-4821 postmortem")

    prov = art.provenance
    assert prov["origin"] == "observed"
    assert prov["space_key"] == "ENG"
    assert prov["space_name"] == "Engineering"
    assert prov["title"] == "Incident INC-4821 postmortem"
    assert prov["labels"] == ["postmortem"]
    # AC6: deep link back to the source page.
    assert prov["url"] == "/spaces/ENG/pages/200"
    assert prov["heading_map"]  # structure hints carried alongside the text


def test_evidence_pointer_is_valid_and_observed():
    ing = ConfluenceIngestor()
    rec = next(r for r in _all_records(ing) if r["artifact_id"] == "ENG:100")
    art = build_content_artifact(ing, ORG, rec)
    ptr = EvidencePointer.from_dict(art.provenance["evidence_pointer"])
    assert ptr.is_valid()
    assert ptr.origin == "observed"
    assert ptr.source_system == "confluence"
    assert ptr.source_artifact == "ENG:100"


def test_non_page_blogpost_records_are_skipped():
    ing = ConfluenceIngestor()
    comment_record = {
        "artifact_id": "ENG:450",
        "content_type": "comment",
        "space_key": "ENG",
        "content_id": "450",
    }
    assert build_content_artifact(ing, ORG, comment_record) is None
    assert content_artifacts(ing, ORG, [comment_record]) == []


def test_missing_ids_are_skipped_not_raised(caplog):
    import logging

    ing = ConfluenceIngestor()
    bad = {"artifact_id": "x", "content_type": "page"}  # no space_key/content_id
    with caplog.at_level(logging.WARNING):
        assert build_content_artifact(ing, ORG, bad) is None
    assert "missing identity" in caplog.text
    assert "space_key" in caplog.text
    assert "content_id" in caplog.text


def test_a_page_with_no_body_fixture_hands_off_truthful_empty_content():
    # A page/blogpost id with no "bodies" fixture entry degrades to an empty
    # (not a crash) — the substrate records it as a truthful empty hand-off.
    ing = ConfluenceIngestor()
    rec = {
        "artifact_id": "OPS:999",
        "content_type": "page",
        "space_key": "OPS",
        "content_id": "999",
        "title": "No body page",
        "modified_at": "2026-06-10T09:00:00Z",
    }
    art = build_content_artifact(ing, ORG, rec)
    assert art is not None
    assert art.content == ""


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — chunked on headings (the real substrate chunker, no DB required)
# ─────────────────────────────────────────────────────────────────────────────
def test_chunking_splits_on_headings_and_carries_provenance():
    ing = ConfluenceIngestor()
    rec = next(r for r in _all_records(ing) if r["artifact_id"] == "ENG:400")
    art = build_content_artifact(ing, ORG, rec)

    records = build_records(ORG, art)
    assert records  # at least one chunk written
    for r in records:
        assert r.source_system == "confluence"
        assert r.source_artifact == "ENG:400"
        assert r.content_type == "prose"
    # The heading text from the rendered body is present in the chunked content —
    # proof the ATX-style rendering round-trips through the real chunker.
    joined = "\n".join(r.content for r in records)
    assert "# Architecture overview" in joined
    assert "## Services" in joined


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — permission boundary holds at depth (ungranted/archived spaces)
# ─────────────────────────────────────────────────────────────────────────────
def test_ungranted_and_archived_spaces_never_reach_depth():
    ing = ConfluenceIngestor()
    records = _all_records(ing)
    # The reach-phase listing already excludes HR (ungranted) / OLD (archived) —
    # assert the depth path never even sees, let alone fetches, their content.
    assert not any(r["artifact_id"].startswith("HR:") for r in records)
    assert not any(r["artifact_id"].startswith("OLD:") for r in records)

    artifacts = content_artifacts(ing, ORG, records)
    assert not any(a.source_artifact.startswith("HR:") for a in artifacts)
    assert not any(a.source_artifact.startswith("OLD:") for a in artifacts)


# ─────────────────────────────────────────────────────────────────────────────
# ingest_confluence_content — the hand-off, end to end (fake substrate)
# ─────────────────────────────────────────────────────────────────────────────
def test_ingest_confluence_content_hands_off_every_changed_page():
    ing = ConfluenceIngestor()
    records = _all_records(ing)
    substrate = FakeSubstrate()

    result = ingest_confluence_content(ORG, records, ingestor=ing, ingest_fn=substrate)

    expected_ids = {"ENG:100", "ENG:200", "ENG:300", "ENG:400", "OPS:500", "OPS:600"}
    assert substrate.artifact_ids == expected_ids
    assert result.pages_seen == len(expected_ids)
    assert result.artifacts_handed_off == len(expected_ids)
    assert result.artifacts_indexed == len(expected_ids)
    assert result.artifacts_failed == 0
    assert all(a.source_system == "confluence" for a in substrate.artifacts)


def test_ingest_confluence_content_ignores_non_page_records():
    ing = ConfluenceIngestor()
    substrate = FakeSubstrate()
    records = [
        {"artifact_id": "ENG:450", "content_type": "comment", "space_key": "ENG", "content_id": "450"},
    ]
    result = ingest_confluence_content(ORG, records, ingestor=ing, ingest_fn=substrate)
    assert result.pages_seen == 0
    assert substrate.calls == []


def test_ingest_confluence_content_counts_missing_identity_separately():
    ing = ConfluenceIngestor()
    substrate = FakeSubstrate()
    records = [
        {"artifact_id": "ENG:missing", "content_type": "page", "space_key": "ENG"},
    ]
    result = ingest_confluence_content(ORG, records, ingestor=ing, ingest_fn=substrate)
    assert result.pages_seen == 0
    assert result.pages_identity_missing == 1
    assert result.pages_render_failed == 0
    assert substrate.calls == []


def test_ingest_confluence_content_never_raises_on_substrate_failure(caplog):
    ing = ConfluenceIngestor()
    records = _all_records(ing)

    def _boom(org_id, artifacts):
        raise RuntimeError("store unavailable")

    result = ingest_confluence_content(ORG, records, ingestor=ing, ingest_fn=_boom)
    assert result.artifacts_handed_off == 0  # nothing recorded — hand-off never completed
    expected_pages = [
        r for r in records
        if classify_confluence_content(r.get("content_type")) == ContentRoute.PAGE_CONTENT
    ]
    assert result.pages_seen == len(expected_pages)


def test_ingest_confluence_content_isolates_a_bad_page(monkeypatch):
    """One page whose body fetch blows up must not sink the rest of the batch."""
    ing = ConfluenceIngestor()
    records = _all_records(ing)
    substrate = FakeSubstrate()

    real = ing._raw_page_body

    def _flaky(org_id, space_key, content_id):
        if content_id == "200":
            raise RuntimeError("simulated fetch failure")
        return real(org_id, space_key, content_id)

    monkeypatch.setattr(ing, "_raw_page_body", _flaky)
    result = ingest_confluence_content(ORG, records, ingestor=ing, ingest_fn=substrate)

    assert "ENG:200" not in substrate.artifact_ids
    assert result.pages_render_failed == 1
    # The other five pages still made it through.
    assert result.artifacts_handed_off == 5


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — incremental runs ingest only pages changed since the checkpoint
# ─────────────────────────────────────────────────────────────────────────────
def test_incremental_run_hands_off_only_newly_changed_pages():
    store = Store()
    ing = ConfluenceIngestor()

    # First run: full load, drive through the real checkpoint lifecycle.
    first_collected: List[dict] = []
    change_runner.ingest_with_checkpoint(
        ing,
        ORG,
        process_batch=lambda batch: first_collected.extend(batch.records),
        read_checkpoint=store.read,
        save_checkpoint=store.save,
    )
    first_substrate = FakeSubstrate()
    ingest_confluence_content(ORG, first_collected, ingestor=ing, ingest_fn=first_substrate)
    assert first_substrate.artifact_ids == {
        "ENG:100", "ENG:200", "ENG:300", "ENG:400", "OPS:500", "OPS:600",
    }

    # Second run over the SAME unchanged fixture: no new deltas, so nothing new
    # is handed to the substrate (AC5 rides the ingestor's own checkpoint).
    second_collected: List[dict] = []
    change_runner.ingest_with_checkpoint(
        ConfluenceIngestor(),
        ORG,
        process_batch=lambda batch: second_collected.extend(batch.records),
        read_checkpoint=store.read,
        save_checkpoint=store.save,
    )
    second_substrate = FakeSubstrate()
    result = ingest_confluence_content(
        ORG, second_collected, ingestor=ing, ingest_fn=second_substrate
    )
    assert second_collected == []
    assert second_substrate.calls == []
    assert result.pages_seen == 0
