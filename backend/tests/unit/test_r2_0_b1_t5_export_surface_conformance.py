"""2.0-B1 T5 (AC5) — export-surface CONFORMANCE guard.

AC5: "Exports contain no unredacted secrets and no host x vulnerability
enumeration (1.9 aggregation floor holds in export)."

Fixing today's export surfaces satisfies AC5 once. This test is what keeps it
satisfied: nothing is enumerated by hand — export surfaces are DISCOVERED by
walking the tree at test time (``Path.rglob`` + ``ast``), and the default posture
for anything not on the allow-list is **FAIL**. A new export route therefore
fails CI without any edit to this file, and the author must either route it
through ``app.export_guard`` or record an explicit justification.

Modelled on ``tests/contract/test_no_env_credential_reads.py`` Section B, which
uses the same discover-then-default-deny shape for env credential reads.

DB-free: pure AST analysis plus in-memory fixtures. Deliberately does NOT import
``app.main`` (that pulls in DB-touching module-level code), so this guard always
runs locally and in CI, not only where PostgreSQL is reachable.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]
_THIS_FILE: Path = Path(__file__).resolve()

# Roots swept for export surfaces.
_SCAN_ROOTS: Tuple[Path, ...] = (
    BACKEND_ROOT / "app",
    BACKEND_ROOT / "discovery",
)

_SKIP_DIRS = frozenset({
    ".venv", "node_modules", "__pycache__", ".git", "build", "dist", "tests",
    "fixtures",
})

# The shared guard every export surface must route through.
_GUARD_MODULE = "export_guard"
_GUARD_CALLS = frozenset({
    "guard_export_payload", "assert_export_safe", "redact_export_content",
    # The discovery-side tolerant bridge onto the same guard
    # (discovery/export_safety.py) — discovery CLIs cannot assume the ``app``
    # package is importable, so they route through it.
    "guard_exported_payload",
})
# The underlying primitives — a surface that calls these directly is also
# protected (the pack/materialize boundary does this and predates the guard).
_FLOOR_CALLS = frozenset({"assert_output_safe", "enforce_pack_output"})
_REDACT_CALLS = frozenset({"scan_and_redact", "scan_and_redact_security"})


# ── what counts as an export surface ────────────────────────────────────────

# A function is an export surface if it constructs a downloadable response, or
# writes a serialised artifact to disk. Route paths alone are NOT used as the
# signal: most run-scoped GETs are ordinary viewer reads that back the UI, not
# exports, and gating those on a RAISING floor check would turn an IPv4-shaped
# version string in LLM prose into a hard error on a user-facing page. AC5 is
# about documents that LEAVE the deployment.
_DOWNLOAD_MARKERS = ("content-disposition", "attachment;")
_FILE_WRITE_CALLS = frozenset({"write_text", "write_bytes"})
_RESPONSE_CLASSES = frozenset({"StreamingResponse", "FileResponse"})


# ── the allow-list: (relative path, function) -> justification ──────────────
#
# PROTECTED[<module>]: the artifact is guarded before it is emitted. The optional
#            [<module>] names where the guard actually runs, for a surface that
#            only SERVES content another module already guarded. That module is
#            then verified to call the guard, so the claim cannot be a rubber stamp.
# EXEMPT:    carries no tenant run content, so neither discipline applies. The
#            reason is recorded so a future reviewer does not "fix" it — and so a
#            surface cannot be quietly exempted without saying why.
_EXPORT_ALLOWLIST: Dict[Tuple[str, str], str] = {
    ("app/routes_evidence_export.py", "_serve"): (
        "PROTECTED[app/evidence_export.py]: serves the already-guarded signed "
        "bundle built by evidence_export.build_export_bundle, which redacts and "
        "enforces the floor before signing (T4/T5). The guard runs in the "
        "builder, not the HTTP edge — signing after the guard is what makes the "
        "signature cover guarded bytes."
    ),
    ("discovery/offline_export.py", "export"): (
        "PROTECTED: calls export_guard.guard_export_payload via _guard() before "
        "writing opportunities.json / evidence.json (T5 / AC5)."
    ),
    ("app/routes_cloud_connectors.py", "download_security_artifact"): (
        "EXEMPT: serves a STATIC file shipped in deployment/ (partner security "
        "artifacts: IAM policy, RBAC role) with a path-containment check. No "
        "tenant, run, or finding content passes through it."
    ),
    ("discovery/runner.py", "main"): (
        "PROTECTED: the runner CLI writes the FULL run payload (opportunities + "
        "evidence, or the Track-A seed) to --output, so it guards via "
        "export_safety.guard_exported_payload before serialising (T5 / AC5)."
    ),
    ("discovery/calibration/calibrator.py", "main"): (
        "PROTECTED: the calibration report embeds run-derived content "
        "(algo_top5, score_debug_summary) and is guarded before --report-path is "
        "written."
    ),
    ("discovery/ingest/live_validator.py", "main"): (
        "PROTECTED: the live-validation report is guarded before --report-path "
        "is written."
    ),
    ("discovery/integration_verifier.py", "main"): (
        "PROTECTED: the integration-verification report is guarded before "
        "--report-path is written."
    ),
    # ── 2.0-C3 Skills SDK (Arc C) ────────────────────────────────────────────
    # Surfaced by this sweep when the C arc merged in. All three write files, but
    # none is a path by which TENANT content leaves the deployment, which is what
    # AC5 governs.
    ("discovery/packs/sdk/bundle.py", "extract_bundle"): (
        "EXEMPT: this is an IMPORT, not an export — it unpacks a partner-supplied "
        ".aiqpack INTO the deployment (signature-verified, zip-slip guarded, "
        "size/count capped). Content moves inward; no tenant, run, or finding "
        "content passes through it."
    ),
    ("discovery/packs/sdk/reference_docs.py", "sync_docs"): (
        "EXEMPT: repo-maintenance tooling. Regenerates docs/partner/*.md from the "
        "platform's OWN declarations (concepts, primitives, lint rules) so the "
        "partner docs cannot drift. Reads no org, run, or finding data."
    ),
    ("discovery/packs/sdk/scaffold.py", "scaffold_pack"): (
        "EXEMPT: author tooling. Writes a new pack project skeleton (pack.json + "
        "fixtures + README) on a pack author's own machine from static templates. "
        "Reads no org, run, or finding data."
    ),
    ("discovery/seed/demo_seeder.py", "save"): (
        "EXEMPT: writes the demo seeder's OWN bookkeeping state (ids of the "
        "Salesforce/ServiceNow/Jira records it just created, plus a timestamp) "
        "to seed_state.json. No findings, evidence, narrative, or security "
        "content — nothing the floor or the redactor could apply to."
    ),
}

_PROTECTED_PREFIX = "PROTECTED"


def _guarding_module(rel: str, justification: str) -> str:
    """The module that must contain the guard call for a PROTECTED entry —
    itself by default, or the one named in ``PROTECTED[<module>]``."""
    if justification.startswith(f"{_PROTECTED_PREFIX}["):
        return justification[len(_PROTECTED_PREFIX) + 1 : justification.index("]")]
    return rel


# ── discovery ───────────────────────────────────────────────────────────────


def _iter_python_files(roots: Iterable[Path]) -> Iterable[Tuple[Path, Path]]:
    """Yield ``(python_file, scan_root)`` pairs for every root."""
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == _THIS_FILE:
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            yield path, root


def _rel(path: Path, root: Path) -> str:
    """Stable allow-list key: repo-relative for real roots, root-relative for a
    synthetic tmp_path root (used by the negative tests)."""
    try:
        return path.relative_to(BACKEND_ROOT).as_posix()
    except ValueError:
        return path.relative_to(root).as_posix()


class _ExportSurfaceVisitor(ast.NodeVisitor):
    """Collect functions that emit a downloadable artifact, plus guard calls."""

    def __init__(self) -> None:
        self.surfaces: List[Tuple[str, int]] = []   # (function name, lineno)
        self.guarded_functions: Set[str] = set()
        self.module_guard_calls: Set[str] = set()
        self._scope: List[str] = []

    # -- scope tracking --------------------------------------------------
    @staticmethod
    def _own_nodes(node) -> Iterable[ast.AST]:
        """Descendants of ``node`` that are NOT inside a nested function.

        Without this, a route-registration wrapper (``register_*_routes``, which
        defines its handlers inline) would be reported as the export surface
        instead of the handler that actually emits the artifact — and allow-listing
        the wrapper would blanket-exempt every route it contains.
        """
        stack = list(ast.iter_child_nodes(node))
        while stack:
            current = stack.pop()
            yield current
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue   # a nested function owns its own body
            stack.extend(ast.iter_child_nodes(current))

    def _visit_scoped(self, node) -> None:
        self._scope.append(node.name)
        is_surface = False
        for child in self._own_nodes(node):
            if self._is_download_marker(child) or self._is_file_write(child):
                is_surface = True
            called = self._called_name(child)
            if called in _GUARD_CALLS or called in _FLOOR_CALLS or called in _REDACT_CALLS:
                self.guarded_functions.add(node.name)
                self.module_guard_calls.add(called)
        if is_surface:
            self.surfaces.append((node.name, node.lineno))
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _visit_scoped
    visit_AsyncFunctionDef = _visit_scoped

    # -- markers ---------------------------------------------------------
    @staticmethod
    def _called_name(node) -> str:
        if not isinstance(node, ast.Call):
            return ""
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""

    @staticmethod
    def _is_download_marker(node) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            return any(marker in lowered for marker in _DOWNLOAD_MARKERS)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            return name in _RESPONSE_CLASSES
        return False

    @classmethod
    def _is_file_write(cls, node) -> bool:
        return cls._called_name(node) in _FILE_WRITE_CALLS


def _analyse(path: Path) -> _ExportSurfaceVisitor:
    visitor = _ExportSurfaceVisitor()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return visitor
    visitor.visit(tree)
    # Guard calls are collected MODULE-wide (a surface may delegate to a helper
    # elsewhere in the file), unlike surface attribution which is per-innermost-
    # function.
    for node in ast.walk(tree):
        called = _ExportSurfaceVisitor._called_name(node)
        if called in _GUARD_CALLS or called in _FLOOR_CALLS or called in _REDACT_CALLS:
            visitor.module_guard_calls.add(called)
    return visitor


def _discover_surfaces(roots: Iterable[Path]) -> Dict[Tuple[str, str], Tuple[int, _ExportSurfaceVisitor]]:
    """Map (relative path, function) -> (lineno, module analysis)."""
    found: Dict[Tuple[str, str], Tuple[int, _ExportSurfaceVisitor]] = {}
    for path, root in _iter_python_files(roots):
        analysis = _analyse(path)
        for name, lineno in analysis.surfaces:
            found[(_rel(path, root), name)] = (lineno, analysis)
    return found


# ── the guard ───────────────────────────────────────────────────────────────


def test_every_export_surface_is_guarded_or_justified():
    """Default-deny: an export surface must be allow-listed with a reason."""
    offenders: List[str] = []
    for (rel, func), (lineno, _analysis) in sorted(_discover_surfaces(_SCAN_ROOTS).items()):
        if (rel, func) in _EXPORT_ALLOWLIST:
            continue
        offenders.append(
            f"  {rel}:{lineno}: export surface {func!r} is not accounted for — "
            f"route it through app.export_guard.guard_export_payload, or add "
            f"('{rel}', '{func}') to _EXPORT_ALLOWLIST with a one-line "
            f"PROTECTED:/EXEMPT: justification"
        )
    assert not offenders, (
        "Unaccounted export surface(s) found (2.0-B1 AC5):\n"
        + "\n".join(offenders)
        + "\n\nEvery path by which content leaves the deployment must redact "
        "secrets and enforce the 1.9 SecOps aggregation floor."
    )


def test_protected_surfaces_actually_reference_the_guard():
    """A "PROTECTED" claim must be backed by a real call in that module.

    Stops the allow-list from becoming a rubber stamp: marking a surface
    PROTECTED without wiring the guard fails here.
    """
    offenders: List[str] = []
    for (rel, func), justification in sorted(_EXPORT_ALLOWLIST.items()):
        if not justification.startswith(_PROTECTED_PREFIX):
            continue
        module_rel = _guarding_module(rel, justification)
        module_path = BACKEND_ROOT / module_rel
        if not module_path.exists():
            offenders.append(
                f"  {rel}::{func} names guarding module {module_rel!r}, which does not exist"
            )
            continue
        calls = _analyse(module_path).module_guard_calls
        if not (calls & (_GUARD_CALLS | _FLOOR_CALLS | _REDACT_CALLS)):
            offenders.append(
                f"  {rel}::{func} is marked PROTECTED but {module_rel} never calls "
                f"the export guard or its primitives"
            )
    assert not offenders, "PROTECTED claim not backed by code:\n" + "\n".join(offenders)


def test_allowlist_has_no_stale_entries():
    """The fence must track the code, not just grow.

    An entry whose surface no longer exists (renamed/removed) fails, so the
    allow-list cannot silently accumulate dead exemptions.
    """
    discovered = set(_discover_surfaces(_SCAN_ROOTS))
    stale = sorted(key for key in _EXPORT_ALLOWLIST if key not in discovered)
    assert not stale, (
        "Stale _EXPORT_ALLOWLIST entries (surface renamed or removed) — "
        f"delete them: {stale}"
    )


def test_scan_reaches_the_known_export_surfaces():
    """The scan is not a no-op: it finds the surfaces we know exist."""
    discovered = set(_discover_surfaces(_SCAN_ROOTS))
    for expected in (
        ("discovery/offline_export.py", "export"),
        ("app/routes_evidence_export.py", "_serve"),
        ("app/routes_cloud_connectors.py", "download_security_artifact"),
    ):
        assert expected in discovered, (
            f"{expected} was not discovered — the export-surface scanner is broken"
        )


def test_a_new_unprotected_export_surface_fails_without_editing_this_test(tmp_path):
    """AC5's durability property, proven rather than asserted.

    A rogue export route written into a fresh tree is discovered and reported
    with no change to this file.
    """
    rogue = tmp_path / "routes_rogue_export.py"
    rogue.write_text(
        "from fastapi import Response\n"
        "def download_everything():\n"
        "    return Response(content=b'{}', media_type='application/json',\n"
        "                    headers={'Content-Disposition': 'attachment; filename=\"x.json\"'})\n",
        encoding="utf-8",
    )
    discovered = _discover_surfaces((tmp_path,))
    assert ("routes_rogue_export.py", "download_everything") in discovered, (
        "a new Content-Disposition export surface was NOT discovered"
    )
    unaccounted = [key for key in discovered if key not in _EXPORT_ALLOWLIST]
    assert unaccounted, "the rogue surface must be reported as unaccounted"


def test_a_file_writing_export_surface_is_discovered(tmp_path):
    """The other export shape: serialising an artifact to disk (offline_export)."""
    rogue = tmp_path / "dump_tool.py"
    rogue.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "def export_findings(findings, out):\n"
        "    Path(out).write_text(json.dumps(findings), encoding='utf-8')\n",
        encoding="utf-8",
    )
    discovered = _discover_surfaces((tmp_path,))
    assert ("dump_tool.py", "export_findings") in discovered


def test_a_guarded_surface_is_recognised_as_guarded(tmp_path):
    """The PROTECTED mechanism itself works, independent of real-codebase drift."""
    guarded = tmp_path / "routes_good_export.py"
    guarded.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "from app.export_guard import guard_export_payload\n"
        "def export_findings(findings, out):\n"
        "    g = guard_export_payload(findings, where='x')\n"
        "    Path(out).write_text(json.dumps(g.payload), encoding='utf-8')\n",
        encoding="utf-8",
    )
    discovered = _discover_surfaces((tmp_path,))
    key = ("routes_good_export.py", "export_findings")
    assert key in discovered
    _lineno, analysis = discovered[key]
    assert "guard_export_payload" in analysis.module_guard_calls
    assert "export_findings" in analysis.guarded_functions


def test_shared_guard_is_the_single_source_of_truth():
    """Structural: the guard must delegate to the canonical modules, never
    reimplement the patterns or the floor locally (mirrors the
    ``scan_and_redact.__module__`` assertion in the MSP-B4/B11 redaction tests).
    """
    from app import export_guard

    scan = export_guard._redactor(strict=False)
    strict_scan = export_guard._redactor(strict=True)
    assert scan is not None and strict_scan is not None
    assert scan.__module__.endswith("discovery.ingest.secret_redaction")
    assert strict_scan.__module__.endswith("discovery.ingest.secret_redaction")
    assert scan is not strict_scan, "base and strict variants must differ"

    floor = export_guard._aggregation_floor()
    assert floor is not None
    assert floor.__name__.endswith("security_ops_aggregation_floor")

    # The guard module must not define its own CVE/IP regexes.
    source = Path(export_guard.__file__).read_text(encoding="utf-8")
    for forbidden in ("CVE-", "re.compile"):
        assert forbidden not in source, (
            f"export_guard must not reimplement detection ({forbidden!r} found) — "
            "it delegates to security_ops_aggregation_floor and secret_redaction"
        )
