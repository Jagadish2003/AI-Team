"""R18-A5 / AT-604 (T5) — source-permission boundary RE-VERIFIED at content depth.

AC4: pages in ungranted (or archived) Confluence spaces / SharePoint sites are
NOT readable at depth — the R17-A2 reach-phase grant boundary holds for the
content path too. Reading a page BODY is more sensitive than reading activity, so
the boundary is re-verified at depth rather than assumed inherited from the reach
phase's earlier filter.

Two things are proven per connector:
  * a full run hands off ONLY granted spaces'/sites' content (ungranted/archived
    never appear); and
  * even a record for an ungranted/archived space handed DIRECTLY to the depth
    path is refused — its body is never fetched (the load-bearing re-verification).

Offline (deterministic fixtures), no database: a fake substrate captures the
hand-off; a spy ingestor records whether an ungranted page body was ever fetched.
"""
from __future__ import annotations

from typing import List, Optional

import pytest

from app.retrieval.ingest import ArtifactIngestResult, ContentArtifact, IngestResult
from discovery.ingest.base import Checkpoint
from discovery.ingest.confluence import ConfluenceIngestor
from discovery.ingest.confluence_content import (
    accessible_space_keys,
    ingest_confluence_content,
)
from discovery.ingest.sharepoint_content import (
    SharePointContentIngestor,
    ingest_sharepoint_content,
)

CONF_ORG = "org_r18a5_t5_confluence"
SP_ORG = "org_r18a5_t5_sharepoint"

# Fixture identities (backend/discovery/ingest/fixtures/*).
_SP_GRANTED = {"S-eng:page:pg-onboarding", "S-eng:page:pg-decisions", "S-eng:list:list-runbooks"}
_SP_UNGRANTED = {"S-secret:page:pg-board", "S-secret:list:list-exec"}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles (no database) — mirror the T1/T2 content-test fakes.
# ─────────────────────────────────────────────────────────────────────────────
class _Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


class _FakeSubstrate:
    """Captures every artifact handed to the substrate (stands in for ingest_content)."""

    def __init__(self):
        self.artifacts: List[ContentArtifact] = []

    def __call__(self, org_id: str, artifacts) -> IngestResult:
        artifacts = list(artifacts)
        self.artifacts.extend(artifacts)
        result = IngestResult(org_id=org_id, artifacts_received=len(artifacts))
        for a in artifacts:
            result.artifacts_indexed += 1
            result.chunks_indexed += 1
            result.artifacts.append(
                ArtifactIngestResult(a.source_system, a.source_artifact, "indexed", chunks_indexed=1)
            )
        return result

    @property
    def artifact_ids(self) -> set:
        return {a.source_artifact for a in self.artifacts}


class _SpyConfluence(ConfluenceIngestor):
    """Records every page-body fetch so a test can prove an ungranted body is
    NEVER fetched at depth (not merely filtered after the fact)."""

    def __init__(self):
        super().__init__()
        self.body_fetches: List[tuple] = []

    def _raw_page_body(self, org_id, space_key, content_id):  # type: ignore[override]
        self.body_fetches.append((space_key, content_id))
        return super()._raw_page_body(org_id, space_key, content_id)


def _all_confluence_records(ing: ConfluenceIngestor) -> List[dict]:
    return [r for b in ing.ingest_changes(CONF_ORG, None) for r in b.records]


def _ungranted_like(sample: dict, *, space_key: str, content_id: str) -> dict:
    """A valid PAGE_CONTENT record shaped exactly like a real granted one, but
    pointed at an ungranted/archived space — the injected-attacker case."""
    rec = dict(sample)
    rec.update(
        space_key=space_key,
        content_id=content_id,
        artifact_id=f"{space_key}:{content_id}",
        space_name=space_key,
    )
    return rec


# ═════════════════════════════════════════════════════════════════════════════
# Confluence — AC4 at depth
# ═════════════════════════════════════════════════════════════════════════════
def test_confluence_full_run_hands_off_only_granted_spaces():
    ing = ConfluenceIngestor()
    records = _all_confluence_records(ing)
    sub = _FakeSubstrate()

    result = ingest_confluence_content(CONF_ORG, records, ingestor=ing, ingest_fn=sub)

    # Only granted, non-archived spaces (ENG/OPS) are ever handed off…
    assert sub.artifact_ids
    assert all(a.source_artifact.split(":")[0] in {"ENG", "OPS"} for a in sub.artifacts)
    # …and nothing from the ungranted (HR) or archived (OLD) spaces.
    assert not any(a.source_artifact.startswith("HR:") for a in sub.artifacts)
    assert not any(a.source_artifact.startswith("OLD:") for a in sub.artifacts)
    # Normal flow: the reach phase already excluded them, so nothing is refused here.
    assert result.pages_ungranted_skipped == 0


def test_confluence_depth_refuses_injected_ungranted_space_body_never_fetched():
    """The load-bearing re-verification: a record for an UNGRANTED space handed
    straight to the depth path is refused and its body is never fetched."""
    spy = _SpyConfluence()
    granted_sample = next(
        r for r in _all_confluence_records(spy) if r["artifact_id"].startswith("ENG:")
    )
    ungranted = _ungranted_like(granted_sample, space_key="HR", content_id="999")
    sub = _FakeSubstrate()

    result = ingest_confluence_content(CONF_ORG, [ungranted], ingestor=spy, ingest_fn=sub)

    assert result.pages_ungranted_skipped == 1
    assert result.pages_seen == 0
    assert result.artifacts_handed_off == 0
    assert "HR:999" not in sub.artifact_ids
    # The body of the ungranted page was NEVER fetched — the boundary held before
    # any read, not after.
    assert ("HR", "999") not in spy.body_fetches


def test_confluence_depth_refuses_archived_space_record():
    """Archived spaces are excluded from the granted set too — a record for one
    is refused at depth exactly like an ungranted space."""
    spy = _SpyConfluence()
    sample = next(
        r for r in _all_confluence_records(spy) if r["artifact_id"].startswith("ENG:")
    )
    archived = _ungranted_like(sample, space_key="OLD", content_id="998")
    sub = _FakeSubstrate()

    result = ingest_confluence_content(CONF_ORG, [archived], ingestor=spy, ingest_fn=sub)

    assert result.pages_ungranted_skipped == 1
    assert "OLD:998" not in sub.artifact_ids
    assert ("OLD", "998") not in spy.body_fetches


def test_confluence_depth_still_ingests_a_granted_record():
    """Positive control: a granted-space record IS fetched and handed off — the
    guard refuses only ungranted spaces, never legitimate content."""
    spy = _SpyConfluence()
    sample = next(
        r for r in _all_confluence_records(spy) if r["artifact_id"].startswith("ENG:")
    )
    sub = _FakeSubstrate()

    result = ingest_confluence_content(CONF_ORG, [sample], ingestor=spy, ingest_fn=sub)

    assert result.pages_ungranted_skipped == 0
    assert sample["artifact_id"] in sub.artifact_ids
    assert (sample["space_key"], sample["content_id"]) in spy.body_fetches


def test_confluence_accessible_space_keys_excludes_ungranted_and_archived():
    keys = accessible_space_keys(ConfluenceIngestor(), CONF_ORG)
    assert keys == {"ENG", "OPS"}
    assert "HR" not in keys   # ungranted
    assert "OLD" not in keys  # archived


# ═════════════════════════════════════════════════════════════════════════════
# SharePoint — AC4 at depth (content path re-verifies via _accessible_sites)
# ═════════════════════════════════════════════════════════════════════════════
def test_sharepoint_full_run_hands_off_only_granted_sites():
    store, sub = _Store(), _FakeSubstrate()
    ingest_sharepoint_content(
        SP_ORG, ingest_fn=sub, read_checkpoint=store.read, save_checkpoint=store.save
    )
    ids = sub.artifact_ids
    assert _SP_GRANTED <= ids                       # granted site content ingested
    assert not (ids & _SP_UNGRANTED)                # ungranted site content never
    assert not any(a.source_artifact.startswith("S-secret") for a in sub.artifacts)


def test_sharepoint_content_accessible_sites_excludes_ungranted():
    sites = {s.get("id") for s in SharePointContentIngestor()._accessible_sites(SP_ORG)}
    assert "S-eng" in sites
    assert "S-secret" not in sites  # ungranted site never enumerated at depth
