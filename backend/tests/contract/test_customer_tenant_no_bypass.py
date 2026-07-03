"""R17-D2 T6 — Confirm no-bypass enforcement still passes with customer_tenant.

R16-D1 established one hard rule: the model gateway package is the ONLY code
permitted to reference a model-provider endpoint, SDK, or API-key header. Every
model call routes through it, so provider selection, telemetry, credential
handling, and failure behaviour stay consistent and no feature can quietly skip
security or customer-policy rules (R17-D2 §Why / AC7).

R17-D2 adds a third provider — customer_tenant — whose adapter legitimately
contains customer-tenant endpoint logic (the managed in-tenant model URL shape
and the ``api-key`` auth header). That logic is allowed ONLY because it lives
inside the gateway boundary. This suite confirms the no-bypass guarantee still
holds now that the new provider exists:

  AC7-A  The canonical R16-D1 no-bypass scan reports zero violations across the
         whole backend, with the customer_tenant provider present.
  AC7-B  The customer_tenant gateway modules live INSIDE the gateway package and
         are therefore excluded from the scan (the permitted location).
  AC7-C  Those modules genuinely DO contain provider-endpoint literals — so the
         exclusion is meaningful, not vacuous: real endpoint logic exists and is
         confined to the boundary, exactly where it is allowed.
  AC7-D  The gateway package is the SOLE location of any forbidden provider
         literal anywhere under backend/ — no feature leaked a direct call.

This reuses the canonical scanner from ``test_model_gateway_no_bypass`` rather
than redefining the forbidden-pattern list, so there is a single source of truth
and this file never self-trips the scan (it holds no provider literals of its own).
"""
from __future__ import annotations

from pathlib import Path

from app.model_gateway import (
    customer_tenant_config,
    customer_tenant_provider,
    customer_tenant_vault,
)

# Reuse the canonical scanner — single source of truth for the forbidden
# patterns and the collection/scan logic (no duplicated literals here).
from tests.contract.test_model_gateway_no_bypass import (
    BACKEND_ROOT,
    GATEWAY_PACKAGE,
    _THIS_FILE as _ENFORCEMENT_TEST_FILE,
    _collect_scan_targets,
    _iter_python_files,
    _scan_file,
)

# Meta test files that describe the forbidden patterns as prose/strings (not as
# live model calls) and so legitimately hold the literals while living outside
# the gateway: the canonical enforcement test (which defines the patterns) and
# this confinement test. The canonical scanner already excludes its own file via
# _THIS_FILE; the whole-backend sweep in AC7-D excludes both.
_META_TEST_FILES = frozenset(
    {_ENFORCEMENT_TEST_FILE.resolve(), Path(__file__).resolve()}
)

# The customer_tenant adapter modules added by R17-D2 — the only place permitted
# to hold customer-tenant endpoint/credential logic (all inside the gateway).
_CUSTOMER_TENANT_MODULES = (
    customer_tenant_provider,
    customer_tenant_config,
    customer_tenant_vault,
)


def _module_path(module) -> Path:
    return Path(module.__file__).resolve()


# ===========================================================================
# AC7-A — the canonical no-bypass scan still passes with customer_tenant present
# ===========================================================================


def test_ac7_no_bypass_scan_passes_with_customer_tenant():
    """No .py file outside the gateway contains a direct model-provider reference.

    This is the R16-D1 guarantee re-confirmed for R17-D2: adding the
    customer_tenant provider must not have introduced (nor required) any direct
    model call outside backend/app/model_gateway/.
    """
    violations: list[str] = []
    for py_file in _collect_scan_targets():
        for lineno, line, pattern in _scan_file(py_file):
            rel = py_file.relative_to(BACKEND_ROOT)
            violations.append(f"  {rel}:{lineno}: [{pattern!r}]  {line}")

    assert not violations, (
        "Direct model-provider references found outside the gateway with "
        "customer_tenant present. Route all model calls through the gateway.\n\n"
        "Violations:\n" + "\n".join(violations)
    )


# ===========================================================================
# AC7-B — customer_tenant modules live inside the gateway boundary
# ===========================================================================


def test_ac7_customer_tenant_modules_inside_gateway_package():
    """Every customer_tenant adapter module resolves under the gateway package."""
    gateway = GATEWAY_PACKAGE.resolve()
    for module in _CUSTOMER_TENANT_MODULES:
        path = _module_path(module)
        assert gateway in path.parents, (
            f"{path} must live inside the gateway package {gateway} — "
            "customer-tenant endpoint logic is only permitted inside the boundary."
        )


def test_ac7_customer_tenant_modules_excluded_from_scan():
    """The customer_tenant modules are NOT among the scanned (non-gateway) files.

    If they were scanned, their legitimate provider-endpoint literals would fail
    the no-bypass test — defeating the gateway package's role as the single
    permitted location for provider-specific logic.
    """
    scan_targets = set(_collect_scan_targets())
    for module in _CUSTOMER_TENANT_MODULES:
        path = _module_path(module)
        assert path not in scan_targets, (
            f"{path} must be excluded from the no-bypass scan (it is inside the "
            "gateway boundary)."
        )


# ===========================================================================
# AC7-C — the exclusion is meaningful: the modules really do hold endpoint logic
# ===========================================================================


def test_ac7_customer_tenant_endpoint_logic_would_trip_scan_if_not_confined():
    """The provider/config modules contain provider-endpoint literals.

    This proves the boundary exclusion is NOT vacuous: the customer_tenant
    adapter genuinely holds the managed in-tenant endpoint shape and auth
    details that the scan forbids everywhere else. Because these files live
    inside the gateway (AC7-B), that logic is confined to exactly where it is
    allowed — anywhere else it would fail the scan.
    """
    for module in (customer_tenant_provider, customer_tenant_config):
        path = _module_path(module)
        assert _scan_file(path), (
            f"expected {path.name} to contain provider-endpoint literals that the "
            "no-bypass scan forbids outside the gateway — if it does not, this "
            "confinement test no longer proves anything and must be revisited."
        )


# ===========================================================================
# AC7-D — the gateway is the SOLE location of any forbidden provider literal
# ===========================================================================


def test_ac7_gateway_is_only_location_of_forbidden_literals():
    """Any backend file containing a forbidden provider literal is in the gateway.

    Scans the ENTIRE backend (gateway included) and asserts every file that
    holds a forbidden literal lives under the gateway package. This is the
    positive form of the no-bypass guarantee: not merely "nothing leaked
    outside", but "the gateway is the one and only home for provider-endpoint
    literals" — the invariant the customer_tenant provider must preserve.

    The enforcement test module (test_model_gateway_no_bypass.py) defines the
    patterns via concatenation, so it holds no literal and is not flagged; this
    file likewise reuses the scanner and holds no literal of its own.
    """
    gateway = GATEWAY_PACKAGE.resolve()
    offenders: list[str] = []
    for py_file in _iter_python_files(BACKEND_ROOT):
        if not _scan_file(py_file):
            continue
        path = py_file.resolve()
        if gateway in path.parents:
            continue  # permitted — inside the boundary
        if path in _META_TEST_FILES:
            continue  # patterns appear only as documentation/strings, not calls
        offenders.append(str(path.relative_to(BACKEND_ROOT)))

    assert not offenders, (
        "Forbidden provider literals found OUTSIDE the gateway package — the "
        "gateway must be the sole location of provider-endpoint logic:\n  "
        + "\n  ".join(offenders)
    )
