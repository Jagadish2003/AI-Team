"""
R18-A2 / AT-529 (T1) — Git content change-based ingestor.

Reads repository CONTENT (source and text files at HEAD, plus the file set each
new commit touches) into the retrieval substrate. This is distinct from the
existing GitHub *signal* connector (``connectors/saas/github.py``), which reads
activity metadata only for the engineering pack — content is a separate, heavier
layer with its own cost profile, opt-in per repository (R18-A2 §4).

The version-control graph IS the change feed (R18-A2 §1)
-------------------------------------------------------
Git is the ideal change-based source, so this connector does NOT re-read a repo
in full on every run:

  * the opaque checkpoint ``value`` is simply the **last-ingested commit SHA per
    repo** (AC1);
  * a **first run** streams the tree at HEAD as checkpointed, **resumable**
    batches — a failure mid-load resumes from the last saved batch rather than
    restarting the whole load (AC1);
  * an **incremental run** asks git for ``since..HEAD`` and processes ONLY the
    files those commits touched — verified by commit-count vs files-processed
    (AC2). No polling heuristics, no timestamps.

Scope of THIS subtask (AT-529 — AC1 + AC2)
------------------------------------------
This file establishes the change-based mechanism the rest of R18-A2 reads off:
the SHA checkpoint, the diff-driven incremental, and the resumable first load.
Deletions surfaced by the diff are emitted as ``change_kind='deleted'`` records
(so the shared runner emits ``ingestion.artifact_changed`` deleted events) — the
foundation the deletion-propagation / freshness-removal work (AT-533) builds on.
The remaining R18-A2 subtasks slot into the clearly-marked seams here without
re-plumbing this mechanism:

  * per-repo include/exclude path configuration — AT-530 (IMPLEMENTED:
    ``_select_paths`` / :class:`PathFilter` / :data:`DEFAULT_EXCLUDE_GLOBS`);
  * secret-pattern scan + redaction before the substrate — AT-531
    (``_secret_scan``, currently a pass-through);
  * commit-message corpus ingestion — AT-532;
  * deletion propagation into retrieval freshness — AT-533 (IMPLEMENTED:
    deleted diff files route to ``retrieval.ingest.remove_content`` so their
    chunks leave retrieval — see ``_remove_content`` / the class docstring);
  * structural (tree/inventory) metadata — AT-534.

Include/exclude path filtering (R18-A2 §1 / AT-530)
---------------------------------------------------
Content is source and text files, not third-party code or machine-generated
noise, so vendored dependencies, generated/build output and dependency lockfiles
are excluded by default (:data:`DEFAULT_EXCLUDE_GLOBS`). The defaults are
editable per org (fixture ``path_defaults`` / ``GIT_CONTENT_PATH_DEFAULTS``) and
per repo (each repo's ``include`` allow-list, ``exclude`` globs, and
``use_default_excludes`` toggle). The filter (:class:`PathFilter`) is applied to
BOTH the first-load tree and the incremental diff, so an excluded path is never
ingested however it was surfaced (AC4). Binary files are additionally skipped-
with-reason and never indexed as garbage text (AC7).

Content handover (R18-A2 §1, and the R18-B1 producer contract)
--------------------------------------------------------------
File content is handed to the retrieval substrate through the ONE standard entry
point ``app.retrieval.ingest.ingest_content(org_id, artifacts)``: the producer
supplies extracted text + provenance (``ContentArtifact``) and NOTHING else —
chunking (the substrate's *code* policy: file/function boundaries), hashing,
embedding and indexing all happen inside the substrate. This connector never
writes vectors. Each file chunk carries ``repo``/``path``/commit-SHA provenance
with ``origin='observed'`` (R16-B1). Binary files are skipped-with-reason and
never indexed as garbage text.

Checkpoint shape (opaque to the runner)
---------------------------------------
A single ``(org_id, 'git_content')`` checkpoint row is persisted by the runner,
but an org can configure many repos each advancing independently. The connector
therefore encodes a per-repo cursor MAP as the opaque checkpoint value, keyed by
repo id::

    {"v": 1, "repos": {"web-app": "c3c3c3c3", "data-pipeline": "d2d2d2d2"}}

A repo entry that is a plain SHA string means "fully synced at that commit" — the
steady state, so the checkpoint literally IS the head SHA per repo (AC1). While a
first load of a repo is still streaming, that repo's entry is instead a small
object carrying how far the HEAD tree has been loaded::

    {"v": 1, "repos": {"web-app": {"sha": "c3c3c3c3", "offset": 3}}}

so an interrupted first load resumes from ``offset`` on the next run rather than
restarting. Once the tree is fully loaded the entry collapses back to the plain
SHA. The runner never interprets any of this (R16-A1 AC5) — only this connector,
which owns the shape, decodes it.

Offline vs live
---------------
Offline (default, ``INGEST_MODE`` != ``live``): reads the deterministic fixture
``fixtures/git_content_sample.json`` — parity with the other connectors, so the
whole pipeline runs with no repositories cloned. Live: shells out to ``git`` in a
local clone declared per deployment via ``GIT_CONTENT_REPOS`` (a JSON array of
secret-free repo configs); ``git rev-parse`` / ``git ls-tree`` / ``git diff`` /
``git show`` provide the head SHA, tree, changed-file set and file content.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from app.provenance import EvidencePointer, utc_now_iso

from . import is_live
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "git_content_sample.json"

#: The retrieval source-system tag for git file content (R18-B1
#: ``KNOWN_SOURCE_SYSTEMS``). Distinct from the change-based ``connector_id``
#: below, which is the checkpoint key — the two live in different namespaces.
SOURCE_SYSTEM = "git"

#: Git source/text files chunk under the substrate's *code* policy (file/function
#: boundaries) — R18-A2 §1.
CONTENT_TYPE = "code"

#: Commit messages are ingested "as conversation-like content" (R18-A2 §1 /
#: AT-532), so they chunk under the substrate's *conversation* policy — distinct
#: from the *code* policy the file bodies use.
COMMIT_CONTENT_TYPE = "conversation"

#: Separator between a repo id and a commit SHA in a commit message's
#: ``source_artifact``. Deliberately different from the ``':'`` used for file
#: paths (``"{repo_id}:{path}"``) so a commit-message artifact can never collide
#: with a file artifact in the substrate's ``(source_system, source_artifact)``
#: identity.
_COMMIT_ARTIFACT_SEP = "@"

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default number of files emitted per :class:`DeltaBatch`. Kept modest so a large
#: initial tree load streams as many small, individually-checkpointed batches
#: (AC1 resumability) rather than one monolithic read.
_DEFAULT_BATCH_SIZE = 100

#: Env var (live mode) holding a JSON array of repo configs for the deployment.
_REPOS_ENV = "GIT_CONTENT_REPOS"

#: Env var (live mode) holding an org-level path-filter defaults object
#: (``{"include": [...], "exclude": [...], "use_default_excludes": bool}``) applied
#: to every repo unless the repo overrides it — the "editable per org" surface.
_PATH_DEFAULTS_ENV = "GIT_CONTENT_PATH_DEFAULTS"

_GIT_TIMEOUT = 120

#: Sensible built-in exclude globs (R18-A2 §1 / AT-530): vendored dependencies,
#: generated/build output, and dependency lockfiles are excluded by default so a
#: content run indexes source, not third-party code or machine-generated noise. A
#: pattern with no ``/`` matches that name as any path segment at any depth
#: (gitignore-style); a pattern with a ``/`` matches the whole path or a subtree.
#: An org opts out with ``use_default_excludes: false`` or re-includes a specific
#: path with an ``include`` allow-list — see :class:`PathFilter`.
DEFAULT_EXCLUDE_GLOBS: Tuple[str, ...] = (
    # ── vendored dependencies ──
    "node_modules",
    "bower_components",
    "vendor",
    "third_party",
    "third-party",
    ".venv",
    "venv",
    "site-packages",
    "Pods",
    # ── generated / build output ──
    "dist",
    "build",
    "out",
    "target",
    "bin",
    "obj",
    "__pycache__",
    ".next",
    ".nuxt",
    "coverage",
    "generated",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*_pb2.py",
    "*.pb.go",
    "*.generated.*",
    # ── dependency lockfiles ──
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Gemfile.lock",
    "composer.lock",
    "Cargo.lock",
    "go.sum",
    "packages.lock.json",
)

# git diff --name-status status letters -> our ChangeKind.
_STATUS_TO_KIND = {
    "A": ChangeKind.CREATED,
    "M": ChangeKind.UPDATED,
    "D": ChangeKind.DELETED,
    "T": ChangeKind.UPDATED,  # type change (e.g. file <-> symlink)
}


class GitContentError(Exception):
    """Raised when live git content ingestion fails with an actionable message."""


# ---------------------------------------------------------------------------
# Opaque per-repo checkpoint encoding (owned here; opaque to the runner)
# ---------------------------------------------------------------------------


def _encode_checkpoint(cursors: Dict[str, Dict[str, Any]]) -> str:
    """Encode the per-repo cursor map as the opaque checkpoint value.

    A cursor whose ``offset`` is None (repo fully synced) is written as a plain
    SHA string so the steady-state checkpoint literally IS the head SHA per repo
    (AC1); an in-progress first load keeps its ``{"sha", "offset"}`` object so it
    can resume. ``sort_keys`` keeps the encoding deterministic so two runs over
    identical state produce byte-identical checkpoints.
    """
    repos: Dict[str, Any] = {}
    for repo_id, cur in cursors.items():
        if cur.get("offset") is None:
            repos[repo_id] = cur["sha"]
        else:
            repos[repo_id] = {"sha": cur["sha"], "offset": cur["offset"]}
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "repos": repos},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Decode an opaque checkpoint value back into the per-repo cursor map.

    Every entry normalises to ``{"sha": str, "offset": int | None}`` where
    ``offset is None`` means the repo is fully synced at ``sha``. Tolerant by
    design: a missing, empty, or unparseable value yields an empty map (every
    repo read from the beginning as a first load) rather than raising — a
    degenerate checkpoint must degrade to a safe full re-read, never crash the run.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning(
            "git_content: could not decode checkpoint value; treating as first run "
            "(full re-read). value=%r",
            value,
        )
        return {}
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for repo_id, entry in repos.items():
        if isinstance(entry, str) and entry:
            out[str(repo_id)] = {"sha": entry, "offset": None}
        elif isinstance(entry, dict) and entry.get("sha"):
            off = entry.get("offset")
            out[str(repo_id)] = {
                "sha": str(entry["sha"]),
                "offset": int(off) if isinstance(off, int) else None,
            }
    return out


def _build_evidence_pointer(
    repo_id: str, path: str, commit_sha: str, timestamp: Optional[str]
) -> Dict[str, Any]:
    """Build the R16-B1 OBSERVED EvidencePointer for one git file artifact.

    Every git file signal must be traceable back to its exact source, so each
    record/chunk carries a fully-populated, observed provenance pointer:

      * ``source_system`` = ``'git'``
      * ``source_artifact`` = ``"{repo_id}:{path}"`` — the stable file identity
        (so ``source_artifact_type`` is ``'record_id'``), identical to the
        record's ``artifact_id``.
      * ``source_timestamp`` = the HEAD commit timestamp; falls back to now only
        when missing, so the mandatory spine is always populated.
      * ``origin`` = ``'observed'`` — read directly from the repository.
    """
    return EvidencePointer.observed(
        source_system=SOURCE_SYSTEM,
        source_artifact=f"{repo_id}:{path}",
        source_timestamp=timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


def _commit_artifact_id(repo_id: str, sha: str) -> str:
    """Stable substrate identity for one commit message (AT-532).

    ``"{repo_id}@{sha}"`` — a namespace deliberately disjoint from file artifacts
    (``"{repo_id}:{path}"``) so a commit message and a file can never share a
    ``(source_system, source_artifact)`` key in the store.
    """
    return f"{repo_id}{_COMMIT_ARTIFACT_SEP}{sha}"


def _build_commit_evidence_pointer(
    repo_id: str, sha: str, timestamp: Optional[str]
) -> Dict[str, Any]:
    """Build the R16-B1 OBSERVED EvidencePointer for one commit message (AT-532).

    Mirrors the file pointer: the commit message was read directly from the
    repository (``origin='observed'``), its ``source_artifact`` is the stable
    ``"{repo_id}@{sha}"`` commit identity (``source_artifact_type='record_id'``),
    and ``source_timestamp`` is the commit's authored date — falling back to now
    only when the date is missing so the mandatory spine is always populated.
    """
    return EvidencePointer.observed(
        source_system=SOURCE_SYSTEM,
        source_artifact=_commit_artifact_id(repo_id, sha),
        source_timestamp=timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


# ---------------------------------------------------------------------------
# Include/exclude path filtering (AT-530)
# ---------------------------------------------------------------------------


def _as_str_tuple(value: Any) -> Tuple[str, ...]:
    """Coerce a config value to a tuple of non-empty glob strings.

    Tolerant of the shapes a hand-edited config produces: a list of patterns, a
    single string pattern, or nothing. Blank entries are dropped so an empty
    string never becomes a match-everything glob.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(v).strip() for v in value if v is not None and str(v).strip())


def _match_path(path: str, pattern: str) -> bool:
    """True when ``path`` matches a single include/exclude glob ``pattern``.

    gitignore-flavoured, deterministic and case-sensitive (git paths are):

      * a pattern with NO ``/`` matches that name as ANY path segment at any depth
        — ``node_modules`` excludes ``frontend/node_modules/x.js``; ``*.min.js``
        matches the basename anywhere; ``yarn.lock`` matches it in any directory;
      * a pattern WITH a ``/`` is matched against the whole path, and a directory
        prefix (``src/generated``) also matches everything beneath it.
    """
    pattern = pattern.strip().strip("/")
    if not pattern:
        return False
    path = path.strip().lstrip("/")
    if "/" not in pattern:
        return any(fnmatchcase(seg, pattern) for seg in path.split("/"))
    if fnmatchcase(path, pattern):
        return True
    return path == pattern or path.startswith(pattern + "/")


@dataclass(frozen=True)
class PathFilter:
    """Resolves whether a repo path is in scope for content ingestion (AT-530).

    Two ordered rules, so behaviour is predictable and re-includable:

      1. ``include`` is an allow-list: when non-empty, a path must match at least
         one include glob to be a candidate (empty ``include`` admits everything);
      2. ``exclude`` then removes any candidate that matches an exclude glob.

    The effective ``exclude`` is the built-in :data:`DEFAULT_EXCLUDE_GLOBS`
    (vendored/generated/lockfiles) unioned with the repo/org excludes — unless the
    built-ins are disabled. Reusable by a future GitLab/Bitbucket content source
    (R18-A2 §4, "General mechanism first").
    """

    include: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()

    def allows(self, path: str) -> bool:
        p = (path or "").strip().lstrip("/")
        if not p:
            return False
        if self.include and not any(_match_path(p, pat) for pat in self.include):
            return False
        if any(_match_path(p, pat) for pat in self.exclude):
            return False
        return True


# ---------------------------------------------------------------------------
# Repo configuration + the file/diff model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitRepoConfig:
    """One configured repository AgentIQ is allowed to read content from.

    Content is opt-in per repository (R18-A2 §4), so an org explicitly declares
    each repo. Offline the config comes from the fixture; live from
    ``GIT_CONTENT_REPOS``. ``path`` is the local clone directory (live only).

    Per-repo path filtering (AT-530): ``include`` / ``exclude`` are glob lists and
    ``use_default_excludes`` toggles the built-in vendored/generated/lockfile
    defaults. These carry the values already merged with the org-level defaults
    (repo settings win); the built-in defaults are applied when the filter is
    built (:meth:`GitContentIngestor._filter_for`).
    """

    repo_id: str
    branch: str = "HEAD"
    path: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    include: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()
    use_default_excludes: bool = True


@dataclass
class _FileWork:
    """One file to emit in a batch: a change record and (unless a deletion) the
    content handed to the substrate."""

    path: str
    change_kind: str
    content: Optional[str] = None  # None for deletions
    commit_ts: Optional[str] = None


@dataclass
class _RepoPlan:
    """The planned work for one repo this run — a slice of files plus the mode
    and the target HEAD sha the repo advances to on completion.

    ``commits`` is the commit-message corpus to hand over this run (AT-532): the
    full corpus at HEAD on a first load, or only the ``since..HEAD`` commits on an
    incremental run. It is content-only (handed to the substrate, never a file-
    change delta record), so a plan can be commit-only (``items`` empty)."""

    repo_id: str
    head_sha: str
    mode: str  # "full" (tree load) | "diff" (incremental)
    items: List[_FileWork]
    base_offset: int  # files of the HEAD tree already loaded before this run (full mode)
    commits: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The ingestor
# ---------------------------------------------------------------------------


class GitContentIngestor(ChangeBasedIngestor):
    """Change-based git *content* ingestor (R18-A2 / AT-529).

    Encodes its position as a per-repo commit-SHA cursor map (opaque to the
    runner) and, per repo, either streams the HEAD tree as a resumable first load
    (``since`` absent for that repo) or processes only the files ``since..HEAD``
    touched (incremental). File content is handed to the retrieval substrate via
    ``ingest_content``; the yielded :class:`DeltaBatch` records drive the shared
    runner's ``ingestion.artifact_changed`` events and the checkpoint lifecycle.

    Deletes / tombstones (R16-A1 §5) + freshness removal (AT-533, AC3)
    ------------------------------------------------------------------
    ``reports_deletes = True``: a git diff natively reports deletions, so a file
    removed by a commit is yielded as a ``change_kind='deleted'`` record (no
    content) — the shared runner emits it as a ``deleted`` event. AT-533 completes
    the loop: the same deleted files are handed to the substrate's freshness
    removal (``retrieval.ingest.remove_content``) IN the batch, so their chunks
    leave retrieval and stop being returned as evidence (AC3). Removal hooks
    directly into the diff mechanism — no separate event-bus consumer — and is
    idempotent, so a re-run of the same delete is a harmless no-op.
    """

    connector_id = "git_content"
    reports_deletes = True

    def __init__(
        self,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        *,
        ingest_fn: Optional[Callable[[str, List[Any]], Any]] = None,
        remove_fn: Optional[Callable[[str, List[Any]], Any]] = None,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size
        # Injectable substrate handover (defaults to the real producer contract,
        # lazy-imported so this discovery module carries no import-time dependency
        # on the app.retrieval package). Tests pass a fake to avoid the store/DB.
        self._ingest_fn = ingest_fn
        # Injectable substrate freshness-removal (AT-533): defaults to
        # ``retrieval.ingest.remove_content``. Deletions surfaced by the diff are
        # routed here so their chunks leave retrieval (AC3).
        self._remove_fn = remove_fn
        # Per-repo PathFilter memo (AT-530): built once per repo per run, since the
        # configured include/exclude rules are stable for the life of the ingestor.
        self._filter_cache: Dict[str, PathFilter] = {}

    # ── ChangeBasedIngestor contract ────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of changed git content since ``since``.

        First run for a repo (its SHA absent from the checkpoint map): stream the
        HEAD tree as checkpointed, resumable batches (AC1). Incremental: only the
        files ``since..HEAD`` touched (AC2). An unchanged source yields a single
        empty :class:`DeltaBatch` whose ``next_checkpoint`` echoes the incoming
        position (AC2).
        """
        cursors = _decode_checkpoint(since.value if since else None)
        # Working copy advanced as batches are emitted; each yielded
        # next_checkpoint encodes the cumulative map so any single batch is a valid
        # resume point on the next run.
        running: Dict[str, Dict[str, Any]] = {
            rid: dict(cur) for rid, cur in cursors.items()
        }

        repos = self._configured_repos(org_id)
        logger.info(
            "git_content: org=%s %s — %d configured repo(s)",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(repos),
        )

        # Plan each repo's work first, so we know the single terminal batch across
        # all repos and can flag is_complete=True on exactly that one.
        pending: List[_RepoPlan] = []
        for repo in sorted(repos, key=lambda r: r.repo_id):
            plan = self._plan_repo(org_id, repo, cursors.get(repo.repo_id), running)
            if plan is not None:
                pending.append(plan)

        # Hand the commit-message corpus to the substrate BEFORE any file batch
        # advances a repo's checkpoint (AT-532 / AC6). Commit messages are
        # content-only — never file-change delta records — so they never inflate
        # the commit-count-vs-files check (AC2). A repo whose only work is its
        # commit corpus (no in-scope file changes) has no file batch to carry its
        # checkpoint advance, so it is advanced here directly.
        for plan in pending:
            self._ingest_commits(org_id, plan)
            if not plan.items:
                running[plan.repo_id] = {"sha": plan.head_sha, "offset": None}

        total_batches = sum(
            (len(p.items) + self.batch_size - 1) // self.batch_size for p in pending
        )
        if total_batches == 0:
            # Unchanged source, or repos that only advanced to HEAD with no file
            # changes (possibly after a commit-only corpus handover above): a
            # single empty delta echoing the (possibly advanced) position.
            yield DeltaBatch(
                records=[],
                next_checkpoint=_encode_checkpoint(running),
                is_complete=True,
            )
            return

        emitted = 0
        for plan in pending:
            if not plan.items:
                continue  # commit-only repo: handed over and advanced above.
            repo_batches = (len(plan.items) + self.batch_size - 1) // self.batch_size
            for bi, start in enumerate(range(0, len(plan.items), self.batch_size)):
                page = plan.items[start : start + self.batch_size]
                records = [
                    self._to_record(plan.repo_id, plan.head_sha, fw) for fw in page
                ]

                # Hand file content to the substrate BEFORE yielding — the secret
                # scan (AT-531) sits here, between extraction and the substrate.
                artifacts = self._to_artifacts(plan.repo_id, plan.head_sha, page)
                self._ingest_content(org_id, self._secret_scan(artifacts))

                # AT-533 (AC3): files deleted in this diff have their chunks removed
                # from retrieval, so a deleted file stops being retrievable. Done in
                # the same batch as the deleted event the runner emits below.
                self._remove_content(org_id, self._to_removals(plan.repo_id, page))

                is_last_repo_batch = bi == repo_batches - 1
                self._advance(running, plan, start + len(page), is_last_repo_batch)

                emitted += 1
                yield DeltaBatch(
                    records=records,
                    next_checkpoint=_encode_checkpoint(running),
                    is_complete=(emitted == total_batches),
                )

    # ── Planning ────────────────────────────────────────────────────────────
    def _plan_repo(
        self,
        org_id: str,
        repo: GitRepoConfig,
        cursor: Optional[Dict[str, Any]],
        running: Dict[str, Dict[str, Any]],
    ) -> Optional[_RepoPlan]:
        """Decide what to read for one repo, or None when there is nothing to do.

        Also records a repo that has advanced to HEAD with no files to emit (empty
        tree, or commits that touched nothing) directly into ``running`` so the
        empty-delta echo still advances it.
        """
        reader = self._reader(org_id, repo)
        try:
            head_sha = reader.head_sha()
        except GitContentError as exc:
            logger.warning(
                "git_content: skipping repo '%s' (org=%s): %s", repo.repo_id, org_id, exc
            )
            return None

        # First load (never seen) or a resumed first load (offset still open).
        if cursor is None or cursor.get("offset") is not None:
            resume_offset = 0
            if cursor is not None and cursor.get("sha") == head_sha:
                resume_offset = int(cursor.get("offset") or 0)
            elif cursor is not None:
                logger.info(
                    "git_content: repo '%s' HEAD moved during a partial first load "
                    "(%s -> %s); restarting the tree load (idempotent).",
                    repo.repo_id,
                    cursor.get("sha"),
                    head_sha,
                )
            all_items = self._tree_items(reader, repo)
            items = all_items[resume_offset:]
            # Full commit corpus at HEAD on a genuine first load (or a restart
            # after HEAD moved — resume_offset reset to 0). A resume that has
            # already loaded some of the tree (resume_offset > 0) also already
            # delivered the corpus, so it is not re-handed (AT-532).
            commits = (
                self._commit_items(reader, repo, None, head_sha)
                if resume_offset == 0
                else []
            )
            if not items and not commits:
                running[repo.repo_id] = {"sha": head_sha, "offset": None}
                return None
            return _RepoPlan(repo.repo_id, head_sha, "full", items, resume_offset, commits)

        # Steady state: repo fully synced at cursor["sha"].
        if cursor.get("sha") == head_sha:
            return None  # unchanged
        items = self._diff_items(reader, repo, cursor["sha"], head_sha)
        # Only the commit messages the new commits (since..HEAD) introduced (AC6),
        # the same delta window the file diff reads off (AC2).
        commits = self._commit_items(reader, repo, cursor["sha"], head_sha)
        if not items and not commits:
            # Commits that touched no in-scope file AND carried no message: still
            # advance to HEAD.
            running[repo.repo_id] = {"sha": head_sha, "offset": None}
            return None
        return _RepoPlan(repo.repo_id, head_sha, "diff", items, 0, commits)

    def _advance(
        self,
        running: Dict[str, Dict[str, Any]],
        plan: _RepoPlan,
        loaded_this_run: int,
        is_last_repo_batch: bool,
    ) -> None:
        """Advance ``running`` for the repo after a batch.

        On the repo's final batch the cursor collapses to the plain HEAD sha
        (fully synced — AC1). For an in-progress FULL load, an intermediate batch
        records the file offset so a genuine-first-run interruption resumes from
        there. For an incremental DIFF, intermediate batches leave the prior
        (complete) cursor untouched — the runner only persists the terminal batch
        in incremental mode, and re-diffing from the same SHA is idempotent.
        """
        if is_last_repo_batch:
            running[plan.repo_id] = {"sha": plan.head_sha, "offset": None}
        elif plan.mode == "full":
            running[plan.repo_id] = {
                "sha": plan.head_sha,
                "offset": plan.base_offset + loaded_this_run,
            }

    # ── Record + artifact shaping ────────────────────────────────────────────
    def _to_record(self, repo_id: str, head_sha: str, fw: _FileWork) -> Dict[str, Any]:
        """Shape one file change into a delta record.

        Carries the identity the shared runner needs (``artifact_id`` +
        ``change_kind`` for ``ingestion.artifact_changed`` events) plus
        repo/path/commit-SHA provenance and an OBSERVED evidence pointer (R16-B1).
        The file *body* travels to the substrate as a ``ContentArtifact``, not on
        this record.
        """
        return {
            "artifact_id": f"{repo_id}:{fw.path}",
            "change_kind": fw.change_kind,
            "source_system": SOURCE_SYSTEM,
            "connector_id": self.connector_id,
            "repo": repo_id,
            "path": fw.path,
            "commit_sha": head_sha,
            "content_type": CONTENT_TYPE,
            "evidence_pointer": _build_evidence_pointer(
                repo_id, fw.path, head_sha, fw.commit_ts
            ),
        }

    def _to_artifacts(
        self, repo_id: str, head_sha: str, page: List[_FileWork]
    ) -> List[Any]:
        """Build the substrate ContentArtifacts for the created/updated files in a
        batch (deletions carry no content). Imported lazily to keep this discovery
        module free of an import-time dependency on the app.retrieval package."""
        from app.retrieval.ingest import ContentArtifact

        artifacts: List[Any] = []
        for fw in page:
            if fw.change_kind == ChangeKind.DELETED:
                continue
            artifacts.append(
                ContentArtifact(
                    source_system=SOURCE_SYSTEM,
                    source_artifact=f"{repo_id}:{fw.path}",
                    content=fw.content or "",
                    content_type=CONTENT_TYPE,
                    source_timestamp=fw.commit_ts,
                    provenance={
                        "repo": repo_id,
                        "path": fw.path,
                        "commit_sha": head_sha,
                        "origin": "observed",
                        "evidence_pointer": _build_evidence_pointer(
                            repo_id, fw.path, head_sha, fw.commit_ts
                        ),
                    },
                )
            )
        return artifacts

    def _secret_scan(self, artifacts: List[Any]) -> List[Any]:
        """Redact committed secrets before content reaches the substrate.

        Seam for AT-531 (secret-pattern scan + redaction). The scan MUST sit here,
        between extraction and the substrate, so a leaked credential is never
        indexed into a retrievable store (R18-A2 §1, "Redact before index,
        always"). AT-529 wires the seam in unconditionally; it is a pass-through
        until AT-531 lands the redaction logic.
        """
        return artifacts

    def _ingest_content(self, org_id: str, artifacts: List[Any]) -> None:
        """Hand file content to the retrieval substrate via the producer contract.

        Uses the injected ``ingest_fn`` when provided (tests), else the real
        ``app.retrieval.ingest.ingest_content`` (lazy-imported). Per-artifact
        failures are already isolated inside ``ingest_content`` and never raised,
        so a bad file never sinks the batch or the checkpoint.
        """
        if not artifacts:
            return
        fn = self._ingest_fn
        if fn is None:
            from app.retrieval.ingest import ingest_content as fn  # type: ignore
        fn(org_id, artifacts)

    def _to_removals(self, repo_id: str, page: List[_FileWork]) -> List[tuple]:
        """The ``(source_system, source_artifact)`` ids of the deleted files in a
        batch — the freshness-removal counterpart of :meth:`_to_artifacts` (AT-533).
        """
        return [
            (SOURCE_SYSTEM, f"{repo_id}:{fw.path}")
            for fw in page
            if fw.change_kind == ChangeKind.DELETED
        ]

    def _remove_content(self, org_id: str, removals: List[tuple]) -> None:
        """Remove deleted files' chunks from the substrate (AC3, freshness).

        Uses the injected ``remove_fn`` when provided (tests), else the real
        ``app.retrieval.ingest.remove_content`` (lazy-imported). Removal is
        idempotent and failure-isolated inside ``remove_content`` — a delete that
        fails or hits an already-absent artifact never sinks the batch or the
        checkpoint. A file that was excluded/never indexed simply removes nothing.
        """
        if not removals:
            return
        fn = self._remove_fn
        if fn is None:
            from app.retrieval.ingest import remove_content as fn  # type: ignore
        fn(org_id, removals)

    # ── Source access: offline fixture vs live git clone ─────────────────────
    def _configured_repos(self, org_id: str) -> List[GitRepoConfig]:
        """Return the repositories configured for content ingestion (opt-in).

        Offline: the deterministic fixture. Live: the ``GIT_CONTENT_REPOS`` env
        JSON array configured per deployment. Either source is explicit
        configuration — no repository is auto-discovered.
        """
        defaults = self._path_defaults()
        default_include = _as_str_tuple(defaults.get("include"))
        default_exclude = _as_str_tuple(defaults.get("exclude"))
        default_use_defaults = bool(defaults.get("use_default_excludes", True))

        repos: List[GitRepoConfig] = []
        seen: set[str] = set()
        for entry in self._raw_repo_entries():
            repo_id = str(entry.get("repo_id", "")).strip()
            if not repo_id:
                logger.warning("git_content: skipping repo entry with no repo_id")
                continue
            if repo_id in seen:
                logger.warning(
                    "git_content: duplicate repo_id '%s' — keeping the first", repo_id
                )
                continue
            seen.add(repo_id)
            meta = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}

            # Path-filter config (AT-530): a repo's own include allow-list overrides
            # the org default; excludes UNION the org defaults with the repo's; and
            # use_default_excludes falls back org -> built-in True. The built-in
            # DEFAULT_EXCLUDE_GLOBS are layered in when the filter is built.
            repo_include = entry.get("include")
            include = (
                _as_str_tuple(repo_include) if repo_include is not None else default_include
            )
            exclude = default_exclude + _as_str_tuple(entry.get("exclude"))
            use_default_excludes = (
                bool(entry["use_default_excludes"])
                if "use_default_excludes" in entry
                else default_use_defaults
            )

            repos.append(
                GitRepoConfig(
                    repo_id=repo_id,
                    branch=str(entry.get("branch") or "HEAD"),
                    path=entry.get("path"),
                    metadata=meta or {},
                    include=include,
                    exclude=exclude,
                    use_default_excludes=use_default_excludes,
                )
            )
        return repos

    def _path_defaults(self) -> Dict[str, Any]:
        """Org-level path-filter defaults applied to every repo (AT-530).

        The "editable per org" surface: offline it is the fixture's top-level
        ``path_defaults`` object; live it is the ``GIT_CONTENT_PATH_DEFAULTS`` env
        JSON object. A repo's own settings override these; these override the
        built-in defaults. Absent/malformed → an empty object (built-ins only).
        """
        if not is_live():
            data = self._fixture().get("path_defaults")
            return data if isinstance(data, dict) else {}
        raw = os.getenv(_PATH_DEFAULTS_ENV, "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise GitContentError(
                f"{_PATH_DEFAULTS_ENV} is not valid JSON: {type(exc).__name__}"
            ) from exc
        return parsed if isinstance(parsed, dict) else {}

    def _raw_repo_entries(self) -> List[Dict[str, Any]]:
        if not is_live():
            return list(self._fixture().get("repos", []))
        raw = os.getenv(_REPOS_ENV, "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise GitContentError(
                f"{_REPOS_ENV} is not valid JSON: {type(exc).__name__}"
            ) from exc
        if not isinstance(parsed, list):
            raise GitContentError(f"{_REPOS_ENV} must be a JSON array of repo configs")
        return [e for e in parsed if isinstance(e, dict)]

    def _tree_items(self, reader: "_RepoReader", repo: GitRepoConfig) -> List[_FileWork]:
        """The HEAD tree as created-file work, deterministically ordered by path.

        Binary files are skipped-with-reason (never indexed as garbage text); the
        include/exclude path filter (AT-530) is applied via ``_select_paths``.
        """
        head_ts = reader.head_ts()
        items: List[_FileWork] = []
        for entry in sorted(reader.tree(), key=lambda e: e["path"]):
            path = entry["path"]
            if not self._select_paths(repo, path):
                continue
            if entry.get("binary"):
                logger.info(
                    "git_content: skipping binary file (repo=%s path=%s) — not indexed",
                    repo.repo_id,
                    path,
                )
                continue
            items.append(
                _FileWork(
                    path=path,
                    change_kind=ChangeKind.CREATED,
                    content=entry.get("content") or "",
                    commit_ts=head_ts,
                )
            )
        return items

    def _diff_items(
        self, reader: "_RepoReader", repo: GitRepoConfig, since_sha: str, head_sha: str
    ) -> List[_FileWork]:
        """The files ``since_sha..head_sha`` touched, as change work (AC2).

        Created/updated files carry their HEAD content; deletions are emitted as
        content-less ``deleted`` records. Binary non-deletions are skipped-
        with-reason. Ordered by path for determinism.
        """
        head_ts = reader.head_ts()
        changes, commit_count = reader.diff(since_sha, head_sha)
        items: List[_FileWork] = []
        for change in sorted(changes, key=lambda c: c["path"]):
            path = change["path"]
            if not self._select_paths(repo, path):
                continue
            kind = change["change_kind"]
            if kind == ChangeKind.DELETED:
                items.append(_FileWork(path=path, change_kind=kind))
                continue
            if change.get("binary"):
                logger.info(
                    "git_content: skipping binary file (repo=%s path=%s) — not indexed",
                    repo.repo_id,
                    path,
                )
                continue
            items.append(
                _FileWork(
                    path=path,
                    change_kind=kind,
                    content=change.get("content") or "",
                    commit_ts=head_ts,
                )
            )
        logger.info(
            "git_content: repo=%s incremental %s..%s — %d commit(s) touched %d "
            "in-scope file(s)",
            repo.repo_id,
            since_sha,
            head_sha,
            commit_count,
            len(items),
        )
        return items

    def _select_paths(self, repo: GitRepoConfig, path: str) -> bool:
        """True when ``path`` is in scope for content ingestion in ``repo`` (AC4).

        Delegates to the repo's memoised :class:`PathFilter`: vendored/generated/
        lockfile paths are excluded by the built-in defaults (unless the repo
        opted out), plus any per-repo/org include allow-list and excludes. Applied
        to BOTH the first-load tree and the incremental diff, so an excluded path
        is never ingested regardless of how it was surfaced.
        """
        return self._filter_for(repo).allows(path)

    def _filter_for(self, repo: GitRepoConfig) -> PathFilter:
        """Build (once per repo) the effective PathFilter, layering the built-in
        DEFAULT_EXCLUDE_GLOBS under the repo/org excludes unless disabled."""
        cached = self._filter_cache.get(repo.repo_id)
        if cached is not None:
            return cached
        exclude: Tuple[str, ...] = repo.exclude
        if repo.use_default_excludes:
            exclude = DEFAULT_EXCLUDE_GLOBS + exclude
        path_filter = PathFilter(include=repo.include, exclude=exclude)
        self._filter_cache[repo.repo_id] = path_filter
        return path_filter

    def _reader(self, org_id: str, repo: GitRepoConfig) -> "_RepoReader":
        if not is_live():
            return _FixtureRepoReader(self._fixture_repo(repo.repo_id))
        return _GitCommandRepoReader(repo)

    def _fixture(self) -> Dict[str, Any]:
        if not FIXTURE_PATH.exists():
            raise GitContentError(f"git_content fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _fixture_repo(self, repo_id: str) -> Dict[str, Any]:
        for repo in self._fixture().get("repos", []):
            if repo.get("repo_id") == repo_id:
                return repo
        raise GitContentError(f"repo '{repo_id}' not found in fixture")


# ---------------------------------------------------------------------------
# Repo readers — the only edge that differs between offline and live
# ---------------------------------------------------------------------------


class _RepoReader:
    """Reads head SHA, HEAD tree, and a diff for one repo. The single edge that
    differs offline vs live, so a future GitLab/Bitbucket source reuses everything
    except this (R18-A2 §4, "General mechanism first")."""

    def head_sha(self) -> str:
        raise NotImplementedError

    def head_ts(self) -> Optional[str]:
        raise NotImplementedError

    def tree(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def diff(self, since_sha: str, head_sha: str) -> Tuple[List[Dict[str, Any]], int]:
        raise NotImplementedError

    def commits(
        self, since_sha: Optional[str], head_sha: str
    ) -> List[Dict[str, Any]]:
        """Commit messages with author/date provenance (AT-532).

        ``since_sha=None`` -> the full corpus reachable at HEAD (first load);
        otherwise the commits ``since_sha..head_sha`` introduced (incremental).
        Each entry is ``{sha, message, author, author_email, date}``.
        """
        raise NotImplementedError


class _FixtureRepoReader(_RepoReader):
    """Deterministic offline reader over one repo entry from the fixture."""

    def __init__(self, repo: Dict[str, Any]):
        self._repo = repo

    def head_sha(self) -> str:
        return str(self._repo.get("head_sha", ""))

    def head_ts(self) -> Optional[str]:
        return self._repo.get("head_ts")

    def tree(self) -> List[Dict[str, Any]]:
        return list(self._repo.get("head_tree", []))

    def diff(self, since_sha: str, head_sha: str) -> Tuple[List[Dict[str, Any]], int]:
        diffs = self._repo.get("diffs", {})
        entry = diffs.get(since_sha)
        if entry is None:
            # An unknown since-SHA (e.g. a rebase/force-push rewrote history):
            # fall back to a full re-read of the HEAD tree as created files, which
            # the substrate replaces idempotently. Never crash the run.
            logger.warning(
                "git_content: since-SHA %r not found in fixture diffs; re-reading "
                "HEAD tree",
                since_sha,
            )
            return (
                [
                    {"path": e["path"], "change_kind": ChangeKind.CREATED, **e}
                    for e in self.tree()
                ],
                0,
            )
        changes = [
            {
                "path": c["path"],
                "change_kind": _STATUS_TO_KIND.get(c.get("change", "M"), ChangeKind.UPDATED),
                "content": c.get("content"),
                "binary": c.get("binary", False),
            }
            for c in entry.get("changes", [])
        ]
        return changes, int(entry.get("commit_count", 0) or 0)

    def commits(
        self, since_sha: Optional[str], head_sha: str
    ) -> List[Dict[str, Any]]:
        """The repo's commit list (newest-first) from the fixture.

        The full corpus when ``since_sha`` is None; otherwise the newest commits
        down to — but not including — ``since_sha`` (the commits the incremental
        window introduced). An unknown ``since_sha`` (history rewrite) falls back
        to the full corpus, matching :meth:`diff`'s tolerant re-read.
        """
        all_commits = [c for c in self._repo.get("commits", []) if isinstance(c, dict)]
        if since_sha is None:
            return list(all_commits)
        if not any(str(c.get("sha")) == since_sha for c in all_commits):
            logger.warning(
                "git_content: since-SHA %r not found in fixture commits; using the "
                "full corpus",
                since_sha,
            )
            return list(all_commits)
        out: List[Dict[str, Any]] = []
        for commit in all_commits:
            if str(commit.get("sha")) == since_sha:
                break
            out.append(commit)
        return out


class _GitCommandRepoReader(_RepoReader):
    """Live reader: shells out to ``git`` in a local clone (live mode only).

    The commit graph is the change feed, so nothing here polls or guesses:
    ``git rev-parse`` gives the head SHA, ``git ls-tree`` the tree, ``git diff
    --name-status`` the exact changed-file set, and ``git show`` the file content.
    Binary content is detected by a NUL byte in the blob (git's own heuristic).
    """

    def __init__(self, repo: GitRepoConfig):
        if not repo.path:
            raise GitContentError(
                f"repo '{repo.repo_id}' has no local clone path; set 'path' in "
                f"{_REPOS_ENV} for live git content ingestion"
            )
        self._repo = repo
        self._path = repo.path
        self._ref = repo.branch or "HEAD"

    def _git(self, *args: str, binary: bool = False):
        try:
            result = subprocess.run(
                ["git", "-C", self._path, *args],
                capture_output=True,
                timeout=_GIT_TIMEOUT,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitContentError("git executable not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitContentError(f"git command timed out: git {' '.join(args)}") from exc
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", "replace").strip()
            raise GitContentError(f"git {' '.join(args)} failed: {err}")
        return result.stdout if binary else result.stdout.decode("utf-8", "replace")

    def head_sha(self) -> str:
        return self._git("rev-parse", self._ref).strip()

    def head_ts(self) -> Optional[str]:
        return self._git("show", "-s", "--format=%cI", self.head_sha()).strip() or None

    def tree(self) -> List[Dict[str, Any]]:
        head = self.head_sha()
        names = self._git("ls-tree", "-r", "--name-only", head).splitlines()
        entries: List[Dict[str, Any]] = []
        for path in names:
            path = path.strip()
            if not path:
                continue
            raw = self._git("show", f"{head}:{path}", binary=True)
            if b"\x00" in raw:
                entries.append({"path": path, "binary": True, "content": ""})
            else:
                entries.append(
                    {"path": path, "binary": False, "content": raw.decode("utf-8", "replace")}
                )
        return entries

    def diff(self, since_sha: str, head_sha: str) -> Tuple[List[Dict[str, Any]], int]:
        commit_count = 0
        try:
            commit_count = int(
                self._git("rev-list", "--count", f"{since_sha}..{head_sha}").strip() or 0
            )
        except (GitContentError, ValueError):
            commit_count = 0

        status = self._git(
            "diff", "--name-status", "-M", f"{since_sha}..{head_sha}"
        ).splitlines()
        changes: List[Dict[str, Any]] = []
        for line in status:
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code = parts[0].strip()
            letter = code[0]
            if letter == "R":  # rename: delete old path, create new path
                old_path, new_path = parts[1], parts[2]
                changes.append({"path": old_path, "change_kind": ChangeKind.DELETED})
                changes.append(self._added(new_path, head_sha))
                continue
            kind = _STATUS_TO_KIND.get(letter, ChangeKind.UPDATED)
            path = parts[1]
            if kind == ChangeKind.DELETED:
                changes.append({"path": path, "change_kind": kind})
            else:
                changes.append(self._file_at(path, head_sha, kind))
        return changes, commit_count

    def commits(
        self, since_sha: Optional[str], head_sha: str
    ) -> List[Dict[str, Any]]:
        """Walk ``git log`` for the commit-message corpus (AT-532).

        The commit graph IS the change feed, so nothing polls: a first load reads
        every commit reachable at HEAD; an incremental run reads only
        ``since_sha..head_sha``. A field-separator (US, ``0x1f``) / record-
        separator (RS, ``0x1e``) format keeps multi-line commit bodies intact —
        newlines inside ``%B`` are not confused with row boundaries. ``%aI`` is the
        strict-ISO author date; ``%B`` is the raw subject+body.
        """
        rng = head_sha if not since_sha else f"{since_sha}..{head_sha}"
        fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%B%x1e"
        out_text = self._git("log", f"--format={fmt}", rng)
        commits: List[Dict[str, Any]] = []
        for record in out_text.split("\x1e"):
            record = record.strip("\n")
            if not record.strip():
                continue
            parts = record.split("\x1f")
            if len(parts) < 5:
                continue
            sha, author, email, date, message = parts[0], parts[1], parts[2], parts[3], parts[4]
            commits.append(
                {
                    "sha": sha.strip(),
                    "author": author.strip() or None,
                    "author_email": email.strip() or None,
                    "date": date.strip() or None,
                    "message": message.strip("\n"),
                }
            )
        return commits

    def _added(self, path: str, head_sha: str) -> Dict[str, Any]:
        return self._file_at(path, head_sha, ChangeKind.CREATED)

    def _file_at(self, path: str, head_sha: str, kind: str) -> Dict[str, Any]:
        raw = self._git("show", f"{head_sha}:{path}", binary=True)
        if b"\x00" in raw:
            return {"path": path, "change_kind": kind, "binary": True}
        return {
            "path": path,
            "change_kind": kind,
            "content": raw.decode("utf-8", "replace"),
            "binary": False,
        }
