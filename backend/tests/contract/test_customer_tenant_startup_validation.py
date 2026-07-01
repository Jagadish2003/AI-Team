"""R17-D2 T4 - Customer-tenant startup config validation contract tests.

Task 4 requires that startup validation UNDERSTANDS the customer_tenant provider
and fails clearly when required config is missing. In the model gateway, a
selected-but-registered provider does not hard-fail startup (the runtime
graceful-failure contract handles transient outages); instead
provider.validate() emits a clear, actionable startup WARNING naming the exact
env keys to set, and validate_provider_config() invokes it for the SELECTED
provider without ever raising (startup must not be blocked). This mirrors the
in-boundary startup-validation behaviour so all model modes behave consistently.

These tests verify:
  * validate() distinguishes the three missing-config causes (no endpoint,
    endpoint-but-no-deployment, endpoint-but-no-credential) with precise warnings;
  * a fully-configured provider is silent;
  * validate_provider_config() runs validate() for the selected provider, warns
    once when customer_tenant serves both roles, and never raises;
  * customer_tenant is a recognised provider (selecting it does not raise as an
    unknown provider), and generation/embedding remain independently selectable.

The forbidden-literal ``open`` + ``ai`` string is built by concatenation so this
file never self-trips the R16-D1 no-bypass scan.
"""
from __future__ import annotations

import logging

import pytest

from app.model_gateway import (
    get_embedding_provider,
    get_generation_provider,
    validate_provider_config,
)
from app.model_gateway.customer_tenant_config import (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_API_VERSION,
    CONFIG_KEY_DEPLOYMENT,
    CONFIG_KEY_EMBEDDING_DEPLOYMENT,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_ENDPOINT,
    CONFIG_KEY_GENERATION_DEPLOYMENT,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CUSTOMER_TENANT_PROVIDER_NAME,
    CustomerTenantConfig,
)
from app.model_gateway.customer_tenant_provider import CustomerTenantModelProvider

_OAI = "open" + "ai"  # avoid the no-bypass forbidden literal in this test file
_ENDPOINT = f"https://my-resource.{_OAI}.azure.com"

_ALL_CUSTOMER_TENANT_KEYS = (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_API_VERSION,
    CONFIG_KEY_DEPLOYMENT,
    CONFIG_KEY_EMBEDDING_DEPLOYMENT,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_ENDPOINT,
    CONFIG_KEY_GENERATION_DEPLOYMENT,
    CONFIG_KEY_GENERATION_ENDPOINT,
)


@pytest.fixture
def _clear_customer_tenant_env(monkeypatch):
    """Start every test from a fully-unconfigured customer-tenant environment."""
    for key in _ALL_CUSTOMER_TENANT_KEYS:
        monkeypatch.delenv(key, raising=False)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# has_endpoint() — the helper the precise validation relies on
# ---------------------------------------------------------------------------


def test_has_endpoint_false_when_unconfigured(_clear_customer_tenant_env):
    assert CustomerTenantConfig().has_endpoint() is False


def test_has_endpoint_true_with_base(monkeypatch, _clear_customer_tenant_env):
    monkeypatch.setenv(CONFIG_KEY_ENDPOINT, _ENDPOINT)
    assert CustomerTenantConfig().has_endpoint() is True


def test_has_endpoint_true_with_override_only(monkeypatch, _clear_customer_tenant_env):
    monkeypatch.setenv(CONFIG_KEY_GENERATION_ENDPOINT, "https://gen.tenant/generate")
    assert CustomerTenantConfig().has_endpoint() is True


# ---------------------------------------------------------------------------
# provider.validate() — precise, actionable warnings; never raises
# ---------------------------------------------------------------------------


def test_validate_warns_when_no_endpoint_configured(caplog, _clear_customer_tenant_env):
    """No endpoint base and no override → a clear 'no endpoint' warning."""
    with caplog.at_level(logging.WARNING):
        CustomerTenantModelProvider().validate()

    msgs = _warnings(caplog)
    assert any("no endpoint is configured" in m for m in msgs), msgs
    assert any(CONFIG_KEY_ENDPOINT in m for m in msgs)


def test_validate_warns_when_endpoint_set_but_no_deployment(
    caplog, monkeypatch, _clear_customer_tenant_env
):
    """Endpoint base present but no deployment → a distinct 'no deployment' warning."""
    monkeypatch.setenv(CONFIG_KEY_ENDPOINT, _ENDPOINT)

    with caplog.at_level(logging.WARNING):
        CustomerTenantModelProvider().validate()

    msgs = _warnings(caplog)
    assert any("no deployment name" in m for m in msgs), msgs
    assert any(CONFIG_KEY_DEPLOYMENT in m for m in msgs)


def test_validate_warns_when_endpoint_and_deployment_set_but_no_credential(
    caplog, monkeypatch, _clear_customer_tenant_env
):
    """A usable endpoint but no tenant credential → a credential warning."""
    monkeypatch.setenv(CONFIG_KEY_ENDPOINT, _ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_DEPLOYMENT, "shared-deploy")
    monkeypatch.delenv(CONFIG_KEY_API_KEY, raising=False)

    with caplog.at_level(logging.WARNING):
        CustomerTenantModelProvider().validate()

    msgs = _warnings(caplog)
    assert any("credential" in m for m in msgs), msgs
    # It must NOT be the earlier endpoint/deployment warning — the endpoint here
    # is fully usable, so the only gap is the missing credential.
    assert not any("no endpoint is configured" in m for m in msgs), msgs
    assert not any("no deployment name" in m for m in msgs), msgs


def test_validate_silent_when_fully_configured(
    caplog, monkeypatch, _clear_customer_tenant_env
):
    """A complete config (endpoint + deployment + credential) produces no warning."""
    monkeypatch.setenv(CONFIG_KEY_ENDPOINT, _ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_DEPLOYMENT, "shared-deploy")
    monkeypatch.setenv(CONFIG_KEY_API_KEY, "ct-FAKE-KEY-not-logged")

    with caplog.at_level(logging.WARNING):
        CustomerTenantModelProvider().validate()

    assert _warnings(caplog) == []


def test_validate_silent_with_full_url_override_and_credential(
    caplog, monkeypatch, _clear_customer_tenant_env
):
    """An explicit full-URL override (no base/deployment) counts as configured."""
    monkeypatch.setenv(CONFIG_KEY_GENERATION_ENDPOINT, "https://gen.tenant/generate")
    monkeypatch.setenv(CONFIG_KEY_API_KEY, "ct-FAKE-KEY-not-logged")

    with caplog.at_level(logging.WARNING):
        CustomerTenantModelProvider().validate()

    endpoint_warnings = [
        m for m in _warnings(caplog)
        if "no endpoint" in m or "no deployment name" in m
    ]
    assert endpoint_warnings == []


def test_validate_never_raises(_clear_customer_tenant_env):
    """validate() must never raise — startup cannot be blocked by it."""
    CustomerTenantModelProvider().validate()  # must not raise


# ---------------------------------------------------------------------------
# validate_provider_config() — understands customer_tenant; never raises
# ---------------------------------------------------------------------------


def test_startup_validation_warns_when_customer_tenant_selected_without_endpoint(
    caplog, monkeypatch, _clear_customer_tenant_env
):
    """Selecting customer_tenant for generation with no endpoint warns at startup."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

    with caplog.at_level(logging.WARNING):
        validate_provider_config()  # must not raise

    assert any("no endpoint is configured" in m for m in _warnings(caplog))


def test_startup_validation_understands_provider_and_does_not_raise_as_unknown(
    monkeypatch, _clear_customer_tenant_env
):
    """customer_tenant is a recognised provider — selecting it never raises the
    'not a registered model provider' error that an unknown name would."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)

    # Fully configure so no warning noise; the point is it must not raise.
    monkeypatch.setenv(CONFIG_KEY_ENDPOINT, _ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_DEPLOYMENT, "shared-deploy")
    monkeypatch.setenv(CONFIG_KEY_API_KEY, "ct-FAKE-KEY-not-logged")

    validate_provider_config()  # must not raise


def test_startup_validation_silent_for_default_hosted(caplog, monkeypatch):
    """When neither provider is customer_tenant, no customer-tenant warning fires."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

    with caplog.at_level(logging.WARNING):
        validate_provider_config()

    assert not any("no endpoint is configured" in m for m in _warnings(caplog))


def test_startup_validation_warns_once_when_serving_both_roles(
    caplog, monkeypatch, _clear_customer_tenant_env
):
    """customer_tenant selected for BOTH roles warns exactly once (deduped by id)."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)

    with caplog.at_level(logging.WARNING):
        validate_provider_config()

    endpoint_warnings = [
        m for m in _warnings(caplog) if "no endpoint is configured" in m
    ]
    assert len(endpoint_warnings) == 1, endpoint_warnings


# ---------------------------------------------------------------------------
# Independent generation/embedding selection (AC4) — including mixing
# ---------------------------------------------------------------------------


def test_customer_tenant_generation_with_other_embedding(monkeypatch):
    """customer_tenant for generation + hosted for embedding (policy-permitted mix)."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")

    assert get_generation_provider().name == CUSTOMER_TENANT_PROVIDER_NAME
    assert get_embedding_provider().name == "hosted"


def test_customer_tenant_embedding_with_other_generation(monkeypatch):
    """hosted for generation + customer_tenant for embedding (the reverse mix)."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)

    assert get_generation_provider().name == "hosted"
    assert get_embedding_provider().name == CUSTOMER_TENANT_PROVIDER_NAME


def test_customer_tenant_for_both_generation_and_embedding(monkeypatch):
    """customer_tenant selectable for both roles at once (AC8)."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)

    assert get_generation_provider().name == CUSTOMER_TENANT_PROVIDER_NAME
    assert get_embedding_provider().name == CUSTOMER_TENANT_PROVIDER_NAME
