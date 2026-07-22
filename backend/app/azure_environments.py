"""
azure_environments.py — MSP-B2 T4 (AT-651): the SINGLE Azure cloud-environment map.

Azure integrations must speak to the right sovereign cloud: Azure Commercial
(``AzureCloud``) and Azure US Government (``AzureUSGovernment``) use DIFFERENT
Microsoft-identity and Azure Resource Manager (ARM) endpoints. This module is the
ONE place those endpoints are defined and resolved, so environment selection is
purely configuration-driven and no connector scatters `login.microsoftonline.us`
literals or per-call conditionals.

It is deliberately dependency-free and credential-free (MSP-B2 §"Cloud-environment
awareness" / AT-651 security rule): it resolves endpoints and cloud METADATA only —
never secrets. Secrets live exclusively in the vault. Because it imports nothing
heavy (no DB, no auth, no network), both the Azure Event Connector
(``discovery.ingest.azure_events_config``) and the model gateway's customer-tenant
Azure model surface (Azure Government, wired in B9) resolve the same environment
concept from here — "shared conceptually with the model-gateway environment map"
made concrete as ONE reusable map.

Public API:
  AZURE_CLOUD, AZURE_US_GOVERNMENT, DEFAULT_ENVIRONMENT
  ENVIRONMENTS: Dict[str, AzureEnvironment]
  resolve_environment(name) -> AzureEnvironment
  list_environments() -> list[str]
  UnknownAzureEnvironmentError
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# Canonical environment names (the values a connector/config stores and selects).
AZURE_CLOUD = "AzureCloud"
AZURE_US_GOVERNMENT = "AzureUSGovernment"

#: The ARM API version used to enumerate subscriptions (non-secret; stable GA).
_SUBSCRIPTIONS_API_VERSION = "2020-01-01"


class UnknownAzureEnvironmentError(ValueError):
    """Raised when an Azure environment name is not one of the supported clouds."""


@dataclass(frozen=True)
class AzureEnvironment:
    """Resolved endpoints for one Azure sovereign cloud (all non-secret).

    ``authority_host`` — the Microsoft-identity (Azure AD) login host used to build
                         the OAuth token endpoint.
    ``resource_manager`` — the Azure Resource Manager (ARM) base endpoint; also the
                         basis of the ARM OAuth scope.
    """
    name: str
    authority_host: str
    resource_manager: str

    @property
    def authority_base(self) -> str:
        """The AAD authority base URL (``https://{authority_host}``)."""
        return f"https://{self.authority_host}"

    @property
    def arm_scope(self) -> str:
        """The ARM OAuth scope for the client-credentials grant (``.default``)."""
        return f"{self.resource_manager.rstrip('/')}/.default"

    def token_endpoint(self, tenant_id: str) -> str:
        """The AAD v2.0 token endpoint for ``tenant_id`` in this environment."""
        return f"https://{self.authority_host}/{tenant_id}/oauth2/v2.0/token"

    def subscriptions_url(self) -> str:
        """The ARM endpoint listing subscriptions visible to the token."""
        return (
            f"{self.resource_manager.rstrip('/')}/subscriptions"
            f"?api-version={_SUBSCRIPTIONS_API_VERSION}"
        )


# The environment map. AzureUSGovernment resolves the sovereign-cloud endpoints
# (login.microsoftonline.us / management.usgovcloudapi.net) — AT-651 AC2.
ENVIRONMENTS: Dict[str, AzureEnvironment] = {
    AZURE_CLOUD: AzureEnvironment(
        name=AZURE_CLOUD,
        authority_host="login.microsoftonline.com",
        resource_manager="https://management.azure.com",
    ),
    AZURE_US_GOVERNMENT: AzureEnvironment(
        name=AZURE_US_GOVERNMENT,
        authority_host="login.microsoftonline.us",
        resource_manager="https://management.usgovcloudapi.net",
    ),
}

#: Default when no environment is configured (AT-651 AC3 — Commercial by default).
DEFAULT_ENVIRONMENT = AZURE_CLOUD


def list_environments() -> List[str]:
    """Return the supported Azure environment names."""
    return list(ENVIRONMENTS)


def resolve_environment(name: Optional[str]) -> AzureEnvironment:
    """Resolve ``name`` to an :class:`AzureEnvironment` (default AzureCloud).

    Configuration-driven (AT-651 AC3): a blank/None value resolves to the default
    Commercial cloud; a recognised value resolves its endpoints; an unrecognised
    value raises :class:`UnknownAzureEnvironmentError` so a typo (or an attempt to
    use an unsupported cloud such as AzureChinaCloud) surfaces loudly rather than
    silently defaulting to Commercial when a sovereign cloud was intended.
    """
    key = (name or "").strip() or DEFAULT_ENVIRONMENT
    env = ENVIRONMENTS.get(key)
    if env is None:
        raise UnknownAzureEnvironmentError(
            f"unknown Azure environment {key!r}; supported: {sorted(ENVIRONMENTS)}"
        )
    return env
