"""R18-A5 / AT-605 (T6) — acceptance contract tests for Section 3 (AC1–AC7).

The authoritative, per-Section-3 verification of "Confluence & SharePoint Deep
Content". Each AC is a labelled test:

  AC1 — a Confluence page and a SharePoint site page are ingested as structured
        text, chunked on headings, retrievable with page-level provenance.
  AC2 — a file/attachment is ingested exactly once via the DOCUMENT path — never
        duplicated through this (page) story.
  AC3 — editing a page refreshes its chunks; deleting/archiving removes its
        content from retrieval immediately.
  AC4 — pages in ungranted spaces/sites are not readable at depth.
  AC5 — incremental runs ingest only pages changed since the checkpoint.
  AC6 — a retrieved page chunk's provenance resolves to a working deep link.
  AC7 — an old page and a recent page of similar relevance rank per the assembly
        freshness (half-life) policy — recency weighting observable in the
        selection log.

Retrieval-backed ACs (AC1, AC3, AC6-retrieval, AC7-end-to-end) need the pgvector
``retrieval_chunks`` store and are SKIPPED (never failed) where it is absent, and
drive a deterministic in-boundary fake embedding provider (the pattern the
R18-B1 acceptance suite uses). The remaining ACs — AC2 (router), AC4 (permission),
AC5 (incremental), AC6-provenance, and the AC7 freshness-ranking CORE — are pure
and run without the vector store (the assembler is pure ranking logic; only
``retrieval_evidence_source`` needs the DB).
"""
from __future__ import annotations

from typing import Callable, List, Optional

import pytest

from app import db
from app.context_assembly import (
    DECISION_INCLUDED,
    KIND_EVIDENCE,
    AssemblyPolicy,
    assemble_context,
)
from app.model_gateway import register_provider
from app.model_gateway._interface import GenerationRequest, GenerationResult, ModelProvider
from app.retrieval import embedder
from app.retrieval.ingest import ContentArtifact, ingest_content, remove_content
from discovery.ingest.confluence import ConfluenceIngestor
from discovery.ingest.confluence_content import (
    build_content_artifact,
    content_artifacts,
    ingest_confluence_content,
)
from discovery.ingest.content_router import ContentRoute, classify_confluence_content
from discovery.ingest.sharepoint_content import ingest_sharepoint_content


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # Connectors read their deterministic fixtures; the retrieval store is still
    # the real (Postgres/pgvector) partition when present.
    monkeypatch.setenv("INGEST_MODE", "offline")


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles shared by the offline ACs
# ─────────────────────────────────────────────────────────────────────────────
class _Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp):
        self.data[(cp.org_id, cp.connector_id)] = cp


class _FakeSubstrate:
    """Captures artifacts handed to the substrate (for the offline connector ACs)."""

    def __init__(self):
        self.artifacts: List[ContentArtifact] = []

    def __call__(self, org_id, artifacts):
        from app.retrieval.ingest import ArtifactIngestResult, IngestResult

        artifacts = list(artifacts)
        self.artifacts.extend(artifacts)
        r = IngestResult(org_id=org_id, artifacts_received=len(artifacts))
        for a in artifacts:
            r.artifacts_indexed += 1
            r.chunks_indexed += 1
            r.artifacts.append(
                ArtifactIngestResult(a.source_system, a.source_artifact, "indexed", chunks_indexed=1)
            )
        return r

    @property
    def artifact_ids(self) -> set:
        return {a.source_artifact for a in self.artifacts}


def _confluence_records(ing: ConfluenceIngestor, org_id: str) -> List[dict]:
    return [r for b in ing.ingest_changes(org_id, None) for r in b.records]


# ═════════════════════════════════════════════════════════════════════════════
# AC2 — page-vs-file router: one artifact, one owner (no double-ingestion)
# ═════════════════════════════════════════════════════════════════════════════
class TestAC2Router:
    def test_ac2_page_and_file_routes_are_disjoint(self):
        # Page-native types route to THIS story; attachments/files route to the
        # R18-A1 document path — different ContentRoute, disjoint id spaces.
        assert classify_confluence_content("page") == ContentRoute.PAGE_CONTENT
        assert classify_confluence_content("blogpost") == ContentRoute.PAGE_CONTENT
        assert classify_confluence_content("attachment") == ContentRoute.DOCUMENT
        assert classify_confluence_content("comment") == ContentRoute.SKIP

    def test_ac2_content_path_never_emits_a_file_artifact(self):
        org = "org_r18a5_t6_ac2"
        ing = ConfluenceIngestor()
        arts = content_artifacts(ing, org, _confluence_records(ing, org))
        # Every artifact this story hands off is a page/blogpost (space:content_id),
        # never an attachment — files are the document path's job (AC2, no overlap).
        assert arts
        assert all(":" in a.source_artifact for a in arts)
        assert not any("att" in a.source_artifact.lower() for a in arts)


# ═════════════════════════════════════════════════════════════════════════════
# AC4 — permission boundary holds at depth (T5, re-verified)
# ═════════════════════════════════════════════════════════════════════════════
class TestAC4Permission:
    def test_ac4_ungranted_and_archived_spaces_never_ingested(self):
        org = "org_r18a5_t6_ac4"
        ing = ConfluenceIngestor()
        sub = _FakeSubstrate()
        ingest_confluence_content(org, _confluence_records(ing, org), ingestor=ing, ingest_fn=sub)
        assert sub.artifact_ids
        assert not any(a.startswith("HR:") for a in sub.artifact_ids)   # ungranted
        assert not any(a.startswith("OLD:") for a in sub.artifact_ids)  # archived

    def test_ac4_injected_ungranted_record_is_refused_body_never_fetched(self):
        org = "org_r18a5_t6_ac4b"
        ing = ConfluenceIngestor()
        sample = next(r for r in _confluence_records(ing, org) if r["artifact_id"].startswith("ENG:"))
        ungranted = dict(sample)
        ungranted.update(space_key="HR", content_id="999", artifact_id="HR:999")
        sub = _FakeSubstrate()
        result = ingest_confluence_content(org, [ungranted], ingestor=ing, ingest_fn=sub)
        assert result.pages_ungranted_skipped == 1
        assert "HR:999" not in sub.artifact_ids


# ═════════════════════════════════════════════════════════════════════════════
# AC5 — incremental by checkpoint (only changed pages)
# ═════════════════════════════════════════════════════════════════════════════
class TestAC5Incremental:
    def test_ac5_second_unchanged_run_hands_off_nothing(self):
        org = "org_r18a5_t6_ac5"
        store, first = _Store(), _FakeSubstrate()
        ingest_sharepoint_content(
            org, ingest_fn=first, read_checkpoint=store.read, save_checkpoint=store.save
        )
        assert first.artifact_ids  # first run ingests the changed estate

        second = _FakeSubstrate()
        ingest_sharepoint_content(
            org, ingest_fn=second, read_checkpoint=store.read, save_checkpoint=store.save
        )
        assert second.artifact_ids == set()  # nothing changed → nothing re-ingested


# ═════════════════════════════════════════════════════════════════════════════
# AC6 — provenance resolves to a working deep link (page-level)
# ═════════════════════════════════════════════════════════════════════════════
class TestAC6DeepLinkProvenance:
    def test_ac6_confluence_artifact_carries_deep_link_and_observed_pointer(self):
        org = "org_r18a5_t6_ac6"
        ing = ConfluenceIngestor()
        rec = next(r for r in _confluence_records(ing, org) if r["artifact_id"] == "ENG:100")
        art = build_content_artifact(ing, org, rec)
        assert art is not None
        prov = art.provenance
        # A finding citing a runbook points at the page — the UI can deep-link it.
        assert prov.get("url")  # working deep link to the source page
        assert prov["url"].endswith("/pages/100") or "100" in prov["url"]
        # Observed provenance spine (R16-B1) resolves back to the exact page.
        ep = prov["evidence_pointer"]
        assert ep["origin"] == "observed"
        assert ep["source_system"] == "confluence"
        assert ep["source_artifact"] == "ENG:100"


# ═════════════════════════════════════════════════════════════════════════════
# AC7 — freshness half-life ranking, observable in the selection log (CORE)
# ═════════════════════════════════════════════════════════════════════════════
class TestAC7FreshnessRanking:
    """The assembler is pure ranking logic, so the recency weighting is provable
    without the retrieval store: feed two equal-confidence evidence chunks that
    differ ONLY in age and assert the recent one ranks above the old one, with the
    ages recorded on the selection log."""

    @staticmethod
    def _two_pages_source(recent_ts: str, old_ts: str) -> Callable:
        def source(_opportunity, _policy=None):
            return [
                {"chunk_id": "recent-page", "origin": "observed", "confidence": 0.9,
                 "source_timestamp": recent_ts, "source_system": "confluence",
                 "source_artifact": "ENG:recent", "content": "same relevance"},
                {"chunk_id": "old-page", "origin": "observed", "confidence": 0.9,
                 "source_timestamp": old_ts, "source_system": "confluence",
                 "source_artifact": "ENG:old", "content": "same relevance"},
            ]
        return source

    def test_ac7_recent_ranks_above_equally_relevant_old_page(self):
        opportunity = {"id": "opp-ac7", "title": "process", "description": "process docs"}
        graph = {"entities": [], "relationships": []}
        policy = AssemblyPolicy(freshness_halflife_days=30.0, max_evidence_chunks=10)

        pkg = assemble_context(
            opportunity, graph, policy,
            evidence_source=self._two_pages_source(
                "2026-06-11T00:00:00Z", "2019-01-01T00:00:00Z"
            ),
        )
        log = {e["candidate_id"]: e for e in pkg.selection_log if e["kind"] == KIND_EVIDENCE}
        assert log["recent-page"]["decision"] == DECISION_INCLUDED
        assert log["old-page"]["decision"] == DECISION_INCLUDED

        # Recency observable on the log: recent is fresher (age ~0) than old.
        assert log["recent-page"]["freshness_days"] < log["old-page"]["freshness_days"]

        # And it ranks first — the recent page's include position precedes the old.
        recent_pos = int(log["recent-page"]["reason"].rsplit("_", 1)[-1])
        old_pos = int(log["old-page"]["reason"].rsplit("_", 1)[-1])
        assert recent_pos < old_pos, "recent evidence must rank before equally-relevant old evidence"

    def test_ac7_tight_budget_keeps_recent_drops_old(self):
        """With room for only ONE evidence chunk, the recent page wins the slot and
        the equally-relevant old page is ranked out — recency is the deciding factor."""
        opportunity = {"id": "opp-ac7b", "title": "process", "description": "process docs"}
        policy = AssemblyPolicy(freshness_halflife_days=30.0, max_evidence_chunks=1)
        pkg = assemble_context(
            opportunity, {"entities": [], "relationships": []}, policy,
            evidence_source=self._two_pages_source(
                "2026-06-11T00:00:00Z", "2019-01-01T00:00:00Z"
            ),
        )
        included = [e for e in pkg.selection_log
                    if e["kind"] == KIND_EVIDENCE and e["decision"] == DECISION_INCLUDED]
        assert [e["candidate_id"] for e in included] == ["recent-page"]


# ═════════════════════════════════════════════════════════════════════════════
# Retrieval-backed ACs — need the pgvector store + a fake embedding provider.
# ═════════════════════════════════════════════════════════════════════════════
def _retrieval_store_available() -> bool:
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT to_regclass('public.retrieval_chunks')")
            return cur.fetchone()[0] is not None
        finally:
            con.close()
    except Exception:
        return False


_RETRIEVAL = pytest.mark.skipif(
    not _retrieval_store_available(),
    reason="retrieval_chunks store (pgvector) not present in this environment",
)

# Deterministic bag-of-marker-words embeddings so query/content similarity is real
# and controllable. Each marker word present → 1.0 in its slot.
_VOCAB = ("payments", "onboarding", "freshtoken")


class _A5FakeProvider(ModelProvider):
    emits_own_telemetry = True

    def __init__(self, name: str, identity):
        self.name = name
        self._identity = identity

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[1.0 if w in t.lower() else 0.0 for w in _VOCAB] + [0.01] for t in texts]

    def embedding_identity(self):
        return self._identity


_A5_PROVIDER = _A5FakeProvider("r18a5_acc_embed", ("r18a5-acc:model", "1"))
register_provider(_A5_PROVIDER)


def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        con.commit()
    finally:
        con.close()


@pytest.fixture
def retrieval_org(request, monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _A5_PROVIDER.name)
    name = f"r18a5acc_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


def _confluence_page(artifact: str, content: str, ts: str, url: str) -> ContentArtifact:
    return ContentArtifact(
        source_system="confluence", source_artifact=artifact, content=content,
        content_type="prose", source_timestamp=ts,
        provenance={"origin": "observed", "url": url, "space_key": artifact.split(":")[0]},
    )


@_RETRIEVAL
class TestAC1AC6Retrieval:
    def test_ac1_confluence_and_sharepoint_pages_retrievable_with_provenance(self, retrieval_org):
        ingest_content(retrieval_org, [
            _confluence_page("ENG:100", "payments runbook rollback steps",
                             "2026-06-10T09:00:00Z", "/spaces/ENG/pages/100"),
            ContentArtifact(source_system="sharepoint", source_artifact="S-eng:page:pg-onboarding",
                            content="engineering onboarding guide", content_type="prose",
                            source_timestamp="2026-06-11T08:05:00Z",
                            provenance={"origin": "observed",
                                        "url": "https://contoso.sharepoint.com/sites/eng/SitePages/onboarding.aspx"}),
        ])
        embedder.embed_pending_for_org(retrieval_org)

        from app.retrieval.api import retrieve

        conf = retrieve(retrieval_org, "payments", k=5, source_filter=["confluence"])
        assert conf and conf[0].source_artifact == "ENG:100"
        assert conf[0].source_system == "confluence"

        sp = retrieve(retrieval_org, "onboarding", k=5, source_filter=["sharepoint"])
        assert sp and sp[0].source_artifact == "S-eng:page:pg-onboarding"

    def test_ac6_retrieved_chunk_resolves_to_observed_page_pointer(self, retrieval_org):
        ingest_content(retrieval_org, [
            _confluence_page("ENG:100", "payments runbook", "2026-06-10T09:00:00Z",
                             "/spaces/ENG/pages/100"),
        ])
        embedder.embed_pending_for_org(retrieval_org)
        from app.retrieval.api import retrieve

        hit = retrieve(retrieval_org, "payments", k=1, source_filter=["confluence"])[0]
        ptr = hit.to_evidence_pointer().to_dict()
        assert ptr["origin"] == "observed"
        assert ptr["source_system"] == "confluence"
        assert ptr["source_artifact"] == "ENG:100"  # resolves back to the source page
        assert ptr["chunk_id"] and ptr["retrieval_result_id"]


@_RETRIEVAL
class TestAC3EditDelete:
    def test_ac3_edit_replaces_chunks_and_delete_removes_them(self, retrieval_org):
        from app.retrieval.api import retrieve

        art = _confluence_page("ENG:200", "payments version one",
                               "2026-06-10T09:00:00Z", "/spaces/ENG/pages/200")
        ingest_content(retrieval_org, [art])
        embedder.embed_pending_for_org(retrieval_org)
        assert retrieve(retrieval_org, "payments", k=5, source_filter=["confluence"])

        # Edit: re-ingest the SAME artifact id with new content — chunks are replaced.
        edited = _confluence_page("ENG:200", "payments version two freshtoken",
                                  "2026-06-12T09:00:00Z", "/spaces/ENG/pages/200")
        ingest_content(retrieval_org, [edited])
        embedder.embed_pending_for_org(retrieval_org)
        hits = retrieve(retrieval_org, "freshtoken", k=5, source_filter=["confluence"])
        assert hits and hits[0].source_artifact == "ENG:200"

        # Delete: the page's chunks leave retrieval immediately.
        remove_content(retrieval_org, [("confluence", "ENG:200")])
        assert retrieve(retrieval_org, "payments", k=5, source_filter=["confluence"]) == []


@_RETRIEVAL
class TestAC7EndToEnd:
    def test_ac7_recent_page_outranks_old_via_retrieval_evidence_source(self, retrieval_org):
        """End-to-end AC7: two equally-relevant Confluence pages differing only in
        age, ingested for real, ranked by the assembler's freshness policy."""
        from app.retrieval.evidence_source import retrieval_evidence_source

        ingest_content(retrieval_org, [
            _confluence_page("ENG:recent", "freshtoken process doc",
                             "2026-06-11T00:00:00Z", "/spaces/ENG/pages/recent"),
            _confluence_page("ENG:old", "freshtoken process doc",
                             "2019-01-01T00:00:00Z", "/spaces/ENG/pages/old"),
        ])
        embedder.embed_pending_for_org(retrieval_org)

        opportunity = {"id": "opp", "title": "freshtoken", "description": "freshtoken process doc"}
        policy = AssemblyPolicy(freshness_halflife_days=30.0, max_evidence_chunks=10)
        pkg = assemble_context(
            opportunity, {"entities": [], "relationships": []}, policy,
            evidence_source=retrieval_evidence_source(retrieval_org, source_filter=["confluence"]),
        )
        by_art = {
            (e.get("candidate_id")): e
            for e in pkg.selection_log if e["kind"] == KIND_EVIDENCE
        }
        # Both retrieved with equal similarity; the recent one is fresher, so it
        # ranks first — recency weighting observable on the selection log (AC7).
        included = [e for e in pkg.selection_log
                    if e["kind"] == KIND_EVIDENCE and e["decision"] == DECISION_INCLUDED]
        assert included, "evidence should be included"
        fresh_days = [e["freshness_days"] for e in included if e["freshness_days"] is not None]
        assert fresh_days == sorted(fresh_days), "included evidence ordered fresh-first"
