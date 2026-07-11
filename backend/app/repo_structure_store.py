"""
R18-A2 / AT-534 (T6) — persistence for repository structural metadata.

The *graph-facing* store for the directory-tree + file-inventory metadata the Git
content ingestor captures (:mod:`discovery.ingest.repo_structure`). This is the
persistence EDGE only: the shape is built in the pure discovery module, this
module just writes/reads it — mirroring how ``app.retrieval.ingest`` is the edge
for file *content*. The two are deliberately different destinations:

  * file BODIES  → ``app.retrieval.ingest.ingest_content`` → chunked + embedded
                   into the vector store (retrievable content);
  * repo SHAPE   → here → a plain org-scoped metadata record (NOT embedded), which
                   the Sprint-2 Java/.NET application-structure story reads to
                   reason about applications.

Storage model
-------------
One snapshot per ``(org_id, repo_id)`` at the repo's current HEAD, keyed in the
shared ``kv`` table. The key embeds ``org_id`` so a snapshot is org-scoped and
cross-run (graph state is cross-run — a later run's structure supersedes the
earlier one for that repo), exactly like the connector's per-repo checkpoint but
in a separate namespace. Re-persisting the same commit is idempotent: the value is
a deterministic function of the tree at that SHA.

No new table / migration is introduced: the metadata is small, per-repo, and
read as a whole by its consumer, so the existing key/value store is the right
weight. If graph traversal ever needs to query INTO the structure (e.g. "which
repos contain a ``Dockerfile``"), that is a Sprint-2 concern that can promote this
to first-class graph rows then — this story only has to CAPTURE and STORE it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app import db

logger = logging.getLogger(__name__)

#: KV key namespace for structural snapshots. Disjoint from the connector's
#: checkpoint namespace and from run-scoped KV, so nothing collides.
_STRUCTURE_KEY_PREFIX = "git_content:structure"


def structure_key(org_id: str, repo_id: str) -> str:
    """The KV key for one org's snapshot of one repo. org_id first so a prefix
    scan by org is possible; both parts are embedded so cross-org / cross-repo
    reads can never alias."""
    return f"{_STRUCTURE_KEY_PREFIX}:{org_id}:{repo_id}"


def persist_repo_structure(org_id: str, structure: Dict[str, Any]) -> None:
    """Persist one repo's structural snapshot as graph-facing metadata (AT-534).

    ``structure`` is a :meth:`RepoStructure.to_dict` payload; its ``repo_id``
    identifies which repo the snapshot belongs to. Writing REPLACES any previous
    snapshot for ``(org_id, repo_id)`` — a repo has exactly one current shape, at
    its latest HEAD. Raises ``ValueError`` on a malformed call (blank ``org_id`` or
    a payload without a ``repo_id``); the caller isolates persistence failures so a
    metadata write never sinks content ingestion.
    """
    if org_id is None or not str(org_id).strip():
        raise ValueError("org_id is required")
    if not isinstance(structure, dict):
        raise ValueError("structure must be a RepoStructure.to_dict() payload")
    repo_id = str(structure.get("repo_id", "")).strip()
    if not repo_id:
        raise ValueError("structure payload must carry a non-empty repo_id")

    db.kv_set(structure_key(org_id, repo_id), structure)
    logger.info(
        "repo_structure: stored shape for org=%s repo=%s sha=%s "
        "(files=%s dirs=%s binary=%s)",
        org_id,
        repo_id,
        structure.get("commit_sha"),
        structure.get("file_count"),
        structure.get("directory_count"),
        structure.get("binary_file_count"),
    )


def load_repo_structure(org_id: str, repo_id: str) -> Optional[Dict[str, Any]]:
    """Return the stored structural snapshot for ``(org_id, repo_id)``, or ``None``.

    Used both by the ingestor (to seed an incremental update from the last
    snapshot) and by downstream consumers (the Sprint-2 application-structure
    work). Never raises for a missing snapshot — an absent key is a plain
    ``None``.
    """
    if org_id is None or not str(org_id).strip() or not str(repo_id or "").strip():
        return None
    value = db.kv_get(structure_key(org_id, repo_id))
    return value if isinstance(value, dict) else None
