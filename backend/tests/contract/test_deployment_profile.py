"""HP-2 T1 — the DEPLOYMENT_PROFILE deployment profile.

HP-2's story item 1 ("Deployment profile drives the default") assumes a profile
concept exists. Before T1 it did not: ``app/deployment_profile.py`` answered only
"is this process production". T1 adds the orthogonal question — *who runs this
deployment* — as ``DEPLOYMENT_PROFILE = 'saas' | 'customer_hosted'``.

These tests cover:

  * the reader — unset/blank default, both explicit values, case/whitespace
    tolerance, and the refusal of an unrecognised value;
  * the deliberate DIFFERENCE from :mod:`app.network_profile` (which degrades an
    unknown value): there is no safe default to degrade to here, so a typo like
    ``on_prem`` raises rather than silently reinstating the cloud-calling default
    HP-2 exists to remove;
  * orthogonality with :func:`is_production` across all four combinations —
    the distinction T1 exists to record, and the one a later engineer is most
    likely to collapse;
  * a STRUCTURAL guard: no module outside ``app/deployment_profile.py`` reads
    ``DEPLOYMENT_PROFILE`` from the environment, so HP-2.2 / HP-2.3 / HP-2.5
    cannot each grow their own copy of the comparison. The guard is proven to
    fail (negative control), because a guard never observed failing is not known
    to be a guard.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app import deployment_profile
from app.deployment_profile import (
    DEFAULT_DEPLOYMENT_PROFILE,
    DEPLOYMENT_PROFILE_CUSTOMER_HOSTED,
    DEPLOYMENT_PROFILE_SAAS,
    ENV_DEPLOYMENT_PROFILE,
    VALID_DEPLOYMENT_PROFILES,
    InvalidDeploymentProfile,
    get_deployment_profile,
    is_customer_hosted,
    is_production,
    is_saas,
    validate_deployment_profile,
)

# Resolves to backend/
BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# The reader — default, explicit values, tolerance
# ---------------------------------------------------------------------------


def test_unset_defaults_to_saas(monkeypatch):
    """Backward compatible: every deployment predating HP-2 is SaaS (AC2 basis)."""
    monkeypatch.delenv(ENV_DEPLOYMENT_PROFILE, raising=False)
    assert get_deployment_profile() == DEPLOYMENT_PROFILE_SAAS
    assert is_saas() is True
    assert is_customer_hosted() is False


def test_blank_is_treated_as_unset(monkeypatch):
    """A blank value is 'not configured', not 'configured wrongly'."""
    for blank in ("", "   ", "\t", "\n"):
        monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, blank)
        assert get_deployment_profile() == DEFAULT_DEPLOYMENT_PROFILE
        assert is_customer_hosted() is False


def test_saas_explicitly(monkeypatch):
    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, "saas")
    assert get_deployment_profile() == DEPLOYMENT_PROFILE_SAAS
    assert is_saas() is True
    assert is_customer_hosted() is False


def test_customer_hosted_explicitly(monkeypatch):
    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, "customer_hosted")
    assert get_deployment_profile() == DEPLOYMENT_PROFILE_CUSTOMER_HOSTED
    assert is_customer_hosted() is True
    assert is_saas() is False


def test_case_and_whitespace_tolerant(monkeypatch):
    """A hand-edited .env is an obvious intent, not an ambiguous one."""
    for raw in ("  Customer_Hosted  ", "CUSTOMER_HOSTED", "customer_hosted\n"):
        monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, raw)
        assert get_deployment_profile() == DEPLOYMENT_PROFILE_CUSTOMER_HOSTED
    for raw in (" SaaS ", "SAAS"):
        monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, raw)
        assert get_deployment_profile() == DEPLOYMENT_PROFILE_SAAS


def test_profile_is_read_live_not_cached(monkeypatch):
    """An operator or a test can flip it without reloading the module."""
    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, "saas")
    assert get_deployment_profile() == DEPLOYMENT_PROFILE_SAAS
    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, "customer_hosted")
    assert get_deployment_profile() == DEPLOYMENT_PROFILE_CUSTOMER_HOSTED


# ---------------------------------------------------------------------------
# The refusal — the deliberate difference from network_profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "on_prem",          # the term the HP-2 pack itself uses in prose
        "on-prem",
        "onprem",
        "customer-hosted",  # hyphen instead of underscore — the likeliest typo
        "customerhosted",
        "self_hosted",
        "cloud",
        "production",       # the is_production() answer, in the wrong variable
        "bogus",
    ],
)
def test_unrecognised_value_raises(monkeypatch, bad):
    """No safe default exists, so an unrecognised value is refused, never guessed.

    Degrading to 'saas' would hand a customer-hosted deployment the cloud-calling
    default HP-2 removes — the exact defect, silently reinstated by a typo.
    """
    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, bad)
    with pytest.raises(InvalidDeploymentProfile):
        get_deployment_profile()


def test_refusal_message_names_the_variable_and_every_valid_value(monkeypatch):
    """The fix must not require reading the source."""
    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, "on_prem")
    with pytest.raises(InvalidDeploymentProfile) as exc:
        get_deployment_profile()
    message = str(exc.value)
    assert ENV_DEPLOYMENT_PROFILE in message
    assert "on_prem" in message
    for valid in VALID_DEPLOYMENT_PROFILES:
        assert valid in message


def test_invalid_profile_is_a_value_error():
    """A ValueError subclass, so generic config-error handling keeps working."""
    assert issubclass(InvalidDeploymentProfile, ValueError)


def test_convenience_helpers_do_not_swallow_the_refusal(monkeypatch):
    """is_customer_hosted() must never report False for a MISCONFIGURED profile.

    Swallowing the error into False is the silent reinstating of the cloud
    default this module refuses — so both helpers propagate.
    """
    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, "on_prem")
    with pytest.raises(InvalidDeploymentProfile):
        is_customer_hosted()
    with pytest.raises(InvalidDeploymentProfile):
        is_saas()


def test_vocabulary_is_closed():
    assert VALID_DEPLOYMENT_PROFILES == (
        DEPLOYMENT_PROFILE_SAAS,
        DEPLOYMENT_PROFILE_CUSTOMER_HOSTED,
    )
    assert DEFAULT_DEPLOYMENT_PROFILE == DEPLOYMENT_PROFILE_SAAS


def test_posture_differs_from_network_profile_on_purpose(monkeypatch):
    """Pin the asymmetry so nobody 'harmonises' the two readers.

    NETWORK_PROFILE degrades an unknown value toward the FULL experience (safe).
    DEPLOYMENT_PROFILE has no safe default, so it refuses. Both are correct; the
    safe direction is simply opposite.
    """
    from app import network_profile

    monkeypatch.setenv("NETWORK_PROFILE", "bogus")
    assert network_profile.get_network_profile() == "standard"  # degrades

    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, "bogus")
    with pytest.raises(InvalidDeploymentProfile):  # refuses
        get_deployment_profile()


# ---------------------------------------------------------------------------
# Orthogonality with is_production() — the distinction T1 exists to record
# ---------------------------------------------------------------------------


def test_is_production_is_unchanged_by_hp2(monkeypatch):
    """R1.9.1-H1 T4 behaviour preserved exactly: ENVIRONMENT=production only."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
    assert is_production() is False

    monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
    assert is_production() is False

    monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert is_production() is True

    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert is_production() is False


@pytest.mark.parametrize(
    "profile,environment,expect_customer_hosted,expect_production",
    [
        ("saas", "production", False, True),
        ("saas", "staging", False, False),
        # The case that proves they are orthogonal: a customer-hosted STAGING box
        # is customer_hosted (its boundary is real) and NOT production.
        ("customer_hosted", "staging", True, False),
        ("customer_hosted", "production", True, True),
    ],
)
def test_profile_and_production_are_independent(
    monkeypatch, profile, environment, expect_customer_hosted, expect_production
):
    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, profile)
    monkeypatch.setenv("ENVIRONMENT", environment)
    assert is_customer_hosted() is expect_customer_hosted
    assert is_production() is expect_production


def test_neither_function_consults_the_other_variable(monkeypatch):
    """ENVIRONMENT must not move the profile, and vice versa."""
    monkeypatch.delenv(ENV_DEPLOYMENT_PROFILE, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert get_deployment_profile() == DEPLOYMENT_PROFILE_SAAS

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, "customer_hosted")
    assert is_production() is False


def test_is_production_code_does_not_read_the_profile():
    """Structural: the two readers must not become entangled in CODE either.

    The docstring legitimately mentions the profile (recording the orthogonality
    is the point of T1), so the docstring is stripped and only executable code is
    inspected — otherwise the test would forbid documenting the distinction.
    """
    module = ast.parse(_PERMITTED_READER.read_text(encoding="utf-8"))
    func = next(
        n
        for n in ast.walk(module)
        if isinstance(n, ast.FunctionDef) and n.name == "is_production"
    )
    body = list(func.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # drop the docstring
    code = "\n".join(ast.unparse(stmt) for stmt in body)
    assert ENV_DEPLOYMENT_PROFILE not in code
    assert "get_deployment_profile" not in code
    assert "ENVIRONMENT" in code  # non-vacuous: it does read its own variable


def test_module_docstring_records_the_orthogonality():
    """T1 requires the distinction be recorded for the next engineer."""
    doc = deployment_profile.__doc__ or ""
    assert "orthogonal" in doc.lower()
    assert "is_production" in doc
    assert DEPLOYMENT_PROFILE_CUSTOMER_HOSTED in doc


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


def test_validate_returns_the_resolved_profile(monkeypatch):
    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, "customer_hosted")
    assert validate_deployment_profile() == DEPLOYMENT_PROFILE_CUSTOMER_HOSTED

    monkeypatch.delenv(ENV_DEPLOYMENT_PROFILE, raising=False)
    assert validate_deployment_profile() == DEPLOYMENT_PROFILE_SAAS


def test_validate_raises_on_an_unrecognised_value(monkeypatch):
    monkeypatch.setenv(ENV_DEPLOYMENT_PROFILE, "on_prem")
    with pytest.raises(InvalidDeploymentProfile):
        validate_deployment_profile()


def test_lifespan_validates_the_profile_at_startup():
    """The refusal must reach BOOT, not the first request that asks.

    Read from the source of app.main.lifespan rather than by booting the app, so
    the assertion is about the wiring and not about whatever else startup needs.
    """
    from app import main as main_module

    source = inspect.getsource(main_module.lifespan)
    assert "validate_deployment_profile" in source


# ---------------------------------------------------------------------------
# Structural guard — one reader, enforced
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset(
    {".venv", "node_modules", "__pycache__", ".git", "build", "dist", "tests"}
)

#: The ONLY production module permitted to read DEPLOYMENT_PROFILE from the env.
_PERMITTED_READER = BACKEND_ROOT / "app" / "deployment_profile.py"


def _iter_production_python_files():
    for path in BACKEND_ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def _string_constants(tree: ast.AST) -> dict:
    """Map every ``NAME = "literal"`` assignment in the module to its value.

    Needed because a literal-only scan is evadable by exactly one line:

        _PROFILE_VAR = "DEPLOYMENT_PROFILE"
        os.getenv(_PROFILE_VAR)

    That is the same blind spot recorded as the F1 finding in
    ``test_no_env_credential_reads.py`` (and the reason HP-7 T3 exists) — this
    module's own reader is written that way, so the guard would otherwise not
    even see its permitted reader, and would silently pass on a copy of it.
    """
    constants: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str) and isinstance(node.target, ast.Name):
                constants[node.target.id] = node.value.value
    return constants


def _resolve_key(node: ast.AST, constants: dict):
    """Resolve an env-read key to its string value, literal or constant-named."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        # A local constant, or the exported name imported from this module.
        if node.id == "ENV_DEPLOYMENT_PROFILE":
            return ENV_DEPLOYMENT_PROFILE
        return constants.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr == "ENV_DEPLOYMENT_PROFILE":
        # deployment_profile.ENV_DEPLOYMENT_PROFILE
        return ENV_DEPLOYMENT_PROFILE
    return None


def _is_env_read(node: ast.Call) -> bool:
    """True for os.getenv(...) / os.environ.get(...) / environ.get(...)."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr == "getenv":
            return True
        if func.attr == "get" and isinstance(func.value, ast.Attribute):
            return func.value.attr == "environ"
        if func.attr == "get" and isinstance(func.value, ast.Name):
            return func.value.id == "environ"
    if isinstance(func, ast.Name) and func.id == "getenv":
        return True
    return False


def _reads_deployment_profile_env(tree: ast.AST) -> bool:
    """True if the module reads DEPLOYMENT_PROFILE from the environment.

    Resolves constant-named keys, not just literals — see :func:`_string_constants`.
    """
    constants = _string_constants(tree)
    for node in ast.walk(tree):
        # os.getenv(X) / os.environ.get(X)
        if isinstance(node, ast.Call) and _is_env_read(node):
            for arg in node.args:
                if _resolve_key(arg, constants) == ENV_DEPLOYMENT_PROFILE:
                    return True
        # os.environ[X]
        if isinstance(node, ast.Subscript):
            base = node.value
            is_environ = (
                isinstance(base, ast.Attribute) and base.attr == "environ"
            ) or (isinstance(base, ast.Name) and base.id == "environ")
            if is_environ and _resolve_key(node.slice, constants) == ENV_DEPLOYMENT_PROFILE:
                return True
    return False


def _offending_files():
    offenders = []
    for path in _iter_production_python_files():
        if path.resolve() == _PERMITTED_READER.resolve():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        if _reads_deployment_profile_env(tree):
            offenders.append(path.relative_to(BACKEND_ROOT).as_posix())
    return offenders


def test_only_deployment_profile_module_reads_the_env_var():
    """HP-2.2/2.3/2.5 must consume the helper, never re-derive the comparison.

    The bug HP-1 exists to fix is eight hand-copied org guards, one of which was
    wrong. This fence stops the same shape forming around the deployment profile
    before the three consuming subtasks are written.
    """
    offenders = _offending_files()
    assert offenders == [], (
        "These modules read DEPLOYMENT_PROFILE from the environment directly. "
        "Use app.deployment_profile.get_deployment_profile() / "
        f"is_customer_hosted() instead: {offenders}"
    )


def test_the_permitted_reader_actually_reads_it():
    """Guard against a vacuous pass — the scanner must be able to see a read."""
    tree = ast.parse(_PERMITTED_READER.read_text(encoding="utf-8"))
    assert _reads_deployment_profile_env(tree) is True


def test_guard_goes_red_when_another_module_reads_the_var(tmp_path):
    """Negative control: a guard never observed failing is not known to be a guard."""
    offending = tmp_path / "some_new_route.py"
    offending.write_text(
        "import os\n"
        "def choose():\n"
        f"    return os.getenv({ENV_DEPLOYMENT_PROFILE!r}, 'saas')\n",
        encoding="utf-8",
    )
    tree = ast.parse(offending.read_text(encoding="utf-8"))
    assert _reads_deployment_profile_env(tree) is True


def test_guard_catches_a_constant_named_read(tmp_path):
    """The F1 evasion: a literal-only scanner walks straight over this.

    ``deployment_profile.py``'s own reader is written this way, so a module that
    copies it would evade a literal-only guard entirely.
    """
    evasive = tmp_path / "evasive.py"
    evasive.write_text(
        "import os\n"
        '_PROFILE_VAR = "DEPLOYMENT_PROFILE"\n'
        "def choose():\n"
        "    return os.environ.get(_PROFILE_VAR, 'saas')\n",
        encoding="utf-8",
    )
    tree = ast.parse(evasive.read_text(encoding="utf-8"))
    assert _reads_deployment_profile_env(tree) is True


def test_guard_catches_an_imported_constant_read(tmp_path):
    """Importing the exported constant is not a loophole either."""
    importer = tmp_path / "importer.py"
    importer.write_text(
        "import os\n"
        "from app.deployment_profile import ENV_DEPLOYMENT_PROFILE\n"
        "def choose():\n"
        "    return os.environ[ENV_DEPLOYMENT_PROFILE]\n",
        encoding="utf-8",
    )
    tree = ast.parse(importer.read_text(encoding="utf-8"))
    assert _reads_deployment_profile_env(tree) is True


def test_guard_ignores_unrelated_env_reads(tmp_path):
    """The scanner must not flag every env read — only this variable."""
    innocent = tmp_path / "innocent.py"
    innocent.write_text(
        "import os\n"
        "def choose():\n"
        "    return os.getenv('ENVIRONMENT', '') or os.environ['NETWORK_PROFILE']\n",
        encoding="utf-8",
    )
    tree = ast.parse(innocent.read_text(encoding="utf-8"))
    assert _reads_deployment_profile_env(tree) is False


def test_scan_reaches_the_modules_hp2_will_touch():
    """Non-vacuity: the sweep must actually cover the future consumers."""
    scanned = {p.relative_to(BACKEND_ROOT).as_posix() for p in _iter_production_python_files()}
    for expected in (
        "app/main.py",
        "app/model_gateway/__init__.py",
        "app/network_profile.py",
        "app/deployment_profile.py",
    ):
        assert expected in scanned, f"{expected} was not scanned"
