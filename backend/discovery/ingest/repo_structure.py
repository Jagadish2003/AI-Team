"""
R18-A2 / AT-534 (T6) — repository structural metadata (tree + inventory).

The Git content ingestor (:mod:`discovery.ingest.git_content`) moves file BODIES
into the retrieval substrate so they can be chunked, embedded and searched. This
module is the *other* half of what a repository tells us: its **shape** — the
directory tree and the file inventory — captured as lightweight, **graph-facing**
metadata that is deliberately NOT embedded.

Why a separate layer (R18-A2 §1, "Structure")
----------------------------------------------
Directory tree and file inventory are structural facts, not prose to retrieve:
"this app has an ``src/`` package with three modules and a generated protobuf" is
a *graph* statement (Application → contains → Module → contains → File), not a
vector to search. Feeding it through ``ingest_content`` would embed path strings
as if they were content — noise in the index. So structure is captured here as
plain metadata that the Sprint-2 Java/.NET application-structure story consumes to
reason about applications; the file bodies still flow through the substrate.

This module is pure (no DB, no ``app`` import) so it is trivially testable and,
per R18-A2 §4 ("General mechanism first"), reusable by a future GitLab/Bitbucket
content source — only the *persistence* edge (:mod:`app.repo_structure_store`) and
the *fetch* edge (the git reader) differ.

Shape of the metadata
----------------------
A :class:`RepoStructure` is a snapshot of ONE repo at ONE commit SHA:

  * ``files``       — the in-scope file inventory: for each file its path, name,
                      parent directory, extension, best-effort language, a binary
                      flag, and its depth. Binary files ARE inventoried (a binary
                      asset is a structural fact); only their *content* is skipped
                      by the content path — never their existence.
  * ``directories`` — the derived directory tree: one node per directory (plus a
                      synthetic root, path ``""``) carrying its parent, depth, and
                      direct file / subdirectory counts. Single-rooted so it maps
                      cleanly onto a containment graph.

The inventory is the source of truth; the directory tree is *derived* from it, so
the two can never disagree. Everything is deterministically ordered by path so two
captures of the same commit produce byte-identical metadata.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: Repo-root directory path. Every top-level file/directory has this as its parent
#: so the directory tree is single-rooted (one Application node for the graph).
ROOT_PATH = ""

#: Best-effort extension → language map. Lightweight and intentionally partial:
#: an unknown extension yields ``None`` rather than a guess, so a consumer can tell
#: "not classified" from a real language. Keyed by lowercase extension WITHOUT the
#: dot. A couple of well-known extension-less filenames are handled in
#: :func:`language_for_path`.
_EXTENSION_LANGUAGE: Dict[str, str] = {
    "py": "Python",
    "pyi": "Python",
    "js": "JavaScript",
    "jsx": "JavaScript",
    "mjs": "JavaScript",
    "cjs": "JavaScript",
    "ts": "TypeScript",
    "tsx": "TypeScript",
    "java": "Java",
    "kt": "Kotlin",
    "kts": "Kotlin",
    "go": "Go",
    "rb": "Ruby",
    "rs": "Rust",
    "cs": "C#",
    "cpp": "C++",
    "cc": "C++",
    "cxx": "C++",
    "hpp": "C++",
    "c": "C",
    "h": "C",
    "swift": "Swift",
    "php": "PHP",
    "scala": "Scala",
    "sql": "SQL",
    "sh": "Shell",
    "bash": "Shell",
    "ps1": "PowerShell",
    "html": "HTML",
    "htm": "HTML",
    "css": "CSS",
    "scss": "SCSS",
    "sass": "SCSS",
    "md": "Markdown",
    "rst": "reStructuredText",
    "json": "JSON",
    "yaml": "YAML",
    "yml": "YAML",
    "xml": "XML",
    "toml": "TOML",
    "ini": "INI",
    "cfg": "INI",
    "proto": "Protocol Buffers",
    "gradle": "Gradle",
    "tf": "Terraform",
}

#: Extension-less filenames worth classifying by name alone.
_FILENAME_LANGUAGE: Dict[str, str] = {
    "dockerfile": "Dockerfile",
    "makefile": "Makefile",
}


def _norm_path(path: str) -> str:
    """Normalise a repo path: strip, drop a leading ``/``, collapse ``\\`` to ``/``.

    Git paths are already forward-slashed and relative, but a hand-built fixture or
    a foreign reader might not be — normalising here keeps the tree derivation
    deterministic regardless of the source.
    """
    return (path or "").strip().replace("\\", "/").lstrip("/")


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else ""


def _extension(name: str) -> str:
    """Lowercase extension WITHOUT the dot, or ``""`` for none.

    A leading-dot name (``.gitignore``) is treated as having no extension — the dot
    marks a hidden file, not a type suffix.
    """
    if "." not in name or name.startswith(".") and name.count(".") == 1:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def language_for_path(path: str) -> Optional[str]:
    """Best-effort language for a file path, or ``None`` when not classified."""
    name = _basename(_norm_path(path)).lower()
    if name in _FILENAME_LANGUAGE:
        return _FILENAME_LANGUAGE[name]
    ext = _extension(name)
    return _EXTENSION_LANGUAGE.get(ext) if ext else None


@dataclass(frozen=True)
class FileInventoryEntry:
    """One file in the repository inventory (lightweight — no content).

    ``directory`` is the parent directory path (``""`` for a top-level file);
    ``depth`` is the number of path segments (``README.md`` → 1,
    ``src/main.py`` → 2). ``is_binary`` records that the file exists but its body
    was skipped by the content path — the inventory still carries the structural
    fact.
    """

    path: str
    name: str
    directory: str
    extension: str
    language: Optional[str]
    is_binary: bool
    depth: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DirectoryNode:
    """One directory in the derived tree, plus its DIRECT child counts.

    ``parent`` is ``None`` only for the synthetic root (``path == ""``). ``depth``
    is the number of path segments (root → 0, ``src`` → 1, ``src/api`` → 2).
    ``file_count`` / ``subdirectory_count`` count DIRECT children only, so a
    consumer can build containment edges without re-deriving the tree.
    """

    path: str
    name: str
    parent: Optional[str]
    depth: int
    file_count: int
    subdirectory_count: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepoStructure:
    """A repo's directory tree + file inventory at one commit SHA (AT-534).

    Graph-facing metadata, not embedded content. Stamped with the ``commit_sha``
    it reflects so a consumer knows exactly which commit the shape describes, and
    with ``captured_at`` for freshness. Rollup counts are precomputed for cheap
    health checks.
    """

    repo_id: str
    commit_sha: str
    captured_at: Optional[str]
    files: List[FileInventoryEntry] = field(default_factory=list)
    directories: List[DirectoryNode] = field(default_factory=list)
    file_count: int = 0
    directory_count: int = 0
    binary_file_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "commit_sha": self.commit_sha,
            "captured_at": self.captured_at,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "binary_file_count": self.binary_file_count,
            "files": [f.to_dict() for f in self.files],
            "directories": [d.to_dict() for d in self.directories],
        }


def build_repo_structure(
    repo_id: str,
    commit_sha: str,
    inventory: Iterable[Tuple[str, bool]],
    captured_at: Optional[str] = None,
) -> RepoStructure:
    """Build a :class:`RepoStructure` from an in-scope file inventory.

    ``inventory`` is an iterable of ``(path, is_binary)`` pairs — already
    path-filtered by the caller (the connector applies its include/exclude rules
    so structure reflects the app's own tree, not vendored/generated noise). Paths
    are normalised and de-duplicated (last write wins, e.g. an ``updated`` file
    after a ``created`` one), then sorted, so the output is deterministic.

    The directory tree is DERIVED from the file paths: every ancestor directory of
    every file becomes a node (plus the synthetic root ``""``), with direct file
    and subdirectory counts. A repo with no in-scope files yields just the root.
    """
    # De-dup by normalised path (last wins), dropping blanks.
    by_path: Dict[str, bool] = {}
    for path, is_binary in inventory:
        norm = _norm_path(path)
        if norm:
            by_path[norm] = bool(is_binary)

    files: List[FileInventoryEntry] = []
    # Direct-child accounting for the directory tree.
    dir_file_count: Dict[str, int] = {ROOT_PATH: 0}
    dir_subdirs: Dict[str, set] = {ROOT_PATH: set()}
    dir_parent: Dict[str, Optional[str]] = {ROOT_PATH: None}

    def _ensure_dir_chain(dir_path: str) -> None:
        """Register ``dir_path`` and every ancestor up to the root."""
        segments = dir_path.split("/") if dir_path else []
        parent = ROOT_PATH
        for i in range(len(segments)):
            current = "/".join(segments[: i + 1])
            if current not in dir_file_count:
                dir_file_count[current] = 0
                dir_subdirs[current] = set()
                dir_parent[current] = parent
            dir_subdirs[parent].add(current)
            parent = current

    for norm in sorted(by_path):
        is_binary = by_path[norm]
        segments = norm.split("/")
        directory = "/".join(segments[:-1])  # "" for a top-level file
        name = segments[-1]
        _ensure_dir_chain(directory)
        dir_file_count[directory] = dir_file_count.get(directory, 0) + 1
        files.append(
            FileInventoryEntry(
                path=norm,
                name=name,
                directory=directory,
                extension=_extension(name),
                language=language_for_path(norm),
                is_binary=is_binary,
                depth=len(segments),
            )
        )

    directories: List[DirectoryNode] = []
    for dir_path in sorted(dir_file_count):
        directories.append(
            DirectoryNode(
                path=dir_path,
                name=_basename(dir_path),
                parent=dir_parent[dir_path],
                depth=len(dir_path.split("/")) if dir_path else 0,
                file_count=dir_file_count[dir_path],
                subdirectory_count=len(dir_subdirs[dir_path]),
            )
        )

    return RepoStructure(
        repo_id=repo_id,
        commit_sha=commit_sha,
        captured_at=captured_at,
        files=files,
        directories=directories,
        file_count=len(files),
        directory_count=len(directories),
        binary_file_count=sum(1 for f in files if f.is_binary),
    )


def inventory_from_structure_dict(data: Optional[Dict[str, Any]]) -> List[Tuple[str, bool]]:
    """Recover the ``(path, is_binary)`` inventory from a persisted structure dict.

    The inverse of :meth:`RepoStructure.to_dict`'s ``files`` list — used to seed an
    incremental update from the last stored snapshot. Tolerant of a missing /
    malformed payload (returns ``[]``) so a degenerate stored value degrades to a
    clean rebuild rather than raising.
    """
    if not isinstance(data, dict):
        return []
    files = data.get("files")
    if not isinstance(files, list):
        return []
    out: List[Tuple[str, bool]] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = _norm_path(str(entry.get("path", "")))
        if path:
            out.append((path, bool(entry.get("is_binary", False))))
    return out


def apply_inventory_delta(
    inventory: Iterable[Tuple[str, bool]],
    upserts: Iterable[Tuple[str, bool]],
    deletes: Iterable[str],
) -> List[Tuple[str, bool]]:
    """Apply an incremental change set to a file inventory (AT-534 incremental).

    Mirrors how the connector maintains structure without re-walking the whole
    tree every run: the commit diff gives created/updated files (``upserts``,
    carrying the current binary flag) and removed files (``deletes``). ``upserts``
    win over prior state; ``deletes`` remove regardless. The result is the current
    in-scope inventory, deterministically sorted by path.
    """
    merged: Dict[str, bool] = {}
    for path, is_binary in inventory:
        norm = _norm_path(path)
        if norm:
            merged[norm] = bool(is_binary)
    for path, is_binary in upserts:
        norm = _norm_path(path)
        if norm:
            merged[norm] = bool(is_binary)
    for path in deletes:
        merged.pop(_norm_path(path), None)
    return sorted(merged.items())
