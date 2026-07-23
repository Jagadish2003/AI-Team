"""MSP-B1 / AT-642 (T2) — AWS authentication & cross-account access.

MSP customers run many AWS accounts behind one management ("hub") identity. This
module implements that access model for the native AWS event connector
(:mod:`discovery.ingest.aws_poll_source`): **one connection, many accounts, each
account a scope** (AT-642). Per managed account the connector obtains temporary,
scoped credentials by having the hub identity call ``sts:AssumeRole`` on a
read-only role in that account; a per-account **direct-keys fallback** covers
accounts that cannot (yet) offer a cross-account role.

Where the credentials live (never in config, never logged)
----------------------------------------------------------
Following the connector conventions, NO AWS secret ever lives in deployment
config or an ``.env`` credential — secrets live in the Fernet-encrypted vault:

* **Hub credentials** — the long-lived access key of the hub identity, vaulted as
  a static credential under the connector id ``aws_events``
  (``username`` = access key id, ``secret`` = secret access key).
* **Direct per-account keys** (fallback) — vaulted under the reserved id
  ``aws_events:account:{account_id}`` (mirroring the Salesforce ``:jwt`` reserved
  id pattern), so each account's keys live in their own row.

Both resolvers are injectable so the whole auth path is unit-testable without a
live vault, and both degrade to ``None`` (never raise, never log the secret) when
a credential is absent. The concrete boto3 STS/service clients are built through
an injectable :class:`AWSClientFactory`, so tests seed fakes and no test needs
boto3 or the network. The one env fallback (CLI/standalone hub keys) reads
``AWS_EVENTS_HUB_ACCESS_KEY_ID`` / ``_SECRET_ACCESS_KEY`` / ``_SESSION_TOKEN``.

The minimal read-only IAM policy the assumed role needs is shipped as a
partner-security artifact at ``deployment/aws_readonly_iam_policy.json`` /
``deployment/AWS_READONLY_IAM_POLICY.md`` (AT-642 AC9).
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .aws_partitions import (
    PARTITION_AWS,
    resolve_partition_for_region,
    resolve_service_endpoint_or_none,
    validate_region,
)

logger = logging.getLogger(__name__)

#: Vault connector id the hub credential is stored under.
HUB_CONNECTOR_ID = "aws_events"

#: Default STS role-session name stamped on every AssumeRole call (visible in the
#: assumed account's CloudTrail, so an MSP auditor sees who assumed the role).
DEFAULT_ROLE_SESSION_NAME = "agentiq-msp-b1"

#: AWS credential-looking config keys that must NEVER appear in account config —
#: a secret belongs in the vault, not in deployment config. Extends the shared
#: ``operational_config.FORBIDDEN_SECRET_KEYS`` with the AWS-specific spellings
#: (which are not caught by the generic set).
_AWS_FORBIDDEN_KEYS: frozenset = frozenset({
    "access_key_id",
    "aws_access_key_id",
    "secret_access_key",
    "aws_secret_access_key",
    "session_token",
    "aws_session_token",
    "secret_key",
})

#: Env fallback for the hub credential (CLI/standalone only — not for production,
#: where the hub key lives in the vault).
_ENV_HUB_ACCESS_KEY = "AWS_EVENTS_HUB_ACCESS_KEY_ID"
_ENV_HUB_SECRET_KEY = "AWS_EVENTS_HUB_SECRET_ACCESS_KEY"
_ENV_HUB_SESSION_TOKEN = "AWS_EVENTS_HUB_SESSION_TOKEN"


class AWSAuthError(RuntimeError):
    """Raised when AWS credentials for an account cannot be resolved.

    Carries a clear, actionable message (which account, which mode) and NEVER any
    secret material. A run degrades to this per-account rather than crashing the
    whole connector — the caller decides whether a scope with no credentials is a
    skip or a hard error.
    """


def account_key_connector_id(account_id: str) -> str:
    """Reserved vault id holding an account's direct read-only access keys."""
    return f"{HUB_CONNECTOR_ID}:account:{account_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Value objects
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AWSCredentials:
    """A resolved AWS credential set (masked in repr — never logged).

    ``source`` records how it was obtained (``'assumed_role'`` / ``'direct_keys'``
    / ``'hub'``) for audit/telemetry, never the secret value itself.
    """

    access_key_id: str
    secret_access_key: str
    session_token: Optional[str] = None
    source: str = ""

    def __post_init__(self) -> None:
        if not self.access_key_id or not self.secret_access_key:
            raise ValueError("AWSCredentials requires an access key id and secret")

    def __repr__(self) -> str:  # never leak the secret in logs / tracebacks
        akid = self.access_key_id
        masked_akid = f"{akid[:4]}…{akid[-2:]}" if len(akid) > 6 else "…"
        return (
            f"AWSCredentials(access_key_id='{masked_akid}', "
            f"secret_access_key='***', session_token={'***' if self.session_token else None}, "
            f"source={self.source!r})"
        )

    def to_boto3_kwargs(self) -> Dict[str, str]:
        """Credential kwargs for ``boto3.client(...)`` / ``boto3.Session(...)``."""
        kwargs = {
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
        }
        if self.session_token:
            kwargs["aws_session_token"] = self.session_token
        return kwargs


@dataclass(frozen=True)
class AWSAccountConfig:
    """One managed AWS account the connector polls (a set of scopes).

    ``role_arn`` is the read-only role the hub assumes in this account; when it is
    absent the account is served by direct per-account keys (the fallback).
    ``external_id`` is the optional STS confused-deputy guard. ``regions`` are the
    regions to poll (empty → the client's default region). Config only — never a
    secret (secrets are vaulted).

    How the account is reached is derived purely from config: an account with a
    ``role_arn`` is assumed into (with a direct-keys fallback), one without uses
    direct per-account keys only. There is no stored/selected "mode" — the access
    concept never enters the ingestion layer.
    """

    account_id: str
    role_arn: Optional[str] = None
    external_id: Optional[str] = None
    regions: Tuple[str, ...] = ()
    partition: str = ""     # "" → derived from regions (default commercial 'aws')
    label: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("AWSAccountConfig.account_id is required")
        # Partition (AT-645): explicit if given, else derived from the region(s);
        # every region is validated against it so a GovCloud region under the
        # commercial partition (or vice-versa) fails at config time, not run time.
        resolved = self.partition or (
            resolve_partition_for_region(self.regions[0]) if self.regions else PARTITION_AWS
        )
        for region in self.regions:
            validate_region(resolved, region)
        object.__setattr__(self, "partition", resolved)

    @property
    def uses_role_assumption(self) -> bool:
        """True when this account is reached by assuming a cross-account role.

        Derived from config (the presence of ``role_arn``) — the decision is a
        function of what is configured, not of any stored access-mode flag.
        """
        return bool(self.role_arn)


# ─────────────────────────────────────────────────────────────────────────────
# Client factory (the boto3 seam — injectable so tests need no boto3/network)
# ─────────────────────────────────────────────────────────────────────────────

class AWSClientFactory(ABC):
    """Builds AWS service clients from resolved credentials.

    The single boto3 seam: production uses :class:`Boto3ClientFactory`; tests
    inject a fake that returns seeded clients, so the auth + poll logic is
    exercised end-to-end with no AWS account.
    """

    @abstractmethod
    def client(
        self, service: str, *, region: Optional[str], credentials: Optional[AWSCredentials]
    ) -> Any:
        """Return a service client (``'sts'`` / ``'cloudwatch'`` / ``'events'`` /
        ``'cloudtrail'``) bound to ``credentials`` in ``region``."""
        raise NotImplementedError


class Boto3ClientFactory(AWSClientFactory):
    """Production :class:`AWSClientFactory` — lazily builds real boto3 clients.

    boto3 is imported lazily so importing this module (and running the offline/
    fixture tests) never requires the SDK; only a genuine live call needs it.
    """

    def client(
        self, service: str, *, region: Optional[str], credentials: Optional[AWSCredentials]
    ) -> Any:
        try:
            import boto3  # lazy — only needed for live ingestion
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise AWSAuthError(
                "boto3 is required for live AWS ingestion but is not installed"
            ) from exc
        kwargs: Dict[str, Any] = {}
        if region:
            kwargs["region_name"] = region
        # Partition-aware endpoint (AT-645): a GovCloud region resolves the
        # aws-us-gov endpoint; None lets boto3's own resolution stand.
        endpoint = resolve_service_endpoint_or_none(service, region)
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        if credentials is not None:
            kwargs.update(credentials.to_boto3_kwargs())
        return boto3.client(service, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Default vault-backed credential resolvers (injectable)
# ─────────────────────────────────────────────────────────────────────────────

HubResolver = Callable[[str], Optional[AWSCredentials]]
AccountKeyResolver = Callable[[str, str], Optional[AWSCredentials]]


def _vault_static_credential(org_id: str, connector_id: str) -> Optional[Tuple[str, str]]:
    """Return ``(username, secret)`` of a vaulted static credential, or None.

    Lazily imports the vault and degrades to ``None`` on any failure (no DB, no
    vault key, not configured) — a credential lookup must never crash the run and
    never surface the secret in a log. Mirrors
    :func:`discovery.ingest.resolve_vault_connector`'s best-effort posture.
    """
    try:
        from app.auth.vault import get_static_credential

        rec = get_static_credential(org_id, connector_id)
    except Exception:  # noqa: BLE001 — degrade, never crash, never log the value
        logger.debug(
            "aws_auth: vault lookup unavailable for %s (org=%s)", connector_id, org_id,
            exc_info=True,
        )
        return None
    if rec is None or not rec.secret:
        return None
    return rec.username, rec.secret


def default_hub_resolver(org_id: str) -> Optional[AWSCredentials]:
    """Resolve the hub credential — vault first, env fallback (CLI/standalone)."""
    found = _vault_static_credential(org_id, HUB_CONNECTOR_ID)
    if found and found[0] and found[1]:
        return AWSCredentials(found[0], found[1], source="hub")
    akid = os.environ.get(_ENV_HUB_ACCESS_KEY)
    secret = os.environ.get(_ENV_HUB_SECRET_KEY)
    if akid and secret:
        return AWSCredentials(
            akid.strip(), secret.strip(),
            session_token=(os.environ.get(_ENV_HUB_SESSION_TOKEN) or None),
            source="hub",
        )
    return None


def default_account_key_resolver(org_id: str, account_id: str) -> Optional[AWSCredentials]:
    """Resolve an account's direct read-only keys from the vault (never env)."""
    found = _vault_static_credential(org_id, account_key_connector_id(account_id))
    if found and found[0] and found[1]:
        return AWSCredentials(found[0], found[1], source="direct_keys")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# The authenticator
# ─────────────────────────────────────────────────────────────────────────────

class AWSAuthenticator:
    """Resolves per-account AWS credentials for the native connector (AT-642).

    Preferred path per account: the hub identity calls ``sts:AssumeRole`` on the
    account's read-only ``role_arn`` and the connector uses the returned temporary
    credentials. Fallback: direct per-account keys from the vault — used when the
    account has no ``role_arn`` (direct keys only), OR when an AssumeRole attempt
    fails and direct keys happen to be configured (so one misconfigured role never
    blocks an otherwise-reachable account).

    Resolved credentials are cached per ``(org_id, account_id)`` for the lifetime
    of the authenticator (one AssumeRole per account per run, not per surface/
    region); :meth:`clear_cache` drops them.
    """

    def __init__(
        self,
        *,
        client_factory: Optional[AWSClientFactory] = None,
        hub_resolver: HubResolver = default_hub_resolver,
        account_key_resolver: AccountKeyResolver = default_account_key_resolver,
        role_session_name: str = DEFAULT_ROLE_SESSION_NAME,
    ) -> None:
        self.client_factory = client_factory or Boto3ClientFactory()
        self._hub_resolver = hub_resolver
        self._account_key_resolver = account_key_resolver
        self.role_session_name = role_session_name
        self._cache: Dict[Tuple[str, str], AWSCredentials] = {}

    def clear_cache(self) -> None:
        """Drop cached per-account credentials (e.g. between runs)."""
        self._cache.clear()

    def credentials_for(self, org_id: str, account: AWSAccountConfig) -> AWSCredentials:
        """Resolve usable credentials for one account (assume-role, else direct keys).

        Raises :class:`AWSAuthError` — never returns invalid credentials — when
        neither path yields a credential, so a scope is either authenticated or
        loudly unauthenticated.
        """
        cache_key = (org_id, account.account_id)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # The path is chosen from CONFIG (whether a role_arn is set), not from any
        # stored access-mode: an account with a role is assumed into (direct-keys
        # fallback), one without uses direct per-account keys only.
        creds: Optional[AWSCredentials]
        if account.role_arn:
            creds = self._try_assume_role(org_id, account)
            if creds is None:
                # Fallback: direct per-account keys if configured.
                creds = self._account_key_resolver(org_id, account.account_id)
                if creds is not None:
                    logger.info(
                        "aws_auth: account %s fell back to direct keys "
                        "(AssumeRole unavailable)", account.account_id,
                    )
        else:
            creds = self._account_key_resolver(org_id, account.account_id)

        if creds is None:
            raise AWSAuthError(
                f"no AWS credentials for account {account.account_id} — configure "
                f"the hub role assumption or vault the account's read-only keys"
            )
        self._cache[cache_key] = creds
        return creds

    def _try_assume_role(
        self, org_id: str, account: AWSAccountConfig
    ) -> Optional[AWSCredentials]:
        """Assume the account's read-only role from the hub identity.

        Returns ``None`` (so the caller can fall back to direct keys) when the hub
        credential is missing or the AssumeRole call fails — never raises and never
        logs the secret.
        """
        hub = self._hub_resolver(org_id)
        if hub is None:
            logger.info(
                "aws_auth: no hub credential for org %s — cannot AssumeRole into %s",
                org_id, account.account_id,
            )
            return None

        region = account.regions[0] if account.regions else None
        try:
            sts = self.client_factory.client("sts", region=region, credentials=hub)
            params: Dict[str, Any] = {
                "RoleArn": account.role_arn,
                "RoleSessionName": self.role_session_name,
            }
            if account.external_id:
                params["ExternalId"] = account.external_id
            resp = sts.assume_role(**params)
        except Exception as exc:  # noqa: BLE001 — degrade to direct-keys fallback
            logger.warning(
                "aws_auth: AssumeRole into %s failed (%s: %s) — will try direct keys",
                account.account_id, type(exc).__name__, exc,
            )
            return None

        c = (resp or {}).get("Credentials") or {}
        akid = c.get("AccessKeyId")
        secret = c.get("SecretAccessKey")
        token = c.get("SessionToken")
        if not akid or not secret:
            logger.warning(
                "aws_auth: AssumeRole into %s returned no usable credentials",
                account.account_id,
            )
            return None
        return AWSCredentials(akid, secret, session_token=token, source="assumed_role")


# ─────────────────────────────────────────────────────────────────────────────
# Account configuration loading (config, never scanning; never a secret)
# ─────────────────────────────────────────────────────────────────────────────

def _forbidden_keys(entry: Dict[str, Any]) -> List[str]:
    """Return the secret-looking keys present in a raw account-config entry.

    Combines the shared operational-config forbidden set with the AWS-specific
    key spellings, so a pasted access key in config is rejected loudly (AC — no
    secret in config). Only KEY names are returned, never values.
    """
    try:
        from .operational_config import FORBIDDEN_SECRET_KEYS as _SHARED
    except Exception:  # pragma: no cover - import guard
        _SHARED = frozenset()
    forbidden = set(_SHARED) | set(_AWS_FORBIDDEN_KEYS)
    return sorted(k for k in entry.keys() if str(k).strip().lower() in forbidden)


def parse_account_config(entry: Dict[str, Any]) -> AWSAccountConfig:
    """Build an :class:`AWSAccountConfig` from one raw config dict (secret-free).

    Raises ``ValueError`` when the entry embeds a credential inline — naming the
    offending keys (never the values) so the operator can move them to the vault.
    """
    bad = _forbidden_keys(entry)
    if bad:
        raise ValueError(
            f"AWS account config for {entry.get('account_id', '?')!r} embeds "
            f"credential-looking keys {bad}; AWS secrets must be vaulted, never in config"
        )
    regions = entry.get("regions") or ([entry["region"]] if entry.get("region") else [])
    return AWSAccountConfig(
        account_id=str(entry["account_id"]),
        role_arn=entry.get("role_arn"),
        external_id=entry.get("external_id"),
        regions=tuple(str(r) for r in regions),
        partition=str(entry.get("partition", "") or ""),
        label=entry.get("label"),
    )


def load_aws_accounts(
    raw: Optional[List[Dict[str, Any]]] = None,
    *,
    env: Optional[Dict[str, str]] = None,
) -> List[AWSAccountConfig]:
    """Load the managed-account configs (explicit list, else ``AWS_EVENT_ACCOUNTS``).

    ``raw`` is an explicit list of account dicts (e.g. an offline fixture). When
    omitted, the ``AWS_EVENT_ACCOUNTS`` env var (a JSON array) is read — config,
    never network scanning (the connector conventions). Every entry is validated
    secret-free by :func:`parse_account_config`.
    """
    if raw is None:
        import json

        environ = env if env is not None else os.environ
        blob = environ.get("AWS_EVENT_ACCOUNTS", "").strip()
        raw = json.loads(blob) if blob else []
    if not isinstance(raw, list):
        raise ValueError("AWS account config must be a list of account objects")
    return [parse_account_config(dict(e)) for e in raw]
