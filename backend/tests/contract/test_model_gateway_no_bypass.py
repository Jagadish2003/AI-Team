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
    for lineno, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.lower() in lowered:
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
