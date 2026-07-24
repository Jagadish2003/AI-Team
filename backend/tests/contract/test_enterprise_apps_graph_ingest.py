"""Contract tests for R18-A6 / AT-608 (T3) — structure -> knowledge graph.

AC3 coverage:
  - Components, dependencies, and endpoints extracted by structure.py (T1/T2)
    are loaded into the SAME entities/entity_relationships tables every other
    graph writer uses, through the SAME write paths
    (resolve_or_create_entity / upsert_relationship) — no schema change.
  - Every entity/relationship carries origin='observed' (never 'inferred').
  - Every entity/relationship's provenance encodes repo, path, and commit SHA.
  - Relationship types used (owns / depends_on / routes_to) are all pre-existing
    in entity_relationships.RELATIONSHIP_TYPES.

Additional coverage:
  - App-scoped identity: the same generic component/module name in two
    different configured apps resolves to two separate (non-ambiguous)
    entities, not one collided/ambiguous row.
  - Idempotency: re-ingesting the same app/repo/commit updates run_count
    without creating duplicate entities or relationships.
  - Missing app configuration raises EnterpriseAppConfigError.
  - No commit_sha_provider -> provenance omits the SHA segment gracefully.
  - A content-provider failure for one repo does not sink the whole app's
    load; the other repo's structure still loads.
  - Ambiguous endpoints are never linked (correctness contract mirrored from
    relationship_mapper's map_directly_observed).
"""
from __future__ import annotations

import json
import os
import sqlite3
from unittest.mock import patch
from uuid import uuid4

import pytest

from database.models.entity_relationships import OBSERVED_CONFIDENCE, RELATIONSHIP_TYPES
from discovery.enterprise_apps.app_repo_map import AppRepoMapping, EnterpriseAppConfigError
from discovery.enterprise_apps.graph_ingest import GraphIngestResult, ingest_app_structure
from discovery.enterprise_apps.structure import RepoFile


def _db() -> str:
    return os.environ.get("DB_PATH", "")


def _entities(org_id: str, entity_type: str | None = None) -> list[dict]:
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        if entity_type:
            rows = conn.execute(
                "SELECT * FROM entities WHERE org_id=%s AND entity_type=%s",
                (org_id, entity_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM entities WHERE org_id=%s", (org_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def _relationships(org_id: str) -> list[dict]:
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM entity_relationships WHERE org_id=%s", (org_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def _metadata(row: dict) -> dict:
    raw = row.get("metadata")
    return json.loads(raw) if isinstance(raw, str) and raw else (raw or {})


def _evidence(row: dict) -> dict:
    raw = row.get("evidence")
    return json.loads(raw) if isinstance(raw, str) and raw else (raw or {})


def _org() -> str:
    return f"org-graph-{uuid4().hex[:10]}"


POM_XML = """<?xml version="1.0"?>
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>widget-app</artifactId>
  <version>1.0.0</version>
  <dependencies>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-webmvc</artifactId>
      <version>6.1.3</version>
    </dependency>
  </dependencies>
</project>
"""

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


def _widget_repo_files() -> list[RepoFile]:
    return [
        RepoFile("pom.xml", POM_XML),
        RepoFile("src/main/java/com/acme/widget/WidgetController.java", CONTROLLER_JAVA),
        RepoFile("src/main/java/com/acme/widget/WidgetService.java", SERVICE_JAVA),
    ]


def _mapping(app_id: str, name: str, repo_ids: tuple[str, ...]) -> AppRepoMapping:
    return AppRepoMapping(
        app_id=app_id, name=name, platform="java", repo_ids=repo_ids, metadata={},
    )


def _stub_provider(files_by_repo: dict[str, list[RepoFile]]):
    def provider(repo_id: str):
        return files_by_repo.get(repo_id, [])
    return provider


def _run(mapping, content_provider, commit_sha_provider=None, *, org_id=None, run_id="run-001", app_id=None):
    org_id = org_id or _org()
    app_id = app_id or mapping.app_id
    with patch(
        "discovery.enterprise_apps.graph_ingest.get_app_mapping",
        return_value=mapping,
    ):
        result = ingest_app_structure(
            org_id, run_id, app_id, content_provider, commit_sha_provider,
        )
    return org_id, result


# ═════════════════════════════════════════════════════════════════════════════
# AC3 — structure enters the graph as observed entities/relationships,
# with repo/path/SHA provenance
# ═════════════════════════════════════════════════════════════════════════════
class TestStructureEntersGraphAsObserved:
    def test_relationship_types_used_are_all_pre_existing(self):
        assert {"owns", "depends_on", "routes_to"} <= RELATIONSHIP_TYPES

    def test_result_counts(self):
        mapping = _mapping("widget-app", "Widget App", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})
        org_id, result = _run(
            mapping, provider, lambda repo_id: "abc1234",
        )
        assert isinstance(result, GraphIngestResult)
        # pom.xml's own artifactId is ALSO a module Component (structure.py T1
        # behavior) -> module "widget-app" + WidgetController + WidgetService.
        assert result.component_count == 3
        assert result.dependency_count == 1  # spring-webmvc
        assert result.endpoint_count == 1  # GET /api/widgets/{id}
        assert result.skipped_count == 0

    def test_app_entity_is_system_type_and_resolved(self):
        mapping = _mapping("widget-app", "Widget App", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})
        org_id, result = _run(mapping, provider, lambda r: "abc1234")

        app_rows = _entities(org_id, "system")
        assert len(app_rows) == 1
        app_row = app_rows[0]
        assert app_row["id"] == result.app_entity_id
        assert app_row["resolution_status"] == "resolved"
        assert app_row["source_system"] == "git"
        assert app_row["source_record_id"] == "widget-app"
        assert _metadata(app_row)["evidence_pointer"]["origin"] == "observed"

    def test_component_dependency_endpoint_entities_are_object_type_observed_with_provenance(self):
        mapping = _mapping("widget-app", "Widget App", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})
        org_id, result = _run(mapping, provider, lambda r: "abc1234")

        object_rows = _entities(org_id, "object")
        assert len(object_rows) == 5  # 3 components + 1 dependency + 1 endpoint

        by_kind: dict[str, list[dict]] = {}
        for row in object_rows:
            md = _metadata(row)
            by_kind.setdefault(md.get("structure_kind"), []).append(row)

        assert len(by_kind.get("component", [])) == 3
        assert len(by_kind.get("dependency", [])) == 1
        assert len(by_kind.get("endpoint", [])) == 1

        for row in object_rows:
            assert row["resolution_status"] == "resolved"
            md = _metadata(row)
            pointer = md["evidence_pointer"]
            assert pointer["origin"] == "observed"
            assert pointer["extraction_job_id"] is None
            # repo/path/SHA provenance (AC3): source_artifact composites all three.
            assert "widget-repo" in pointer["source_artifact"]
            assert "abc1234" in pointer["source_artifact"]
            assert md["repo_id"] == "widget-repo"
            assert md["commit_sha"] == "abc1234"
            assert md["path"]

        controller = next(r for r in by_kind["component"] if _metadata(r)["component_kind"] == "controller")
        assert "WidgetController" in controller["display_name"]
        assert "widget-app" in controller["display_name"]

    def test_relationships_owns_depends_on_routes_to_are_observed_with_provenance(self):
        mapping = _mapping("widget-app", "Widget App", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})
        org_id, result = _run(mapping, provider, lambda r: "abc1234")

        rels = _relationships(org_id)
        by_type = {}
        for r in rels:
            by_type.setdefault(r["relationship_type"], []).append(r)

        assert len(by_type.get("owns", [])) == 3  # app owns 3 components
        assert len(by_type.get("depends_on", [])) == 1  # app depends_on 1 dependency
        assert len(by_type.get("routes_to", [])) == 1  # controller routes_to 1 endpoint

        for r in rels:
            assert r["inferred"] in (False, 0)
            assert float(r["confidence"]) == OBSERVED_CONFIDENCE
            ev = _evidence(r)
            assert ev["evidence_pointer"]["origin"] == "observed"
            assert ev["repo_id"] == "widget-repo"
            assert ev["commit_sha"] == "abc1234"
            assert ev["path"]

        assert result.relationship_count == len(rels) == 5

    def test_no_commit_sha_provider_omits_sha_segment(self):
        mapping = _mapping("widget-app", "Widget App", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})
        org_id, result = _run(mapping, provider, commit_sha_provider=None)

        object_rows = _entities(org_id, "object")
        for row in object_rows:
            md = _metadata(row)
            assert md["commit_sha"] is None
            source_artifact = md["evidence_pointer"]["source_artifact"]
            assert "@" not in source_artifact.split(":", 1)[0]


# ═════════════════════════════════════════════════════════════════════════════
# App-scoped identity — generic names must not collide across apps
# ═════════════════════════════════════════════════════════════════════════════
class TestAppScopedIdentity:
    def test_same_component_name_in_two_apps_does_not_collide(self):
        org_id = _org()
        # Deliberately identical, generic content for both apps — a bare "Core"
        # service class with no build file (so component_count is exactly 1 per
        # app, isolating this test from the pom.xml module-component behavior).
        files = [
            RepoFile(
                "src/main/java/com/acme/Core.java",
                "package com.acme;\n\nimport org.springframework.stereotype.Service;\n\n"
                "@Service\npublic class Core {\n}\n",
            ),
        ]
        mapping_a = _mapping("app-a", "App A", ("repo-a",))
        mapping_b = _mapping("app-b", "App B", ("repo-b",))

        with patch(
            "discovery.enterprise_apps.graph_ingest.get_app_mapping",
            side_effect=lambda org, app_id: mapping_a if app_id == "app-a" else mapping_b,
        ):
            ingest_app_structure(org_id, "run-001", "app-a", _stub_provider({"repo-a": files}))
            ingest_app_structure(org_id, "run-001", "app-b", _stub_provider({"repo-b": files}))

        object_rows = [
            r for r in _entities(org_id, "object")
            if _metadata(r).get("structure_kind") == "component"
        ]
        # Two SEPARATE, non-ambiguous entities — not one collided/merged row.
        assert len(object_rows) == 2
        assert all(r["resolution_status"] == "resolved" for r in object_rows)
        app_ids = {_metadata(r)["app_id"] for r in object_rows}
        assert app_ids == {"app-a", "app-b"}
        canonical_names = {r["canonical_name"] for r in object_rows}
        assert len(canonical_names) == 2  # app_id in display_name keeps them distinct


# ═════════════════════════════════════════════════════════════════════════════
# Idempotency
# ═════════════════════════════════════════════════════════════════════════════
class TestIdempotency:
    def test_reingesting_same_app_updates_run_count_without_duplicating(self):
        mapping = _mapping("widget-app", "Widget App", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})
        org_id = _org()

        with patch(
            "discovery.enterprise_apps.graph_ingest.get_app_mapping",
            return_value=mapping,
        ):
            ingest_app_structure(org_id, "run-001", "widget-app", provider, lambda r: "sha1")
            ingest_app_structure(org_id, "run-002", "widget-app", provider, lambda r: "sha1")

        object_rows = _entities(org_id, "object")
        assert len(object_rows) == 5  # still 5, not 10
        for row in object_rows:
            assert row["run_count"] == 2
            assert row["last_seen_run_id"] == "run-002"
            assert row["first_seen_run_id"] == "run-001"

        rels = _relationships(org_id)
        assert len(rels) == 5  # still 5, not 10
        for r in rels:
            assert r["run_count"] == 2
            assert r["last_seen_run_id"] == "run-002"


# ═════════════════════════════════════════════════════════════════════════════
# Robustness / error isolation
# ═════════════════════════════════════════════════════════════════════════════
class TestRobustness:
    def test_unconfigured_app_raises_enterprise_app_config_error(self):
        org_id = _org()
        with patch(
            "discovery.enterprise_apps.graph_ingest.get_app_mapping",
            return_value=None,
        ):
            with pytest.raises(EnterpriseAppConfigError):
                ingest_app_structure(org_id, "run-001", "ghost-app", _stub_provider({}))

    def test_one_repo_content_failure_does_not_sink_the_whole_app(self):
        mapping = _mapping("widget-app", "Widget App", ("good-repo", "broken-repo"))

        def provider(repo_id: str):
            if repo_id == "broken-repo":
                raise RuntimeError("simulated content-fetch failure")
            return _widget_repo_files()

        org_id, result = _run(mapping, provider, lambda r: "sha1")

        # good-repo's structure still loaded despite broken-repo's failure.
        assert result.component_count == 3
        assert result.dependency_count == 1
        assert result.endpoint_count == 1

    def _seed_two_ambiguous_system_entities(self, org_id: str, display_name: str) -> None:
        """Insert two pre-existing 'system' rows sharing one canonical_name.

        Mirrors test_entity_resolution.py's TestAmbiguousResolution._seed_two_entities:
        resolve_or_create_entity() itself MERGES repeat calls for the same
        canonical name into a single row (that IS resolution), so a genuine
        multi-candidate collision must be seeded directly, bypassing the
        resolver, exactly as the existing entity-resolution contract tests do.
        """
        from datetime import datetime, timezone
        from uuid import uuid4 as _uuid4

        from database.models.entities import Entity

        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(_db()) as conn:
            for source_record_id in ("other-app-1", "other-app-2"):
                entity = Entity(
                    id=str(_uuid4()),
                    org_id=org_id,
                    entity_type="system",
                    canonical_name=display_name.lower(),
                    display_name=display_name,
                    source_system="git",
                    source_record_id=source_record_id,
                    resolution_confidence=1.0,
                    resolution_status="resolved",
                    first_seen_run_id="run-000",
                    last_seen_run_id="run-000",
                    run_count=1,
                    created_at=datetime.fromisoformat(now),
                    updated_at=datetime.fromisoformat(now),
                )
                row = entity.to_db_row()
                conn.execute(
                    """INSERT INTO entities (
                        id, org_id, entity_type, canonical_name, display_name,
                        source_system, source_record_id, resolution_confidence,
                        resolution_status, first_seen_run_id, last_seen_run_id,
                        run_count, metadata, created_at, updated_at
                    ) VALUES (
                        %(id)s, %(org_id)s, %(entity_type)s, %(canonical_name)s, %(display_name)s,
                        %(source_system)s, %(source_record_id)s, %(resolution_confidence)s,
                        %(resolution_status)s, %(first_seen_run_id)s, %(last_seen_run_id)s,
                        %(run_count)s, %(metadata)s, %(created_at)s, %(updated_at)s
                    )""",
                    row,
                )
            conn.commit()

    def test_ambiguous_app_entity_blocks_owns_and_depends_on_edges(self):
        org_id = _org()
        # Pre-seed two DIFFERENT source-backed 'system' entities sharing the
        # exact canonical name, forcing this app's resolution into the
        # ambiguous (N+1) branch.
        self._seed_two_ambiguous_system_entities(org_id, "Shared App Name")

        mapping = _mapping("widget-app", "Shared App Name", ("widget-repo",))
        provider = _stub_provider({"widget-repo": _widget_repo_files()})

        with patch(
            "discovery.enterprise_apps.graph_ingest.get_app_mapping",
            return_value=mapping,
        ):
            result = ingest_app_structure(org_id, "run-001", "widget-app", provider, lambda r: "sha1")

        app_rows = [
            r for r in _entities(org_id, "system")
            if r["source_record_id"] == "widget-app"
        ]
        assert len(app_rows) == 1
        assert app_rows[0]["resolution_status"] == "ambiguous"

        # Components/endpoints/dependencies still resolve (they have their own
        # distinct canonical identity)...
        assert result.component_count == 3
        assert result.dependency_count == 1
        # ...but no owns/depends_on edge was drawn FROM the ambiguous app entity.
        rels = _relationships(org_id)
        assert not any(r["relationship_type"] in ("owns", "depends_on") for r in rels)
