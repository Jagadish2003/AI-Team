"""
azure_events_config.py — MSP-B2 T1 (AT-648): Azure Event Connector configuration.

The Azure half of the MSP-B1/B2 matched pair. This module owns the NON-SECRET,
per-deployment configuration the connector needs BEFORE it authenticates:

  * the cloud ENVIRONMENT (AzureCloud vs AzureUSGovernment) and its ARM/authority
    endpoints — resolved from a map shared conceptually with the model gateway's
    customer-tenant Azure environment surface;
  * the ACCESS MODE (Azure Lighthouse delegated vs direct per-tenant service
    principal);
  * the PINNED SUBSCRIPTION SET — the explicit, Owner-approved list of Azure
    subscriptions the connector is allowed to read.

Scope discipline (MSP-B2 §"The MSP access pattern" / AC7): the subscription set is
CONFIGURED, never auto-discovered. Even in Lighthouse mode — where SMX's tenant may
be delegated access to many customer subscriptions — the connector ingests ONLY the
subscriptions explicitly listed here. A newly delegated subscription is a CANDIDATE
pending Owner approval, never ingested until it is added to the pinned set. This is
the platform's forward-only activation principle, held for estates.

Security (MSP-B2 §1 / AC): a target config carries NO credentials. The service
principal secret lives in the per-org vault (see ``azure_events.py``); this config
carries only non-secret identifiers (subscription ids, environment, mode). An entry
that embeds an inline secret is rejected via the SHARED
``operational_config.find_inline_secret_keys`` guard, exactly as the Java/.NET
operational targets are — the "no secret in config" rule cannot drift per connector.

This module contains NO polling, mapping, or emit logic (that is T2/T3) — only
configuration shaping and validation.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:  # shared "no secret in config" guard (identical rule to Java/.NET targets)
    from .operational_config import find_inline_secret_keys
except ImportError:  # pragma: no cover - import shim
    from discovery.ingest.operational_config import find_inline_secret_keys

try:
    from . import is_live
except Exception:  # pragma: no cover - import shim
    from discovery.ingest import is_live

# MSP-B2 T4 (AT-651): the Azure cloud-environment map lives in ONE shared module
# (app.azure_environments) so it is reused consistently across Azure integrations
# (this connector now; the model gateway's Azure Government model surface per B9)
# and never duplicated. It is dependency-free (no DB/auth/network), so importing it
# keeps this config module offline-safe. Names are re-exported below so existing
# callers/tests keep using ``azure_events_config.AZURE_CLOUD`` etc. unchanged.
from app.azure_environments import (  # noqa: F401 — re-exported public surface
    AZURE_CLOUD,
    AZURE_US_GOVERNMENT,
    DEFAULT_ENVIRONMENT,
    ENVIRONMENTS,
    AzureEnvironment,
    UnknownAzureEnvironmentError,
    list_environments,
)
from app.azure_environments import resolve_environment as _resolve_environment

logger = logging.getLogger(__name__)

# The connector id — the vault key under which this connector's service principal
# secret is stored per org, and B10's Integration Hub system id.
CONNECTOR_ID = "azure_events"

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "azure_events_config_sample.json"

#: Env var (live mode) holding the per-deployment Azure event config (JSON).
_CONFIG_ENV = "AZURE_EVENT_CONFIG"


# ── Cloud environments (AzureCloud / AzureUSGovernment) ──────────────────────────
# The environment map + AzureEnvironment type + constants are imported from the
# shared app.azure_environments module (above) and re-exported here, so this
# connector and any other Azure integration resolve endpoints from ONE source
# (MSP-B2 §1 "Cloud-environment awareness" / AT-651 — no duplicate endpoint maps).


def resolve_environment(name: Optional[str]) -> AzureEnvironment:
    """Return the :class:`AzureEnvironment` for ``name`` (default AzureCloud).

    Thin wrapper over the shared resolver that re-raises its
    :class:`UnknownAzureEnvironmentError` as :class:`AzureEventConfigError`, so a
    bad environment surfaces as a connector-config error (consistent with every
    other invalid-config path here) rather than a bare ValueError. A typo or an
    unsupported cloud surfaces loudly instead of silently defaulting to Commercial.
    """
    try:
        return _resolve_environment(name)
    except UnknownAzureEnvironmentError as exc:
        raise AzureEventConfigError(str(exc)) from exc


# ── Access modes (Lighthouse delegated / direct) ─────────────────────────────────

MODE_LIGHTHOUSE = "lighthouse"
MODE_DIRECT = "direct"
_VALID_MODES = frozenset({MODE_LIGHTHOUSE, MODE_DIRECT})
DEFAULT_MODE = MODE_DIRECT


class AzureEventConfigError(Exception):
    """Raised when the Azure Event Connector configuration is invalid."""


@dataclass(frozen=True)
class AzureEventConfig:
    """Per-deployment Azure Event Connector configuration (non-secret).

    ``subscriptions`` is the PINNED, Owner-approved set — the only subscriptions
    the connector ingests. It never grows automatically (AC4/AC7).
    """
    environment: AzureEnvironment
    mode: str
    subscriptions: List[str]
    credential_ref: str = CONNECTOR_ID
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise AzureEventConfigError(
                f"unknown Azure access mode {self.mode!r}; supported: {sorted(_VALID_MODES)}"
            )
        # Subscriptions must be an explicit list of non-empty ids (pinned set).
        if not isinstance(self.subscriptions, list):
            raise AzureEventConfigError("subscriptions must be a list of subscription ids")

    @property
    def pinned_subscriptions(self) -> List[str]:
        """The explicit, Owner-approved subscription set (a copy — never mutated)."""
        return list(self.subscriptions)

    def is_pinned(self, subscription_id: str) -> bool:
        """True when ``subscription_id`` is in the Owner-approved pinned set."""
        return str(subscription_id) in set(self.subscriptions)

    def filter_to_pinned(self, candidate_subscription_ids: List[str]) -> List[str]:
        """Return only the candidates that are in the pinned set, in pinned order.

        The gate every discovery result passes through: a delegated subscription
        the deployment has not pinned is dropped here, so it is never ingested
        (AC4/AC7). Pinned order is preserved for deterministic polling.
        """
        candidates = {str(c) for c in (candidate_subscription_ids or [])}
        return [s for s in self.subscriptions if s in candidates]

    def newly_delegated(self, candidate_subscription_ids: List[str]) -> List[str]:
        """Return candidates that are NOT pinned — delegated but pending approval.

        These are surfaced for Owner review (and run-health visibility); the
        connector never ingests them until they are explicitly added to
        ``subscriptions``. This is the "never silently growing" report (AC7).
        """
        pinned = set(self.subscriptions)
        seen: set = set()
        out: List[str] = []
        for c in candidate_subscription_ids or []:
            cid = str(c)
            if cid not in pinned and cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out


# ── Loading (config / fixture — never network discovery) ─────────────────────────


def _coerce_config(entry: Dict[str, Any]) -> AzureEventConfig:
    """Build an :class:`AzureEventConfig` from a raw dict, enforcing the no-secret rule."""
    if not isinstance(entry, dict):
        raise AzureEventConfigError("Azure event config must be a JSON object")

    inline_secrets = find_inline_secret_keys(entry)
    if inline_secrets:
        # Never echo the values — only the offending key NAMES — so a rejected
        # secret is never written to a log or file (mirrors Java/.NET targets).
        raise AzureEventConfigError(
            f"Azure event config contains inline credential field(s) {inline_secrets}; "
            "store the service principal in the vault and reference it via "
            "'credential_ref' instead."
        )

    metadata = entry.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    raw_subs = entry.get("subscriptions", [])
    if not isinstance(raw_subs, list):
        raise AzureEventConfigError("'subscriptions' must be a JSON array of subscription ids")
    subscriptions = [str(s).strip() for s in raw_subs if str(s).strip()]

    return AzureEventConfig(
        environment=resolve_environment(entry.get("environment")),
        mode=str(entry.get("mode", DEFAULT_MODE)).strip().lower() or DEFAULT_MODE,
        subscriptions=subscriptions,
        credential_ref=str(entry.get("credential_ref", CONNECTOR_ID)).strip() or CONNECTOR_ID,
        metadata=metadata,
    )


def _raw_config_entry(org_id: str, env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """Return the raw config dict for an org — config/fixture, never scanning.

    Offline: the deterministic fixture. Live: the ``AZURE_EVENT_CONFIG`` env JSON,
    which is EITHER an object keyed by org id (with a ``default``/``*`` fallback)
    OR a single flat config object applied to every org. No network discovery is
    ever performed (MSP-B2 §"Multi-subscription").
    """
    if not is_live():
        if not FIXTURE_PATH.exists():
            return None
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return _select_for_org(data, org_id)

    environ = env if env is not None else os.environ
    raw = (environ.get(_CONFIG_ENV) or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AzureEventConfigError(
            f"{_CONFIG_ENV} is not valid JSON: {type(exc).__name__}"
        ) from exc
    return _select_for_org(parsed, org_id)


def _select_for_org(data: Any, org_id: str) -> Optional[Dict[str, Any]]:
    """Pick the config entry for ``org_id`` from an org-keyed object or a flat one."""
    if not isinstance(data, dict):
        raise AzureEventConfigError("Azure event config must be a JSON object")
    # A flat config carries connector fields directly.
    if "subscriptions" in data or "environment" in data or "mode" in data:
        return data
    # Otherwise it is keyed by org id, with a default/* fallback.
    if org_id in data:
        return data[org_id]
    for fallback in ("default", "*"):
        if fallback in data:
            return data[fallback]
    return None


def load_azure_event_config(
    org_id: str, *, env: Optional[Dict[str, str]] = None
) -> Optional[AzureEventConfig]:
    """Load the Azure Event Connector config for ``org_id`` (or None if unset).

    Returns None when no config is present for the org — the connector simply
    contributes nothing (a not-configured connector is not an error). Raises
    :class:`AzureEventConfigError` on a present-but-invalid config (unknown
    environment/mode, or an inline secret).
    """
    entry = _raw_config_entry(org_id, env=env)
    if entry is None:
        return None
    return _coerce_config(entry)


# ── Integration Hub bridge (MSP-B2 T7 / B13) ─────────────────────────────────────
#
# The env/fixture config above is the per-deployment override. The everyday path,
# though, is an Owner connecting Azure through the Integration Hub: the connect flow
# (routes_cloud_connectors._store_azure_connection) writes the non-secret
# ``environment``/``mode`` onto this org's connector record and vaults the service
# principal, and pinning a subscription (routes_cloud_connectors.pin_scope) appends
# it to ``record["scopes"]``. That IS the pinned-subscription set — the same
# Owner-approved, never-auto-growing contract the env config expresses — so we build
# an AzureEventConfig from it when no explicit env/fixture config is present. This is
# the bridge that makes "a pinned scope is the only thing the connector ingests" true
# end to end; without it a UI-connected connector is invisible to ingestion.


def _pinned_subscription_ids(record: Dict[str, Any]) -> List[str]:
    """The pinned subscription ids on a connector record, in pinned order.

    Reads ONLY ``record["scopes"]`` (the Owner-pinned set), never
    ``candidate_scopes`` (delegated-but-unapproved), so the "never silently
    growing" discipline (AC4/AC7) holds through the bridge exactly as it does for
    the env config. De-duplicates while preserving order.
    """
    scopes = record.get("scopes")
    if not isinstance(scopes, list):
        return []
    out: List[str] = []
    seen: set = set()
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        sid = str(scope.get("scope_id") or "").strip()
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def config_from_connector_record(
    record: Optional[Dict[str, Any]], *, org_id: Optional[str] = None
) -> Optional[AzureEventConfig]:
    """Build an :class:`AzureEventConfig` from an Integration Hub connector record.

    Returns None when the record is absent, not connected, or has no pinned
    subscriptions — in every one of those cases the connector genuinely has nothing
    to ingest yet, so it contributes nothing (not an error). The service principal
    is NOT read here — it stays in the vault, resolved separately by
    ``get_service_principal`` exactly as the env-config path does.
    """
    if not isinstance(record, dict):
        return None
    status = str(record.get("status") or "").strip().lower()
    if status != "connected":
        return None
    subscriptions = _pinned_subscription_ids(record)
    if not subscriptions:
        return None
    return AzureEventConfig(
        environment=resolve_environment(record.get("environment")),
        mode=str(record.get("mode") or DEFAULT_MODE).strip().lower() or DEFAULT_MODE,
        subscriptions=subscriptions,
        credential_ref=CONNECTOR_ID,
        metadata={"source": "integration_hub"},
    )


def _default_record_loader(org_id: str) -> Optional[Dict[str, Any]]:
    """Read this org's ``azure_events`` connector record from the DB.

    Lazily imports ``app.db`` so this module stays offline-safe at import time
    (only calling the bridge in a DB-available context touches the DB). Any failure
    degrades to None so a DB hiccup leaves Azure out rather than crashing the run.
    """
    try:
        from app import db  # local import: keeps module import offline-safe
    except Exception:  # pragma: no cover - import guard
        return None
    try:
        record = db.org_connector_get(org_id, CONNECTOR_ID)
    except Exception:  # pragma: no cover - DB failure degrades to "not configured"
        logger.exception(
            "Failed to read azure_events connector record for org %s", org_id
        )
        return None
    return dict(record) if record else None


def resolve_azure_event_config(
    org_id: str,
    *,
    env: Optional[Dict[str, str]] = None,
    record_loader: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> Optional[AzureEventConfig]:
    """Resolve the effective Azure config: env/fixture first, else the Hub record.

    Precedence (highest first):
      1. the explicit per-deployment ``AZURE_EVENT_CONFIG`` env / offline fixture
         (``load_azure_event_config``) — an operator override always wins;
      2. the Integration Hub connector record (``config_from_connector_record``) —
         the everyday UI-connected path.

    Returns None when neither yields a config (the connector contributes nothing).
    Both callers that gate Azure ingestion — ``_resolve_azure_events`` (systems set)
    and ``build_ingestor`` (the poller) — resolve through here, so the two can never
    disagree about whether Azure is configured. ``record_loader`` is injectable for
    tests; it defaults to the DB read.
    """
    explicit = load_azure_event_config(org_id, env=env)
    if explicit is not None:
        return explicit
    loader = record_loader or _default_record_loader
    try:
        record = loader(org_id)
    except Exception:  # pragma: no cover - loader failure degrades to "not configured"
        logger.exception(
            "Azure connector record loader failed for org %s", org_id
        )
        return None
    return config_from_connector_record(record, org_id=org_id)
