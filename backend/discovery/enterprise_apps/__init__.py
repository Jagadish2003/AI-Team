"""R18-A6 — Java & .NET application code & structure extraction.

Phase two of enterprise-application ingestion. Where phase one
(:mod:`discovery.ingest.operational_signals`, R17-A3/A4) sees the running
application *behave*, this package sees how it is *built*: it reads repository
content already moved into AgentIQ by R18-A2 Git content ingestion and extracts
the application's STRUCTURE — components, build-declared dependencies, declared
REST endpoints, and configuration shape (keys kept, values redacted).

Scope discipline (R18-A6 §"what this story is NOT"): this is not a
static-analysis engine, a linter, or a security scanner. It extracts structure
and makes code retrievable as evidence so operational findings can be located
and explained in the application's own terms — the findings remain
operational-friction findings, now with code-level grounding.

The extraction is DETERMINISTIC — build files, annotations/attributes and
directory convention only, no LLM in the extraction path (structure is observed,
never inferred). The shared model lives in :mod:`.structure`; platform-specific
parsers sit at the edges (Java: T1/AT-606; .NET: T2/AT-607), mirroring the
share-the-extraction discipline established for the operational phase.

:mod:`.app_repo_map` (T6/AT-611) owns the per-org, configured (never
auto-discovered) declaration of which repos ARE an application — the scope T1/T2
parse over and T5 scopes retrieval against.

:mod:`.component_retrieval` (T5/AT-610) bridges the structure map into the
platform retrieval API, so retrieval can be scoped to one component's real
files — never a path/name coincidence.
"""

from .app_repo_map import (
    AppRepoMapping,
    ContentProvider,
    EnterpriseAppConfigError,
    app_for_repo,
    extract_app_structure,
    get_app_mapping,
    load_app_repo_mappings,
    repo_content_for_app,
    repo_ids_for_app,
)
from .component_retrieval import (
    resolve_component_artifacts,
    retrieve_component_code,
)
from .structure import (
    AppStructure,
    Component,
    Dependency,
    Endpoint,
    RepoFile,
    extract_structure,
)

__all__ = [
    # structure (T1)
    "AppStructure",
    "Component",
    "Dependency",
    "Endpoint",
    "RepoFile",
    "extract_structure",
    # app→repo mapping (T6)
    "AppRepoMapping",
    "ContentProvider",
    "EnterpriseAppConfigError",
    "app_for_repo",
    "extract_app_structure",
    "get_app_mapping",
    "load_app_repo_mappings",
    "repo_content_for_app",
    "repo_ids_for_app",
    # component-scoped retrieval (T5)
    "resolve_component_artifacts",
    "retrieve_component_code",
]
