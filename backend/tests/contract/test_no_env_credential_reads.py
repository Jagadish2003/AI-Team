"""R17-D3 Addendum A T14 (AT-515) — no-env-credential-reads enforcement test.

Build-breaking guard, same spirit as the model-gateway no-bypass test
(``test_model_gateway_no_bypass.py``): scans every production Python source file
outside the vault/credentials layer and FAILS if any of them reads a
credential-shaped environment variable directly.

All connector credentials must resolve through the one path
(``app.auth.credentials.get_connector_credentials`` → ``app.auth.vault``); a
process-global env credential can never be per-org, so a single such read
re-opens the multi-tenant leak R17-D3 closed (Addendum §3).

Detection is AST-based, not substring-based, so it:
  * flags only real ``os.getenv("X")`` / ``os.environ.get("X")`` /
    ``os.environ["X"]`` reads whose env-var NAME is a string literal, and
  * ignores comments/docstrings that merely mention a name, and
  * ignores DYNAMIC reads via a variable (e.g. ``os.environ.get(secret_key)`` in
    ``secrets.py``) — that generic path is how OAuth *client* secrets are read and
    is deliberately connector-agnostic.

Acceptance Criteria covered
---------------------------
AC13  Fails the build if any module outside the vault/credentials layer reads a
      credential-shaped env var (``test_no_env_credential_reads_outside_vault``).
AC8   Validates the migration (T11): every ingestor/health check resolves via
      ``get_connector_credentials`` — a leftover env read fails this test.
AC15  Combined with the core R17-D3 ACs, closes out the complete isolation claim.
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import List, Optional, Tuple

# Resolves to backend/
BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]

_THIS_FILE: Path = Path(__file__).resolve()

# Never scanned: generated/third-party trees, and test code (tests legitimately
# set/read credential-shaped env vars as fixtures — the guarantee is about
# PRODUCTION source, not test scaffolding).
_SKIP_DIRS = frozenset(
    {".venv", "node_modules", "__pycache__", ".git", "build", "dist", "tests"}
)

# ---------------------------------------------------------------------------
# Forbidden env-var names (R17-D3 Addendum §3). Exact per-client connector
# credential names, plus the credential-shaped suffixes.
# ---------------------------------------------------------------------------
_FORBIDDEN_EXACT = frozenset(
    {
        "SF_ACCESS_TOKEN",
        "JIRA_TOKEN",
        "SERVICENOW_PASS",
        "SERVICENOW_TOKEN",
        "NCINO_ACCESS_TOKEN",
    }
)
_FORBIDDEN_SUFFIXES: Tuple[str, ...] = ("_SECRET", "_PASSWORD", "_TOKEN")

# ---------------------------------------------------------------------------
# The ONLY files permitted to read a credential-shaped env var — the credential
# layer itself (the Addendum's ALLOWED list). vault.py reads CREDENTIAL_VAULT_KEY;
# credentials.py is the single resolution path.
# ---------------------------------------------------------------------------
_ALLOWED_FILES = frozenset(
    {
        (BACKEND_ROOT / "app" / "auth" / "vault.py").resolve(),
        (BACKEND_ROOT / "app" / "auth" / "credentials.py").resolve(),
    }
)

# Env-var NAMES that match a forbidden suffix but are NOT per-client connector
# credentials — per-DEPLOYMENT infrastructure secrets that legitimately live in
# the environment, exactly like CREDENTIAL_VAULT_KEY and the OAuth *_CLIENT_SECRET
# app secrets the Addendum explicitly keeps in .env (Addendum §3). These are not
# per-org and are not resolved through the connector vault.
_ALLOWED_NAMES = frozenset(
    {
        "SMTP_PASSWORD",        # email transport (app/email_service.py)
        "OAUTH_STATE_SECRET",   # OAuth state signing key (app/auth/oauth_state.py)
        "JWT_SECRET",           # session JWT signing key (app/auth/user_auth.py)
        "CREDENTIAL_VAULT_KEY",  # the key that encrypts the vault (never per-org)
    }
)


def _is_forbidden_name(name: str) -> bool:
    """True when a literal env-var name is a per-client connector credential."""
    if name in _ALLOWED_NAMES:
        return False
    # OAuth *application* client secrets are per-deployment, not per-client — the
    # Addendum keeps them in env. Read via secrets.py's generic (dynamic) path.
    if name.endswith("_CLIENT_SECRET"):
        return False
    return name in _FORBIDDEN_EXACT or name.endswith(_FORBIDDEN_SUFFIXES)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _const_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_environ(node: ast.AST) -> bool:
    """True for ``os.environ`` or a bare ``environ`` (from os import environ)."""
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return True
    if isinstance(node, ast.Name) and node.id == "environ":
        return True
    return False


def _env_read_name(node: ast.AST) -> Optional[str]:
    """Return the literal env-var name this node reads, or None.

    Recognises os.getenv("X"), os.environ.get("X"), os.environ["X"], and the
    bare-import equivalents. Only LITERAL names are returned (dynamic reads via a
    variable are intentionally not matched)."""
    # os.getenv("X") / getenv("X")
    if isinstance(node, ast.Call):
        func = node.func
        is_getenv = (
            (isinstance(func, ast.Attribute) and func.attr == "getenv")
            or (isinstance(func, ast.Name) and func.id == "getenv")
        )
        if is_getenv and node.args:
            return _const_str(node.args[0])
        # os.environ.get("X") / environ.get("X")
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and _is_environ(func.value)
            and node.args
        ):
            return _const_str(node.args[0])
    # os.environ["X"] / environ["X"]
    if isinstance(node, ast.Subscript) and _is_environ(node.value):
        return _const_str(node.slice)
    return None


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def _collect_scan_targets() -> List[Path]:
    targets: List[Path] = []
    for py_file in _iter_python_files(BACKEND_ROOT):
        resolved = py_file.resolve()
        if resolved in _ALLOWED_FILES or resolved == _THIS_FILE:
            continue
        targets.append(py_file)
    return targets


def _scan_file(path: Path) -> List[Tuple[int, str]]:
    """Return (line_number, env_var_name) for every forbidden credential read."""
    violations: List[Tuple[int, str]] = []
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return violations
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return violations
    for node in ast.walk(tree):
        name = _env_read_name(node)
        if name and _is_forbidden_name(name):
            violations.append((getattr(node, "lineno", 0), name))
    return violations


# ---------------------------------------------------------------------------
# AC13 / AC8 — the production codebase is clean
# ---------------------------------------------------------------------------


def test_no_env_credential_reads_outside_vault():
    """Build fails if any module outside the vault/credentials layer reads a
    credential-shaped environment variable.

    All connector credentials must resolve via
    ``app.auth.credentials.get_connector_credentials(org_id, connector_id)``.
    A direct env read re-opens the multi-tenant credential leak (R17-D3 §3)."""
    offenders: List[str] = []
    for py_file in _collect_scan_targets():
        for lineno, name in _scan_file(py_file):
            rel = py_file.relative_to(BACKEND_ROOT)
            offenders.append(f"  {rel}:{lineno}: reads env credential {name!r}")

    assert not offenders, (
        "Env credential read outside the vault layer:\n"
        + "\n".join(offenders)
        + "\n\nAll connector credentials must resolve via "
        "get_connector_credentials(org_id, connector_id). If this is a genuine "
        "per-deployment instance secret (not a per-client connector credential), "
        "add it to _ALLOWED_NAMES with a justification."
    )


# ---------------------------------------------------------------------------
# The scanner actually catches a new bypass (AC13's "fails the build if…")
# ---------------------------------------------------------------------------


def test_scanner_flags_a_new_env_credential_read(tmp_path):
    """A new file that reads a forbidden credential env var is flagged.

    Proves the guard works independent of the current codebase state — if this
    ever stops flagging, the enforcement test is broken and AC13 is void."""
    bypass = tmp_path / "rogue_ingestor.py"
    # Build the forbidden name at runtime so this test file never self-matches.
    forbidden = "JIRA" + "_TOKEN"
    bypass.write_text(
        textwrap.dedent(
            f"""\
            import os
            token = os.getenv("{forbidden}", "")
            token2 = os.environ.get("SF" "_ACCESS_TOKEN")
            token3 = os.environ["SERVICENOW" "_TOKEN"]
            """
        ),
        encoding="utf-8",
    )
    violations = _scan_file(bypass)
    names = {n for _, n in violations}
    assert names == {"JIRA_TOKEN", "SF_ACCESS_TOKEN", "SERVICENOW_TOKEN"}, (
        f"scanner missed a forbidden env credential read: got {names}"
    )


def test_scanner_ignores_dynamic_and_allowed_reads(tmp_path):
    """Dynamic reads (via a variable) and allowed instance secrets are NOT flagged.

    ``os.environ.get(secret_key)`` is how the auth layer resolves OAuth client
    secrets generically; SMTP_PASSWORD / JWT_SECRET / OAUTH_STATE_SECRET and
    *_CLIENT_SECRET are per-deployment instance secrets, not connector credentials."""
    ok = tmp_path / "legit.py"
    ok.write_text(
        textwrap.dedent(
            """\
            import os
            secret_key = "SALESFORCE_CLIENT_SECRET"
            dynamic = os.environ.get(secret_key)          # variable, not a literal
            client = os.getenv("SALESFORCE_CLIENT_SECRET")  # app secret, allowed
            smtp = os.getenv("SMTP_PASSWORD")               # instance secret, allowed
            jwt = os.environ["JWT_SECRET"]                  # instance secret, allowed
            vault_key = os.getenv("CREDENTIAL_VAULT_KEY")   # the vault key, allowed
            # os.getenv("JIRA_TOKEN") in a comment must not match
            """
        ),
        encoding="utf-8",
    )
    assert _scan_file(ok) == [], (
        "scanner false-positived on a dynamic read / allowed instance secret / comment"
    )


# ---------------------------------------------------------------------------
# Sanity — scope of the scan
# ---------------------------------------------------------------------------


def test_credential_layer_and_this_file_excluded_from_scan():
    """vault.py, credentials.py, and this enforcement test are not scan targets."""
    targets = set(_collect_scan_targets())
    assert _THIS_FILE not in targets
    for allowed in _ALLOWED_FILES:
        assert allowed not in targets, f"credential-layer file scanned: {allowed}"


def test_scan_covers_the_ingestion_layer():
    """The scan actually reaches the migrated ingestion modules — otherwise a
    regression there would go undetected and the test would be a no-op."""
    targets = {p.resolve() for p in _collect_scan_targets()}
    for rel in (
        "discovery/ingest/salesforce.py",
        "discovery/ingest/jira.py",
        "discovery/ingest/connector_health.py",
        "app/connector_health.py",
    ):
        assert (BACKEND_ROOT / rel).resolve() in targets, (
            f"expected {rel} to be in the scan targets"
        )
