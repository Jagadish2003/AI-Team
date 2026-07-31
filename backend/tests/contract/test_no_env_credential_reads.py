"""R17-D3 Addendum A T14 (AT-515) + R1.9.1-H1 T3 — no-env-credential-reads enforcement.

Build-breaking guard, same spirit as the model-gateway no-bypass test
(``test_model_gateway_no_bypass.py``). This file has two sections:

SECTION A (unchanged, AT-515): scans every production Python source file
outside the vault/credentials layer and FAILS if any of them reads a
credential-shaped environment variable by a LITERAL name. This is the
whole-backend guarantee from R17-D3 Addendum A.

SECTION B (R1.9.1-H1 T3): a stricter, dedicated sweep of
``backend/discovery/ingest/`` — the connector credential-resolution layer
where the 1.8 verification's critical F1 finding lived
(``operational_config.py``). Section A's scanner is LITERAL-name-only by
design (it deliberately ignores ``os.environ.get(some_variable)`` because
that is how ``app/auth/secrets.py`` generically resolves OAuth *client*
secrets). F1 hid in exactly that blind spot: ``resolve_target_secret`` read
``environ.get(key)`` where ``key`` was a variable, not a literal, so Section
A's scanner walked right over it even though it scans the whole tree.

Section B closes that hole for the ingest layer specifically: it flags
EVERY ``os.environ``/``os.getenv`` READ under ``backend/discovery/ingest/``
— literal or dynamic — unless the exact (file, name-or-scope) pair is on the
explicit, justified ``_INGEST_ALLOWLIST`` below. Nothing is enumerated by
hand: the module list is discovered by walking the directory tree at test
time (``Path.rglob``), so a module added after this test was written is
still swept, and the default posture for anything not on the allow-list is
FAIL, so a new unlisted read fails CI without any test edit (AC3).

Acceptance Criteria covered
----------------------------
AC13 (AT-515) Fails the build if any module outside the vault/credentials
      layer reads a credential-shaped env var by literal name.
AC2   (R1.9.1-H1) No module under ``backend/discovery/ingest/`` reads a
      credential-shaped key (token, secret, password, api_key,
      client_secret, connection URL) from the environment except
      allow-listed, justified entries — enforced dynamically, not by
      enumerating a fixed module list.
AC3   (R1.9.1-H1) A new ingest module containing an unlisted credential-
      shaped or dynamic env read fails CI without any test edit.
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Resolves to backend/
BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]

_THIS_FILE: Path = Path(__file__).resolve()

# Never scanned: generated/third-party trees, and test code (tests legitimately
# set/read credential-shaped env vars as fixtures — the guarantee is about
# PRODUCTION source, not test scaffolding).
_SKIP_DIRS = frozenset(
    {".venv", "node_modules", "__pycache__", ".git", "build", "dist", "tests"}
)

# =============================================================================
# SECTION A — R17-D3 Addendum A / AT-515 whole-backend literal-name guard.
# Unchanged: still the backend-wide guarantee that no production module reads
# a per-client connector credential by a literal env-var name.
# =============================================================================

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

# The ONLY files permitted to read a credential-shaped env var — the credential
# layer itself (the Addendum's ALLOWED list). vault.py reads CREDENTIAL_VAULT_KEY;
# credentials.py is the single resolution path.
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
# AST helpers shared by both sections
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


def _is_env_read_call(func: ast.AST) -> bool:
    """True if ``func`` is ``os.getenv``/``getenv`` or ``os.environ.get``/``environ.get``."""
    if isinstance(func, ast.Attribute) and func.attr == "getenv":
        return True
    if isinstance(func, ast.Name) and func.id == "getenv":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "get" and _is_environ(func.value):
        return True
    return False


def _env_read_name(node: ast.AST) -> Optional[str]:
    """Return the literal env-var name this node reads, or None.

    Recognises os.getenv("X"), os.environ.get("X"), os.environ["X"], and the
    bare-import equivalents. Only LITERAL names are returned (dynamic reads via a
    variable are intentionally not matched)."""
    if isinstance(node, ast.Call) and _is_env_read_call(node.func) and node.args:
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


def test_scanner_ignores_dynamic_and_allowed_reads():
    """Dynamic reads (via a variable) and allowed instance secrets are NOT flagged
    by Section A. (Section B below closes this exact gap for the ingest layer,
    where a dynamic read is how the F1 regression hid.)

    ``os.environ.get(secret_key)`` is how the auth layer resolves OAuth client
    secrets generically; SMTP_PASSWORD / JWT_SECRET / OAUTH_STATE_SECRET and
    *_CLIENT_SECRET are per-deployment instance secrets, not connector credentials."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ok = Path(tmp) / "legit.py"
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


# =============================================================================
# SECTION B — R1.9.1-H1 T3: generalized, dynamic sweep of
# backend/discovery/ingest/ (AC2, AC3).
# =============================================================================

INGEST_ROOT: Path = BACKEND_ROOT / "discovery" / "ingest"

# Credential-shaped suffixes swept in the ingest layer. Broader than Section A's
# _FORBIDDEN_SUFFIXES on purpose — the story explicitly calls out api_key and
# connection URLs in addition to token/secret/password.
_CREDENTIAL_SUFFIXES: Tuple[str, ...] = (
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_API_KEY",
    "_APIKEY",
    "_URL",
)
_CREDENTIAL_EXACT_NAMES = frozenset(
    {
        "SF_ACCESS_TOKEN",
        "JIRA_TOKEN",
        "SERVICENOW_PASS",
        "SERVICENOW_TOKEN",
        "NCINO_ACCESS_TOKEN",
        "STRS_ACCESS_TOKEN",
        "JAVA_APP_TOKEN",
        "DOTNET_APP_TOKEN",
    }
)

# Per-deployment infrastructure vars that are never a per-org connector
# credential, regardless of which ingest file reads them — kept as a short,
# global list (mirrors Section A's _ALLOWED_NAMES), not per-file, because
# they are infra config, not connector-specific.
_GLOBAL_ALLOWED_NAMES = frozenset(
    {
        "CREDENTIAL_VAULT_KEY",  # the key that encrypts the vault (never per-org)
        "DATABASE_URL",          # app-wide Postgres DSN (CLAUDE.md), not a connector cred
        "TEST_DATABASE_URL",     # contract-test DB DSN override, same rationale
    }
)


def _is_credential_shaped(name: str) -> bool:
    """True when a literal env-var name looks like a per-org connector credential."""
    if name in _GLOBAL_ALLOWED_NAMES:
        return False
    # R191-H1 names client_secret explicitly. Under discovery/ingest/ the guard
    # treats *_CLIENT_SECRET as credential-shaped so a future connector cannot
    # bypass the vault path by reading an OAuth/client secret from env.
    return name in _CREDENTIAL_EXACT_NAMES or name.endswith(_CREDENTIAL_SUFFIXES)


# ---------------------------------------------------------------------------
# Explicit, justified allow-list. Keyed by (path relative to the scanned root,
# marker), where marker is either:
#   * the literal env-var name (for a credential-shaped LITERAL read), or
#   * "<dynamic:{function_or_method_name}>" (for a read whose key is a
#     variable, not a literal — the exact shape the F1 regression used).
#
# Every entry below is a real, current read in backend/discovery/ingest/ — see
# test_allowlist_has_no_stale_entries, which fails if an entry no longer
# corresponds to anything in the tree.
# ---------------------------------------------------------------------------
_INGEST_ALLOWLIST: Dict[Tuple[str, str], str] = {
    # --- Connection-URL fallbacks: these remain only where a separate
    # standalone/legacy path still has an explicit one-line justification.
    # Salesforce/Jira health probes are intentionally NOT listed here: they use
    # the credential record URL only (R191-H1 literal AC4 posture).
    ("confluence.py", "CONFLUENCE_URL"): (
        "Standalone base-URL fallback for the depth-content body fetch "
        "(_raw_page_body); connection URL only, not the OAuth _get_client() "
        "path T2 fixed."
    ),
    ("connector_health.py", "SERVICENOW_URL"): (
        "Health-probe base-URL fallback; connection URL only, credential "
        "resolves via the vault regardless."
    ),
    ("fsc.py", "FSC_INSTANCE_URL"): (
        "2.0-D1 T2 FSC ingest: CLI/standalone instance-URL fallback mirroring "
        "ncino.py's; non-credential — the FSC access token is vault-only and has "
        "no env fallback, and _get_client() returns None before any credential "
        "lookup when not live."
    ),
    ("fsc.py", "SF_INSTANCE_URL"): (
        "FSC runs on the connected Salesforce org and reuses its instance URL as "
        "a documented CLI/standalone fallback; connection URL only."
    ),
    ("live_validator.py", "SF_INSTANCE_URL"): (
        "Standalone live-ingest validator CLI fallback; connection URL only, "
        "out of T2 scope per CLAUDE.md."
    ),
    ("ncino.py", "NCINO_INSTANCE_URL"): (
        "Documented CLI/standalone instance-URL fallback (CLAUDE.md Runtime "
        "Notes); non-credential — no NCINO_ACCESS_TOKEN env fallback exists."
    ),
    ("ncino.py", "SF_INSTANCE_URL"): (
        "nCino reuses the connected Salesforce org's instance URL as a "
        "documented CLI/standalone fallback; connection URL only."
    ),
    ("servicenow.py", "SERVICENOW_URL"): (
        "ServiceNow's own base-URL fallback; connection URL only, the "
        "credential itself is vault-only (never falls back to env)."
    ),
    ("strs_benefits.py", "STRS_INSTANCE_URL"): (
        "Documented CLI/standalone instance-URL fallback (CLAUDE.md Runtime "
        "Notes); non-credential."
    ),
    ("strs_benefits.py", "SF_INSTANCE_URL"): (
        "STRS reuses the connected Salesforce org's instance URL as a "
        "documented CLI/standalone fallback; connection URL only."
    ),
    # --- Dynamic reads: local helper functions whose only call sites pass a
    # feature-flag / numeric-tuning / config-discovery name, never a
    # credential. Each justification names every call site so a future call
    # site passing a credential-shaped name is a visible review question, not
    # a silent bypass.
    ("salesforce.py", "<dynamic:_env_flag>"): (
        "Local feature-flag helper; call sites pass only "
        "SF_DISABLE_DEPENDENCY_API / SF_SCAN_APEX_NC_REFS (boolean tuning "
        "knobs documented in the enclosing docstring), never a credential."
    ),
    ("salesforce.py", "<dynamic:_env_float>"): (
        "Local numeric-tuning helper; its only call site passes "
        "SF_FLOW_SCAN_BUDGET_SECONDS (a wall-clock budget), never a "
        "credential."
    ),
    ("documents.py", "<dynamic:_env_int>"): (
        "Local size/budget-cap helper; call sites pass only "
        "DOCUMENT_MAX_FILE_BYTES / DOCUMENT_EXTRACTION_BUDGET_BYTES, never a "
        "credential."
    ),
    ("documents_source.py", "<dynamic:_load_locations>"): (
        "Reads DOCUMENT_LOCATIONS (a JSON array of configured scan "
        "directories, R18-A1); configuration discovery, not a credential."
    ),
    ("dotnet_app_config.py", "<dynamic:_raw_target_entries>"): (
        "Reads DOTNET_APP_TARGETS (a JSON array of configured per-deployment "
        "targets); configuration discovery, not a credential — the "
        "credential itself resolves via the vault only (resolve_secret)."
    ),
    ("java_app_config.py", "<dynamic:_raw_target_entries>"): (
        "Reads JAVA_APP_TARGETS (a JSON array of configured per-deployment "
        "targets); configuration discovery, not a credential — the "
        "credential itself resolves via the vault only (resolve_secret)."
    ),
    ("git_content.py", "<dynamic:_path_defaults>"): (
        "Reads GIT_CONTENT_PATH_DEFAULTS (an org-level path-filter config "
        "object, AT-530); configuration discovery, not a credential."
    ),
    ("git_content.py", "<dynamic:_raw_repo_entries>"): (
        "Reads GIT_CONTENT_REPOS (a JSON array of configured repos); "
        "configuration discovery, not a credential."
    ),
    ("aws_auth.py", "<dynamic:default_hub_resolver>"): (
        "CLI/standalone hub-credential fallback (MSP-B1): reads the "
        "AWS_EVENTS_HUB_ACCESS_KEY_ID/_SECRET_ACCESS_KEY/_SESSION_TOKEN "
        "infrastructure env vars only — a deployment-level hub identity, never a "
        "per-org connector credential — and only after the vault "
        "(_vault_static_credential) has been checked first. Documented in "
        "CLAUDE.md as a non-production, never-in-.env fallback."
    ),
    ("aws_events_config.py", "<dynamic:_raw_config_entry>"): (
        "Reads AWS_EVENT_ACCOUNTS (a non-secret JSON config of pinned managed "
        "accounts / role ARNs / regions / partition, MSP-B1 — the AWS mirror of "
        "AZURE_EVENT_CONFIG below); configuration discovery, not a credential — "
        "the hub access key and per-account direct keys resolve via the vault "
        "only, and inline AWS keys in the config are rejected by "
        "aws_auth.parse_account_config."
    ),
    ("azure_events_config.py", "<dynamic:_raw_config_entry>"): (
        "Reads AZURE_EVENT_CONFIG (a non-secret JSON config of pinned "
        "subscriptions / environment / access mode, MSP-B2); configuration "
        "discovery, not a credential — the service principal secret resolves via "
        "the vault only, and inline secrets in the config are rejected."
    ),
    ("servicenow.py", "<dynamic:get_incident_metrics>"): (
        "Reads SERVICENOW_FIRST_ASSIGNED_FIELD (MSP-B4): the NAME of the "
        "ServiceNow field holding the first-assigned timestamp — a schema/config "
        "knob added to the query field list, never a credential."
    ),
    ("servicenow.py", "<dynamic:ingest>"): (
        "Reads SERVICENOW_FIRST_ASSIGNED_FIELD (MSP-B4) on the offline-fixture "
        "path — the same non-credential field-name config as get_incident_metrics; "
        "the credential itself resolves via the vault only."
    ),
}


class _EnvReadFinding:
    """One os.environ/os.getenv READ site found while walking a module."""

    __slots__ = ("lineno", "literal_name", "scope")

    def __init__(self, lineno: int, literal_name: Optional[str], scope: str) -> None:
        self.lineno = lineno
        self.literal_name = literal_name
        self.scope = scope


class _EnvReadVisitor(ast.NodeVisitor):
    """Records every env-var READ in a module — literal or dynamic — along
    with the literal name (if any) and the enclosing function/method name
    (used to key dynamic reads, which have no literal name to key on).

    Deliberately does NOT special-case dynamic reads as "safe" the way
    Section A's ``_env_read_name`` does — that exact exemption is the blind
    spot the F1 regression exploited (a variable-keyed
    ``environ.get(key)``), so here every read must be accounted for, by
    literal name or by allow-listed call site.
    """

    def __init__(self) -> None:
        self._scope_stack: List[str] = []
        self.findings: List[_EnvReadFinding] = []

    def _current_scope(self) -> str:
        return self._scope_stack[-1] if self._scope_stack else "<module>"

    def _visit_scoped(self, node: ast.AST) -> None:
        self._scope_stack.append(getattr(node, "name", "<module>"))
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_scoped(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_scoped(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if _is_env_read_call(node.func) and node.args:
            literal = _const_str(node.args[0])
            self.findings.append(
                _EnvReadFinding(node.lineno, literal, self._current_scope())
            )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        if _is_environ(node.value) and isinstance(node.ctx, ast.Load):
            literal = _const_str(node.slice)
            self.findings.append(
                _EnvReadFinding(node.lineno, literal, self._current_scope())
            )
        self.generic_visit(node)


def _iter_ingest_python_files(root: Path):
    """Dynamically discover every module under ``root`` — no fixed list.

    A module created after this test was written is still yielded, because
    this walks the directory tree at call time (AC2/AC3)."""
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def _scan_ingest_module(path: Path) -> List[_EnvReadFinding]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    visitor = _EnvReadVisitor()
    visitor.visit(tree)
    return visitor.findings


def _allowlist_key(rel: str, finding: _EnvReadFinding) -> Tuple[str, str]:
    if finding.literal_name is not None:
        return (rel, finding.literal_name)
    return (rel, f"<dynamic:{finding.scope}>")


def _unjustified_ingest_violations(root: Path = INGEST_ROOT) -> List[str]:
    """Return one human-readable line per env read under ``root`` that is
    either credential-shaped (by literal name) or dynamic (unknown shape,
    which defaults to unsafe) and is not covered by ``_INGEST_ALLOWLIST``."""
    offenders: List[str] = []
    for py_file in _iter_ingest_python_files(root):
        rel = py_file.relative_to(root).as_posix()
        for finding in _scan_ingest_module(py_file):
            if finding.literal_name is not None and not _is_credential_shaped(
                finding.literal_name
            ):
                continue  # a literal, non-credential-shaped name — not a concern
            key = _allowlist_key(rel, finding)
            if key in _INGEST_ALLOWLIST:
                continue
            label = finding.literal_name or f"<dynamic read in {finding.scope}()>"
            offenders.append(
                f"  {rel}:{finding.lineno}: unjustified env read {label!s} — "
                f"add {key!r} to _INGEST_ALLOWLIST with a one-line justification, "
                "or remove the env read"
            )
    return offenders


# ---------------------------------------------------------------------------
# AC2 — the ingest layer is clean today
# ---------------------------------------------------------------------------


def test_no_unjustified_env_reads_in_discovery_ingest():
    """No module under backend/discovery/ingest/ reads a credential-shaped or
    dynamic env var except an explicitly justified, allow-listed call site.

    This is AC2: the guard no longer depends on someone remembering to add a
    new ingest module to a list — it walks the tree itself."""
    offenders = _unjustified_ingest_violations()
    assert not offenders, (
        "Unjustified env read under backend/discovery/ingest/:\n"
        + "\n".join(offenders)
        + "\n\nEvery connector credential must resolve via the vault "
        "(get_connector_credentials / resolve_target_secret), never the "
        "environment. If this is a genuine non-credential value (a feature "
        "flag, a numeric tuning knob, a config-discovery var, or a "
        "documented CLI/standalone instance URL), add it to "
        "_INGEST_ALLOWLIST with a one-line justification."
    )


def test_allowlist_has_no_stale_entries():
    """Every _INGEST_ALLOWLIST entry must correspond to a real env read in the
    current tree.

    A stale entry (the code moved or was deleted) is dead weight that quietly
    widens the guard's blind spot the next time someone reuses that name —
    the fence must track the code, not just grow."""
    live_keys = set()
    for py_file in _iter_ingest_python_files(INGEST_ROOT):
        rel = py_file.relative_to(INGEST_ROOT).as_posix()
        for finding in _scan_ingest_module(py_file):
            live_keys.add(_allowlist_key(rel, finding))

    stale = [f"{path} / {marker}" for path, marker in _INGEST_ALLOWLIST if (path, marker) not in live_keys]
    assert not stale, (
        "Stale _INGEST_ALLOWLIST entries (no matching env read found in the "
        f"tree): {stale}"
    )


def test_ingest_scan_reaches_known_modules():
    """The scan actually reaches real ingest modules, including a nested
    subpackage — otherwise a regression there would go undetected and the
    guard would be a no-op."""
    targets = {p.resolve() for p in _iter_ingest_python_files(INGEST_ROOT)}
    for rel in (
        "salesforce.py",
        "jira.py",
        "operational_config.py",
        "connector_health.py",
        "extraction/pdf.py",
    ):
        assert (INGEST_ROOT / rel).resolve() in targets, (
            f"expected {rel} to be in the ingest scan targets"
        )


# ---------------------------------------------------------------------------
# AC3 — a new module / a new read fails CI without any test edit
# ---------------------------------------------------------------------------


def test_scan_dynamically_discovers_a_new_module(tmp_path):
    """The scan walks the directory tree at test time — it does not enumerate
    a fixed module list — so a brand-new module is discovered and swept
    without touching this test."""
    (tmp_path / "brand_new_ingestor.py").write_text(
        "import os\nTOKEN = os.getenv('BRAND_NEW_CONNECTOR_TOKEN')\n",
        encoding="utf-8",
    )
    offenders = _unjustified_ingest_violations(root=tmp_path)
    assert any("BRAND_NEW_CONNECTOR_TOKEN" in o for o in offenders), (
        "a brand-new module with a literal credential-shaped env read was "
        "not caught — dynamic module discovery is broken"
    )


def test_ac3_new_module_with_unlisted_token_read_fails_without_test_edit(tmp_path):
    """AC3, verbatim: a new ingest module containing os.getenv("X_TOKEN")
    fails the guard — with zero changes to this test file."""
    (tmp_path / "some_future_connector.py").write_text(
        textwrap.dedent(
            """\
            import os

            def get_token():
                return os.getenv("FUTURE_CONNECTOR_TOKEN")
            """
        ),
        encoding="utf-8",
    )
    offenders = _unjustified_ingest_violations(root=tmp_path)
    assert offenders, (
        "a new module reading an unlisted *_TOKEN env var must fail the guard"
    )


def test_ingest_client_secret_read_is_credential_shaped(tmp_path):
    """R191-H1 names client_secret explicitly; ingest reads must be caught."""
    (tmp_path / "future_oauth_ingestor.py").write_text(
        textwrap.dedent(
            """\
            import os

            def get_secret():
                return os.getenv("FUTURE_CONNECTOR_CLIENT_SECRET")
            """
        ),
        encoding="utf-8",
    )
    offenders = _unjustified_ingest_violations(root=tmp_path)
    assert any("FUTURE_CONNECTOR_CLIENT_SECRET" in o for o in offenders), (
        "an ingest module reading *_CLIENT_SECRET from env must fail the guard"
    )


def test_dynamic_env_read_is_caught_like_f1(tmp_path):
    """Reproduces the exact 1.8 F1 shape: a credential resolved via a
    variable-keyed os.environ.get(key), not a literal name — the pattern
    Section A's literal-only matcher missed and that this section closes."""
    (tmp_path / "rogue_operational_config.py").write_text(
        textwrap.dedent(
            """\
            import os

            def resolve_target_secret(credential_ref):
                environ = os.environ
                key = credential_ref.upper() + "_TOKEN"
                return environ.get(key)
            """
        ),
        encoding="utf-8",
    )
    offenders = _unjustified_ingest_violations(root=tmp_path)
    assert any("resolve_target_secret" in o for o in offenders), (
        "a dynamic (variable-keyed) env read was not flagged — this is "
        "exactly the F1 pattern (operational_config.py) the guard must close"
    )


def test_new_connection_url_fallback_in_an_existing_file_is_flagged(tmp_path):
    """A NEW credential-shaped env var in an already-allow-listed file still
    fails — the allow-list is keyed per (file, name), not per file, so
    justifying one variable in a file never blanket-exempts the whole file."""
    (tmp_path / "confluence.py").write_text(
        "import os\nurl = os.getenv('CONFLUENCE_API_KEY')\n",
        encoding="utf-8",
    )
    offenders = _unjustified_ingest_violations(root=tmp_path)
    assert any("CONFLUENCE_API_KEY" in o for o in offenders), (
        "a new credential-shaped var in an already-allow-listed file must "
        "still be flagged — allow-list entries are per (file, name)"
    )


# ---------------------------------------------------------------------------
# Allow-list mechanism sanity
# ---------------------------------------------------------------------------


def test_allowlisted_entry_is_not_flagged(tmp_path):
    """Proves the allow-list mechanism itself works, independent of the real
    codebase's current content (so this test does not rot if those files
    change)."""
    (tmp_path / "legit.py").write_text(
        "import os\nURL = os.getenv('SOME_CONNECTOR_URL')\n", encoding="utf-8"
    )
    key = ("legit.py", "SOME_CONNECTOR_URL")
    _INGEST_ALLOWLIST[key] = "test-only justification"
    try:
        offenders = _unjustified_ingest_violations(root=tmp_path)
    finally:
        del _INGEST_ALLOWLIST[key]
    assert offenders == [], "an allow-listed (file, name) pair must not be flagged"


def test_salesforce_numeric_tuning_helpers_are_justified():
    """The story's own example (feature flags / numeric tuning like
    salesforce.py's dependency-scan controls) is justified today, not
    silently ignored — proves the allow-list, not a scanner gap, is why it
    passes."""
    offenders = _unjustified_ingest_violations()
    assert not any("_env_flag" in o for o in offenders)
    assert not any("_env_float" in o for o in offenders)
