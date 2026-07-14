"""R18-A6 / AT-610 (T5) — ``artifact_filter`` on the platform retrieval API.

Covers the retrieval-substrate half of AC5 ("retrieval scoped to a component
returns that component's code, not path-coincidental matches"): the primitive
``artifact_filter`` on ``store.search()`` / ``api.retrieve()`` that
``discovery.enterprise_apps.component_retrieval`` builds component scoping on
top of. That module-level integration is covered separately in
``test_enterprise_apps_component_retrieval.py``; this suite proves the
retrieval-API primitive itself, mirroring ``test_retrieval_acceptance.py``'s
AC4 (``source_filter``) coverage style for the new filter dimension:

  - ``artifact_filter`` scopes results to an EXACT ``source_artifact`` set.
  - A query that would otherwise match a decoy artifact (same embedding
    signal, different file) is excluded once scoped — proving this is a real
    exact-match filter, not a no-op.
  - An explicit filter naming nothing valid returns ``[]`` (never widens),
    exactly like ``source_filter``'s existing contract.
  - Combines with ``source_filter``/``min_score`` (AND semantics).
  - ``store.list_chunks_by_artifacts()`` — the no-query direct listing used
    when a caller wants a component's code, not an answer to a question.
"""
from __future__ import annotations

from typing import List

import pytest

from app import db
from app.model_gateway import register_provider
from app.model_gateway._interface import GenerationRequest, GenerationResult, ModelProvider
from app.retrieval import embedder, store
from app.retrieval.api import retrieve
from app.retrieval.ingest import ingest_content


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


pytestmark = pytest.mark.skipif(
    not _retrieval_store_available(),
    reason="retrieval_chunks store (pgvector) not present in this environment",
)


class _ArtFakeProvider(ModelProvider):
    """Deterministic, content-shaped embeddings — same technique as the T4/T8
    suites, with a unique provider name so registration never collides."""

    emits_own_telemetry = True

    def __init__(self, name: str, identity):
        self.name = name
        self._identity = identity

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        out = []
        for t in texts:
            low = t.lower()
            out.append([1.0 if "covenant" in low else 0.0, 0.01])
        return out

    def embedding_identity(self):
        return self._identity


_ART_A = _ArtFakeProvider("art_embed_a", ("art:model-a", "1"))
register_provider(_ART_A)


def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        con.commit()
    finally:
        con.close()


@pytest.fixture
def org(request, monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _ART_A.name)
    name = f"art_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


def _seed(org_id: str) -> None:
    ingest_content(org_id, [
        dict(source_system="git", source_artifact="covenant-service:CovenantService.java",
             content="covenant service business logic", content_type="code"),
        # A decoy: same embedding signal ("covenant"), DIFFERENT file/component —
        # exactly the "path-coincidental match" AC5 rules out.
        dict(source_system="git", source_artifact="covenant-service:CovenantServiceTest.java",
             content="covenant service unit test", content_type="code"),
        dict(source_system="git", source_artifact="unrelated-repo:Notes.md",
             content="covenant meeting notes, unrelated repo", content_type="prose"),
    ])
    embedder.embed_pending_for_org(org_id)


# ═════════════════════════════════════════════════════════════════════════════
# AC5 — artifact_filter is an EXACT scope, not a widened/coincidental match
# ═════════════════════════════════════════════════════════════════════════════
def test_artifact_filter_scopes_to_exact_artifact_only(org):
    _seed(org)
    scoped = retrieve(
        org, "covenant", k=10,
        artifact_filter=["covenant-service:CovenantService.java"],
    )
    assert scoped
    assert {h.source_artifact for h in scoped} == {"covenant-service:CovenantService.java"}
    # The decoy (same embedding signal, different file) is excluded once scoped.
    assert "covenant-service:CovenantServiceTest.java" not in {h.source_artifact for h in scoped}


def test_unscoped_query_would_match_the_decoy_too(org):
    """Sanity check that the decoy IS a real match absent scoping — proving the
    scoped test above is excluding it via the filter, not because it never
    matched in the first place."""
    _seed(org)
    unscoped = retrieve(org, "covenant", k=10)
    assert {"covenant-service:CovenantService.java", "covenant-service:CovenantServiceTest.java"} <= {
        h.source_artifact for h in unscoped
    }


def test_artifact_filter_naming_nothing_valid_returns_nothing(org):
    _seed(org)
    assert retrieve(org, "covenant", k=10, artifact_filter=["no-such-artifact"]) == []
    assert retrieve(org, "covenant", k=10, artifact_filter=["  "]) == []


def test_artifact_filter_combines_with_source_filter_and_min_score(org):
    _seed(org)
    combined = retrieve(
        org, "covenant", k=10,
        source_filter=["git"],
        artifact_filter=[
            "covenant-service:CovenantService.java",
            "covenant-service:CovenantServiceTest.java",
        ],
        min_score=-1.0,
    )
    assert {h.source_artifact for h in combined} == {
        "covenant-service:CovenantService.java",
        "covenant-service:CovenantServiceTest.java",
    }

    # A floor above the max similarity admits nothing, even within a valid scope.
    assert retrieve(
        org, "covenant", k=10,
        artifact_filter=["covenant-service:CovenantService.java"],
        min_score=1.01,
    ) == []


def test_artifact_filter_multiple_exact_artifacts(org):
    _seed(org)
    hits = retrieve(
        org, "covenant", k=10,
        artifact_filter=[
            "covenant-service:CovenantService.java",
            "unrelated-repo:Notes.md",
        ],
    )
    assert {h.source_artifact for h in hits} == {
        "covenant-service:CovenantService.java",
        "unrelated-repo:Notes.md",
    }


# ═════════════════════════════════════════════════════════════════════════════
# store.list_chunks_by_artifacts — no-query direct listing
# ═════════════════════════════════════════════════════════════════════════════
def test_list_chunks_by_artifacts_returns_exact_scope_only(org):
    _seed(org)
    rows = store.list_chunks_by_artifacts(
        org, ["covenant-service:CovenantService.java"], limit=10,
    )
    assert rows
    assert all(r["source_artifact"] == "covenant-service:CovenantService.java" for r in rows)


def test_list_chunks_by_artifacts_empty_list_returns_nothing(org):
    _seed(org)
    assert store.list_chunks_by_artifacts(org, [], limit=10) == []


def test_list_chunks_by_artifacts_unknown_artifact_returns_nothing(org):
    _seed(org)
    assert store.list_chunks_by_artifacts(org, ["no-such-artifact"], limit=10) == []


def test_list_chunks_by_artifacts_respects_limit(org):
    _seed(org)
    rows = store.list_chunks_by_artifacts(
        org,
        [
            "covenant-service:CovenantService.java",
            "covenant-service:CovenantServiceTest.java",
            "unrelated-repo:Notes.md",
        ],
        limit=1,
    )
    assert len(rows) == 1


def test_list_chunks_by_artifacts_excludes_unembedded_content(org):
    # Indexed but never embedded (no embed_pending_for_org call for this artifact).
    ingest_content(org, [dict(source_system="git", source_artifact="repo:Pending.java",
                              content="not yet embedded", content_type="code")])
    assert store.list_chunks_by_artifacts(org, ["repo:Pending.java"], limit=10) == []
