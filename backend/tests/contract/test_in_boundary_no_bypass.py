"""R17-D1 T6 - No-bypass verification for in-boundary mode.

Verifies the R16-D1 no-bypass enforcement still holds after the in-boundary
provider (T1-T5) was added, and extends the guarantee to in-boundary-specific
details: no application code outside ``backend/app/model_gateway/`` may know
about the in-boundary endpoint, auth header, chat/embeddings HTTP paths, config
keys, or model-specific HTTP details.

Why this matters (Story §2, AC6)
--------------------------------
The gateway is the ONLY approved place for a model-provider call.  For
in-boundary mode this is doubly important: a single direct call to a hosted
provider or external API outside the gateway would void the "data never leaves
the customer's boundary" guarantee that regulated customers depend on.

What this test proves
---------------------
  T6-A  The R16-D1 enforcement scan (its hosted-endpoint / API-key-header /
        SDK-method forbidden patterns) still finds NOTHING outside the
        gateway — adding the in-boundary provider introduced no hosted-style
        direct call.
  T6-B  No application code outside the gateway references the in-boundary
        endpoint config keys, the in-boundary config module/class, or the
        chat/embeddings HTTP paths — all such references are contained inside
        the gateway package.
  T6-C  The scanner catches a NEW in-boundary bypass immediately (so a future
        regression fails the build), mirroring R16-D1 T4-AC4.
  T6-D  Sanity: the in-boundary endpoint/auth details genuinely live inside the
        gateway package, and callers reach the provider only through the gateway
        (no direct import is required by application code).

This file constructs every forbidden pattern by string concatenation so it does
not self-match — neither under the R16-D1 scan (which scans this file) nor under
its own in-boundary scan.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import List, Tuple

# Reuse the R16-D1 enforcement scanner directly so "the R16-D1 test still
# passes" is proven with the same logic, not a reimplementation.  pytest's
# prepend import mode puts tests/contract on sys.path, so the sibling module
# imports by name.
import test_model_gateway_no_bypass as r16d1

from app.model_gateway import (
    _PROVIDER_REGISTRY,
    get_embedding_provider,
    get_generation_provider,
)
from app.model_gateway.in_boundary_config import IN_BOUNDARY_PROVIDER_NAME
from app.model_gateway.in_boundary_provider import InBoundaryModelProvider

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]
GATEWAY_PACKAGE: Path = BACKEND_ROOT / "app" / "model_gateway"
_THIS_FILE: Path = Path(__file__).resolve()

# Generated / third-party dirs never scanned.
_SKIP_DIRS = frozenset(
    {".venv", "myvenv", "venv", "node_modules", "__pycache__", ".git", "build", "dist"}
)

# ---------------------------------------------------------------------------
# In-boundary leakage patterns (T6-B).
#
# Application code outside the gateway must not contain any of these.  Each is
# concatenated so this file does not self-match.  Matching is case-insensitive,
# consistent with the R16-D1 scanner.
# ---------------------------------------------------------------------------

_PAT_IB_CONFIG_KEY = "IN_BOUND" + "ARY_"          # IN_BOUNDARY_* endpoint/auth/model keys
_PAT_IB_CONFIG_MODULE = "in_boundary" + "_config"  # the config module that owns endpoint+auth
_PAT_IB_CONFIG_CLASS = "InBound" + "aryConfig"     # the config class that exposes endpoint+auth
_PAT_IB_GEN_PATH = "/v1/chat/" + "completions"     # chat/embeddings generation path
_PAT_IB_EMB_PATH = "/v1/" + "embeddings"           # chat/embeddings embedding path

IN_BOUNDARY_FORBIDDEN_PATTERNS: List[str] = [
    _PAT_IB_CONFIG_KEY,
    _PAT_IB_CONFIG_MODULE,
    _PAT_IB_CONFIG_CLASS,
    _PAT_IB_GEN_PATH,
    _PAT_IB_EMB_PATH,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def _is_test_code(path: Path) -> bool:
    """True for test code — not 'application code' for the in-boundary scan.

    Test files legitimately import the in-boundary config/provider to exercise
    them, so they are excluded from the in-boundary containment scan (T6-B).
    The stricter R16-D1 scan (T6-A) still covers them.
    """
    if path.name == "conftest.py" or path.name.startswith("test_"):
        return True
    return "tests" in path.parts


def _collect_application_targets() -> List[Path]:
    """Every .py file that is application code outside the gateway package."""
    targets: List[Path] = []
    for py_file in _iter_python_files(BACKEND_ROOT):
        # Skip the gateway package — the one permitted location.
        try:
            py_file.relative_to(GATEWAY_PACKAGE)
            continue
        except ValueError:
            pass
        if _is_test_code(py_file):
            continue
        if py_file == _THIS_FILE:
            continue
        targets.append(py_file)
    return targets


def _scan_file(path: Path, patterns: List[str]) -> List[Tuple[int, str, str]]:
    """Return (line_number, line_text, matched_pattern) for every violation."""
    violations: List[Tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return violations
    for lineno, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for pattern in patterns:
            if pattern.lower() in lowered:
                violations.append((lineno, line.rstrip(), pattern))
                break
    return violations


# ===========================================================================
# T6-A — the R16-D1 no-bypass enforcement still passes with in-boundary added
# ===========================================================================


def test_t6a_r16d1_no_bypass_scan_still_clean():
    """No file outside the gateway contains a direct hosted-style model call —
    re-run with R16-D1's own scanner so adding in-boundary changed nothing."""
    all_violations: List[str] = []
    for py_file in r16d1._collect_scan_targets():
        for lineno, line, pattern in r16d1._scan_file(py_file):
            rel = py_file.relative_to(r16d1.BACKEND_ROOT)
            all_violations.append(f"  {rel}:{lineno}: [{pattern!r}]  {line}")

    assert not all_violations, (
        "R16-D1 no-bypass enforcement regressed after adding in-boundary mode.\n"
        "Route all model calls through get_generation_provider() / "
        "get_embedding_provider().\n\nViolations:\n" + "\n".join(all_violations)
    )


def test_t6a_in_boundary_provider_does_not_add_hosted_patterns_outside_gateway():
    """The in-boundary provider lives in the gateway, so none of its files appear
    as R16-D1 scan targets — confirming the addition introduced no scanned file
    that could carry a direct hosted call."""
    targets = r16d1._collect_scan_targets()
    leaked = [t for t in targets if GATEWAY_PACKAGE in t.parents]
    assert not leaked, (
        "Gateway files must never be scan targets (they are the permitted location)."
    )


# ===========================================================================
# T6-B — in-boundary endpoint/auth/HTTP details contained inside the gateway
# ===========================================================================


def test_t6b_no_application_code_references_in_boundary_details():
    """No application code outside the gateway references the in-boundary
    endpoint config keys, config module/class, or chat/embeddings paths."""
    all_violations: List[str] = []
    for py_file in _collect_application_targets():
        for lineno, line, pattern in _scan_file(py_file, IN_BOUNDARY_FORBIDDEN_PATTERNS):
            rel = py_file.relative_to(BACKEND_ROOT)
            all_violations.append(f"  {rel}:{lineno}: [{pattern!r}]  {line}")

    assert not all_violations, (
        "In-boundary endpoint/auth/model-call details leaked outside the gateway "
        "package.\nAll endpoint, auth, chat/embeddings path, and config-key "
        "references must stay inside backend/app/model_gateway/.\n\n"
        "Violations:\n" + "\n".join(all_violations)
    )


def test_t6b_scan_actually_covers_application_files():
    """Guard against a vacuous pass: the application scan must cover real files
    (e.g. app/main.py) so a clean result means 'checked', not 'checked nothing'."""
    targets = _collect_application_targets()
    assert len(targets) > 50, f"expected a substantial scan set; got {len(targets)}"
    assert any(t.name == "main.py" for t in targets), "app/main.py must be scanned"
    # And no gateway/test file slipped into the application target set.
    assert not any(GATEWAY_PACKAGE in t.parents for t in targets)
    assert not any(_is_test_code(t) for t in targets)


# ===========================================================================
# T6-C — a new in-boundary bypass is caught immediately (mirrors R16-D1 T4-AC4)
# ===========================================================================


def test_t6c_new_in_boundary_bypass_is_detected(tmp_path):
    """A new file that reads an in-boundary endpoint/auth detail directly is
    flagged by the scanner — proving a future bypass fails the build."""
    bypass_file = tmp_path / "rogue_caller.py"
    # Build the forbidden content at runtime so this test file does not self-match.
    endpoint_key = "IN_BOUND" + "ARY_GENERATION_ENDPOINT"
    api_key_name = "IN_BOUND" + "ARY_API_KEY"
    bypass_file.write_text(
        textwrap.dedent(
            f"""\
            import os
            import urllib.request

            # A direct, gateway-bypassing call to the customer's in-boundary model.
            url = os.environ["{endpoint_key}"]
            token = os.environ["{api_key_name}"]
            urllib.request.urlopen(url)
            """
        ),
        encoding="utf-8",
    )

    violations = _scan_file(bypass_file, IN_BOUNDARY_FORBIDDEN_PATTERNS)
    assert violations, "scanner failed to detect a direct in-boundary bypass"
    detected = {v[2] for v in violations}
    assert detected.issubset(set(IN_BOUNDARY_FORBIDDEN_PATTERNS))


def test_t6c_clean_file_is_not_flagged(tmp_path):
    """A file that uses only the gateway entry points is NOT flagged — the
    scanner does not produce false positives on compliant code."""
    clean_file = tmp_path / "good_caller.py"
    clean_file.write_text(
        textwrap.dedent(
            """\
            from app.model_gateway import generate, GenerationRequest

            result = generate(GenerationRequest(prompt="hi", max_tokens=8))
            text = result.text if result.ok else None
            """
        ),
        encoding="utf-8",
    )
    assert _scan_file(clean_file, IN_BOUNDARY_FORBIDDEN_PATTERNS) == []


# ===========================================================================
# T6-D — sanity: details live inside the gateway; callers use the gateway only
# ===========================================================================


def test_t6d_in_boundary_details_exist_inside_gateway():
    """The in-boundary endpoint/auth/config details DO exist — inside the gateway
    package — so the containment scan is meaningful, not checking absent code."""
    gateway_text = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in GATEWAY_PACKAGE.rglob("*.py")
    ).lower()
    # At least the config-key prefix and one chat/embeddings path are present.
    assert _PAT_IB_CONFIG_KEY.lower() in gateway_text
    assert _PAT_IB_GEN_PATH.lower() in gateway_text
    assert _PAT_IB_EMB_PATH.lower() in gateway_text


def test_t6d_provider_reachable_only_through_gateway(monkeypatch):
    """Callers select the in-boundary provider purely by config and reach it via
    the gateway resolver — no direct import/instantiation needed in app code."""
    assert isinstance(_PROVIDER_REGISTRY.get(IN_BOUNDARY_PROVIDER_NAME), InBoundaryModelProvider)

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    assert get_generation_provider().name == IN_BOUNDARY_PROVIDER_NAME
    assert get_embedding_provider().name == IN_BOUNDARY_PROVIDER_NAME
