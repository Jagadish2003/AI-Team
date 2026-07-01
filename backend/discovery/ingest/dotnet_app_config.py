"""
R17-A4 / T3 — Per-deployment configuration of .NET application targets + vault
credential resolution.

The .NET counterpart to :mod:`discovery.ingest.java_app_config`. Phase one is
**configured, not auto-discovered** (R17-A4 §3): AgentIQ does NOT scan the network
to find .NET apps; each deployment explicitly declares which applications are in
scope, and their credentials live in the vault, never in config. A .NET target
declares a ``diagnostics_url`` (its ASP.NET Core health checks + EventCounters
surface) where a Java target declares an ``actuator_url``.

The credential rule — secrets live in the vault, never in config or logs
------------------------------------------------------------------------
The configuration carries only a ``credential_ref`` naming where the secret lives;
:func:`resolve_secret` reads the decrypted value from the per-run credential
context (the same mechanism the SaaS connectors use), with an env var fallback for
CLI use. Any target entry carrying an inline secret-looking field is REJECTED so a
pasted credential surfaces as a rejected target rather than a silently-persisted
plaintext secret. The resolved secret is never attached to the target, logged, or
echoed.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import get_live_connector, is_live

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dotnet_app_sample.json"

#: Env var (live mode) holding a JSON array of target configs for the deployment.
_TARGETS_ENV = "DOTNET_APP_TARGETS"

#: Default vault connector key used when a target does not name its own
#: ``credential_ref``. Mirrors the connector_id of the ingestor.
DEFAULT_CREDENTIAL_REF = "dotnet_app"

#: Field names that must NEVER appear in a target config entry — a credential
#: belongs in the vault, not in deployment config. A target carrying any of these
#: is rejected by :func:`load_targets` so a pasted secret cannot slip through into
#: config files or logs.
_FORBIDDEN_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "token",
        "access_token",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "basic_auth",
        "authorization",
        "bearer",
        "credentials",
    }
)


class DotNetAppConfigError(Exception):
    """Raised when the .NET application target configuration is invalid."""


@dataclass(frozen=True)
class DotNetAppTarget:
    """One configured .NET application AgentIQ is allowed to read (R17-A4 §3).

    Pure, non-secret configuration. The credential needed to read the application's
    health/diagnostics surface / logs is referenced by ``credential_ref`` and
    resolved from the vault at ingest time — never stored on the target.
    """

    app_id: str
    name: str
    diagnostics_url: str
    log_source: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    #: Vault connector key naming where this app's secret lives. None means the
    #: endpoint needs no credential (e.g. an internal unauthenticated surface).
    credential_ref: Optional[str] = DEFAULT_CREDENTIAL_REF

    def __post_init__(self) -> None:
        if not self.app_id or not isinstance(self.app_id, str):
            raise DotNetAppConfigError("DotNetAppTarget.app_id must be a non-empty string")
        # A target must expose at least one operational surface (diagnostics or logs).
        if not (self.diagnostics_url or self.log_source):
            raise DotNetAppConfigError(
                f"DotNetAppTarget '{self.app_id}' declares neither a diagnostics_url "
                "nor a log_source — nothing to read"
            )

    @property
    def service(self) -> str:
        """The service name used to link this app's signal to other systems.

        Falls back to ``app_id`` when ``metadata.service`` is absent, so cross-
        system corroboration ("the same service") always has a key.
        """
        svc = self.metadata.get("service") if isinstance(self.metadata, dict) else None
        return str(svc) if svc else self.app_id


def _coerce_target(entry: Dict[str, Any]) -> DotNetAppTarget:
    """Build a :class:`DotNetAppTarget` from a raw config dict, rejecting inline secrets."""
    if not isinstance(entry, dict):
        raise DotNetAppConfigError("each .NET app target must be a JSON object")

    inline_secrets = sorted(
        k for k in entry.keys() if str(k).strip().lower() in _FORBIDDEN_SECRET_KEYS
    )
    if inline_secrets:
        # Name only the offending KEYS, never the values, so a rejected secret is
        # never written to the log either.
        raise DotNetAppConfigError(
            f".NET app target '{entry.get('app_id', '?')}' contains inline "
            f"credential field(s) {inline_secrets}; store credentials in the vault "
            "and reference them via 'credential_ref' instead."
        )

    metadata = entry.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    credential_ref = (
        entry["credential_ref"] if "credential_ref" in entry else DEFAULT_CREDENTIAL_REF
    )

    return DotNetAppTarget(
        app_id=str(entry.get("app_id", "")).strip(),
        name=str(entry.get("name", entry.get("app_id", ""))).strip(),
        diagnostics_url=str(entry.get("diagnostics_url", "")).strip(),
        log_source=str(entry.get("log_source", "")).strip(),
        metadata=metadata,
        credential_ref=credential_ref,
    )


def _raw_target_entries(org_id: str) -> List[Dict[str, Any]]:
    """Return the raw (dict) target entries for an org — config, never scanning."""
    if not is_live():
        if not FIXTURE_PATH.exists():
            return []
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return list(data.get("targets", []))

    raw = os.getenv(_TARGETS_ENV, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise DotNetAppConfigError(
            f"{_TARGETS_ENV} is not valid JSON: {type(exc).__name__}"
        ) from exc
    if not isinstance(parsed, list):
        raise DotNetAppConfigError(f"{_TARGETS_ENV} must be a JSON array of targets")
    return [e for e in parsed if isinstance(e, dict)]


def load_targets(org_id: str) -> List[DotNetAppTarget]:
    """Return the configured .NET application targets for ``org_id`` (no discovery).

    A single malformed/insecure entry is skipped (logged by app_id, never by value)
    so one bad target does not block the rest — the project's "degrade, don't crash"
    ingestion rule.
    """
    targets: List[DotNetAppTarget] = []
    seen: set[str] = set()
    for entry in _raw_target_entries(org_id):
        try:
            target = _coerce_target(entry)
        except DotNetAppConfigError as exc:
            logger.warning("dotnet_app: skipping invalid target (org=%s): %s", org_id, exc)
            continue
        if target.app_id in seen:
            logger.warning(
                "dotnet_app: duplicate target app_id '%s' (org=%s) — keeping the first",
                target.app_id,
                org_id,
            )
            continue
        seen.add(target.app_id)
        targets.append(target)
    return targets


def resolve_secret(
    org_id: str,
    target: DotNetAppTarget,
    *,
    connector_lookup: Callable[[str], Optional[Dict[str, str]]] = get_live_connector,
    env: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Resolve a target's credential from the vault — never from config or logs.

    Resolution mirrors the SaaS connectors: the per-run credential context first (a
    DB-sourced vault token, isolated per org/run via ``contextvars``), then an env
    var (``DOTNET_APP_TOKEN`` for the default ref, else ``{REF}_TOKEN``) as a
    CLI/standalone fallback. Returns ``None`` when the target needs no credential.
    The returned secret is handed straight to the client and never attached to the
    target, logged, or echoed. ``connector_lookup``/``env`` are injectable so tests
    can exercise resolution without a live vault.
    """
    ref = target.credential_ref
    if not ref:
        return None  # endpoint declared as needing no credential

    cred = None
    try:
        cred = connector_lookup(ref)
    except Exception:  # noqa: BLE001 — credential lookup must not break the run.
        logger.warning(
            "dotnet_app: credential lookup failed for target '%s' (org=%s); "
            "trying env fallback",
            target.app_id,
            org_id,
        )
    if cred and cred.get("token"):
        return str(cred["token"]).strip()

    environ = env if env is not None else os.environ
    env_key = (
        "DOTNET_APP_TOKEN"
        if ref == DEFAULT_CREDENTIAL_REF
        else f"{ref.upper()}_TOKEN"
    )
    token = environ.get(env_key)
    return token.strip() if token else None
