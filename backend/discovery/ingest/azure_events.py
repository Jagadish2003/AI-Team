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
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional

from .azure_events_config import (
    CONNECTOR_ID,
    AzureEventConfig,
    AzureEventConfigError,
    load_azure_event_config,
)
from .azure_alerts import (
    AzureAlertsClient,
    alert_fired_at,
    alert_id,
    default_alerts_client,
    filter_new_alerts,
    max_fired_at,
)
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch

try:
    from discovery.signals.reference_mappers import map_azure_monitor
except ModuleNotFoundError:  # pragma: no cover - import shim
    from backend.discovery.signals.reference_mappers import map_azure_monitor

logger = logging.getLogger(__name__)

#: The transport provider tag stamped on emitted alert records (not a detector
#: field — it lives on the record wrapper, alongside account_scope).
PROVIDER_AZURE = "azure"


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


# ── Per-subscription checkpoints (opaque to the runner) ──────────────────────────
# Each subscription polls independently and keeps its OWN last-seen alert time, so
# a re-run re-reads nothing (T2-AC2/AC4) and one subscription's position never
# affects another's. The whole per-subscription map is encoded as the single opaque
# Checkpoint.value the change-based runner persists (a JSON object keyed by
# subscription id → last firedDateTime ISO string).


def decode_checkpoints(value: Any) -> Dict[str, str]:
    """Decode the opaque checkpoint value into a {subscription_id: last_iso} map.

    Tolerant: None/blank/unparseable → ``{}`` (a safe full re-read), never a crash.
    Accepts a dict (already decoded) or a JSON string (as persisted).
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if v}
    text = str(value).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        logger.warning("azure_events: unreadable checkpoint %r; starting fresh", value)
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if v}


def encode_checkpoints(checkpoints: Dict[str, str]) -> str:
    """Encode a {subscription_id: last_iso} map to the opaque checkpoint string."""
    return json.dumps({k: v for k, v in checkpoints.items() if v}, sort_keys=True)


@dataclass
class AzureAlertsResult:
    """Outcome of an alerts ingest across the pinned subscriptions.

    ``records`` are the emitted operational-event records (B0-shaped events wrapped
    with transport metadata). ``next_checkpoint`` is the opaque per-subscription
    checkpoint string to persist. ``subscription_status`` reports per-subscription
    outcome (polled/emitted counts, or an error) so a failure is LOUD and never
    silently thins a run (MSP-B2 §"Failure posture").
    """
    records: List[Dict[str, Any]] = field(default_factory=list)
    next_checkpoint: str = "{}"
    subscription_status: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def emitted_count(self) -> int:
        return len(self.records)

    @property
    def failed_subscriptions(self) -> List[str]:
        return [s for s, st in self.subscription_status.items() if st.get("status") == "error"]

    @property
    def all_ok(self) -> bool:
        return not self.failed_subscriptions


# ── The connector (auth + subscription discipline + alerts polling) ──────────────


class AzureEventIngestor(ChangeBasedIngestor):
    """Azure Event Connector — auth, subscription discipline, and alerts polling.

    Reuses the existing change-based ingestion abstraction (:class:`ChangeBasedIngestor`)
    — the same one the MSP-B8 bridge uses — so this connector plugs into the current
    pipeline and migrates cleanly onto the MSP-B1 shared cloud-event skeleton when it
    lands, with minimal change. Transport-only: it invents NO detector-visible fields
    and emits ONLY normalised MSP-B0 events.

    T1 (AT-648) delivered auth + the pinned-subscription discipline. T2 (AT-649) adds
    Azure Monitor **Alerts** polling with per-subscription checkpoints, normalised
    through ``map_azure_monitor`` — Alerts ONLY (scope defence). Activity Log and
    Service Health are MSP-B2 T3 and extend :meth:`ingest_alerts`'s per-subscription
    loop with additional streams.
    """

    connector_id = CONNECTOR_ID
    #: Alerts Management is an append-only fired-instance stream; a native poll has
    #: no deletion to propagate (matches the bridge's declared limitation).
    reports_deletes = False

    def __init__(
        self,
        org_id: str,
        config: AzureEventConfig,
        *,
        vault_reader: Optional[Callable[[str, str], Any]] = None,
        token_fn: Optional[TokenFn] = None,
        alerts_client: Optional[AzureAlertsClient] = None,
        raw_store: Optional[Any] = None,
    ) -> None:
        self.org_id = org_id
        self.config = config
        self._vault_reader = vault_reader
        self._token_fn = token_fn
        self._alerts_client = alerts_client
        self._raw_store = raw_store

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

    # ── Alerts polling (MSP-B2 T2 / AT-649) ─────────────────────────────────────

    def _alerts(self) -> AzureAlertsClient:
        return self._alerts_client or default_alerts_client()

    def _to_record(
        self, event: Any, subscription_id: str, raw: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Wrap a normalised OperationalEvent as a pipeline delta record.

        The detector-visible payload is ``event`` (the provider-agnostic MSP-B0
        event, IDENTICAL in shape to what any other cloud connector emits). The
        wrapper carries transport-only metadata — the change kind, provider,
        ``account_scope`` (the subscription, B0's account scope, kept OFF the event
        so no provider-specific detector field is invented), the dedupe id, and the
        evidence pointer that resolves to the raw payload.
        """
        aid = alert_id(raw)
        return {
            "artifact_id": f"{event.source_system}:{aid}",
            "change_kind": ChangeKind.CREATED,
            "source_system": event.source_system,
            "provider": PROVIDER_AZURE,
            "account_scope": subscription_id,
            "provider_event_id": aid,
            "event": event.to_dict(),
            "evidence_pointer": event.provenance,
        }

    def ingest_alerts(
        self,
        *,
        token: Optional[str] = None,
        checkpoint: Any = None,
    ) -> AzureAlertsResult:
        """Poll Azure Monitor Alerts for every PINNED subscription, incrementally.

        Per subscription: fetch alerts, keep only those newer than that
        subscription's checkpoint (:func:`filter_new_alerts`), normalise each
        through ``map_azure_monitor`` (T2-AC3), wrap as a delta record carrying the
        subscription as ``account_scope``, and advance that subscription's
        checkpoint to the newest alert seen — ONLY after the subscription's alerts
        are processed successfully (T2-AC2/AC4). Subscriptions are INDEPENDENT: one
        subscription's auth/throttle/parse failure is caught, reported loudly in
        ``subscription_status``, and leaves its checkpoint unadvanced while the
        others continue (MSP-B2 §"Failure posture").

        ``token`` is acquired once from the vaulted service principal when not
        supplied (one SP serves all subscriptions in both Lighthouse and direct
        modes). Alerts ONLY — no Activity Log / Service Health / metrics / Log
        Analytics (scope defence).
        """
        arm_token = token or acquire_arm_token_blocking(
            self.org_id,
            self.config,
            vault_reader=self._vault_reader,
            token_fn=self._token_fn,
        )
        client = self._alerts()
        env = self.config.environment
        checkpoints = decode_checkpoints(checkpoint)
        next_checkpoints: Dict[str, str] = dict(checkpoints)
        records: List[Dict[str, Any]] = []
        status: Dict[str, Dict[str, Any]] = {}

        for sub in self.authorized_subscriptions():
            since_iso = checkpoints.get(sub)
            try:
                fetched = client.fetch_alerts(
                    token=arm_token,
                    subscription_id=sub,
                    environment=env,
                    since_iso=since_iso,
                )
                new_alerts = filter_new_alerts(fetched, since_iso)
                emitted = 0
                skipped = 0
                for raw in new_alerts:
                    try:
                        event = map_azure_monitor(raw, org_id=self.org_id)
                    except Exception:  # one malformed alert must not fail the sub
                        logger.warning(
                            "azure_events: map_azure_monitor failed for alert %s "
                            "(sub=%s) — skipped", alert_id(raw), sub, exc_info=True,
                        )
                        skipped += 1
                        continue
                    if self._raw_store is not None:
                        try:
                            self._raw_store.put(
                                self.org_id, event.source_system, alert_id(raw), raw
                            )
                        except Exception:  # evidence store is best-effort
                            logger.warning(
                                "azure_events: raw-store put failed for alert %s (sub=%s)",
                                alert_id(raw), sub, exc_info=True,
                            )
                    records.append(self._to_record(event, sub, raw))
                    emitted += 1
                # Advance to the newest alert SEEN (incl. loud-skipped ones, so a
                # malformed alert is not re-read forever) — only reached when the
                # fetch itself succeeded.
                advanced = max_fired_at(new_alerts, floor=since_iso)
                if advanced:
                    next_checkpoints[sub] = advanced
                status[sub] = {
                    "status": "ok",
                    "polled": len(fetched),
                    "emitted": emitted,
                    **({"skipped": skipped} if skipped else {}),
                }
            except Exception as exc:  # noqa: BLE001 — isolate per-subscription failure
                logger.warning(
                    "azure_events: alerts poll FAILED for subscription %s (org=%s): %s",
                    sub, self.org_id, exc,
                )
                # Do NOT advance this subscription's checkpoint (no silent thinning).
                status[sub] = {"status": "error", "error": str(exc)}

        return AzureAlertsResult(
            records=records,
            next_checkpoint=encode_checkpoints(next_checkpoints),
            subscription_status=status,
        )

    # ── ChangeBasedIngestor contract (pipeline entrypoint) ───────────────────────

    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint] = None
    ) -> Iterator[DeltaBatch]:
        """Yield one delta batch of normalised Azure alert events since ``since``.

        Adapts :meth:`ingest_alerts` to the change-based pipeline: the opaque
        checkpoint carries the per-subscription map. A subscription failure is
        isolated inside ``ingest_alerts`` (loud in ``subscription_status``); the
        batch still advances the checkpoints of the subscriptions that succeeded,
        so a failing subscription never blocks or thins the others. Emitted here as
        a single complete batch (alert volume per poll is bounded).
        """
        if org_id and org_id != self.org_id:
            raise ValueError(
                f"ingest_changes org_id {org_id!r} does not match this ingestor's "
                f"org {self.org_id!r}"
            )
        result = self.ingest_alerts(checkpoint=since.value if since else None)
        yield DeltaBatch(
            records=result.records,
            next_checkpoint=result.next_checkpoint,
            is_complete=True,
        )


def build_ingestor(
    org_id: str,
    *,
    env: Optional[Dict[str, str]] = None,
    vault_reader: Optional[Callable[[str, str], Any]] = None,
    token_fn: Optional[TokenFn] = None,
    alerts_client: Optional[AzureAlertsClient] = None,
    raw_store: Optional[Any] = None,
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
        org_id,
        config,
        vault_reader=vault_reader,
        token_fn=token_fn,
        alerts_client=alerts_client,
        raw_store=raw_store,
    )
