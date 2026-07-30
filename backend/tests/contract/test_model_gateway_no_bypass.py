"""R16-D1 T4 — No-bypass enforcement test.

Scans every Python source file outside ``backend/app/model_gateway/`` and
fails the build if any file contains a direct model-provider endpoint, SDK
method, or API-key header reference.

The gateway is the ONLY code permitted to make an outbound model call.  A
single bypass voids the "data never leaves the boundary" guarantee that
regulated customers depend on (R16-D1 §2).

Acceptance Criteria covered
----------------------------
T4-AC1  test_no_direct_model_calls_outside_gateway passes when no direct
        model calls exist outside backend/app/model_gateway/.
T4-AC2  The test fails the build when api.anthropic.com, x-api-key, openai,
        messages.create, or urllib+anthropic appear outside the gateway.
T4-AC3  The test passes on the current codebase once T3's migration is done.
T4-AC4  Introducing a direct model call in a new file causes this test to
        fail immediately (verified by test_ac4_new_bypass_detected).
"""
from __future__ import annotations

import sys
import textwrap
import tempfile
from pathlib import Path
from typing import List, Tuple

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Resolves to backend/
BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]

# Only directory permitted to contain direct provider references.
GATEWAY_PACKAGE: Path = BACKEND_ROOT / "app" / "model_gateway"

# This file — excluded so the pattern strings defined below don't self-match.
_THIS_FILE: Path = Path(__file__).resolve()

# Directories whose contents are never scanned (generated / third-party code).
_SKIP_DIRS = frozenset({".venv", "node_modules", "__pycache__", ".git", "build", "dist"})

# ---------------------------------------------------------------------------
# Forbidden patterns (T4-AC2).
#
# Each string is constructed by concatenation so this source file itself does
# not match when scanned — a naive inline literal would self-trigger and
# produce a false positive on every CI run.
# ---------------------------------------------------------------------------

_PAT_ANTHROPIC_ENDPOINT = "api.anthrop" + "ic.com"          # direct REST endpoint
_PAT_XAPI_KEY           = "x-api-" + "key"                   # provider API-key header
_PAT_OPENAI             = "open" + "ai"                       # OpenAI SDK / URL
_PAT_MESSAGES_CREATE    = "messages" + ".create"              # Anthropic/OpenAI SDK call
_PAT_ANTHROPIC_VERSION  = "anthropic-" + "version"           # Anthropic version header

FORBIDDEN_PATTERNS: List[str] = [
    _PAT_ANTHROPIC_ENDPOINT,
    _PAT_XAPI_KEY,
    _PAT_OPENAI,
    _PAT_MESSAGES_CREATE,
    _PAT_ANTHROPIC_VERSION,
]


# ---------------------------------------------------------------------------
# Allow-list — narrow, justified exemptions.
#
# Some forbidden patterns are not exclusive to model providers. ``x-api-key`` in
# particular is a generic HTTP auth header (AWS API Gateway uses it), so a file
# that talks to a NON-model service can trip the scan without any model call
# existing. Rather than weaken the pattern (which would blind the scan to real
# provider calls everywhere) or obfuscate the header at the call site (which
# would hide a genuine bypass from this very test), each false positive is
# exempted here as an explicit ``(path, pattern)`` pair with a justification.
#
# Keys are POSIX paths relative to BACKEND_ROOT. The exemption is per-PATTERN,
# never per-file: every other forbidden pattern still fails the listed file, so
# introducing an actual model call there is still caught immediately.
# ``test_allowlist_has_no_stale_entries`` fails if an entry stops matching a real
# line, so an exemption cannot silently outlive its cause.
# ---------------------------------------------------------------------------

_ALLOWLIST: dict = {
    ("license/issuance.py", _PAT_XAPI_KEY): (
        "R-1.9.1-L3 (AT-680): this header authenticates the license-SIGNING "
        "service (the DevOps Lambda behind LICENSE_API_URL / AWS API Gateway) — "
        "it is not a model-provider credential. backend/license/ is vendor ops "
        "tooling excluded from the customer image via backend/.dockerignore, and "
        "holds no anthropic/openai/SDK reference at all, so it cannot affect the "
        "R16-D1 data-boundary guarantee."
    ),
}


def _allowlist_key(path: Path) -> str:
    """Return the allow-list key for a path (POSIX, relative to BACKEND_ROOT).

    A path outside BACKEND_ROOT (e.g. a tmp_path file written by the scanner's
    own self-tests) can never be allow-listed, so it gets a sentinel that never
    matches a key.
    """
    try:
        return path.resolve().relative_to(BACKEND_ROOT.resolve()).as_posix()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iter_python_files(root: Path):
    """Yield all .py files under root, skipping known non-source directories."""
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def _collect_scan_targets() -> List[Path]:
    """Return every .py file outside the gateway package and this test file."""
    targets: List[Path] = []
    for py_file in _iter_python_files(BACKEND_ROOT):
        # Skip the gateway package — it is the only permitted location.
        try:
            py_file.relative_to(GATEWAY_PACKAGE)
            continue
        except ValueError:
            pass
        # Skip this enforcement test (it contains the patterns as string literals).
        if py_file == _THIS_FILE:
            continue
        targets.append(py_file)
    return targets


def _scan_file(path: Path) -> List[Tuple[int, str, str]]:
    """Return (line_number, line_text, matched_pattern) for every violation."""
    violations: List[Tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return violations
    rel_key = _allowlist_key(path)
    for lineno, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.lower() not in lowered:
                continue
            # An allow-listed pattern is skipped WITHOUT breaking, so a genuine
            # violation from a different pattern on the same line is still found.
            if (rel_key, pattern) in _ALLOWLIST:
                continue
            violations.append((lineno, line.rstrip(), pattern))
            break  # one report per line is enough
    return violations


# ---------------------------------------------------------------------------
# T4-AC1 / T4-AC3 — passes on the clean post-T3 codebase
# ---------------------------------------------------------------------------


def test_no_direct_model_calls_outside_gateway():
    """No Python file outside backend/app/model_gateway/ may contain a direct
    model-provider endpoint, SDK method, or API-key header reference.

    Failing this test means a bypass exists that could void the data-boundary
    guarantee for regulated customers (R16-D1 §2).  Route all model calls
    through get_generation_provider() or get_embedding_provider() instead.
    """
    all_violations: List[str] = []

    for py_file in _collect_scan_targets():
        file_violations = _scan_file(py_file)
        for lineno, line, pattern in file_violations:
            rel = py_file.relative_to(BACKEND_ROOT)
            all_violations.append(
                f"  {rel}:{lineno}: [{pattern!r}]  {line}"
            )

    assert not all_violations, (
        "Direct model-provider references found outside backend/app/model_gateway/.\n"
        "Route all model calls through get_generation_provider() or "
        "get_embedding_provider().\n\n"
        "Violations:\n" + "\n".join(all_violations)
    )


# ---------------------------------------------------------------------------
# T4-AC4 — a new bypass is caught immediately
# ---------------------------------------------------------------------------


def test_ac4_new_bypass_detected(tmp_path):
    """Introducing a direct model call in any new file causes this test to fail.

    Writes a temporary .py file that contains one of the forbidden patterns,
    then asserts that _scan_file() flags it.  This proves the scanner works
    without relying on the production codebase state.
    """
    bypass_file = tmp_path / "direct_caller.py"
    # Construct the forbidden string at runtime so this test file itself does
    # not self-match.  The content written to the file IS the bare string.
    forbidden_line = "x-api-" + "key"
    bypass_file.write_text(
        textwrap.dedent(f"""\
            import urllib.request
            req = urllib.request.Request(
                "https://api.anthrop" + "ic.com/v1/messages",
                headers={{"{forbidden_line}": "sk-..."}},
            )
        """),
        encoding="utf-8",
    )

    violations = _scan_file(bypass_file)
    assert violations, (
        "Scanner did not detect the forbidden pattern in a file that clearly "
        "contains a direct model call.  The enforcement test is broken."
    )
    # Every reported violation must name a pattern from FORBIDDEN_PATTERNS.
    detected_patterns = {v[2] for v in violations}
    assert detected_patterns.issubset(set(FORBIDDEN_PATTERNS)), (
        f"Unexpected pattern names returned: {detected_patterns - set(FORBIDDEN_PATTERNS)}"
    )


def test_ac4_full_scan_pipeline_collects_and_flags_bypass_under_backend(
    tmp_path, monkeypatch
):
    """End-to-end: a bypass file placed anywhere under BACKEND_ROOT is both
    COLLECTED by _collect_scan_targets() AND flagged by _scan_file().

    test_ac4_new_bypass_detected above exercises _scan_file() in isolation on a
    file written to tmp_path (outside BACKEND_ROOT), so it would still pass even
    if _collect_scan_targets() had a bug that wrongly excluded a real directory
    (e.g. backend/tests/unit/). This test covers the collection step too: it
    points BACKEND_ROOT / GATEWAY_PACKAGE at a temporary backend layout and
    drives the exact loop the production scan uses.

    The temp tree is built under tmp_path — never inside the real repo — so a
    crash can never leave a bypass file behind that would fail the real
    enforcement scan on the next run.
    """
    import sys

    fake_root = tmp_path / "backend"
    gateway = fake_root / "app" / "model_gateway"
    gateway.mkdir(parents=True)
    rogue_dir = fake_root / "tests" / "unit"
    rogue_dir.mkdir(parents=True)

    # Construct the forbidden string at runtime so THIS file does not self-match.
    forbidden = "x-api-" + "key"

    # A direct-call bypass OUTSIDE the gateway — must be collected and flagged.
    rogue = rogue_dir / "rogue_caller.py"
    rogue.write_text(f'HEADERS = {{"{forbidden}": "sk-..."}}\n', encoding="utf-8")
    # The same pattern INSIDE the gateway — the permitted location, must be excluded.
    allowed = gateway / "permitted_provider.py"
    allowed.write_text(f'HEADERS = {{"{forbidden}": "sk-..."}}\n', encoding="utf-8")

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "BACKEND_ROOT", fake_root)
    monkeypatch.setattr(module, "GATEWAY_PACKAGE", gateway)

    # Collection step: rogue file is included; gateway file is excluded.
    targets = _collect_scan_targets()
    assert rogue in targets, (
        "the full scan pipeline must COLLECT a bypass file placed under BACKEND_ROOT"
    )
    assert allowed not in targets, (
        "gateway-package files must be excluded from the collected scan targets"
    )

    # Scan step driven over the collected targets — the same loop the production
    # test (test_no_direct_model_calls_outside_gateway) runs.
    flagged = {t for t in targets if _scan_file(t)}
    assert rogue in flagged, "the collected bypass file must be flagged by the scanner"
    assert allowed not in flagged, "gateway file must never be flagged (it was excluded)"


# ---------------------------------------------------------------------------
# Allow-list integrity — an exemption must stay narrow, live, and justified
# ---------------------------------------------------------------------------


def test_allowlist_has_no_stale_entries():
    """Every allow-list entry names a real file that really contains the pattern.

    Keeps the exemption list honest: once the flagged line is removed (or the
    file deleted), the entry must go too. Without this, a stale exemption would
    silently keep a future genuine violation hidden.
    """
    stale: List[str] = []
    for (rel_path, pattern), justification in _ALLOWLIST.items():
        assert justification.strip(), (
            f"allow-list entry {rel_path!r}/{pattern!r} has no justification — "
            "every exemption must say why it is not a model call."
        )
        target = BACKEND_ROOT / rel_path
        if not target.is_file():
            stale.append(f"  {rel_path}: file does not exist")
            continue
        text = target.read_text(encoding="utf-8", errors="replace").lower()
        if pattern.lower() not in text:
            stale.append(f"  {rel_path}: no longer contains [{pattern!r}]")

    assert not stale, (
        "Stale no-bypass allow-list entries — remove them from _ALLOWLIST:\n"
        + "\n".join(stale)
    )


def test_allowlist_never_exempts_an_entire_file():
    """No file is exempted from ALL forbidden patterns.

    An exemption is per-(file, pattern). A file exempted from every pattern
    would be a blind spot where a real direct model call could hide.
    """
    exempt_by_file: dict = {}
    for rel_path, pattern in _ALLOWLIST:
        exempt_by_file.setdefault(rel_path, set()).add(pattern)

    for rel_path, patterns in exempt_by_file.items():
        remaining = set(FORBIDDEN_PATTERNS) - patterns
        assert remaining, (
            f"{rel_path} is exempted from every forbidden pattern — that is a "
            "blanket exclusion, not a targeted false-positive exemption."
        )


def test_allowlist_does_not_mask_a_real_bypass_in_the_same_file(tmp_path, monkeypatch):
    """An allow-listed file is STILL flagged for a non-exempt pattern.

    Builds a fake backend tree containing the allow-listed path, with both the
    exempted line and a genuine direct-model-call line (including one line that
    holds BOTH patterns at once). The exempted pattern must be ignored while the
    real bypass is reported — proving the exemption is narrow and cannot be used
    to smuggle a model call into that file.
    """
    fake_root = tmp_path / "backend"
    target = fake_root / "license" / "issuance.py"
    target.parent.mkdir(parents=True)

    # Built at runtime so this test file does not self-match the scan.
    exempt = "x-api-" + "key"
    real_bypass = "api.anthrop" + "ic.com"

    target.write_text(
        textwrap.dedent(f"""\
            HEADERS = {{"{exempt}": token}}                 # exempt — signing service
            URL = "https://{real_bypass}/v1/messages"      # genuine bypass
            BOTH = {{"{exempt}": k, "url": "{real_bypass}"}}  # exempt + bypass on one line
        """),
        encoding="utf-8",
    )

    monkeypatch.setattr(sys.modules[__name__], "BACKEND_ROOT", fake_root)

    flagged = _scan_file(target)
    patterns = {p for _, _, p in flagged}
    lines = {lineno for lineno, _, _ in flagged}

    assert exempt not in patterns, (
        "the allow-listed pattern must not be reported for the allow-listed file"
    )
    assert patterns == {real_bypass}, (
        f"expected only the genuine bypass to be reported, got {patterns}"
    )
    # Line 3 carries both patterns — the exemption must not suppress its report.
    assert lines == {2, 3}, (
        f"expected both bypass lines (2 and 3) to be flagged, got {sorted(lines)}"
    )


def test_allowlist_is_not_consulted_for_other_files(tmp_path, monkeypatch):
    """The same exempted pattern in a DIFFERENT file is still a violation.

    The exemption is keyed to one path; any other file using the header is
    reported as before.
    """
    fake_root = tmp_path / "backend"
    other = fake_root / "app" / "some_feature.py"
    other.parent.mkdir(parents=True)

    exempt = "x-api-" + "key"
    other.write_text(f'HEADERS = {{"{exempt}": "sk-..."}}\n', encoding="utf-8")

    monkeypatch.setattr(sys.modules[__name__], "BACKEND_ROOT", fake_root)

    assert _scan_file(other), (
        "an allow-listed pattern must still be flagged in a non-allow-listed file"
    )


# ---------------------------------------------------------------------------
# Sanity — gateway package itself is excluded from scanning
# ---------------------------------------------------------------------------


def test_gateway_package_excluded_from_scan():
    """The gateway package is NOT in the scan targets.

    If it were included, hosted_provider.py would always fail the enforcement
    test — defeating its purpose as the single permitted location for direct
    calls.
    """
    targets = _collect_scan_targets()
    gateway_files = [f for f in targets if GATEWAY_PACKAGE in f.parents]
    assert not gateway_files, (
        "Gateway package files must not appear in the scan targets:\n"
        + "\n".join(str(f) for f in gateway_files)
    )


# ---------------------------------------------------------------------------
# Sanity — this test file excluded from scan
# ---------------------------------------------------------------------------


def test_this_file_excluded_from_scan():
    """This enforcement test file is excluded from the scan.

    The forbidden pattern strings are defined here as concatenated literals;
    including this file in the scan would always produce a false positive.
    """
    targets = _collect_scan_targets()
    assert _THIS_FILE not in targets, (
        "The enforcement test file itself must not appear in scan targets."
    )
