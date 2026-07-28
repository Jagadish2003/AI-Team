"""MSP-B1 — AWS Event Connector per-org configuration resolution.

The AWS half of the MSP-B1/B2 matched pair's *config* surface, deliberately built
as the mirror image of :mod:`discovery.ingest.azure_events_config` so the two
connectors resolve "what am I allowed to poll for this org?" the SAME way. Before
this module existed, :func:`discovery.ingest.aws_auth.load_aws_accounts` read a
process-wide ``AWS_EVENT_ACCOUNTS`` env var and nothing ever read back the
accounts an Owner pinned through the Integration Hub — so a UI-connected AWS
connector was invisible to ingestion (gap G2).

What this module owns
---------------------
* the NON-SECRET, per-deployment configuration the connector needs BEFORE it
  authenticates: the partition, the managed-account set, and each account's
  role ARN / external id / regions;
* the **Integration Hub bridge** — turning the Owner-pinned scopes on this org's
  ``aws_events`` connector record into :class:`AWSAccountConfig` objects;
* the precedence rule between the two (operator override wins).

Scope discipline (MSP-B1 "one connection, many accounts" / B13 AC4): the account
set is CONFIGURED, never auto-discovered. An account the hub identity *could*
assume into but which nobody pinned is a CANDIDATE, never ingested. This is the
platform's forward-only activation principle, held for AWS estates exactly as
:mod:`azure_events_config` holds it for Azure subscriptions.

Security: a config entry carries NO credentials — the hub access key and any
per-account direct keys live in the per-org vault (see :mod:`aws_auth`). An entry
that embeds an inline secret is rejected by
:func:`discovery.ingest.aws_auth.parse_account_config`, which combines the SHARED
``operational_config.find_inline_secret_keys`` guard with the AWS-specific key
spellings — the "no secret in config" rule cannot drift per connector.

On ``external_id`` (gap G3)
---------------------------
The STS ExternalId is the confused-deputy guard named in the account role's trust
policy. It is *configuration*, not a credential: AWS documents it as a shared
identifier the customer gives their MSP, and an AssumeRole call FAILS without it
when the trust policy requires one. It must therefore survive from the pin flow
through to run time, so it is persisted on the connector record next to the role
ARN and read back here. It is still never echoed by an API response — the scope
views expose only ``external_id_set`` (see ``app/routes_cloud_connectors.py``).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .aws_auth import AWSAccountConfig, parse_account_config
from .aws_partitions import PARTITION_AWS, PartitionError, partition_for

try:
    from . import is_live
except Exception:  # pragma: no cover - import shim
    from discovery.ingest import is_live  # type: ignore

logger = logging.getLogger(__name__)

#: The connector id — the vault key the hub credential is stored under, the
#: Integration Hub system id, and the ``(org, connector)`` checkpoint key.
CONNECTOR_ID = "aws_events"

#: Deterministic offline config (used when ``INGEST_MODE`` is not ``live``).
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "aws_events_config_sample.json"

#: Env var (live mode) holding the per-deployment managed-account config (JSON).
#: Accepts EITHER a plain array of account objects (applied to every org — the
#: original MSP-B1 shape, kept working) OR an org-keyed object with a
#: ``default``/``*`` fallback, matching ``ENTERPRISE_APP_REPOS``.
_CONFIG_ENV = "AWS_EVENT_ACCOUNTS"


class AWSEventConfigError(Exception):
    """Raised when the AWS Event Connector configuration is invalid."""


@dataclass(frozen=True)
class AWSEventConfig:
    """Per-org AWS Event Connector configuration (non-secret).

    ``accounts`` is the PINNED, Owner-approved set — the only accounts the
    connector polls. It never grows automatically (B13 AC4).
    """

    partition: str
    accounts: List[AWSAccountConfig]
    credential_ref: str = CONNECTOR_ID
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            partition_for(self.partition)
        except PartitionError as exc:
            raise AWSEventConfigError(str(exc)) from exc
        if not isinstance(self.accounts, list):
            raise AWSEventConfigError("accounts must be a list of account configs")

    @property
    def account_ids(self) -> List[str]:
        """The pinned account ids, in pinned order (a copy — never mutated)."""
        return [a.account_id for a in self.accounts]

    def is_pinned(self, account_id: str) -> bool:
        """True when ``account_id`` is in the Owner-approved pinned set."""
        return str(account_id) in set(self.account_ids)

    def newly_discovered(self, candidate_account_ids: List[str]) -> List[str]:
        """Candidates that are NOT pinned — reachable but pending Owner approval.

        Reported for Owner review and run-health visibility; the connector never
        polls them. The "never silently growing" report, mirroring
        ``AzureEventConfig.newly_delegated``.
        """
        pinned = set(self.account_ids)
        seen: set = set()
        out: List[str] = []
        for candidate in candidate_account_ids or []:
            cid = str(candidate)
            if cid not in pinned and cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out


# ── Loading (config / fixture — never network discovery) ─────────────────────────


def _coerce_config(entry: Any) -> AWSEventConfig:
    """Build an :class:`AWSEventConfig` from a raw config value.

    Accepts either a bare list of account entries (partition derived from the
    accounts) or an object with ``partition``/``accounts``. Every account entry is
    validated secret-free by :func:`aws_auth.parse_account_config`.
    """
    if isinstance(entry, list):
        entry = {"accounts": entry}
    if not isinstance(entry, dict):
        raise AWSEventConfigError("AWS event config must be a JSON object or array")

    raw_accounts = entry.get("accounts")
    if raw_accounts is None:
        raw_accounts = []
    if not isinstance(raw_accounts, list):
        raise AWSEventConfigError("'accounts' must be a JSON array of account objects")

    partition = str(entry.get("partition") or "").strip()

    accounts: List[AWSAccountConfig] = []
    for raw in raw_accounts:
        if not isinstance(raw, dict):
            raise AWSEventConfigError("each AWS account config must be a JSON object")
        item = dict(raw)
        # A connection-level partition applies to every account that does not
        # state its own, so GovCloud is selected once per connection (AT-645).
        if partition and not str(item.get("partition") or "").strip():
            item["partition"] = partition
        try:
            accounts.append(parse_account_config(item))
        except (ValueError, PartitionError) as exc:
            raise AWSEventConfigError(str(exc)) from exc

    resolved_partition = partition or (
        accounts[0].partition if accounts else PARTITION_AWS
    )

    metadata = entry.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    return AWSEventConfig(
        partition=resolved_partition,
        accounts=accounts,
        credential_ref=str(entry.get("credential_ref", CONNECTOR_ID)).strip() or CONNECTOR_ID,
        metadata=metadata,
    )


def _select_for_org(data: Any, org_id: str) -> Optional[Any]:
    """Pick the config entry for ``org_id`` from an org-keyed object, array, or flat one."""
    # A plain array is the original MSP-B1 shape — applied to every org.
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise AWSEventConfigError("AWS event config must be a JSON object or array")
    # A flat config carries connector fields directly.
    if "accounts" in data or "partition" in data:
        return data
    # Otherwise it is keyed by org id, with a default/* fallback.
    if org_id in data:
        return data[org_id]
    for fallback in ("default", "*"):
        if fallback in data:
            return data[fallback]
    return None


def _raw_config_entry(org_id: str, env: Optional[Dict[str, str]] = None) -> Optional[Any]:
    """Return the raw config value for an org — config/fixture, never scanning."""
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
        raise AWSEventConfigError(
            f"{_CONFIG_ENV} is not valid JSON: {type(exc).__name__}"
        ) from exc
    return _select_for_org(parsed, org_id)


def load_aws_event_config(
    org_id: str, *, env: Optional[Dict[str, str]] = None
) -> Optional[AWSEventConfig]:
    """Load the explicit AWS Event Connector config for ``org_id`` (or None).

    Returns None when no config is present for the org — the connector simply
    contributes nothing (a not-configured connector is not an error). Raises
    :class:`AWSEventConfigError` on a present-but-invalid config (unknown
    partition, contradictory region, or an inline secret).
    """
    entry = _raw_config_entry(org_id, env=env)
    if entry is None:
        return None
    config = _coerce_config(entry)
    if not config.accounts:
        # An explicitly-empty account set means "nothing pinned" — treat it the
        # same as unconfigured so the Hub record can still supply the estate.
        return None
    return config


# ── Integration Hub bridge (MSP-B13) ─────────────────────────────────────────────
#
# The env/fixture config above is the per-deployment override. The everyday path is
# an Owner connecting AWS through the Integration Hub: the connect flow
# (routes_cloud_connectors._store_aws_connection) writes the non-secret
# ``partition`` onto this org's connector record and vaults the hub access key, and
# pinning an account (routes_cloud_connectors.pin_scope) appends it to
# ``record["scopes"]``. That IS the managed-account set — the same Owner-approved,
# never-auto-growing contract the env config expresses — so we build an
# AWSEventConfig from it when no explicit env/fixture config is present. This is the
# bridge that makes "a pinned scope is the only thing the connector polls" true end
# to end; without it a UI-connected connector is invisible to ingestion (gap G2).


def _account_from_scope(
    scope: Dict[str, Any], *, default_partition: str
) -> Optional[AWSAccountConfig]:
    """Build one :class:`AWSAccountConfig` from a pinned connector-record scope.

    Returns ``None`` for an unusable scope (no account id, or a partition/region
    contradiction persisted by an older record) rather than failing the whole
    estate — one bad scope must not hide every other account. The failure is
    logged loudly.
    """
    account_id = str(scope.get("scope_id") or "").strip()
    if not account_id:
        return None
    regions = tuple(
        str(r).strip() for r in (scope.get("regions") or []) if str(r).strip()
    )
    try:
        return AWSAccountConfig(
            account_id=account_id,
            role_arn=(str(scope.get("role_arn") or "").strip() or None),
            # Gap G3: the ExternalId is captured at pin time and MUST reach the
            # AssumeRole call at run time, or a trust policy that requires it
            # rejects every poll. Persisted as config (never echoed by the API).
            external_id=(str(scope.get("external_id") or "").strip() or None),
            regions=regions,
            partition=str(scope.get("partition") or default_partition or "").strip(),
            label=(scope.get("label") or None),
        )
    except (ValueError, PartitionError) as exc:
        logger.warning(
            "aws_events: pinned account %s has an invalid scope config (%s) — "
            "skipped; other accounts continue",
            account_id, exc,
        )
        return None


def config_from_connector_record(
    record: Optional[Dict[str, Any]], *, org_id: Optional[str] = None
) -> Optional[AWSEventConfig]:
    """Build an :class:`AWSEventConfig` from an Integration Hub connector record.

    Returns None when the record is absent, not connected, or has no pinned
    accounts — in every one of those cases the connector genuinely has nothing to
    poll yet, so it contributes nothing (not an error). The hub credential is NOT
    read here — it stays in the vault, resolved separately by
    :class:`aws_auth.AWSAuthenticator`, exactly as the env-config path does.
    """
    if not isinstance(record, dict):
        return None
    status = str(record.get("status") or "").strip().lower()
    if status != "connected":
        return None

    default_partition = str(record.get("partition") or PARTITION_AWS).strip() or PARTITION_AWS
    scopes = record.get("scopes")
    if not isinstance(scopes, list):
        return None

    accounts: List[AWSAccountConfig] = []
    seen: set = set()
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        account = _account_from_scope(scope, default_partition=default_partition)
        if account is None or account.account_id in seen:
            continue
        seen.add(account.account_id)
        accounts.append(account)

    if not accounts:
        return None
    try:
        return AWSEventConfig(
            partition=default_partition,
            accounts=accounts,
            credential_ref=CONNECTOR_ID,
            metadata={"source": "integration_hub"},
        )
    except AWSEventConfigError:
        logger.exception(
            "aws_events: connector record for org %s carries an invalid partition", org_id
        )
        return None


def _default_record_loader(org_id: str) -> Optional[Dict[str, Any]]:
    """Read this org's ``aws_events`` connector record from the DB.

    Lazily imports ``app.db`` so this module stays offline-safe at import time.
    Any failure degrades to None so a DB hiccup leaves AWS out rather than
    crashing the run.
    """
    try:
        from app import db  # local import: keeps module import offline-safe
    except Exception:  # pragma: no cover - import guard
        return None
    try:
        record = db.org_connector_get(org_id, CONNECTOR_ID)
    except Exception:  # pragma: no cover - DB failure degrades to "not configured"
        logger.exception(
            "Failed to read aws_events connector record for org %s", org_id
        )
        return None
    return dict(record) if record else None


def resolve_aws_event_config(
    org_id: str,
    *,
    env: Optional[Dict[str, str]] = None,
    record_loader: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> Optional[AWSEventConfig]:
    """Resolve the effective AWS config: env/fixture first, else the Hub record.

    Precedence (highest first):
      1. the explicit per-deployment ``AWS_EVENT_ACCOUNTS`` env / offline fixture
         (:func:`load_aws_event_config`) — an operator override always wins;
      2. the Integration Hub connector record
         (:func:`config_from_connector_record`) — the everyday UI-connected path.

    Returns None when neither yields a config (the connector contributes nothing).
    Both callers that gate AWS ingestion — ``_resolve_aws_events`` (systems set) and
    ``build_ingestor`` (the poller) — resolve through here, so the two can never
    disagree about whether AWS is configured. ``record_loader`` is injectable for
    tests; it defaults to the DB read.
    """
    explicit = load_aws_event_config(org_id, env=env)
    if explicit is not None:
        return explicit
    loader = record_loader or _default_record_loader
    try:
        record = loader(org_id)
    except Exception:  # pragma: no cover - loader failure degrades to "not configured"
        logger.exception("AWS connector record loader failed for org %s", org_id)
        return None
    return config_from_connector_record(record, org_id=org_id)
