"""R18-A6 / AT-608 (T3) — load extracted structure into the knowledge graph.

Bridges T1/T2 (:mod:`.structure`'s deterministic ``AppStructure`` extraction) and
T6 (:mod:`.app_repo_map`'s configured app→repo scope) into the Stage 2 Knowledge
Graph (``entities`` / ``entity_relationships``, R16-B1/T3-S12-A/T3-S13-A) that
:mod:`app.entity_resolution` and :mod:`app.relationship_mapper` already own.

No new tables and no schema change: every structural fact is stored through the
SAME two write paths every other extractor uses —
:func:`app.entity_resolution.resolve_or_create_entity` and
:func:`app.relationship_mapper.upsert_relationship` — using entity types and
relationship types already in the locked enums (AC3):

* the application itself            -> ``entity_type='system'``
* each component/dependency/endpoint -> ``entity_type='object'``
  (``metadata['structure_kind']`` distinguishes ``component``/``dependency``/
  ``endpoint``; ``metadata['component_kind']`` further distinguishes
  controller/service/repository/module for components)
* app --``owns``--> component
* app --``depends_on``--> dependency
* component --``routes_to``--> the endpoint it declares

Every entity/relationship this module writes is OBSERVED, never inferred:
``resolve_or_create_entity``/``upsert_relationship`` always stamp an
``EvidencePointer`` with ``origin='observed'`` (never ``'inferred'`` — this
module never sets ``inferred=True`` and never needs an ``extraction_job_id``).
Repo/path/SHA provenance (AC3) is carried in ``source_record_id`` (and hence the
entity's ``evidence_pointer.source_artifact``) as ``"{repo_id}@{commit_sha}:{path}
#{qualifier}"`` — the same composite-string convention
:mod:`discovery.ingest.git_content` already uses for repo+path (and repo+sha)
identifiers — plus mirrored as plain ``repo_id``/``commit_sha``/``path`` metadata
keys for structured access without parsing.

Entity identity is intentionally scoped differently by structural role:

* the application, each component, and each endpoint are APP-SCOPED (their
  ``display_name`` embeds ``app_id``) — a generic module name like ``"core"`` or
  a common route like ``"GET /health"`` must never resolve two unrelated
  applications' facts into one canonical entity.
* a dependency is ALSO app-scoped here (not shared org-wide across apps) so its
  own entity carries direct repo/path/SHA provenance per AC3, rather than only
  on the edge — cross-app dependency-impact analysis is future scope, not asked
  for here, and reusing one entity across apps would blur "observed in THIS
  repo at THIS commit" into an average of many repos.

Runtime→structure resolution (T4/AT-609, "the service emitting errors IS this
component") is NOT this module's job — it only makes structural facts
resolvable/joinable; the conservative join logic is a separate, later step.

This module is NOT wired into the per-run discovery pipeline (unlike
``entity_extractor.extract_entities()``): AppStructure extraction runs over a
configured, per-org app→repo mapping that is orthogonal to a discovery run's
connector set (mirrors how java/.NET operational ingestion is config-driven, not
auto-discovered). Callers — a future on-demand route or background job —
supply ``run_id`` themselves, exactly like every other graph writer here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from app.entity_resolution import resolve_or_create_entity
from app.relationship_mapper import upsert_relationship
from database.models.entities import Entity
from database.models.entity_relationships import OBSERVED_CONFIDENCE

from .app_repo_map import (
    AppRepoMapping,
    ContentProvider,
    EnterpriseAppConfigError,
    get_app_mapping,
)
from .structure import Component, Dependency, Endpoint, extract_structure

logger = logging.getLogger(__name__)

#: Resolves a repo id to the commit SHA its currently-provided content reflects.
#: Optional — a caller with no SHA tracking may omit it; provenance then omits
#: the SHA segment (``"{repo_id}:{path}#..."``) rather than fabricating one.
CommitShaProvider = Callable[[str], Optional[str]]

# Relationship types are all pre-existing (entity_relationships.RELATIONSHIP_TYPES)
# — no schema change (AC3).
REL_OWNS = "owns"
REL_DEPENDS_ON = "depends_on"
REL_ROUTES_TO = "routes_to"

_SOURCE_SYSTEM = "git"  # matches the retrieval substrate's canonical source_system for git content


@dataclass(frozen=True)
class GraphIngestResult:
    """Summary of one application's structure load — counts, not the full graph."""

    app_id: str
    app_entity_id: str
    component_count: int = 0
    dependency_count: int = 0
    endpoint_count: int = 0
    relationship_count: int = 0
    skipped_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_id": self.app_id,
            "app_entity_id": self.app_entity_id,
            "component_count": self.component_count,
            "dependency_count": self.dependency_count,
            "endpoint_count": self.endpoint_count,
            "relationship_count": self.relationship_count,
            "skipped_count": self.skipped_count,
        }


def _artifact_ref(repo_id: str, commit_sha: Optional[str], path: str) -> str:
    """Composite ``{repo_id}[@{commit_sha}]:{path}`` provenance identifier."""
    base = f"{repo_id}@{commit_sha}" if commit_sha else repo_id
    return f"{base}:{path}"


def _dependency_label(dependency: Dependency) -> str:
    return f"{dependency.group}:{dependency.name}" if dependency.group else dependency.name


def _resolve_app_entity(org_id: str, run_id: str, mapping: AppRepoMapping) -> Entity:
    return resolve_or_create_entity(
        org_id=org_id,
        entity_type="system",
        display_name=mapping.name,
        source_system=_SOURCE_SYSTEM,
        source_record_id=mapping.app_id,
        run_id=run_id,
        metadata={
            "structure_kind": "application",
            "app_id": mapping.app_id,
            "platform": mapping.platform,
            "repo_ids": list(mapping.repo_ids),
        },
    )


def _resolve_component_entity(
    *,
    org_id: str,
    run_id: str,
    app_id: str,
    repo_id: str,
    commit_sha: Optional[str],
    component: Component,
) -> Entity:
    artifact = _artifact_ref(repo_id, commit_sha, component.path)
    return resolve_or_create_entity(
        org_id=org_id,
        entity_type="object",
        display_name=f"{component.qualified_name} ({app_id})",
        source_system=_SOURCE_SYSTEM,
        source_record_id=f"{artifact}#{component.qualified_name}",
        run_id=run_id,
        metadata={
            "structure_kind": "component",
            "component_kind": component.kind,
            "app_id": app_id,
            "platform": component.platform,
            "repo_id": repo_id,
            "commit_sha": commit_sha,
            "path": component.path,
            "qualified_name": component.qualified_name,
        },
    )


def _resolve_dependency_entity(
    *,
    org_id: str,
    run_id: str,
    app_id: str,
    repo_id: str,
    commit_sha: Optional[str],
    dependency: Dependency,
) -> Entity:
    label = _dependency_label(dependency)
    artifact = _artifact_ref(repo_id, commit_sha, dependency.path)
    return resolve_or_create_entity(
        org_id=org_id,
        entity_type="object",
        display_name=f"{label} ({app_id})",
        source_system=_SOURCE_SYSTEM,
        source_record_id=f"{artifact}#{label}",
        run_id=run_id,
        metadata={
            "structure_kind": "dependency",
            "app_id": app_id,
            "manifest": dependency.manifest,
            "repo_id": repo_id,
            "commit_sha": commit_sha,
            "path": dependency.path,
            "name": dependency.name,
            "group": dependency.group,
            "version": dependency.version,
            "scope": dependency.scope,
        },
    )


def _resolve_endpoint_entity(
    *,
    org_id: str,
    run_id: str,
    app_id: str,
    repo_id: str,
    commit_sha: Optional[str],
    endpoint: Endpoint,
) -> Entity:
    label = f"{endpoint.method} {endpoint.path}"
    artifact = _artifact_ref(repo_id, commit_sha, endpoint.source_path)
    return resolve_or_create_entity(
        org_id=org_id,
        entity_type="object",
        display_name=f"{label} ({app_id})",
        source_system=_SOURCE_SYSTEM,
        source_record_id=f"{artifact}#{label}#{endpoint.handler or ''}",
        run_id=run_id,
        metadata={
            "structure_kind": "endpoint",
            "app_id": app_id,
            "platform": endpoint.platform,
            "repo_id": repo_id,
            "commit_sha": commit_sha,
            "path": endpoint.source_path,
            "method": endpoint.method,
            "route": endpoint.path,
            "component": endpoint.component,
            "handler": endpoint.handler,
        },
    )


def _link(
    *,
    org_id: str,
    run_id: str,
    from_entity: Entity,
    to_entity: Entity,
    relationship_type: str,
    repo_id: str,
    commit_sha: Optional[str],
    path: str,
) -> bool:
    """Draw one OBSERVED edge between two RESOLVED structural entities.

    Mirrors relationship_mapper's map_directly_observed correctness contract: an
    edge is never drawn to/from an ambiguous endpoint — it would be semantically
    meaningless and would corrupt the evidence trace. Returns False (no edge
    written) rather than raising, so one skipped edge never aborts the load.
    """
    if from_entity.resolution_status == "ambiguous" or to_entity.resolution_status == "ambiguous":
        return False
    result = upsert_relationship(
        org_id,
        str(from_entity.id),
        str(to_entity.id),
        relationship_type,
        OBSERVED_CONFIDENCE,
        False,
        run_id,
        evidence={
            "source": _SOURCE_SYSTEM,
            "source_artifact": _artifact_ref(repo_id, commit_sha, path),
            "repo_id": repo_id,
            "commit_sha": commit_sha,
            "path": path,
        },
    )
    return result is not None


def ingest_app_structure(
    org_id: str,
    run_id: str,
    app_id: str,
    content_provider: ContentProvider,
    commit_sha_provider: Optional[CommitShaProvider] = None,
) -> GraphIngestResult:
    """Load one configured application's extracted structure into the graph.

    Resolves the app's configured mapping (platform + repo scope) via
    :mod:`.app_repo_map`, then extracts and loads structure PER REPO (rather than
    over the union :func:`extract_app_structure` builds) so every component,
    dependency, and endpoint can be stamped with the repo it actually came from
    — the union loses that association once repos are flattened together.

    Raises :class:`EnterpriseAppConfigError` when ``app_id`` is not configured
    (mirrors :func:`extract_app_structure`'s contract — a caller must not
    silently load nothing). A per-repo content-fetch or extraction failure is
    logged and that repo is skipped (other repos still load); a per-item
    resolution failure is logged and that item is skipped (counted in
    ``skipped_count``) — one bad component must not sink the whole app's load.
    """
    mapping = get_app_mapping(org_id, app_id)
    if mapping is None:
        raise EnterpriseAppConfigError(
            f"no configured app→repo mapping for app_id '{app_id}' (org={org_id})"
        )

    app_entity = _resolve_app_entity(org_id, run_id, mapping)

    component_count = 0
    dependency_count = 0
    endpoint_count = 0
    relationship_count = 0
    skipped_count = 0

    for repo_id in mapping.repo_ids:
        commit_sha = commit_sha_provider(repo_id) if commit_sha_provider else None

        try:
            files = list(content_provider(repo_id) or [])
        except Exception as exc:  # noqa: BLE001 — isolate one repo's failure
            logger.warning(
                "enterprise_apps graph ingest: content lookup failed "
                "(org=%s app=%s repo=%s): %s",
                org_id, app_id, repo_id, exc,
            )
            continue

        try:
            structure = extract_structure(files, mapping.platform)
        except Exception as exc:  # noqa: BLE001 — isolate one repo's failure
            logger.warning(
                "enterprise_apps graph ingest: structure extraction failed "
                "(org=%s app=%s repo=%s): %s",
                org_id, app_id, repo_id, exc,
            )
            continue

        # Endpoint -> declaring-component edges only make sense within the same
        # repo/extraction pass, so this map is rebuilt per repo.
        component_by_name: Dict[str, Entity] = {}

        for component in structure.components:
            try:
                entity = _resolve_component_entity(
                    org_id=org_id,
                    run_id=run_id,
                    app_id=app_id,
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    component=component,
                )
            except Exception as exc:  # noqa: BLE001 — one bad item, not the run
                skipped_count += 1
                logger.warning(
                    "enterprise_apps graph ingest: component resolution failed "
                    "for %s (org=%s app=%s repo=%s): %s",
                    component.qualified_name, org_id, app_id, repo_id, exc,
                )
                continue
            component_count += 1
            component_by_name.setdefault(component.name, entity)
            if _link(
                org_id=org_id,
                run_id=run_id,
                from_entity=app_entity,
                to_entity=entity,
                relationship_type=REL_OWNS,
                repo_id=repo_id,
                commit_sha=commit_sha,
                path=component.path,
            ):
                relationship_count += 1

        for dependency in structure.dependencies:
            try:
                entity = _resolve_dependency_entity(
                    org_id=org_id,
                    run_id=run_id,
                    app_id=app_id,
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    dependency=dependency,
                )
            except Exception as exc:  # noqa: BLE001 — one bad item, not the run
                skipped_count += 1
                logger.warning(
                    "enterprise_apps graph ingest: dependency resolution failed "
                    "for %s (org=%s app=%s repo=%s): %s",
                    dependency.name, org_id, app_id, repo_id, exc,
                )
                continue
            dependency_count += 1
            if _link(
                org_id=org_id,
                run_id=run_id,
                from_entity=app_entity,
                to_entity=entity,
                relationship_type=REL_DEPENDS_ON,
                repo_id=repo_id,
                commit_sha=commit_sha,
                path=dependency.path,
            ):
                relationship_count += 1

        for endpoint in structure.endpoints:
            try:
                entity = _resolve_endpoint_entity(
                    org_id=org_id,
                    run_id=run_id,
                    app_id=app_id,
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    endpoint=endpoint,
                )
            except Exception as exc:  # noqa: BLE001 — one bad item, not the run
                skipped_count += 1
                logger.warning(
                    "enterprise_apps graph ingest: endpoint resolution failed "
                    "for %s %s (org=%s app=%s repo=%s): %s",
                    endpoint.method, endpoint.path, org_id, app_id, repo_id, exc,
                )
                continue
            endpoint_count += 1
            owner = component_by_name.get(endpoint.component) if endpoint.component else None
            if owner is not None and _link(
                org_id=org_id,
                run_id=run_id,
                from_entity=owner,
                to_entity=entity,
                relationship_type=REL_ROUTES_TO,
                repo_id=repo_id,
                commit_sha=commit_sha,
                path=endpoint.source_path,
            ):
                relationship_count += 1

    return GraphIngestResult(
        app_id=app_id,
        app_entity_id=str(app_entity.id),
        component_count=component_count,
        dependency_count=dependency_count,
        endpoint_count=endpoint_count,
        relationship_count=relationship_count,
        skipped_count=skipped_count,
    )
