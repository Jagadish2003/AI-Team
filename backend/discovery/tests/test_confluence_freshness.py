"""
R18-A5 / AT-603 (T4) — edit/delete/archive propagation through freshness.

Covers:

  AC3 — editing a page refreshes its chunks; deleting or archiving a page
        removes its content from retrieval immediately.
  AC5 — incremental runs ingest only pages changed since the checkpoint
        (deletion detection must not regress this for unrelated pages).

Three layers, offline (no database required):

  1. ``confluence.py``'s known-id diff: a page that vanishes from a space's
     listing, or whose ``status`` flips to trashed/archived, is emitted as a
     ``change_kind='deleted'`` tombstone; a whole space losing its grant
     tombstones everything previously known for it.
  2. ``confluence_content.py``'s hand-off: a deletion record is routed to
     ``retrieval.ingest.remove_content`` (a fake substrate captures the call),
     never to the body-rendering path.
  3. End-to-end through the REAL change runner + the REAL freshness subscriber
     (with the store layer faked, so no database is needed): a deleted page's
     ``ingestion.artifact_changed`` event reaches ``store.purge_artifact``.
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from discovery.ingest import change_runner
from discovery.ingest.base import ChangeKind, Checkpoint
from discovery.ingest.confluence import ConfluenceIngestor
from discovery.ingest.confluence_content import ingest_confluence_content

ORG = "org_confluence_freshness"


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _drive(ingestor, org_id, store: Store) -> List[dict]:
    collected: List[dict] = []
    change_runner.ingest_with_checkpoint(
        ingestor,
        org_id,
        process_batch=lambda batch: collected.extend(batch.records),
        read_checkpoint=store.read,
        save_checkpoint=store.save,
    )
    return collected


class MutableFixtureIngestor(ConfluenceIngestor):
    """A ConfluenceIngestor whose per-space content list can be edited between
    runs, so a test can simulate a page vanishing / its status flipping without
    touching the real on-disk fixture."""

    def __init__(self, content: Dict[str, List[dict]], spaces=None, **kw):
        super().__init__(**kw)
        self._content = content
        self._spaces_override = spaces

    def _raw_spaces(self, org_id):
        if self._spaces_override is not None:
            return self._spaces_override
        return super()._raw_spaces(org_id)

    def _raw_content(self, org_id, space):
        return list(self._content.get(space["key"], []))


def _page(id_, title, when, *, status="current", version=1, type_="page"):
    return {
        "id": id_,
        "type": type_,
        "title": title,
        "status": status,
        "version": {"number": version, "when": when, "by": {"displayName": "Ada"}},
        "_links": {"webui": f"/spaces/ENG/pages/{id_}"},
    }


_ENG_SPACE = {"key": "ENG", "name": "Engineering", "status": "current", "is_accessible": True}


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — confluence.py: deletion / archival detection
# ─────────────────────────────────────────────────────────────────────────────
def test_page_that_vanishes_from_listing_is_tombstoned():
    store = Store()
    content = {
        "ENG": [
            _page("100", "Runbook", "2026-06-10T09:00:00Z"),
            _page("200", "Postmortem", "2026-06-10T09:10:00Z"),
        ]
    }
    first = _drive(MutableFixtureIngestor(content, spaces=[_ENG_SPACE]), ORG, store)
    assert {r["artifact_id"] for r in first} == {"ENG:100", "ENG:200"}
    assert all(r["change_kind"] != ChangeKind.DELETED for r in first)

    # Page 200 is deleted/trashed: it simply stops appearing in the listing.
    content["ENG"] = [_page("100", "Runbook", "2026-06-10T09:00:00Z")]
    second = _drive(MutableFixtureIngestor(content, spaces=[_ENG_SPACE]), ORG, store)

    assert len(second) == 1
    assert second[0]["artifact_id"] == "ENG:200"
    assert second[0]["change_kind"] == ChangeKind.DELETED


def test_page_whose_status_flips_to_archived_is_tombstoned_even_if_still_listed():
    store = Store()
    content = {"ENG": [_page("100", "Runbook", "2026-06-10T09:00:00Z")]}
    _drive(MutableFixtureIngestor(content, spaces=[_ENG_SPACE]), ORG, store)

    # Still present in the listing, but its status flips away from current —
    # must tombstone even though the id never disappeared.
    content["ENG"] = [_page("100", "Runbook", "2026-06-11T09:00:00Z", status="archived", version=2)]
    second = _drive(MutableFixtureIngestor(content, spaces=[_ENG_SPACE]), ORG, store)

    assert len(second) == 1
    assert second[0]["artifact_id"] == "ENG:100"
    assert second[0]["change_kind"] == ChangeKind.DELETED


def test_trashed_page_is_never_emitted_as_an_upsert():
    """A page whose status is already non-current in the SAME run it is first
    observed must never be treated as a created/updated page — it simply never
    entered known_ids, so there's nothing to tombstone either."""
    store = Store()
    content = {
        "ENG": [
            _page("100", "Runbook", "2026-06-10T09:00:00Z"),
            _page("999", "Old draft", "2026-06-10T09:00:00Z", status="trashed"),
        ]
    }
    records = _drive(MutableFixtureIngestor(content, spaces=[_ENG_SPACE]), ORG, store)
    assert {r["artifact_id"] for r in records} == {"ENG:100"}


def test_space_losing_its_grant_tombstones_everything_known_without_reading_it():
    store = Store()
    content = {
        "ENG": [_page("100", "Runbook", "2026-06-10T09:00:00Z")],
        "HR": [_page("900", "Confidential", "2026-06-10T09:00:00Z")],
    }
    hr_space = {"key": "HR", "name": "HR", "status": "current", "is_accessible": True}
    first = _drive(MutableFixtureIngestor(content, spaces=[_ENG_SPACE, hr_space]), ORG, store)
    assert {r["artifact_id"] for r in first} == {"ENG:100", "HR:900"}

    class RevokedHRIngestor(MutableFixtureIngestor):
        def _raw_content(self, org_id, space):
            if space["key"] == "HR":
                raise AssertionError("must never read a space AgentIQ is no longer granted")
            return super()._raw_content(org_id, space)

    # HR's grant is revoked — only ENG remains accessible.
    second = _drive(RevokedHRIngestor(content, spaces=[_ENG_SPACE]), ORG, store)
    assert len(second) == 1
    assert second[0]["artifact_id"] == "HR:900"
    assert second[0]["change_kind"] == ChangeKind.DELETED


def test_unrelated_pages_are_unaffected_by_deletion_detection():
    """AC5: deletion detection must not regress incremental scoping — an
    unrelated, unchanged page is never re-emitted alongside a deletion."""
    store = Store()
    content = {
        "ENG": [
            _page("100", "Runbook", "2026-06-10T09:00:00Z"),
            _page("200", "Postmortem", "2026-06-10T09:10:00Z"),
        ]
    }
    _drive(MutableFixtureIngestor(content, spaces=[_ENG_SPACE]), ORG, store)

    content["ENG"] = [_page("100", "Runbook", "2026-06-10T09:00:00Z")]  # 200 removed
    second = _drive(MutableFixtureIngestor(content, spaces=[_ENG_SPACE]), ORG, store)
    assert [r["artifact_id"] for r in second] == ["ENG:200"]  # page 100 not re-emitted


def test_reports_deletes_is_true():
    assert ConfluenceIngestor().reports_deletes is True


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — confluence_content.py: deletions route to remove_content
# ─────────────────────────────────────────────────────────────────────────────
class _FakeRemoval:
    def __init__(self):
        self.calls: List[tuple] = []
        self.artifacts_removed = 0
        self.artifacts_absent = 0
        self.artifacts_failed = 0
        self.chunks_removed = 0

    def __call__(self, org_id, removals):
        removals = list(removals)
        self.calls.append((org_id, removals))
        self.artifacts_removed = len(removals)
        self.chunks_removed = len(removals) * 3
        return self


def test_deletion_records_are_routed_to_remove_content_not_rendered():
    ing = ConfluenceIngestor()
    removal = _FakeRemoval()

    def _boom_ingest(org_id, artifacts):  # pragma: no cover - must not be called
        raise AssertionError("a deletion must never reach the content ingest_fn")

    records = [
        {"artifact_id": "ENG:200", "change_kind": ChangeKind.DELETED, "space_key": "ENG", "content_id": "200"},
    ]
    result = ingest_confluence_content(
        ORG, records, ingestor=ing, ingest_fn=_boom_ingest, remove_fn=removal
    )
    assert result.deletions_seen == 1
    assert result.pages_seen == 0  # never classified as page content
    assert removal.calls == [(ORG, [("confluence", "ENG:200")])]
    assert result.artifacts_removed == 1
    assert result.chunks_removed == 3


def test_deletion_removal_failure_is_non_blocking(caplog):
    ing = ConfluenceIngestor()

    def _boom_remove(org_id, removals):
        raise RuntimeError("store unavailable")

    records = [
        {"artifact_id": "ENG:200", "change_kind": ChangeKind.DELETED, "space_key": "ENG", "content_id": "200"},
    ]
    result = ingest_confluence_content(ORG, records, ingestor=ing, remove_fn=_boom_remove)
    assert result.deletions_seen == 1
    assert result.artifacts_removed == 0  # never updated — the call raised


def test_mixed_batch_deletes_and_upserts_are_both_handled():
    ing = ConfluenceIngestor()
    removal = _FakeRemoval()

    class _FakeIngest:
        def __init__(self):
            self.artifacts = []

        def __call__(self, org_id, artifacts):
            from app.retrieval.ingest import IngestResult

            artifacts = list(artifacts)
            self.artifacts.extend(artifacts)
            result = IngestResult(org_id=org_id, artifacts_received=len(artifacts))
            result.artifacts_indexed = len(artifacts)
            return result

    ingest_fn = _FakeIngest()
    records = [
        {
            "artifact_id": "ENG:100",
            "change_kind": ChangeKind.UPDATED,
            "content_type": "page",
            "space_key": "ENG",
            "content_id": "100",
            "title": "Runbook",
            "modified_at": "2026-06-10T09:00:00Z",
        },
        {"artifact_id": "ENG:200", "change_kind": ChangeKind.DELETED, "space_key": "ENG", "content_id": "200"},
    ]
    result = ingest_confluence_content(
        ORG, records, ingestor=ing, ingest_fn=ingest_fn, remove_fn=removal
    )
    assert result.pages_seen == 1
    assert result.deletions_seen == 1
    assert removal.calls == [(ORG, [("confluence", "ENG:200")])]
    assert {a.source_artifact for a in ingest_fn.artifacts} == {"ENG:100"}


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — end-to-end: deletion event reaches retrieval freshness
# ─────────────────────────────────────────────────────────────────────────────
def test_deleted_page_purges_the_retrieval_index_via_the_standard_freshness_path(monkeypatch):
    """Drives the REAL change runner (which fires ingestion.artifact_changed)
    and the REAL freshness subscriber, faking only the store layer so no
    database is required — proving the whole pipeline from connector tombstone
    to store.purge_artifact is wired correctly (AC3: removed immediately)."""
    from app.retrieval import freshness

    purged: List[tuple] = []

    def fake_purge_artifact(org_id, source_system, source_artifact):
        purged.append((org_id, source_system, source_artifact))
        return (2, 0)  # (chunks_removed, queue_rows_dropped)

    monkeypatch.setattr(freshness.store, "purge_artifact", fake_purge_artifact)
    # record_event/telemetry are already fire-and-forget-guarded; no patch needed.

    store = Store()
    content = {"ENG": [_page("100", "Runbook", "2026-06-10T09:00:00Z")]}
    _drive(MutableFixtureIngestor(content, spaces=[_ENG_SPACE]), ORG, store)

    content["ENG"] = []  # page 100 deleted
    _drive(MutableFixtureIngestor(content, spaces=[_ENG_SPACE]), ORG, store)

    assert (ORG, "confluence", "ENG:100") in purged
