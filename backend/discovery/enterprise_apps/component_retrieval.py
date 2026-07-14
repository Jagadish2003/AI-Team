"""R18-A6 / AT-610 (T5) — component-scoped retrieval over A2-ingested code.

Bridges T1/T2 (:mod:`.structure`'s deterministic ``AppStructure`` extraction) and
T6 (:mod:`.app_repo_map`'s configured app→repo scope) into the platform retrieval
API (:mod:`app.retrieval.api`), so a caller can ask for "the code of the covenant
service" and get exactly that component's real files — never a path/name
coincidence.

Code content is already retrievable via R18-A2 Git content ingestion
(``source_system='git'``, ``source_artifact`` = ``"{repo_id}:{path}"`` — the
EXACT convention :mod:`discovery.ingest.git_content` writes). What this module
adds is the STRUCTURE-AWARE filter: it reads the structure map (component ->
declaring file) to build an EXACT allowlist of ``source_artifact`` ids, then
scopes retrieval to precisely that set via ``retrieve(..., artifact_filter=...)``
/ ``store.list_chunks_by_artifacts()``.

Why this can't be a substring/path match (AC5): a naive ``LIKE '%CovenantService%'``
would also match ``CovenantServiceTest.java``, a comment mentioning the class in
an unrelated file, or another component that happens to share a word. The
structure map is the only source of truth for "which file(s) this component
actually is" — matching Java's/​.NET's own stereotype-based extraction, never a
heuristic on the path text itself.

Component identity is matched EXACTLY (case-insensitively) against a
component's ``qualified_name`` or simple ``name`` — never a substring — so
``"CovenantService"`` never accidentally matches ``"CovenantServiceV2"``.
"""
from __future__ import annotations

import logging
from typing import List, Optional
from uuid import uuid4

from app.retrieval import store
from app.retrieval.api import RetrievedChunk, retrieve

from .app_repo_map import ContentProvider, EnterpriseAppConfigError, get_app_mapping
from .structure import Component, extract_structure

logger = logging.getLogger(__name__)

#: Matches discovery.ingest.git_content's own source_system for A2-ingested code.
GIT_SOURCE_SYSTEM = "git"


def _artifact_id(repo_id: str, path: str) -> str:
    """The EXACT retrieval ``source_artifact`` for one repo file.

    Matches :mod:`discovery.ingest.git_content`'s own convention byte-for-byte
    (``f"{repo_id}:{path}"``), so an allowlist built from these ids lines up
    exactly with what A2 indexed — no separate identifier scheme to keep in
    sync.
    """
    return f"{repo_id}:{path}"


def _component_matches(component: Component, component_ref: str) -> bool:
    """True iff ``component_ref`` EXACTLY (case-insensitively) names this component.

    Matches on ``qualified_name`` or the simple ``name`` — never a substring —
    so a coincidental partial match (a differently-named component, a decoy
    file mentioning the name) can never widen the scope (AC5).
    """
    ref = (component_ref or "").strip().lower()
    if not ref:
        return False
    return (
        component.qualified_name.strip().lower() == ref
        or component.name.strip().lower() == ref
    )


def resolve_component_artifacts(
    org_id: str,
    app_id: str,
    component_ref: str,
    content_provider: ContentProvider,
) -> List[str]:
    """The exact retrieval ``source_artifact`` ids for one component's files.

    Resolves the app's configured repo scope (:mod:`.app_repo_map`), extracts
    structure PER REPO (rather than over the flattened union
    :func:`extract_app_structure` builds) so each matching component can be
    paired with the repo it actually came from, and returns the
    ``"{repo_id}:{path}"`` ids — exactly A2's own ``source_artifact``
    convention — of every file where a component matching ``component_ref``
    (exact match on ``qualified_name``/``name``, never a substring) was
    declared.

    Raises :class:`EnterpriseAppConfigError` when ``app_id`` is not configured
    (mirrors :func:`extract_app_structure`'s contract — a caller must not
    silently scope to nothing without knowing why). Returns ``[]`` when the app
    IS configured but no component matches ``component_ref``, or a repo's
    content/extraction fails — a scoping miss is a normal outcome, never a
    crash, matching the retrieval substrate's own philosophy.
    """
    mapping = get_app_mapping(org_id, app_id)
    if mapping is None:
        raise EnterpriseAppConfigError(
            f"no configured app→repo mapping for app_id '{app_id}' (org={org_id})"
        )

    artifacts: List[str] = []
    seen: set = set()
    for repo_id in mapping.repo_ids:
        try:
            files = list(content_provider(repo_id) or [])
        except Exception as exc:  # noqa: BLE001 — isolate one repo's failure
            logger.warning(
                "enterprise_apps component retrieval: content lookup failed "
                "(org=%s app=%s repo=%s): %s",
                org_id, app_id, repo_id, exc,
            )
            continue

        try:
            structure = extract_structure(files, mapping.platform)
        except Exception as exc:  # noqa: BLE001 — isolate one repo's failure
            logger.warning(
                "enterprise_apps component retrieval: structure extraction failed "
                "(org=%s app=%s repo=%s): %s",
                org_id, app_id, repo_id, exc,
            )
            continue

        for component in structure.components:
            if not _component_matches(component, component_ref):
                continue
            artifact = _artifact_id(repo_id, component.path)
            if artifact not in seen:
                seen.add(artifact)
                artifacts.append(artifact)

    return artifacts


def retrieve_component_code(
    org_id: str,
    app_id: str,
    component_ref: str,
    content_provider: ContentProvider,
    query_text: Optional[str] = None,
    k: int = 10,
    min_score: Optional[float] = None,
    include_stale: bool = False,
) -> List[RetrievedChunk]:
    """Retrieval scoped to ONE component's real files — AC5.

    Builds the component's exact file scope via
    :func:`resolve_component_artifacts` and retrieves ONLY from those files —
    never a path/name substring match, so a coincidentally-named file (a
    comment mentioning the component, an unrelated ``*Test`` file, a different
    component sharing a word) can never be returned as this component's code.

    ``query_text`` runs a normal semantic search scoped to the component (best
    match first, respecting ``k``/``min_score``) via the platform
    :func:`~app.retrieval.api.retrieve` API. Omit it (or pass blank) to simply
    list the component's currently-indexed code directly, ordered file-then-
    position, capped at ``k`` — useful when the caller wants the component's
    code itself rather than an answer to a question about it.

    Returns ``[]`` when the component has no matching files, no indexed
    content, or (with a query) no chunk clears ``min_score`` — a scoping miss,
    not an error. Raises :class:`EnterpriseAppConfigError` only when ``app_id``
    itself is not configured (see :func:`resolve_component_artifacts`).
    """
    artifacts = resolve_component_artifacts(org_id, app_id, component_ref, content_provider)
    if not artifacts:
        return []

    if query_text and query_text.strip():
        return retrieve(
            org_id,
            query_text,
            k=k,
            source_filter=[GIT_SOURCE_SYSTEM],
            artifact_filter=artifacts,
            min_score=min_score,
            include_stale=include_stale,
        )

    return _list_component_chunks(org_id, artifacts, k, include_stale=include_stale)


def _list_component_chunks(
    org_id: str, artifacts: List[str], k: int, *, include_stale: bool
) -> List[RetrievedChunk]:
    """All currently-indexed, embedded chunks for an exact artifact set, no query.

    There is no query vector to rank against, so this is a direct read
    (:func:`app.retrieval.store.list_chunks_by_artifacts`), not a similarity
    search — each result carries similarity ``1.0`` (an exact-scope listing,
    not a ranked match) so the shape stays consistent with a normal
    :class:`RetrievedChunk`.
    """
    if k <= 0:
        return []
    rows = store.list_chunks_by_artifacts(
        org_id, artifacts, limit=k, include_stale=include_stale
    )
    results: List[RetrievedChunk] = []
    for row in rows:
        ts = row.get("source_timestamp")
        results.append(
            RetrievedChunk(
                content=row["content"],
                similarity=1.0,
                source_system=row["source_system"],
                source_artifact=row["source_artifact"],
                chunk_id=row["chunk_id"],
                retrieval_result_id=str(uuid4()),
                source_timestamp=ts.isoformat() if hasattr(ts, "isoformat") else (ts or ""),
                is_stale=bool(row.get("is_stale", False)),
            )
        )
    return results
