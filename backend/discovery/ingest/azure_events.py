"""
azure_events.py — MSP-B2 T1 (AT-648): Azure Event Connector authentication.

The Azure half of the MSP-B1/B2 matched pair. THIS task (AT-648) delivers the
connector's authentication + subscription-access foundation; the event polling and
normalisation (Alerts Management, Activity Log, Service Health → MSP-B0 mappers)
are T2/T3 and are left as clearly-marked seams below.

Authentication flow (MSP-B2 §1, reusing the EXISTING Azure AD plumbing — a second
auth implementation would be a bug):

    per-org vault (service principal: client_id + secret + tenant)
      ↓
    Azure AD (Microsoft identity) client-credentials grant
      ↓  (app.auth.oauth.request_client_credentials_token — the SAME token
      ↓   exchange the Teams/SharePoint/Graph connectors use)
    ARM access token (scope = {resource_manager}/.default, environment-aware)
      ↓
    Azure Resource Manager APIs (polled outbound-only in T2/T3)

Access modes (both supported — AC3):
  * Lighthouse — one service principal in SMX's tenant, delegated Reader on many
    customer subscriptions. ARM authorises the cross-tenant reads; the token is
    still minted against the SP's HOME tenant.
  * Direct — a per-tenant service principal (org == customer). Same token flow.

Subscription discipline (AC4 / MSP-B2 AC7): the connector reads ONLY the pinned,
Owner-approved subscription set in the config. Lighthouse discovery may enumerate
delegated subscriptions, but a newly delegated one is NEVER auto-ingested — it is a
candidate for Owner approval until added to the pinned set. ``authorized_subscriptions``
is always the pinned set, regardless of what discovery returns.

Security (MSP-B2 §1): the service principal secret lives ONLY in the per-org vault
(``app.auth.vault`` static-credential machinery, Fernet-encrypted at rest). It is
never in config, never logged, and used only for the outbound token exchange. Only
Reader-level RBAC is required on the subscriptions (the minimal role definition is
the T5 partner-security artifact).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .azure_events_config import (
    CONNECTOR_ID,
    AzureEventConfig,
    AzureEventConfigError,
    load_azure_event_config,
)

logger = logging.getLogger(__name__)


class AzureAuthError(Exception):
    """Raised when Azure authentication cannot proceed (no SP, or token failure)."""


# ── Service principal (vault-held) ───────────────────────────────────────────────


@dataclass(frozen=True)
class AzureServicePrincipal:
    """An Azure AD service principal resolved from the per-org vault.

    ``client_secret`` is masked in ``repr`` so it can never leak into a log line
    or traceback (the same hygiene as the vault's StaticCredentialRecord).
    """
    client_id: str
    client_secret: str
    tenant_id: str

    def __repr__(self) -> str:  # never expose the secret
        return (
            f"AzureServicePrincipal(client_id={self.client_id!r}, "
            f"tenant_id={self.tenant_id!r}, client_secret=***)"
        )

    def is_complete(self) -> bool:
        return bool(self.client_id and self.client_secret and self.tenant_id)


#: Default vault reader — the existing static-credential machinery. Injectable so
#: tests exercise resolution without a live vault/DB. The service principal reuses
#: the static-credential record shape: username=client_id, secret=client_secret,
#: base_url=tenant_id (all Fernet-encrypted / non-secret exactly as that record
#: defines) — no new vault table or encryption path is introduced.
def _default_vault_reader(org_id: str, connector_id: str):
    from app.auth.vault import get_static_credential  # local import: heavy module
    return get_static_credential(org_id, connector_id)


def get_service_principal(
    org_id: str,
    *,
    credential_ref: str = CONNECTOR_ID,
    vault_reader: Optional[Callable[[str, str], Any]] = None,
) -> Optional[AzureServicePrincipal]:
    """Resolve the org's Azure service principal from the vault, or None if unset.

    Reuses the vault's static-credential read path — the credential is NEVER read
    from config or the environment (MSP-B2 §1). Returns None when no SP is stored
    for the org (a not-connected connector), so the caller can degrade rather than
    crash. The secret is handed to the token exchange only; it is never logged.
    """
    reader = vault_reader or _default_vault_reader
    record = reader(org_id, credential_ref)
    if record is None:
        return None
    return AzureServicePrincipal(
        client_id=str(getattr(record, "username", "") or ""),
        client_secret=str(getattr(record, "secret", "") or ""),
        tenant_id=str(getattr(record, "base_url", "") or ""),
    )


def store_service_principal(
    org_id: str,
    *,
    client_id: str,
    client_secret: str,
    tenant_id: str,
    credential_ref: str = CONNECTOR_ID,
) -> None:
    """Store (rotate) the org's Azure service principal in the vault.

    Thin wrapper over the existing ``vault.store_static_credential`` so the
    field mapping (username=client_id, secret=client_secret, base_url=tenant_id)
    lives in ONE place and store/read agree. The secret is Fernet-encrypted by the
    vault and never logged here.
    """
    if not client_id or not tenant_id:
        raise AzureAuthError("service principal requires a client_id and tenant_id")
    from app.auth.vault import store_static_credential
    store_static_credential(
        org_id,
        credential_ref,
        username=client_id,
        secret=client_secret,
        base_url=tenant_id,
    )


# ── ARM token acquisition (reuses the shared client-credentials exchange) ────────

#: A token function: (token_url, client_id, client_secret, scope) -> token dict.
#: Defaults to the shared OAuth client-credentials exchange; injectable for tests.
TokenFn = Callable[..., Awaitable[Dict[str, Any]]]


async def _default_token_fn(
    *, token_url: str, client_id: str, client_secret: str, scope: str
) -> Dict[str, Any]:
    from app.auth.oauth import request_client_credentials_token  # local import
    return await request_client_credentials_token(
        connector_id=CONNECTOR_ID,
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        scopes=[scope],
    )


async def acquire_arm_token(
    org_id: str,
    config: AzureEventConfig,
    *,
    service_principal: Optional[AzureServicePrincipal] = None,
    vault_reader: Optional[Callable[[str, str], Any]] = None,
    token_fn: Optional[TokenFn] = None,
) -> str:
    """Acquire an ARM-scoped access token for ``org_id`` (client-credentials grant).

    Resolves the service principal from the vault (unless one is supplied), builds
    the environment-aware AAD token endpoint and ARM ``.default`` scope, and mints
    the token via the SHARED OAuth client-credentials exchange. Works identically
    for Lighthouse and direct modes — the token is minted against the SP's home
    tenant either way; ARM authorises the (possibly cross-tenant) subscription
    reads. Raises :class:`AzureAuthError` when no SP is configured or the exchange
    yields no access token. The secret is never logged.
    """
    sp = service_principal or get_service_principal(
        org_id, credential_ref=config.credential_ref, vault_reader=vault_reader
    )
    if sp is None or not sp.is_complete():
        raise AzureAuthError(
            f"no complete Azure service principal in the vault for org {org_id!r} "
            f"(credential_ref={config.credential_ref!r}); connect the connector first"
        )

    env = config.environment
    fn = token_fn or _default_token_fn
    token = await fn(
        token_url=env.token_endpoint(sp.tenant_id),
        client_id=sp.client_id,
        client_secret=sp.client_secret,
        scope=env.arm_scope,
    )
    access_token = str((token or {}).get("access_token") or "").strip()
    if not access_token:
        raise AzureAuthError(
            f"Azure ARM token exchange for org {org_id!r} returned no access_token"
        )
    logger.info(
        "azure_events: acquired ARM token for org=%s env=%s mode=%s tenant=%s "
        "(subscriptions pinned=%d)",
        org_id, env.name, config.mode, sp.tenant_id, len(config.subscriptions),
    )
    return access_token


def acquire_arm_token_blocking(org_id: str, config: AzureEventConfig, **kwargs) -> str:
    """Synchronous convenience wrapper around :func:`acquire_arm_token`.

    For CLI / standalone use where there is no running event loop. Inside async
    code, await :func:`acquire_arm_token` directly.
    """
    return asyncio.run(acquire_arm_token(org_id, config, **kwargs))


# ── The connector (auth + subscription discipline; poll/emit are T2/T3) ──────────


class AzureEventIngestor:
    """Azure Event Connector — authentication + subscription-access foundation.

    Designed to slot onto the MSP-B1 shared cloud-event skeleton (poll/checkpoint/
    emit) once it lands: the ``ingest`` method here is the seam T2/T3 fill with the
    Alerts Management / Activity Log / Service Health polling and the MSP-B0
    mappers. This T1 delivers the auth + pinned-subscription discipline the whole
    connector depends on. The connector is transport-only: it invents NO
    detector-visible fields and emits ONLY normalised MSP-B0 events (T2/T3).
    """

    connector_id = CONNECTOR_ID

    def __init__(
        self,
        org_id: str,
        config: AzureEventConfig,
        *,
        vault_reader: Optional[Callable[[str, str], Any]] = None,
        token_fn: Optional[TokenFn] = None,
    ) -> None:
        self.org_id = org_id
        self.config = config
        self._vault_reader = vault_reader
        self._token_fn = token_fn

    # ── subscription access (the pinned-set discipline — AC4) ───────────────────

    def authorized_subscriptions(self) -> List[str]:
        """The subscriptions this connector will read — the pinned set ONLY.

        This is the single source of truth for "what gets ingested". It is the
        Owner-approved pinned set and never includes an unpinned delegated
        subscription, so the connected estate can never grow without an explicit
        config change (AC4 / MSP-B2 AC7).
        """
        return self.config.pinned_subscriptions

    def pending_delegated_subscriptions(self, discovered: List[str]) -> List[str]:
        """Delegated subscriptions discovery found that are NOT yet pinned.

        Surfaced for Owner review / run-health visibility — reported, never
        ingested. The "never silently growing" report (AC7).
        """
        return self.config.newly_delegated(discovered)

    async def acquire_token(self) -> str:
        """Acquire an ARM-scoped access token using the org's vaulted SP."""
        return await acquire_arm_token(
            self.org_id,
            self.config,
            vault_reader=self._vault_reader,
            token_fn=self._token_fn,
        )

    async def discover_delegated_subscriptions(
        self,
        *,
        arm_lister: Optional[Callable[[str, AzureEventConfig], Awaitable[List[str]]]] = None,
    ) -> List[str]:
        """Enumerate subscriptions the token can see (Lighthouse discovery).

        DISCOVERY ONLY — the result is filtered to the pinned set before any read
        and the unpinned remainder is reported for approval, never ingested
        (AC4/AC7). ``arm_lister`` is injectable; the live ARM ``/subscriptions``
        call is T2 wiring. Returns [] when no lister is available (T1).
        """
        if arm_lister is None:
            return []
        token = await self.acquire_token()
        return list(await arm_lister(token, self.config))

    # ── poll/emit seam (MSP-B2 T2/T3) ───────────────────────────────────────────

    async def ingest(self, checkpoint: Optional[Dict[str, Any]] = None) -> None:
        """Poll → map → admit, per pinned subscription. Implemented in T2/T3.

        The auth + subscription-access foundation (this task) is complete; the
        per-subscription Alerts Management / Activity Log / Service Health polling
        with per-subscription checkpoints and the MSP-B0 mappers is MSP-B2 T2/T3.
        """
        raise NotImplementedError(
            "Azure event polling is MSP-B2 T2/T3; AT-648 delivers auth + "
            "subscription-access foundation only"
        )


def build_ingestor(
    org_id: str,
    *,
    env: Optional[Dict[str, str]] = None,
    vault_reader: Optional[Callable[[str, str], Any]] = None,
    token_fn: Optional[TokenFn] = None,
) -> Optional[AzureEventIngestor]:
    """Build an :class:`AzureEventIngestor` for ``org_id`` from configuration.

    Returns None when the connector is not configured for the org (not an error —
    the connector simply contributes nothing). Raises
    :class:`AzureEventConfigError` on a present-but-invalid config.
    """
    config = load_azure_event_config(org_id, env=env)
    if config is None:
        return None
    return AzureEventIngestor(
        org_id, config, vault_reader=vault_reader, token_fn=token_fn
    )
