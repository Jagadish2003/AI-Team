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
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional

from .azure_events_config import (
    CONNECTOR_ID,
    AzureEventConfig,
    AzureEventConfigError,
    load_azure_event_config,
    resolve_azure_event_config,
)
from .azure_alerts import (
    AzureAlertsClient,
    alert_fired_at,
    alert_id,
    default_alerts_client,
)
from .azure_admin_events import (
    AzureEventStreamClient,
    activity_id,
    activity_subscription_id,
    activity_timestamp,
    default_activity_log_client,
    default_service_health_client,
    is_administrative,
    service_health_id,
    service_health_timestamp,
)
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch

try:
    from discovery.signals.reference_mappers import (
        map_app_insights,
        map_azure_activity_log,
        map_azure_monitor,
        map_service_health,
    )
except ModuleNotFoundError:  # pragma: no cover - import shim
    from backend.discovery.signals.reference_mappers import (
        map_app_insights,
        map_azure_activity_log,
        map_azure_monitor,
        map_service_health,
    )

try:
    from discovery.signals.ops_stream import (
        DEFAULT_ACTIVE_PERIOD_SECONDS,
        Admission,
        OpsEventStream,
    )
except ModuleNotFoundError:  # pragma: no cover - import shim
    from backend.discovery.signals.ops_stream import (  # type: ignore
        DEFAULT_ACTIVE_PERIOD_SECONDS,
        Admission,
        OpsEventStream,
    )

try:
    from app.provenance import EvidencePointer
except ModuleNotFoundError:  # pragma: no cover - import shim
    from backend.app.provenance import EvidencePointer  # type: ignore

#: The ``source_artifact_type`` the SHARED cloud-event skeleton stamps on a native
#: cloud event's OBSERVED pointer. Imported (never re-spelled) so the AWS and Azure
#: native paths key their evidence pointers identically.
from .cloud_event_connector import CLOUD_EVENT_ARTIFACT_TYPE

#: 2.0-D3 T1 — the bounded Application Insights read scope. A pure classification
#: + scope-defence layer over the surfaces this connector ALREADY reads; it adds no
#: client, credential, or ARM path of its own (see azure_app_insights.py).
from .azure_app_insights import (
    SURFACE_AZURE_MONITOR,
    SURFACE_AZURE_SERVICE_HEALTH,
    app_insights_scope,
    is_excluded_telemetry,
)

#: 2.0-D3 T3 — explicit-reference association of the monitored application to a
#: configured .NET application or a known CMDB CI. Additive record-wrapper
#: information only: it never touches the MSP-B0 event, so the deterministic event
#: signature and transport equivalence are unaffected by whether an association
#: happens to be configured.
from .app_insights_association import AppInsightsAssociationResolver

logger = logging.getLogger(__name__)

#: The transport provider tag stamped on emitted records (not a detector field —
#: it lives on the record wrapper, alongside account_scope).
PROVIDER_AZURE = "azure"

#: The three V1 Azure event streams (scope defence: these three ONLY).
STREAM_ALERTS = "alerts"
STREAM_ACTIVITY_LOG = "activity_log"
STREAM_SERVICE_HEALTH = "service_health"
V1_STREAMS = (STREAM_ALERTS, STREAM_ACTIVITY_LOG, STREAM_SERVICE_HEALTH)

#: Stream key → the MSP-B0 source_system its mapper stamps. Used for health
#: reporting AND as each record's ``surface``: since AC4 re-stamps the EVENT's
#: source_system to the provider family, this is where the per-stream B0 source
#: system remains visible (on the transport wrapper, never on the event).
_STREAM_SOURCE_SYSTEM = {
    STREAM_ALERTS: "azure_monitor",
    STREAM_ACTIVITY_LOG: "azure_activity",
    STREAM_SERVICE_HEALTH: "azure_service_health",
}

#: Stream key → the human-readable Azure surface name, used in LOG TEXT ONLY.
#: Observability vocabulary: nothing branches on it and no record carries it.
_STREAM_LABEL = {
    STREAM_ALERTS: "Azure Monitor Alerts",
    STREAM_ACTIVITY_LOG: "Azure Activity Log",
    STREAM_SERVICE_HEALTH: "Azure Service Health",
}


def _filter_new(records: List[Dict[str, Any]], since_iso: Optional[str], ts_of) -> List[Dict[str, Any]]:
    """Keep only records whose timestamp is strictly newer than ``since_iso``.

    ISO-8601 UTC timestamps compare correctly as strings. ``since_iso`` None/''
    means first run (take everything). A record with no timestamp is kept (it
    cannot be proven old) so nothing is silently dropped.
    """
    if not since_iso:
        return list(records)
    out: List[Dict[str, Any]] = []
    for r in records:
        ts = ts_of(r)
        if not ts or ts > since_iso:
            out.append(r)
    return out


def _max_ts(records: List[Dict[str, Any]], ts_of, *, floor: Optional[str]) -> Optional[str]:
    """The maximum timestamp across ``records`` (never below ``floor``)."""
    best = floor or None
    for r in records:
        ts = ts_of(r)
        if ts and (best is None or ts > best):
            best = ts
    return best


# ── Failure classification + retry/backoff (MSP-B2 T6 / AT-653) ──────────────────
# Per-subscription resilience: classify a poll failure, retry ONLY transient classes
# with bounded exponential backoff, and report every failure loudly into run health.
# Never retry a non-transient failure (bad credentials / permission / not-found /
# malformed body); never silently drop events or advance a failed checkpoint.

# Failure categories (also the run-health `category` vocabulary).
CATEGORY_AUTHENTICATION = "authentication"     # bad/expired SP credentials (401 on token)
CATEGORY_AUTHORIZATION = "authorization"       # RBAC/consent denied (403)
CATEGORY_NOT_FOUND = "not_found"               # invalid/absent subscription (404)
CATEGORY_THROTTLED = "throttled"               # rate limited (429)
CATEGORY_SERVER_ERROR = "server_error"         # transient Azure 5xx
CATEGORY_TIMEOUT = "timeout"                    # request timed out
CATEGORY_NETWORK = "network"                    # connection/network failure
CATEGORY_MALFORMED = "malformed_response"       # unparseable/invalid response body
CATEGORY_CLIENT_ERROR = "client_error"          # other non-retryable 4xx
CATEGORY_UNEXPECTED = "unexpected"              # anything unclassified (fail safe: no retry)

#: The categories that are TRANSIENT — eligible for bounded backoff retry. Every
#: other category is permanent (operator action needed) and is never retried.
_RETRYABLE_CATEGORIES = frozenset({
    CATEGORY_THROTTLED, CATEGORY_SERVER_ERROR, CATEGORY_TIMEOUT, CATEGORY_NETWORK,
})

_TRANSIENT_5XX = frozenset({500, 502, 503, 504})

# Bounded retry defaults (env-overridable, same convention as other connectors).
DEFAULT_MAX_RETRIES = int(os.getenv("AZURE_EVENT_MAX_RETRIES", "3") or "3")
DEFAULT_BACKOFF_BASE_SECONDS = float(os.getenv("AZURE_EVENT_BACKOFF_BASE_SECONDS", "0.5") or "0.5")
DEFAULT_BACKOFF_MAX_SECONDS = float(os.getenv("AZURE_EVENT_BACKOFF_MAX_SECONDS", "8") or "8")


def _status_code_of(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status extraction (works for httpx errors and duck-typed ones)."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def classify_failure(exc: BaseException) -> str:
    """Classify a poll/auth failure into a run-health :data:`CATEGORY_*`.

    Duck-typed so it needs no httpx import: an HTTP status (429/5xx/401/403/404),
    then timeout/network by exception-type name, then auth/malformed by type, else
    ``unexpected`` (fail-safe → never retried). Deterministic, side-effect-free.
    """
    if isinstance(exc, AzureAuthError):
        return CATEGORY_AUTHENTICATION

    status = _status_code_of(exc)
    if status is not None:
        if status == 429:
            return CATEGORY_THROTTLED
        if status in _TRANSIENT_5XX or (500 <= status <= 599):
            return CATEGORY_SERVER_ERROR
        if status == 401:
            return CATEGORY_AUTHENTICATION
        if status == 403:
            return CATEGORY_AUTHORIZATION
        if status == 404:
            return CATEGORY_NOT_FOUND
        if 400 <= status <= 499:
            return CATEGORY_CLIENT_ERROR

    name = type(exc).__name__.lower()
    if "timeout" in name:
        return CATEGORY_TIMEOUT
    if any(k in name for k in ("connect", "network", "readerror", "requesterror", "connection")):
        return CATEGORY_NETWORK
    if "oautherror" in name:
        return CATEGORY_AUTHENTICATION
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return CATEGORY_MALFORMED
    return CATEGORY_UNEXPECTED


def is_retryable(category: str) -> bool:
    """True when a failure category is transient (eligible for bounded retry)."""
    return category in _RETRYABLE_CATEGORIES


def _recoverable(category: str) -> bool:
    """Whether a LATER run may recover unaided (transient) vs needs operator action."""
    return category in _RETRYABLE_CATEGORIES


@dataclass
class RetryPolicy:
    """Bounded exponential-backoff retry policy (deterministic)."""
    max_retries: int = DEFAULT_MAX_RETRIES
    base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS

    def backoff_seconds(self, attempt: int) -> float:
        """Backoff before the Nth retry (attempt = 1 is the first retry)."""
        delay = self.base_seconds * (2 ** max(0, attempt - 1))
        return min(delay, self.max_seconds)


class AzureSubscriptionError(Exception):
    """A subscription poll failed after classification + any retries.

    Carries the run-health facts (category, retryable, attempts) so the per-stream
    loop records a loud, structured failure without re-inspecting the cause.
    """

    def __init__(self, category: str, *, retryable: bool, attempts: int, cause: BaseException) -> None:
        super().__init__(f"{category} after {attempts} attempt(s): {cause}")
        self.category = category
        self.retryable = retryable
        self.attempts = attempts
        self.cause = cause


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


def decode_stream_checkpoints(value: Any) -> Dict[str, Dict[str, str]]:
    """Decode the connector's opaque checkpoint into ``{stream: {sub: iso}}``.

    Namespaced per stream (alerts / activity_log / service_health) so the three
    V1 streams keep INDEPENDENT per-subscription positions in one opaque value.
    Tolerant: None/blank/unparseable → empty per-stream maps (safe full re-read).
    Backward tolerant: a legacy flat ``{sub: iso}`` value (T2, alerts-only) is
    read as the alerts stream's map so an in-flight checkpoint is never lost.
    """
    out: Dict[str, Dict[str, str]] = {s: {} for s in V1_STREAMS}
    if value is None:
        return out
    parsed: Any = value
    if not isinstance(value, dict):
        text = str(value).strip()
        if not text:
            return out
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            logger.warning("azure_events: unreadable checkpoint %r; starting fresh", value)
            return out
    if not isinstance(parsed, dict):
        return out
    # Namespaced form: keys are stream names.
    if any(k in parsed for k in V1_STREAMS):
        for stream in V1_STREAMS:
            out[stream] = decode_checkpoints(parsed.get(stream))
        return out
    # Legacy flat form (alerts-only, T2): treat the whole map as the alerts stream.
    out[STREAM_ALERTS] = decode_checkpoints(parsed)
    return out


def encode_stream_checkpoints(checkpoints: Dict[str, Dict[str, str]]) -> str:
    """Encode ``{stream: {sub: iso}}`` to the connector's opaque checkpoint string."""
    return json.dumps(
        {
            stream: {k: v for k, v in (checkpoints.get(stream) or {}).items() if v}
            for stream in V1_STREAMS
        },
        sort_keys=True,
    )


@dataclass
class AzureStreamResult:
    """Outcome of ingesting one (or all) Azure event stream(s) across subscriptions.

    ``records`` are the emitted operational-event records (B0-shaped events wrapped
    with transport metadata). ``next_checkpoint`` is the opaque checkpoint string to
    persist. ``subscription_status`` reports per-subscription (or per stream+sub)
    outcome (polled/emitted counts, or an error) so a failure is LOUD and never
    silently thins a run (MSP-B2 §"Failure posture").
    """
    records: List[Dict[str, Any]] = field(default_factory=list)
    next_checkpoint: str = "{}"
    subscription_status: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: The MSP-B7 T4 per-run event-budget outcome for this poll (the deferral proof
    #: — see :meth:`AzureEventIngestor.budget_report`). Empty when no budget is set.
    budget: Dict[str, Any] = field(default_factory=dict)

    @property
    def emitted_count(self) -> int:
        return len(self.records)

    @property
    def failed_subscriptions(self) -> List[str]:
        return [s for s, st in self.subscription_status.items() if st.get("status") == "error"]

    @property
    def deferred_subscriptions(self) -> List[str]:
        """Subscriptions the per-run event budget cut short (resumable, not failed).

        Kept DISTINCT from ``failed_subscriptions``: a deferral is a bounded, declared
        degradation whose checkpoint was deliberately left unadvanced, not an error.
        """
        return [s for s, st in self.subscription_status.items() if st.get("status") == "deferred"]

    @property
    def all_ok(self) -> bool:
        """True only when every subscription drained cleanly.

        A budget deferral counts against this: reporting a poll that skipped part of
        its backlog as "all ok" is the silent-partial-ingest failure mode the rest of
        this connector is written to avoid.
        """
        return not self.failed_subscriptions and not self.deferred_subscriptions


#: Back-compat alias — T2 named the alerts outcome AzureAlertsResult; the type is
#: stream-agnostic and now serves all three streams.
AzureAlertsResult = AzureStreamResult


# ── The connector (auth + subscription discipline + alerts polling) ──────────────


class AzureEventIngestor(ChangeBasedIngestor):
    """Azure Event Connector — auth, subscription discipline, and alerts polling.

    Reuses the existing change-based ingestion abstraction (:class:`ChangeBasedIngestor`)
    — the same one the MSP-B8 bridge uses — so this connector plugs into the current
    pipeline and migrates cleanly onto the MSP-B1 shared cloud-event skeleton when it
    lands, with minimal change. Transport-only: it invents NO detector-visible fields
    and emits ONLY normalised MSP-B0 events.

    T1 (AT-648) delivered auth + the pinned-subscription discipline. T2 (AT-649) adds
    Azure Monitor **Alerts** polling; T3 (AT-650) adds Azure **Activity Log**
    (Administrative events only) and **Service Health** polling. All three V1 streams
    share ONE per-subscription poll/checkpoint engine (:meth:`_ingest_stream`) and
    are normalised through their MSP-B0 mappers (``map_azure_monitor`` /
    ``map_azure_activity_log`` / ``map_service_health``) — those three classes ONLY
    (scope defence). :meth:`ingest_all` runs the three streams for a full poll.

    Two responsibilities the shared cloud-event skeleton (MSP-B1 / AT-641) defines
    for EVERY native cloud connector, held here so the AWS and Azure paths behave
    identically rather than diverging:

    * **Transport re-stamp (AC4)** — each mapped event's ``source_system`` is
      re-stamped to the provider family (``'azure'``) while the mapper's
      ``event_signature`` is left untouched, so a native event equals its bridged
      twin in every field except that one. The mapper's per-stream MSP-B0 source
      system remains visible on the record wrapper as ``surface``.
    * **MSP-B7 admission** — the connector owns its own :class:`OpsEventStream` and
      admits every event it maps (:meth:`_ingest_stream`), so re-fires fold into one
      active signal with a count and the per-run event budget is enforced while
      polling. :meth:`active_signals` / :meth:`budget_report` are the read side.
      Admission is part of ingesting a cloud event — never something a downstream
      caller has to remember to do.
    """

    connector_id = CONNECTOR_ID
    #: The three native streams are append-only event streams; a native poll has no
    #: deletion to propagate (matches the bridge's declared limitation).
    reports_deletes = False
    #: A cloud event is an observation, not an indexed retrieval artifact — the change
    #: runner must not emit per-event artifact_changed/freshness work for it (see
    #: ChangeBasedIngestor.produces_retrieval_content).
    produces_retrieval_content = False

    def __init__(
        self,
        org_id: str,
        config: AzureEventConfig,
        *,
        vault_reader: Optional[Callable[[str, str], Any]] = None,
        token_fn: Optional[TokenFn] = None,
        alerts_client: Optional[AzureAlertsClient] = None,
        activity_log_client: Optional[AzureEventStreamClient] = None,
        service_health_client: Optional[AzureEventStreamClient] = None,
        raw_store: Optional[Any] = None,
        retry_policy: Optional[RetryPolicy] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        stream: Optional[OpsEventStream] = None,
        active_period_seconds: int = DEFAULT_ACTIVE_PERIOD_SECONDS,
        budget: Optional[int] = None,
        association_resolver: Optional[Any] = None,
    ) -> None:
        self.org_id = org_id
        self.config = config
        self._vault_reader = vault_reader
        self._token_fn = token_fn
        self._alerts_client = alerts_client
        self._activity_log_client = activity_log_client
        self._service_health_client = service_health_client
        self._raw_store = raw_store
        # MSP-B7 admission (dedup + per-run budget) is owned by the CONNECTOR, exactly
        # as the shared cloud-event skeleton owns it for AWS — admission is part of
        # ingesting a cloud event, not something a caller may forget to do. The stream
        # is stateful for this ingestor's lifetime: after a poll, `active_signals()`
        # is the deduplicated view and `budget_report()` the deferral proof.
        self.stream = stream if stream is not None else OpsEventStream(
            active_period_seconds=active_period_seconds, budget=budget
        )
        # T6: bounded retry/backoff for transient per-subscription failures, and an
        # injectable sleep so tests exercise backoff without real delay.
        self._retry = retry_policy or RetryPolicy()
        self._sleep = sleep_fn or time.sleep
        # D3 T3: resolved once per ingestor (the configuration does not change
        # mid-run). Injectable so the association decision table is testable with
        # no database and no configured estate.
        self._associations = association_resolver
        self._associations_loaded = association_resolver is not None
        # 2.0-D3 T4: every poll the per-run event budget cut short, accumulated for
        # the ingestor's lifetime. See `deferral_report` for why the budget report
        # alone is not enough.
        self._deferrals: List[Dict[str, Any]] = []

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

    # ── Budget deferrals (D3 T4) ────────────────────────────────────────────────

    def _note_deferral(self, stream: str, subscription: str, *, fetched: bool) -> None:
        """Record one poll the per-run event budget cut short."""
        self._deferrals.append({
            "stream": stream,
            "subscription": subscription,
            "reason": "run_event_budget_exhausted",
            # False when the budget stopped the run before it even asked the
            # provider for the page — the desired behaviour, and the case the
            # budget counters cannot see.
            "fetched": fetched,
        })

    def deferral_report(self) -> Dict[str, Any]:
        """What the per-run event budget cut short, and whether the poll completed.

        The MSP-B7 budget's own report counts DEFERRED EVENTS, which it can only do
        for events it actually saw. That leaves a reporting hole precisely where the
        budget behaves best: when capacity is exhausted the connector stops
        REQUESTING further pages (it must — otherwise the budget would bound the
        data but not the work), so those subscriptions contribute no deferred-event
        count and ``BudgetReport.breached`` stays False. A run that skipped whole
        subscriptions would then report a clean budget.

        This closes that hole the same way the AWS connector's ``poll_report`` does:
        ``complete`` is False whenever anything was cut short, and every affected
        (stream, subscription) is named. The runner merges it into the run's
        ``azureEvents`` health block and degrades the reported status, so a partial
        ingest is never reported as a complete one.
        """
        return {
            "complete": not self._deferrals,
            "deferred_polls": len(self._deferrals),
            "deferred": list(self._deferrals),
            **({"reason": "run_event_budget_exhausted"} if self._deferrals else {}),
        }

    # ── App Insights association (D3 T3) ────────────────────────────────────────

    def _association_resolver(self):
        """The association resolver, built on first use.

        Deferred rather than built in ``__init__`` so a connector that never meets
        an App Insights signal never reads the association configuration at all.
        Built at most once; a construction failure degrades to "no associations"
        rather than failing the poll, because an association is additive
        information and must never be able to block an otherwise valid ingest.
        """
        if not self._associations_loaded:
            self._associations_loaded = True
            try:
                self._associations = AppInsightsAssociationResolver(self.org_id)
            except Exception:  # noqa: BLE001 — additive info never breaks a run
                logger.warning(
                    "azure_events: App Insights association config unusable for "
                    "org=%s — events still ingest, without associations",
                    self.org_id, exc_info=True,
                )
                self._associations = None
        return self._associations

    def _app_insights_wrapper(self, scope) -> Dict[str, Any]:
        """The record-wrapper fragment for an in-scope App Insights signal.

        The T1 scope block, plus (D3 T3) any explicitly-configured association for
        the monitored application. Both live on the WRAPPER, never on the event.
        """
        block = scope.to_dict()
        resolver = self._association_resolver()
        if resolver is None:
            return block
        try:
            outcome = resolver.resolve(scope.component_id)
        except Exception:  # noqa: BLE001 — never let association break ingestion
            logger.warning(
                "azure_events: association resolution failed for %s (org=%s) — "
                "event still ingested without an association",
                scope.component_id, self.org_id, exc_info=True,
            )
            return block
        block.update(outcome.to_wrapper())
        return block

    # ── record shaping (shared by every stream) ─────────────────────────────────

    def _stamp_transport(self, event: Any) -> None:
        """Re-stamp the event's transport to the provider FAMILY (MSP-B1 AC4).

        The shared cloud-event skeleton's contract, applied identically here: a
        natively-ingested event's ``source_system`` is the provider family
        (``'azure'``) — the same value its bridged twin carries as ``'bridge:azure'``
        differs in — so a native event equals its bridged twin in EVERY other field.

        The recurrence fingerprint is NEVER recomputed: ``event_signature`` is left
        exactly as the ``map_azure_*`` mapper derived it. (``OperationalEvent``
        derives the signature only when it is empty, so assigning ``source_system``
        afterwards cannot overwrite it — that is what keeps a native event's
        signature equal to its bridged twin's, which is the whole point of the
        equivalence guarantee.) The per-stream MSP-B0 source system stays visible on
        the record WRAPPER as ``surface``, so nothing is lost.

        Provenance is re-pointed at the native cloud artifact and keyed so it
        resolves through the raw-event store under the same ``(provider, signal_id)``
        pair :meth:`_store_raw` writes.
        """
        event.source_system = PROVIDER_AZURE
        event.provenance = EvidencePointer.observed(
            source_system=PROVIDER_AZURE,
            source_artifact=event.signal_id,
            source_timestamp=event.observed_at,
            source_artifact_type=CLOUD_EVENT_ARTIFACT_TYPE,
        ).to_dict()

    def _store_raw(self, event: Any, raw: Dict[str, Any], *, stream: str, sub: str) -> None:
        """Persist the raw provider payload against the event's OBSERVED pointer.

        Keyed ``(org, provider, signal_id)`` — exactly the tuple the re-pointed
        evidence pointer carries, so ``resolve_raw_event`` walks a normalised event
        back to its raw payload (MSP-B0 / AT-638). The detector-visible event never
        embeds it. Best-effort: the evidence store is not a run-critical path.
        """
        if self._raw_store is None:
            return
        try:
            self._raw_store.put(self.org_id, PROVIDER_AZURE, event.signal_id, raw)
        except Exception:  # evidence store is best-effort
            logger.warning(
                "azure_events: raw-store put failed for %s %s (sub=%s)",
                stream, event.signal_id, sub, exc_info=True,
            )

    def _to_record(
        self,
        event: Any,
        subscription_id: str,
        provider_event_id: str,
        *,
        stream: str,
        surface: str,
        admission: Optional[Admission] = None,
        app_insights: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Wrap a normalised OperationalEvent as a pipeline delta record.

        The detector-visible payload is ``event`` (the provider-agnostic MSP-B0
        event, IDENTICAL in shape to what any other cloud connector emits). The
        wrapper carries transport-only metadata — the change kind, provider,
        ``account_scope`` (the subscription, B0's account scope, kept OFF the event
        so no provider-specific detector field is invented), the dedupe id, the B7
        admission ``disposition``, and the evidence pointer that resolves to the raw
        payload. Identical shape for all three streams; since the event's
        ``source_system`` is now the provider family (AC4), the stream is carried
        here as ``surface`` (the mapper's MSP-B0 source system — azure_monitor /
        azure_activity / azure_service_health) plus the ``stream`` key, mirroring
        the AWS skeleton's per-surface record metadata.

        ``artifact_id`` keeps the surface in the key so two streams can never
        collide on a shared provider event id.

        ``app_insights`` (2.0-D3 T1) is present ONLY when the record explicitly
        referenced an Application Insights component. Like ``account_scope`` and
        ``surface`` it lives on the WRAPPER, never on the event, so D3 invents no
        provider-specific detector field — turning it into event-level
        normalisation is T2's B0 mapper. Absent when out of scope, so a
        non-App-Insights record is byte-identical to what B2 emitted before D3.
        """
        return {
            "artifact_id": f"{PROVIDER_AZURE}:{surface}:{provider_event_id}",
            "change_kind": ChangeKind.CREATED,
            "source_system": PROVIDER_AZURE,
            "provider": PROVIDER_AZURE,
            "stream": stream,
            "surface": surface,
            "account_scope": subscription_id,
            "provider_event_id": provider_event_id,
            "event_signature": event.event_signature,
            "event": event.to_dict(),
            "evidence_pointer": event.provenance,
            **({"admission": admission.disposition} if admission is not None else {}),
            **({"app_insights": app_insights} if app_insights else {}),
        }

    # ── the shared per-subscription stream engine (reused by all 3 streams) ──────

    def _ingest_stream(
        self,
        *,
        stream: str,
        token: str,
        checkpoints: Dict[str, str],
        fetch,
        mapper,
        ts_of,
        id_of,
        prefilter=None,
        scope_of=None,
        scope_mapper=None,
    ) -> AzureStreamResult:
        """Poll ONE event stream for every pinned subscription, incrementally.

        The single poll → scope-filter → incremental-filter → map → emit →
        advance-checkpoint loop shared by alerts, activity log, and service health.
        Per subscription: fetch, drop out-of-scope records (``prefilter``, e.g. the
        Administrative-only gate — T3-AC4), keep only records newer than the
        subscription's checkpoint, normalise each through its B0 ``mapper``, re-stamp
        the transport to the provider family (AC4), store its raw payload, ADMIT it
        to this connector's MSP-B7 stream (dedup + per-run budget), wrap as a delta
        record carrying the subscription as ``account_scope``, and advance that
        subscription's checkpoint to the newest record SEEN — ONLY after the
        subscription processed cleanly. Subscriptions are INDEPENDENT: a fetch
        failure is caught, reported loudly, and leaves that subscription's checkpoint
        unadvanced while the others continue; a single malformed record is
        loud-skipped without failing the subscription. A budget deferral is likewise
        loud and leaves the checkpoint unadvanced (no silent thinning).

        Two 2.0-D3 T1 additions, both additive to the above:

        * **Scope defence (D3-AC2).** Every fetched record passes the
          ``is_excluded_telemetry`` gate BEFORE it can be mapped, so Application
          Insights raw telemetry or Log Analytics/KQL output seeded into any stream
          is dropped loudly and counted (``telemetry_excluded``) instead of becoming
          an event. The gate short-circuits on the alert / activity / health
          envelopes, so it cannot touch a record MSP-B2 legitimately ingests. An
          excluded record never advances the subscription watermark — so it is
          re-read on every run, which is a deliberate trade (an out-of-scope record
          must not be able to push the position past real alerts). See the
          ``advanced`` computation for why advancing past them is not the fix it
          looks like.
        * **App Insights scope (D3-AC1).** ``scope_of`` resolves a record's
          Application Insights component by EXPLICIT reference; when it does, the
          scope rides the record wrapper, the count appears in run health
          (``app_insights``), and the record is normalised by ``scope_mapper``
          (D3 T2's ``map_app_insights``) instead of the stream's default mapper —
          so the monitored application becomes the event's resource. A ``None``
          scope selects the default mapper unchanged, which is why D3 never
          re-classifies or narrows a B2 record.
        """
        env = self.config.environment
        next_checkpoints: Dict[str, str] = dict(checkpoints)
        records: List[Dict[str, Any]] = []
        status: Dict[str, Dict[str, Any]] = {}

        surface = _STREAM_SOURCE_SYSTEM.get(stream, stream)
        for sub in self.authorized_subscriptions():
            since_iso = checkpoints.get(sub)
            # MSP-B7 T4: the budget must stop the run FETCHING, not merely admitting —
            # otherwise it bounds the data but never the work. Checked between
            # subscriptions (each still gets its full page or none of it), so a
            # budget-exhausted run stops paying for provider pages whose events would
            # only be deferred. Loud, and the checkpoint stays put.
            if not self.stream.has_capacity():
                logger.warning(
                    "azure_events: %s poll SKIPPED for subscription %s (org=%s) — "
                    "per-run event budget exhausted; checkpoint preserved, resumes next run",
                    stream, sub, self.org_id,
                )
                status[sub] = {
                    "status": "deferred",
                    "reason": "run_event_budget_exhausted",
                    "polled": 0,
                    "emitted": 0,
                    "checkpoint_advanced": False,
                }
                self._note_deferral(stream, sub, fetched=False)
                continue
            try:
                fetched, attempts = self._fetch_with_retry(
                    fetch, stream=stream, token=token, subscription_id=sub,
                    environment=env, since_iso=since_iso,
                )
                if not fetched:
                    # An empty provider page is a FACT worth stating: without it, a
                    # zero-record poll is indistinguishable from one whose records were
                    # all filtered, folded, or suppressed further down. Informational —
                    # an empty page is a normal steady-state outcome, not a fault.
                    logger.info(
                        "azure_events: %s returned 0 records for subscription %s",
                        _STREAM_LABEL.get(stream, stream), sub,
                    )
                in_scope = [r for r in fetched if prefilter(r)] if prefilter else list(fetched)
                # 2.0-D3 T1 / AC2 — the scope-defence gate. Sits ahead of the
                # incremental filter so an excluded record is never mapped, never
                # admitted, never emitted, and never counted towards the watermark:
                # it is not data this connector processes at all. The `advanced`
                # computation below records why that last part must stay true.
                telemetry_excluded = 0
                kept: List[Dict[str, Any]] = []
                for candidate in in_scope:
                    if is_excluded_telemetry(candidate):
                        telemetry_excluded += 1
                        continue
                    kept.append(candidate)
                # ONE warning per poll carrying the count, not one per record. The
                # exclusion must stay loud (a silently narrowed read scope is the
                # failure this guard exists to prevent), but a per-record line made
                # the volume proportional to the telemetry in the feed and repeated
                # in full on every run, because an excluded record never advances the
                # watermark (see `advanced` below) and is therefore re-fetched every
                # time. The count is the actionable part and is on the run status too.
                if telemetry_excluded:
                    logger.warning(
                        "azure_events: %s DROPPED %d out-of-scope record(s) for "
                        "subscription %s — Application Insights raw telemetry / "
                        "analytics output is not ingested (2.0-D3 scope defence). "
                        "These are re-read each run: an excluded record never "
                        "advances the checkpoint.",
                        stream, telemetry_excluded, sub,
                    )
                new_records = _filter_new(kept, since_iso, ts_of)
                emitted = 0
                skipped = 0
                deduped = 0
                deferred = 0
                app_insights_count = 0
                for raw in new_records:
                    # D3 T1/T2: resolve the App Insights scope BEFORE mapping — it
                    # selects the mapper. An in-scope record is normalised by the
                    # App Insights B0 mapper (resource = the monitored application);
                    # everything else keeps the stream's own mapper untouched.
                    ai_scope = scope_of(raw) if scope_of else None
                    record_mapper = (
                        scope_mapper
                        if (ai_scope is not None and scope_mapper is not None)
                        else mapper
                    )
                    try:
                        event = record_mapper(raw, org_id=self.org_id)
                    except Exception:  # one malformed record must not fail the sub
                        logger.warning(
                            "azure_events: %s mapper failed for %s (sub=%s) — skipped",
                            stream, id_of(raw), sub, exc_info=True,
                        )
                        skipped += 1
                        continue
                    # AC4 transport re-stamp (signature untouched) → raw evidence →
                    # B7 admission. The connector performs its OWN admission, so no
                    # caller can consume its events un-deduplicated.
                    self._stamp_transport(event)
                    self._store_raw(event, raw, stream=stream, sub=sub)
                    admission: Admission = self.stream.admit(event)
                    if admission.is_deferred:
                        # Budget exhausted mid-page: stop this subscription and leave
                        # its checkpoint UNADVANCED so the whole page is re-polled next
                        # run (admission is idempotent, so a re-poll never double-counts).
                        deferred += 1
                        break
                    if admission.is_duplicate:
                        # An at-least-once redelivery of a firing already counted —
                        # handled by B7, not silently dropped.
                        deduped += 1
                        continue
                    if ai_scope is not None:
                        app_insights_count += 1
                    records.append(
                        self._to_record(
                            event, sub, id_of(raw),
                            stream=stream, surface=surface, admission=admission,
                            app_insights=(
                                self._app_insights_wrapper(ai_scope)
                                if ai_scope else None
                            ),
                        )
                    )
                    emitted += 1
                # Only records this connector PROCESSED move the watermark: an excluded
                # record contributes nothing, deliberately, and this must stay that way.
                #
                # The tempting change is to advance past excluded records too, on the
                # grounds that the drop is deterministic so re-reading them is pure
                # waste. It buys nothing and costs correctness. Nothing, because
                # `is_excluded_telemetry` fires on telemetry/analytics ENVELOPES, and
                # those carry no alert/health timestamp — `ts_of` returns '' for every
                # shape in `azure_app_insights_sample.json`, so `_max_ts` already skips
                # them and the watermark is unchanged either way. Correctness, because
                # the only records it WOULD move are excluded ones carrying a readable
                # alert-shaped timestamp, and then a single out-of-scope record with a
                # far-future timestamp would push the position past every real alert
                # that follows and silently suppress them — an out-of-scope record must
                # never decide what in-scope data we skip.
                #
                # The residual cost is real but bounded and visible: a subscription
                # whose feed is dominated by telemetry keeps re-fetching it, which is
                # why the drop is counted into `telemetry_excluded` on the per-run
                # status and summarised in one WARNING per poll above rather than left
                # to be inferred. Pinned by
                # `test_azure_app_insights_scope.py::test_excluded_records_do_not_advance_a_checkpoint`.
                advanced = None if deferred else _max_ts(new_records, ts_of, floor=since_iso)
                if advanced:
                    next_checkpoints[sub] = advanced
                if deferred:
                    logger.warning(
                        "azure_events: %s poll DEFERRED for subscription %s (org=%s) — "
                        "per-run event budget exhausted after %d emitted; checkpoint "
                        "preserved, resumes next run",
                        stream, sub, self.org_id, emitted,
                    )
                    self._note_deferral(stream, sub, fetched=True)
                status[sub] = {
                    "status": "deferred" if deferred else "ok",
                    "polled": len(fetched),
                    "emitted": emitted,
                    "attempts": attempts,
                    **({"in_scope": len(in_scope)} if prefilter else {}),
                    **({"skipped": skipped} if skipped else {}),
                    **({"deduped": deduped} if deduped else {}),
                    # D3 T1: both are omitted when zero, so a stream that met no
                    # App Insights signal and no excluded record reports exactly the
                    # status shape it reported before D3.
                    **({"telemetry_excluded": telemetry_excluded} if telemetry_excluded else {}),
                    **({"app_insights": app_insights_count} if app_insights_count else {}),
                    **(
                        {
                            "reason": "run_event_budget_exhausted",
                            "checkpoint_advanced": False,
                        }
                        if deferred
                        else {}
                    ),
                }
                # The per-subscription ingestion funnel, emitted on EVERY successful
                # poll including an all-zero one. Each stage the records could have
                # been lost at is a separate number, so "the endpoint succeeded but no
                # events appeared" is answerable from one log line instead of a
                # debugger. Reports the same counts already recorded in status[sub];
                # nothing here is computed for logging alone.
                logger.info(
                    "azure_events: stream=%s subscription=%s polled=%d in_scope=%d "
                    "telemetry_excluded=%d new=%d mapped=%d mapper_skipped=%d "
                    "deduped=%d deferred=%d app_insights=%d since=%s checkpoint=%s",
                    stream, sub, len(fetched), len(in_scope), telemetry_excluded,
                    len(new_records), emitted, skipped, deduped, deferred,
                    app_insights_count,
                    since_iso or "(first_run)",
                    next_checkpoints.get(sub) or "(unchanged)",
                )
            except AzureSubscriptionError as se:
                # Loud, structured, isolated. Checkpoint is NOT advanced for this
                # subscription (no silent thinning — it is retried next run).
                logger.warning(
                    "azure_events: %s poll FAILED for subscription %s (org=%s): "
                    "category=%s retryable=%s attempts=%s cause=%s",
                    stream, sub, self.org_id, se.category, se.retryable, se.attempts, se.cause,
                )
                status[sub] = {
                    "status": "error",
                    "category": se.category,
                    "retryable": se.retryable,
                    "attempts": se.attempts,
                    "recoverable": _recoverable(se.category),
                    "error": str(se.cause),
                }
                self._emit_subscription_health(stream, sub, status[sub])

        return AzureStreamResult(
            records=records,
            next_checkpoint=encode_checkpoints(next_checkpoints),
            subscription_status=status,
            budget=self.budget_report(),
        )

    def _fetch_with_retry(
        self, fetch, *, stream: str, token: str, subscription_id: str,
        environment, since_iso: Optional[str],
    ):
        """Call a stream ``fetch`` with bounded backoff on TRANSIENT failures.

        Returns ``(records, attempts)``. Retries only transient categories
        (throttled/5xx/timeout/network) up to ``RetryPolicy.max_retries`` with
        exponential backoff (the injected ``sleep_fn`` makes it test-fast); a
        non-transient failure (auth/authorization/not-found/malformed) is NOT
        retried. On give-up (or a permanent failure) raises
        :class:`AzureSubscriptionError` carrying the classification + attempt count
        — never silently returns empty (which would thin the run).
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return (
                    fetch(
                        token=token, subscription_id=subscription_id,
                        environment=environment, since_iso=since_iso,
                    ),
                    attempt,
                )
            except Exception as exc:  # noqa: BLE001 — classify, maybe retry, else raise
                category = classify_failure(exc)
                retryable = is_retryable(category)
                if retryable and attempt <= self._retry.max_retries:
                    delay = self._retry.backoff_seconds(attempt)
                    logger.warning(
                        "azure_events: %s transient %s for subscription %s "
                        "(attempt %d/%d) — backing off %.2fs",
                        stream, category, subscription_id, attempt,
                        self._retry.max_retries + 1, delay,
                    )
                    self._sleep(delay)
                    continue
                raise AzureSubscriptionError(
                    category, retryable=retryable, attempts=attempt, cause=exc
                )

    def _emit_subscription_health(
        self, stream: str, subscription_id: str, status_entry: Dict[str, Any],
        *, source_system: Optional[str] = None,
    ) -> None:
        """Emit a loud run-health telemetry event for a failed subscription.

        Non-blocking and secret-free: identifiers + classification + counts only. A
        telemetry failure never breaks ingestion (health reporting is observability,
        not a run-critical path). Imported lazily so offline runs never pull the
        telemetry/DB subsystem at import time.
        """
        try:
            from app.telemetry import record_event
        except Exception:  # pragma: no cover - telemetry optional in some contexts
            return
        payload = {
            "org_id": self.org_id,
            "connector_id": self.connector_id,
            "source_system": source_system or _STREAM_SOURCE_SYSTEM.get(stream, stream),
            "account_scope": subscription_id,
            "stream": stream,
            "status": "error",
            "category": str(status_entry.get("category", CATEGORY_UNEXPECTED)),
            "retryable": bool(status_entry.get("retryable", False)),
            "attempts": int(status_entry.get("attempts", 1)),
            "recoverable": bool(status_entry.get("recoverable", False)),
            "error_summary": str(status_entry.get("error", ""))[:300],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            record_event("ingestion.subscription_health", payload)
        except Exception:  # noqa: BLE001 — never let health reporting break the run
            logger.debug("azure_events: subscription_health emit failed", exc_info=True)

    def _resolve_token(self, token: Optional[str]) -> str:
        return token or acquire_arm_token_blocking(
            self.org_id,
            self.config,
            vault_reader=self._vault_reader,
            token_fn=self._token_fn,
        )

    # ── Alerts polling (MSP-B2 T2 / AT-649) ─────────────────────────────────────

    def ingest_alerts(self, *, token: Optional[str] = None, checkpoint: Any = None) -> AzureStreamResult:
        """Poll Azure Monitor Alerts for every pinned subscription, incrementally.

        Alerts ONLY (scope defence); normalised through ``map_azure_monitor``
        (T2-AC3). See :meth:`_ingest_stream` for the per-subscription checkpoint /
        failure-isolation semantics.

        2.0-D3 T1: this is one of the two surfaces that can carry an Application
        Insights operational signal (availability, application-failure and
        dependency-failure alerts), so it resolves the App Insights scope by
        explicit component reference. The alert set read here is UNCHANGED — D3
        classifies, it does not filter.
        """
        client = self._alerts_client or default_alerts_client()
        return self._ingest_stream(
            stream=STREAM_ALERTS,
            token=self._resolve_token(token),
            checkpoints=decode_checkpoints(checkpoint),
            fetch=client.fetch_alerts,
            mapper=map_azure_monitor,
            ts_of=alert_fired_at,
            id_of=alert_id,
            scope_of=lambda raw: app_insights_scope(raw, surface=SURFACE_AZURE_MONITOR),
            scope_mapper=map_app_insights,
        )

    # ── Activity Log (administrative) polling (MSP-B2 T3 / AT-650) ───────────────

    def ingest_activity_log(self, *, token: Optional[str] = None, checkpoint: Any = None) -> AzureStreamResult:
        """Poll Azure Activity Log ADMINISTRATIVE events for every pinned subscription.

        Administrative events ONLY — the ``is_administrative`` prefilter drops every
        other Activity Log category (ServiceHealth/Security/Policy/…) so out-of-scope
        classes are never ingested (T3-AC4). Normalised through
        ``map_azure_activity_log`` (T3-AC1/AC3). Same per-subscription checkpoint and
        failure-isolation semantics as alerts.
        """
        client = self._activity_log_client or default_activity_log_client()
        return self._ingest_stream(
            stream=STREAM_ACTIVITY_LOG,
            token=self._resolve_token(token),
            checkpoints=decode_checkpoints(checkpoint),
            fetch=client.fetch,
            mapper=map_azure_activity_log,
            ts_of=activity_timestamp,
            id_of=activity_id,
            prefilter=is_administrative,
        )

    # ── Service Health polling (MSP-B2 T3 / AT-650) ─────────────────────────────

    def ingest_service_health(self, *, token: Optional[str] = None, checkpoint: Any = None) -> AzureStreamResult:
        """Poll Azure Service Health events for every pinned subscription.

        Normalised through ``map_service_health`` (T3-AC2/AC3). Same per-subscription
        checkpoint and failure-isolation semantics as the other streams.

        2.0-D3 T1: the second surface that can carry an Application Insights
        operational signal — a health/failure event whose impacted resources
        explicitly name an App Insights component is a health transition for the
        monitored application. The event set read here is likewise UNCHANGED.
        """
        client = self._service_health_client or default_service_health_client()
        return self._ingest_stream(
            stream=STREAM_SERVICE_HEALTH,
            token=self._resolve_token(token),
            checkpoints=decode_checkpoints(checkpoint),
            fetch=client.fetch,
            mapper=map_service_health,
            ts_of=service_health_timestamp,
            id_of=service_health_id,
            scope_of=lambda raw: app_insights_scope(
                raw, surface=SURFACE_AZURE_SERVICE_HEALTH
            ),
            scope_mapper=map_app_insights,
        )

    # ── Admission read side (the deduplicated view) ─────────────────────────────
    #
    # Mirrors the shared cloud-event skeleton's read side exactly, so a caller reads
    # the same two surfaces off either native cloud connector.

    def active_signals(self, org_id: Optional[str] = None):
        """The folded, deduplicated active signals produced by admission (AC5)."""
        return self.stream.active_signals(org_id)

    def budget_report(self) -> Dict[str, Any]:
        """The run's MSP-B7 event-budget outcome (deferred volume; loud degradation)."""
        report = self.stream.budget_report()
        to_dict = getattr(report, "to_dict", None)
        return to_dict() if callable(to_dict) else dict(report or {})

    # ── Full poll across all three V1 streams ───────────────────────────────────

    def ingest_all(self, *, token: Optional[str] = None, checkpoint: Any = None) -> AzureStreamResult:
        """Poll all three V1 streams (alerts + activity log + service health).

        Each stream keeps its OWN per-subscription checkpoint inside the namespaced
        opaque value (``{stream: {sub: iso}}``). The ARM token is acquired ONCE and
        shared across streams. Records are concatenated; ``subscription_status`` is
        keyed ``"{stream}:{sub}"`` so a per-stream, per-subscription failure is
        individually visible (no silent thinning).

        A connector-level AUTHENTICATION failure (the one SP that serves every
        subscription) is caught here and reported LOUDLY into run health for every
        pinned subscription — visible, checkpoints preserved, never a silent drop
        (T6-AC2). It does not crash the run.
        """
        ns = decode_stream_checkpoints(checkpoint)
        try:
            arm_token = self._resolve_token(token)
        except Exception as exc:  # noqa: BLE001 — auth failure is loud, not fatal
            return self._auth_failure_result(exc, ns)

        alerts = self.ingest_alerts(token=arm_token, checkpoint=ns[STREAM_ALERTS])
        activity = self.ingest_activity_log(token=arm_token, checkpoint=ns[STREAM_ACTIVITY_LOG])
        health = self.ingest_service_health(token=arm_token, checkpoint=ns[STREAM_SERVICE_HEALTH])

        next_ns = {
            STREAM_ALERTS: decode_checkpoints(alerts.next_checkpoint),
            STREAM_ACTIVITY_LOG: decode_checkpoints(activity.next_checkpoint),
            STREAM_SERVICE_HEALTH: decode_checkpoints(health.next_checkpoint),
        }
        status: Dict[str, Dict[str, Any]] = {}
        for stream, res in (
            (STREAM_ALERTS, alerts),
            (STREAM_ACTIVITY_LOG, activity),
            (STREAM_SERVICE_HEALTH, health),
        ):
            for sub, st in res.subscription_status.items():
                status[f"{stream}:{sub}"] = st

        return AzureStreamResult(
            records=alerts.records + activity.records + health.records,
            next_checkpoint=encode_stream_checkpoints(next_ns),
            subscription_status=status,
            # One stream serves all three surfaces, so this is the whole poll's proof.
            budget=self.budget_report(),
        )

    def _auth_failure_result(
        self, exc: BaseException, ns: Dict[str, Dict[str, str]]
    ) -> AzureStreamResult:
        """Report a connector-level auth failure loudly for every subscription.

        No records, checkpoints PRESERVED (the incoming namespaced positions are
        re-emitted unchanged — nothing advances, nothing is thinned), and a
        run-health event is emitted per pinned subscription per stream. Auth is not
        retried (a bad credential will not fix itself within a run)."""
        category = classify_failure(exc)  # → authentication
        logger.warning(
            "azure_events: AUTHENTICATION failed for org=%s (%s); all %d pinned "
            "subscription(s) reported unhealthy, checkpoints preserved",
            self.org_id, exc, len(self.authorized_subscriptions()),
        )
        status: Dict[str, Dict[str, Any]] = {}
        entry_template = {
            "status": "error",
            "category": category,
            "retryable": False,
            "attempts": 1,
            "recoverable": _recoverable(category),
            "error": str(exc),
        }
        for stream in V1_STREAMS:
            for sub in self.authorized_subscriptions():
                status[f"{stream}:{sub}"] = dict(entry_template)
                self._emit_subscription_health(stream, sub, entry_template)
        return AzureStreamResult(
            records=[],
            next_checkpoint=encode_stream_checkpoints(ns),  # preserved, not advanced
            subscription_status=status,
            budget=self.budget_report(),
        )

    # ── ChangeBasedIngestor contract (pipeline entrypoint) ───────────────────────

    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint] = None
    ) -> Iterator[DeltaBatch]:
        """Yield one delta batch of ALL normalised Azure events since ``since``.

        Adapts :meth:`ingest_all` to the change-based pipeline: the opaque checkpoint
        carries the namespaced per-stream, per-subscription map. A per-stream /
        per-subscription failure is isolated inside ``ingest_all`` (loud in
        ``subscription_status``); the batch still advances the positions that
        succeeded, so a failing subscription never blocks or thins the others.
        Emitted as one complete batch (per-poll volume is bounded).
        """
        if org_id and org_id != self.org_id:
            raise ValueError(
                f"ingest_changes org_id {org_id!r} does not match this ingestor's "
                f"org {self.org_id!r}"
            )
        result = self.ingest_all(checkpoint=since.value if since else None)
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
    activity_log_client: Optional[AzureEventStreamClient] = None,
    service_health_client: Optional[AzureEventStreamClient] = None,
    raw_store: Optional[Any] = None,
    budget: Optional[int] = None,
) -> Optional[AzureEventIngestor]:
    """Build an :class:`AzureEventIngestor` for ``org_id`` from configuration.

    Returns None when the connector is not configured for the org (not an error —
    the connector simply contributes nothing). Raises
    :class:`AzureEventConfigError` on a present-but-invalid config.

    Resolves through :func:`resolve_azure_event_config` so an Owner who connected
    Azure through the Integration Hub (environment/mode + pinned subscriptions on
    the connector record) is picked up automatically, with the explicit
    ``AZURE_EVENT_CONFIG`` env / offline fixture still taking precedence.
    """
    config = resolve_azure_event_config(org_id, env=env)
    if config is None:
        return None
    if budget is None:
        # The calibrated per-run event budget (MSP-B7 T6), resolved here exactly as
        # the AWS connector's build_ingestor resolves it — so the runner-driven path
        # is budgeted on both clouds while a directly-constructed test ingestor stays
        # unbounded. Calibration is advisory: its absence must not block ingestion.
        try:
            from discovery.signals.ops_calibration import CALIBRATED_RUN_EVENT_BUDGET

            budget = CALIBRATED_RUN_EVENT_BUDGET
        except Exception:  # pragma: no cover - calibration is advisory here
            budget = None
    return AzureEventIngestor(
        org_id,
        config,
        vault_reader=vault_reader,
        token_fn=token_fn,
        alerts_client=alerts_client,
        activity_log_client=activity_log_client,
        service_health_client=service_health_client,
        raw_store=raw_store,
        budget=budget,
    )
