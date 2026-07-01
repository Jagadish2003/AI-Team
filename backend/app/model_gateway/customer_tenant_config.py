"""R17-D2 T1/T4 - Customer-tenant model provider configuration.

This module owns the endpoint, deployment-name, auth, and API-version
configuration for the ``customer_tenant`` provider. Like the hosted and
in-boundary config modules it lives INSIDE the model gateway package so callers
stay unaware of the customer's private model endpoint and credentials.

Customer-tenant mode targets a *managed* model service the customer operates
inside their own cloud tenant — the canonical example is Azure OpenAI
provisioned in the customer's subscription. It differs from in-boundary mode
(R17-D1) in what the customer operates: in-boundary is a model the customer runs
themselves (often open-weights on their own serving stack); customer-tenant is a
managed model in the customer's cloud. Both keep data within the customer's
control (R17-D2 §Why-it-matters / §7).

Azure OpenAI endpoint shape
---------------------------
A managed Azure OpenAI deployment is addressed per *deployment name* rather than
per model name, with an ``api-version`` query parameter::

    {endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...
    {endpoint}/openai/deployments/{deployment}/embeddings?api-version=...

so the config resolves an endpoint base + a deployment name + an API version and
the provider builds the full URL. For customers whose managed endpoint does not
follow the Azure path convention, an explicit full-URL override may be supplied
per capability instead (``CUSTOMER_TENANT_GENERATION_ENDPOINT`` /
``CUSTOMER_TENANT_EMBEDDING_ENDPOINT``).

Credential handling (R17-D2 T1/T2)
----------------------------------
These are credentials into the customer's own cloud tenant, so they get the same
hygiene as connector tokens: read from config/secrets only, never hardcoded,
never logged, and re-read live on every call so a rotation or revocation by the
customer takes effect without a process restart. ``repr``/``str`` redact the
credential. T1 resolves the credential from configuration/secrets; T2 layers the
Fernet-encrypted credential vault behind the same accessor.
"""
from __future__ import annotations

import os

# Provider name used by MODEL_GENERATION_PROVIDER / MODEL_EMBEDDING_PROVIDER.
CUSTOMER_TENANT_PROVIDER_NAME: str = "customer_tenant"

# The customer's managed model endpoint base (non-secret), e.g. the Azure
# OpenAI resource root ``https://my-resource.openai.azure.com``.
CONFIG_KEY_ENDPOINT: str = "CUSTOMER_TENANT_ENDPOINT"

# The managed-service API version (non-secret). Required by Azure OpenAI as a
# query parameter; a sensible default is supplied so a minimal config works.
CONFIG_KEY_API_VERSION: str = "CUSTOMER_TENANT_API_VERSION"

# Secret credential into the customer's tenant. Resolved live, never logged.
CONFIG_KEY_API_KEY: str = "CUSTOMER_TENANT_API_KEY"

# Deployment names. CUSTOMER_TENANT_DEPLOYMENT is a common fallback; the
# generation and embedding deployments can be pinned independently.
CONFIG_KEY_DEPLOYMENT: str = "CUSTOMER_TENANT_DEPLOYMENT"
CONFIG_KEY_GENERATION_DEPLOYMENT: str = "CUSTOMER_TENANT_GENERATION_DEPLOYMENT"
CONFIG_KEY_EMBEDDING_DEPLOYMENT: str = "CUSTOMER_TENANT_EMBEDDING_DEPLOYMENT"

# Full-URL overrides for a managed endpoint that does not follow the Azure
# deployment-path convention. When set, the value is used verbatim and the
# endpoint base / deployment / api-version are not consulted for that capability.
CONFIG_KEY_GENERATION_ENDPOINT: str = "CUSTOMER_TENANT_GENERATION_ENDPOINT"
CONFIG_KEY_EMBEDDING_ENDPOINT: str = "CUSTOMER_TENANT_EMBEDDING_ENDPOINT"

# Default Azure OpenAI GA API version. Non-secret; overridable via config so an
# operator can move to a newer version without a code change.
_DEFAULT_API_VERSION: str = "2024-02-01"

# Azure OpenAI deployment path segments (non-secret; this module is inside the
# gateway package, the only place permitted to reference provider paths).
_GENERATION_PATH: str = "chat/completions"
_EMBEDDING_PATH: str = "embeddings"


def _clean_url(value: str) -> str:
    """Normalize a configured URL without inventing a default external endpoint."""
    return value.strip().rstrip("/")


def _resolve_deployment(specific_key: str) -> str:
    """Resolve a specific deployment name, falling back to the common key."""
    return (
        os.getenv(specific_key, "").strip()
        or os.getenv(CONFIG_KEY_DEPLOYMENT, "").strip()
    )


class CustomerTenantConfig:
    """Config holder for the customer's managed tenant model endpoint.

    Only the non-secret endpoint base and explicit URL overrides are read at
    construction. The credential, deployment names, and API version are resolved
    live so credential rotation and deployment/version repinning take effect
    without a process restart (there is deliberately no cached credential slot).
    """

    __slots__ = (
        "_endpoint",
        "_explicit_generation_url",
        "_explicit_embedding_url",
    )

    def __init__(self) -> None:
        self._endpoint: str = _clean_url(os.getenv(CONFIG_KEY_ENDPOINT, ""))
        self._explicit_generation_url: str = _clean_url(
            os.getenv(CONFIG_KEY_GENERATION_ENDPOINT, "")
        )
        self._explicit_embedding_url: str = _clean_url(
            os.getenv(CONFIG_KEY_EMBEDDING_ENDPOINT, "")
        )

    @property
    def endpoint(self) -> str:
        """The customer's managed endpoint base URL, if provided (non-secret)."""
        return self._endpoint

    def api_version(self) -> str:
        """The managed-service API version, resolved live (non-secret)."""
        return os.getenv(CONFIG_KEY_API_VERSION, "").strip() or _DEFAULT_API_VERSION

    def generation_deployment(self) -> str:
        """Generation deployment name, resolved live from config."""
        return _resolve_deployment(CONFIG_KEY_GENERATION_DEPLOYMENT)

    def embedding_deployment(self) -> str:
        """Embedding deployment name, resolved live from config."""
        return _resolve_deployment(CONFIG_KEY_EMBEDDING_DEPLOYMENT)

    def _deployment_url(self, deployment: str, path: str) -> str:
        """Build the Azure-style per-deployment URL, or "" when unconfigured.

        Returns "" (never a partial/invalid URL) when the endpoint base or the
        deployment name is missing, so the provider degrades gracefully rather
        than POSTing to a malformed endpoint.
        """
        if not self._endpoint or not deployment:
            return ""
        return (
            f"{self._endpoint}/openai/deployments/{deployment}/{path}"
            f"?api-version={self.api_version()}"
        )

    def generation_endpoint(self) -> str:
        """Full generation URL: explicit override if set, else Azure-style build."""
        if self._explicit_generation_url:
            return self._explicit_generation_url
        return self._deployment_url(self.generation_deployment(), _GENERATION_PATH)

    def embedding_endpoint(self) -> str:
        """Full embedding URL: explicit override if set, else Azure-style build."""
        if self._explicit_embedding_url:
            return self._explicit_embedding_url
        return self._deployment_url(self.embedding_deployment(), _EMBEDDING_PATH)

    def resolve_api_key(self) -> str:
        """Return the live tenant credential without logging it.

        Read live on every call so a rotation or revocation by the customer
        takes effect immediately. Returns "" when unset; the provider treats
        that as "skip the call, degrade gracefully". Must never be logged and
        must never be returned to a caller outside the gateway package.
        """
        return os.getenv(CONFIG_KEY_API_KEY, "")

    def has_credential(self) -> bool:
        """True when a non-empty tenant credential is configured (live read)."""
        return bool(self.resolve_api_key())

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            "CustomerTenantConfig("
            f"endpoint={self._endpoint!r}, "
            f"generation_endpoint_override={self._explicit_generation_url!r}, "
            f"embedding_endpoint_override={self._explicit_embedding_url!r}, "
            "api_key='***REDACTED***'"
            ")"
        )

    __str__ = __repr__


__all__ = [
    "CONFIG_KEY_API_KEY",
    "CONFIG_KEY_API_VERSION",
    "CONFIG_KEY_DEPLOYMENT",
    "CONFIG_KEY_EMBEDDING_DEPLOYMENT",
    "CONFIG_KEY_EMBEDDING_ENDPOINT",
    "CONFIG_KEY_ENDPOINT",
    "CONFIG_KEY_GENERATION_DEPLOYMENT",
    "CONFIG_KEY_GENERATION_ENDPOINT",
    "CUSTOMER_TENANT_PROVIDER_NAME",
    "CustomerTenantConfig",
]
