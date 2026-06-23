"""
backend/tests/contract/test_record_event_contract.py

Contract audit for Task 5A (AT-182 / AT-211) — record_event() call-site contract.

AT-211 acceptance criteria:
  AC1  test_unregistered_event_type_raises   — ValueError on unknown event_type.
  AC2  test_all_call_sites_use_registered_event_type — grep-based static audit.
  AC3  test_all_registered_types_have_typeddict — REGISTERED_EVENT_TYPES ↔
       EVENT_PAYLOAD_TYPES in sync (no orphaned registration).

The locked telemetry signature (T3-S10-A) is:

    record_event(event_type: str, payload: Optional[dict] = None) -> None

All event metadata (org_id, source, run_id, success, count, connector_id, ...)
travels *inside* the payload dict — record_event() extracts it there. Passing
any of those as keyword arguments raises TypeError and, because every call site
is wrapped in a best-effort try/except, silently drops the event.

These tests statically audit every record_event() call site under backend/ and
fail the build if:
  * an unregistered event_type is used (AC2), or
  * a call passes anything other than the two locked parameters (regression
    guard for the connector_health.py drift fixed in AT-209).

Pure static analysis — no DB or app startup required.
"""

import ast
from pathlib import Path

import pytest

from app.telemetry import (
    EVENT_PAYLOAD_TYPES,
    EVENT_REGISTRY,
    REGISTERED_EVENT_TYPES,
    record_event,
)

# backend/ root: tests/contract/<this file> -> parents[2] == backend/
BACKEND_ROOT = Path(__file__).resolve().parents[2]

# Directories that are not first-party source and must not be audited.
# "tests" is excluded so test files can legitimately call record_event() with
# unregistered event_type values (e.g. "unknown.event") to exercise the guard
# without triggering the production call-site audit.
_EXCLUDED_DIR_NAMES = {
    ".venv", "venv", "env", "site-packages", "__pycache__",
    "node_modules", ".git", ".mypy_cache", ".pytest_cache", "tests",
}

# The only keyword arguments the locked signature accepts.
_ALLOWED_KWARGS = {"event_type", "payload"}


def _iter_backend_py_files():
    """Yield every first-party .py file under backend/, excluding vendored dirs."""
    for path in BACKEND_ROOT.rglob("*.py"):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        yield path


def _module_str_constants(tree: ast.AST) -> dict[str, str]:
    """Collect simple module-level ``NAME = "literal"`` assignments.

    Used to resolve indirected event types such as
    ``_EVENT_TYPE = "db.ingestor_completed"`` referenced at the call site.
    """
    constants: dict[str, str] = {}
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            constants[node.target.id] = node.value.value
    return constants


def _record_event_calls():
    """Yield (file, lineno, call_node, module_constants) for each record_event() call."""
    for path in _iter_backend_py_files():
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "record_event(" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        constants = _module_str_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name != "record_event":
                continue
            yield path, node.lineno, node, constants


def _resolve_event_type(node: ast.Call, constants: dict[str, str]):
    """Return the statically-known event_type string for a call, or None.

    Checks the first positional argument, then an ``event_type=`` keyword,
    resolving a bare Name through module-level string constants.
    """
    candidate = None
    if node.args:
        candidate = node.args[0]
    if candidate is None:
        for kw in node.keywords:
            if kw.arg == "event_type":
                candidate = kw.value
                break
    if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
        return candidate.value
    if isinstance(candidate, ast.Name):
        return constants.get(candidate.id)
    return None


def test_all_call_sites_use_registered_event_type():
    """AC2 — every record_event() call site uses an event_type in EVENT_REGISTRY."""
    violations = []
    audited = 0
    for path, lineno, node, constants in _record_event_calls():
        event_type = _resolve_event_type(node, constants)
        if event_type is None:
            # Non-literal / dynamically-built event_type — cannot audit statically.
            continue
        audited += 1
        if event_type not in EVENT_REGISTRY:
            rel = path.relative_to(BACKEND_ROOT)
            violations.append(f"{rel}:{lineno} -> {event_type!r}")

    assert audited > 0, "audit found no record_event() call sites — scanner is broken"
    assert not violations, (
        "record_event() called with unregistered event_type(s):\n  "
        + "\n  ".join(violations)
        + f"\nRegistered types: {sorted(EVENT_REGISTRY)}"
    )


def test_no_call_site_uses_legacy_keyword_signature():
    """Regression (AT-209) — record_event() accepts only (event_type, payload).

    Passing org_id/source/run_id/success/count/connector_id as keyword
    arguments raises TypeError at runtime and the event is silently dropped.
    """
    violations = []
    for path, lineno, node, _constants in _record_event_calls():
        bad_kwargs = sorted(
            kw.arg for kw in node.keywords
            if kw.arg is not None and kw.arg not in _ALLOWED_KWARGS
        )
        # More than two positional args also cannot match (event_type, payload).
        too_many_positional = len(node.args) > 2
        if bad_kwargs or too_many_positional:
            rel = path.relative_to(BACKEND_ROOT)
            detail = f"unexpected kwargs={bad_kwargs}" if bad_kwargs else \
                f"{len(node.args)} positional args"
            violations.append(f"{rel}:{lineno} -> {detail}")

    assert not violations, (
        "record_event() called with arguments outside the locked "
        "(event_type, payload) signature:\n  " + "\n  ".join(violations)
    )


def test_every_registered_type_has_a_payload_schema():
    """AC3 — every registered event_type maps to a TypedDict/type schema."""
    missing = [
        et for et, schema in EVENT_REGISTRY.items()
        if not isinstance(schema, type)
    ]
    assert not missing, (
        f"registered event types without a usable payload schema: {missing}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# AT-211 — Three tests required by the Section 1c contract spec
# ─────────────────────────────────────────────────────────────────────────────

def test_unregistered_event_type_raises():
    """AT-211 AC1 — record_event() raises ValueError on an unknown event_type.

    The guard added in AT-210 (5A-2) must be wired correctly: passing an
    event_type not in REGISTERED_EVENT_TYPES must raise ValueError with a
    message that includes 'unregistered event type'.
    """
    with pytest.raises(ValueError, match="unregistered event type"):
        record_event(
            "unknown.event",
            {"org_id": "org1", "source": "test", "run_id": "r1"},
        )


def test_all_registered_types_have_typeddict():
    """AT-211 AC3 — REGISTERED_EVENT_TYPES and EVENT_PAYLOAD_TYPES are in sync.

    Every entry in REGISTERED_EVENT_TYPES must have a corresponding TypedDict
    (or type) in EVENT_PAYLOAD_TYPES. Since both are aliases of EVENT_REGISTRY
    (a single type→schema mapping), a key present in one is always present in
    the other. The test guards against any future refactor that splits them.
    """
    for event_type in REGISTERED_EVENT_TYPES:
        assert event_type in EVENT_PAYLOAD_TYPES, (
            f"{event_type!r} is registered in REGISTERED_EVENT_TYPES "
            f"but has no entry in EVENT_PAYLOAD_TYPES"
        )
        assert isinstance(EVENT_PAYLOAD_TYPES[event_type], type), (
            f"{event_type!r} maps to {EVENT_PAYLOAD_TYPES[event_type]!r} "
            f"which is not a type/TypedDict class"
        )


def test_all_sprint_10_11_call_sites_use_registered_types():
    """AT-211 AC2 — grep-based audit: every event_type= literal in the codebase
    is in REGISTERED_EVENT_TYPES.

    This test automatically catches any new call site added with an unregistered
    type — even if the developer forgets to run the manual audit. It complements
    test_all_call_sites_use_registered_event_type (which uses AST resolution)
    by also matching raw regex patterns on the source text.
    """
    import re
    violations = []
    for path in BACKEND_ROOT.rglob("*.py"):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "record_event(" not in src:
            continue
        for match in re.finditer(r"event_type\s*=\s*['\"]([^'\"]+)['\"]", src):
            et = match.group(1)
            if et not in REGISTERED_EVENT_TYPES:
                rel = path.relative_to(BACKEND_ROOT)
                violations.append(f"{rel}: {et!r}")

    assert not violations, (
        "record_event() called with unregistered event_type(s):\n  "
        + "\n  ".join(violations)
        + f"\nRegistered: {sorted(REGISTERED_EVENT_TYPES)}"
    )
