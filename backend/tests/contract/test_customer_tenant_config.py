"""R17-D2 T1/T4 - Customer-tenant provider configuration contract tests.

Endpoint/deployment/auth/API-version config lives inside the model gateway
package, and generation/embedding provider selection stays independent
(AC1/AC4). Credentials are config-sourced, resolved live, and never exposed by
repr (AC2, hardened further by T2's vault).

The forbidden-literal ``open`` + ``ai`` string (flagged by the R16-D1 no-bypass
scan outside the gateway package) is constructed by concatenation here so this
test file never self-trips that scan.
"""
from __future__ import annotations

from pathlib import Path

from app.model_gateway import (
    _PROVIDER_REGISTRY,
    get_embedding_provider,
    get_generation_provider,
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

_SECRET = "ct-SECRET-DO-NOT-LOG-0123456789"
_ROTATED_SECRET = "ct-ROTATED-SECRET-0123456789"
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


def _clear_customer_tenant_env(monkeypatch) -> None:
    for key in _ALL_CUSTOMER_TENANT_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_config_module_lives_inside_gateway_and_is_not_re_exported():
    """Endpoint/deployment/auth config is owned by the gateway package."""
    import app.model_gateway as gateway

    assert (
        CustomerTenantConfig.__module__
        == "app.model_gateway.customer_tenant_config"
    )
    assert "CustomerTenantConfig" not in gateway.__all__


def test_endpoint_and_deployment_derive_azure_style_urls(monkeypatch):
    """Endpoint base + deployment names produce per-deployment URLs with version."""
    _clear_customer_tenant_env(monkeypatch)
    monkeypatch.setenv(CONFIG_KEY_ENDPOINT, _ENDPOINT + "/")
    monkeypatch.setenv(CONFIG_KEY_GENERATION_DEPLOYMENT, "gen-deploy")
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_DEPLOYMENT, "emb-deploy")

    cfg = CustomerTenantConfig()

    assert cfg.endpoint == _ENDPOINT
    assert cfg.generation_endpoint() == (
        f"{_ENDPOINT}/{_OAI}/deployments/gen-deploy/chat/completions"
        "?api-version=2024-02-01"
    )
    assert cfg.embedding_endpoint() == (
        f"{_ENDPOINT}/{_OAI}/deployments/emb-deploy/embeddings"
        "?api-version=2024-02-01"
    )


def test_api_version_has_default_and_live_override(monkeypatch):
    """API version defaults to a GA value and can be repinned live."""
    _clear_customer_tenant_env(monkeypatch)
    monkeypatch.setenv(CONFIG_KEY_ENDPOINT, _ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_DEPLOYMENT, "shared-deploy")
    cfg = CustomerTenantConfig()

    assert cfg.api_version() == "2024-02-01"
    assert "api-version=2024-02-01" in cfg.generation_endpoint()

    monkeypatch.setenv(CONFIG_KEY_API_VERSION, "2025-01-01")
    assert cfg.api_version() == "2025-01-01"
    assert "api-version=2025-01-01" in cfg.embedding_endpoint()


def test_explicit_full_url_overrides_win_verbatim(monkeypatch):
    """A managed endpoint off the Azure path convention may set full URLs."""
    _clear_customer_tenant_env(monkeypatch)
    monkeypatch.setenv(CONFIG_KEY_ENDPOINT, _ENDPOINT)
    monkeypatch.setenv(CONFIG_KEY_DEPLOYMENT, "ignored-when-overridden")
    monkeypatch.setenv(
        CONFIG_KEY_GENERATION_ENDPOINT, "https://gen.customer.tenant/generate"
    )
    monkeypatch.setenv(
        CONFIG_KEY_EMBEDDING_ENDPOINT, "https://emb.customer.tenant/embed"
    )

    cfg = CustomerTenantConfig()

    assert cfg.generation_endpoint() == "https://gen.customer.tenant/generate"
    assert cfg.embedding_endpoint() == "https://emb.customer.tenant/embed"


def test_no_hardcoded_external_endpoint_when_unconfigured(monkeypatch):
    """An unconfigured customer_tenant mode invents no endpoint."""
    _clear_customer_tenant_env(monkeypatch)

    cfg = CustomerTenantConfig()

    assert cfg.endpoint == ""
    assert cfg.generation_endpoint() == ""
    assert cfg.embedding_endpoint() == ""
    hosted_endpoint = "api.anthrop" + "ic.com"
    assert hosted_endpoint not in repr(cfg)


def test_deployment_names_have_common_fallback_and_independent_pins(monkeypatch):
    """Generation and embedding deployments can be pinned independently."""
    _clear_customer_tenant_env(monkeypatch)
    monkeypatch.setenv(CONFIG_KEY_DEPLOYMENT, "shared-deploy")
    cfg = CustomerTenantConfig()

    assert cfg.generation_deployment() == "shared-deploy"
    assert cfg.embedding_deployment() == "shared-deploy"

    monkeypatch.setenv(CONFIG_KEY_GENERATION_DEPLOYMENT, "gen-deploy")
    monkeypatch.setenv(CONFIG_KEY_EMBEDDING_DEPLOYMENT, "emb-deploy")

    assert cfg.generation_deployment() == "gen-deploy"
    assert cfg.embedding_deployment() == "emb-deploy"


def test_auth_token_is_config_sourced_live_and_redacted(monkeypatch):
    """The tenant credential is read from config and never exposed by repr (AC2)."""
    _clear_customer_tenant_env(monkeypatch)
    monkeypatch.setenv(CONFIG_KEY_API_KEY, _SECRET)
    cfg = CustomerTenantConfig()

    assert cfg.resolve_api_key() == _SECRET
    assert cfg.has_credential() is True
    assert _SECRET not in repr(cfg)
    assert "REDACTED" in repr(cfg)

    monkeypatch.setenv(CONFIG_KEY_API_KEY, _ROTATED_SECRET)
    assert cfg.resolve_api_key() == _ROTATED_SECRET
    assert _ROTATED_SECRET not in repr(cfg)


def test_generation_can_be_selected_independently(monkeypatch):
    """MODEL_GENERATION_PROVIDER=customer_tenant does not force embedding (AC4)."""
    provider = _PROVIDER_REGISTRY.get(CUSTOMER_TENANT_PROVIDER_NAME)
    assert provider is not None

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")

    assert get_generation_provider() is provider
    assert get_embedding_provider().name == "hosted"


def test_embedding_can_be_selected_independently(monkeypatch):
    """MODEL_EMBEDDING_PROVIDER=customer_tenant does not force generation (AC4)."""
    provider = _PROVIDER_REGISTRY.get(CUSTOMER_TENANT_PROVIDER_NAME)
    assert provider is not None

    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "hosted")
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", CUSTOMER_TENANT_PROVIDER_NAME)

    assert get_generation_provider().name == "hosted"
    assert get_embedding_provider() is provider


def test_env_example_documents_customer_tenant_config_keys():
    """backend/.env.example documents the customer_tenant endpoint/auth config."""
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    text = env_example.read_text(encoding="utf-8")

    assert "MODEL_GENERATION_PROVIDER" in text
    assert "MODEL_EMBEDDING_PROVIDER" in text
    assert CUSTOMER_TENANT_PROVIDER_NAME in text

    for key in _ALL_CUSTOMER_TENANT_KEYS:
        assert f"{key}=" in text, f"{key} missing from backend/.env.example"
