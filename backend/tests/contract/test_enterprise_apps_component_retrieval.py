"""Contract tests for R18-A6 / AT-610 (T5) — component-scoped retrieval.

AC5 coverage: "Retrieval scoped to a component returns that component's code,
not path-coincidental matches."

  - ``resolve_component_artifacts()`` resolves a component (exact name match)
    to the EXACT ``source_artifact`` id(s) A2 indexed it under — never a
    substring/path guess.
  - A decoy file that merely CONTAINS the component's name as a substring (in
    its own path, or as text inside an unrelated file) is never included —
    the defining test of "not path-coincidental matches".
  - ``retrieve_component_code()`` end-to-end: ingest real code content for two
    similarly-named components, embed it, and confirm a query scoped to one
    component never returns the other's (or a decoy's) chunks.
  - The no-query direct-listing path returns a component's code without
    needing a search term.
  - Unconfigured app raises; unmatched component / un-indexed content returns
    ``[]`` rather than raising.
"""
from __future__ import annotations

from typing import List
from unittest.mock import patch

import pytest

from app import db
from app.model_gateway import register_provider
from app.model_gateway._interface import GenerationRequest, GenerationResult, ModelProvider
from app.retrieval import embedder
from app.retrieval.ingest import ingest_content
from discovery.enterprise_apps.app_repo_map import AppRepoMapping, EnterpriseAppConfigError
from discovery.enterprise_apps.component_retrieval import (
    resolve_component_artifacts,
    retrieve_component_code,
)
from discovery.enterprise_apps.structure import RepoFile


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


class _CompFakeProvider(ModelProvider):
    """Deterministic, content-shaped embeddings — unique provider name so
    registration never collides with other suites."""

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


_COMP_A = _CompFakeProvider("comp_embed_a", ("comp:model-a", "1"))
register_provider(_COMP_A)


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
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _COMP_A.name)
    name = f"comp_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


CONTROLLER_JAVA = """
package com.acme.widget;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/widgets")
public class WidgetController {
    @GetMapping("/{id}")
    public String getWidget() { return null; }
}
"""

SERVICE_JAVA = """
package com.acme.widget;

import org.springframework.stereotype.Service;

@Service
public class WidgetService {
}
"""

# A DIFFERENTLY-named, unrelated component in the same repo — the structural
# analogue of the "decoy" file: it must never be swept in by a name/path
# coincidence in a WidgetService-scoped query.
OTHER_SERVICE_JAVA = """
package com.acme.widget;

import org.springframework.stereotype.Service;

@Service
public class OtherWidgetHelperService {
}
"""


def _widget_repo_files() -> list[RepoFile]:
    return [
        RepoFile("src/main/java/com/acme/widget/WidgetController.java", CONTROLLER_JAVA),
        RepoFile("src/main/java/com/acme/widget/WidgetService.java", SERVICE_JAVA),
        RepoFile("src/main/java/com/acme/widget/OtherWidgetHelperService.java", OTHER_SERVICE_JAVA),
    ]


def _mapping(app_id: str, repo_ids: tuple[str, ...]) -> AppRepoMapping:
    return AppRepoMapping(
        app_id=app_id, name="Widget App", platform="java", repo_ids=repo_ids, metadata={},
    )


def _stub_provider(files_by_repo: dict):
    def provider(repo_id: str):
        return files_by_repo.get(repo_id, [])
    return provider


# ═════════════════════════════════════════════════════════════════════════════
# resolve_component_artifacts — exact structural match, never a substring
# ═════════════════════════════════════════════════════════════════════════════
class TestResolveComponentArtifacts:
    def test_exact_qualified_name_match(self):
        mapping = _mapping("widget-app", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})
        with patch(
            "discovery.enterprise_apps.component_retrieval.get_app_mapping",
            return_value=mapping,
        ):
            artifacts = resolve_component_artifacts(
                "org-x", "widget-app", "com.acme.widget.WidgetService", provider,
            )
        assert artifacts == [
            "widget-repo:src/main/java/com/acme/widget/WidgetService.java"
        ]

    def test_exact_simple_name_match(self):
        mapping = _mapping("widget-app", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})
        with patch(
            "discovery.enterprise_apps.component_retrieval.get_app_mapping",
            return_value=mapping,
        ):
            artifacts = resolve_component_artifacts(
                "org-x", "widget-app", "WidgetService", provider,
            )
        assert artifacts == [
            "widget-repo:src/main/java/com/acme/widget/WidgetService.java"
        ]

    def test_no_match_returns_empty_not_a_substring_guess(self):
        """'WidgetService' must not fuzzily match 'OtherWidgetHelperService' or
        any other component that merely shares a substring."""
        mapping = _mapping("widget-app", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})
        with patch(
            "discovery.enterprise_apps.component_retrieval.get_app_mapping",
            return_value=mapping,
        ):
            artifacts = resolve_component_artifacts(
                "org-x", "widget-app", "Widget", provider,  # substring, not exact
            )
        assert artifacts == []

    def test_unconfigured_app_raises(self):
        with patch(
            "discovery.enterprise_apps.component_retrieval.get_app_mapping",
            return_value=None,
        ):
            with pytest.raises(EnterpriseAppConfigError):
                resolve_component_artifacts(
                    "org-x", "ghost-app", "WidgetService", _stub_provider({}),
                )

    def test_one_repo_failure_does_not_block_others(self):
        mapping = _mapping("widget-app", ("good-repo", "broken-repo"))

        def provider(repo_id: str):
            if repo_id == "broken-repo":
                raise RuntimeError("simulated failure")
            return _widget_repo_files()

        with patch(
            "discovery.enterprise_apps.component_retrieval.get_app_mapping",
            return_value=mapping,
        ):
            artifacts = resolve_component_artifacts(
                "org-x", "widget-app", "WidgetService", provider,
            )
        assert artifacts == ["good-repo:src/main/java/com/acme/widget/WidgetService.java"]


# ═════════════════════════════════════════════════════════════════════════════
# retrieve_component_code — end to end (AC5)
# ═════════════════════════════════════════════════════════════════════════════
class TestRetrieveComponentCodeEndToEnd:
    def _ingest_and_embed(self, org_id: str) -> None:
        ingest_content(org_id, [
            dict(
                source_system="git",
                source_artifact="widget-repo:src/main/java/com/acme/widget/WidgetService.java",
                content="covenant widget service business logic",
                content_type="code",
            ),
            dict(
                source_system="git",
                source_artifact="widget-repo:src/main/java/com/acme/widget/OtherWidgetHelperService.java",
                content="covenant other widget helper logic",
                content_type="code",
            ),
        ])
        embedder.embed_pending_for_org(org_id)

    def test_query_scoped_to_component_excludes_the_decoy(self, org):
        self._ingest_and_embed(org)
        mapping = _mapping("widget-app", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})

        with patch(
            "discovery.enterprise_apps.component_retrieval.get_app_mapping",
            return_value=mapping,
        ):
            hits = retrieve_component_code(
                org, "widget-app", "WidgetService", provider,
                query_text="covenant", k=10,
            )

        assert hits
        assert {h.source_artifact for h in hits} == {
            "widget-repo:src/main/java/com/acme/widget/WidgetService.java"
        }
        # The decoy (same embedding signal, genuinely a different component)
        # never appears once scoped — proving this is a real structural
        # filter, not a coincidental path/keyword match.
        assert "widget-repo:src/main/java/com/acme/widget/OtherWidgetHelperService.java" not in {
            h.source_artifact for h in hits
        }

    def test_unscoped_query_would_match_the_decoy_too(self, org):
        """Sanity check: absent component scoping the decoy IS a real semantic
        match, proving the scoped test above excludes it via the filter."""
        self._ingest_and_embed(org)
        from app.retrieval.api import retrieve

        unscoped = retrieve(org, "covenant", k=10, source_filter=["git"])
        assert {
            "widget-repo:src/main/java/com/acme/widget/WidgetService.java",
            "widget-repo:src/main/java/com/acme/widget/OtherWidgetHelperService.java",
        } <= {h.source_artifact for h in unscoped}

    def test_no_query_lists_the_components_code_directly(self, org):
        self._ingest_and_embed(org)
        mapping = _mapping("widget-app", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})

        with patch(
            "discovery.enterprise_apps.component_retrieval.get_app_mapping",
            return_value=mapping,
        ):
            hits = retrieve_component_code(
                org, "widget-app", "WidgetService", provider, k=10,
            )

        assert hits
        assert all(
            h.source_artifact == "widget-repo:src/main/java/com/acme/widget/WidgetService.java"
            for h in hits
        )
        assert all(h.similarity == 1.0 for h in hits)

    def test_component_not_in_structure_returns_empty(self, org):
        mapping = _mapping("widget-app", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})
        with patch(
            "discovery.enterprise_apps.component_retrieval.get_app_mapping",
            return_value=mapping,
        ):
            assert retrieve_component_code(
                org, "widget-app", "NoSuchComponent", provider, query_text="covenant",
            ) == []

    def test_component_with_no_indexed_content_returns_empty(self, org):
        """Structure resolves the component's file, but A2 has not (yet)
        indexed any content for it — a miss, not an error."""
        mapping = _mapping("widget-app", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})
        with patch(
            "discovery.enterprise_apps.component_retrieval.get_app_mapping",
            return_value=mapping,
        ):
            assert retrieve_component_code(
                org, "widget-app", "WidgetService", provider, query_text="covenant",
            ) == []
