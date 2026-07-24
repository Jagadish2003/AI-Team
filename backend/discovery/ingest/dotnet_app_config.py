"""
R17-A4 / T3 — Per-deployment configuration of .NET application targets + vault
credential resolution.

The .NET counterpart to :mod:`discovery.ingest.java_app_config`. Phase one is
**configured, not auto-discovered** (R17-A4 §3): AgentIQ does NOT scan the network
to find .NET apps; each deployment explicitly declares which applications are in
scope, and their credentials live in the vault, never in config (AC4). The
security-critical secret-rejection and credential-resolution logic is the SHARED
:mod:`discovery.ingest.operational_config` — identical to Java, so it cannot drift
between the two platforms. Only the target dataclass differs: a .NET target names
a ``diagnostics_url`` (its ASP.NET Core health checks + EventCounters/diagnostics
surface) where a Java target names an ``actuator_url``.

What a target declares
----------------------
Each :class:`DotNetAppTarget` names exactly one running .NET application AgentIQ is
allowed to read:

  * ``app_id``          — stable identity used as the artifact-id prefix and the
                          per-app checkpoint key (so a signal traces back to the
                          right service — R16-B1).
  * ``diagnostics_url`` — base URL of the .NET health/diagnostics surface (ASP.NET
                          Core health checks + EventCounters/diagnostics). The
                          OPERATIONAL surface only (R17-A4 §1) — never source code.
  * ``log_source``      — where the application's logs are read from (a file path
                          or a log endpoint).
  * ``environment``     — the deployment environment (``production`` / ``staging``
                          / ...), so a signal can be scoped to the right stage.
  * ``metadata``        — non-secret service metadata (service name, owning team,
                          runtime) used to link the signal back to the right
                          service for cross-system corroboration (R17-A4 §3).
  * ``credential_ref``  — a *reference* (a vault connector key), NOT a credential.
                          The actual secret — API key, token, or certificate
                          reference — is resolved at ingest time from the
                          credential vault, never stored here.

The credential rule (AC4) — secrets live in the vault, never in config or logs
------------------------------------------------------------------------------
API keys, tokens, usernames, passwords, certificate references, connection
strings, and any other secret MUST NOT appear in the target configuration, in
code, or in logs. The configuration carries only a ``credential_ref`` naming
where the secret lives; :func:`resolve_secret` reads the decrypted value from the
per-run credential context (vault only, **fail-closed** — no env fallback;
R191-H1 / T1). To make the rule enforceable rather than merely documented,
:func:`load_targets` REJECTS any target entry that carries an inline
secret-looking field (the shared ``FORBIDDEN_SECRET_KEYS``) — a misconfigured
deployment that pastes a credential into config surfaces as a rejected target
rather than silently persisting a plaintext secret.

Safe failure reporting
----------------------
When a diagnostics or log endpoint fails, error handling must report only SAFE
information — application id, endpoint type, and an error *category* — never
credentials or sensitive connection strings (which frequently appear inside raw
driver/HTTP exception messages). :func:`safe_endpoint_error` builds exactly that
credential-free record; callers log it instead of the raw exception.

Offline vs live
---------------
Offline (default, ``INGEST_MODE`` != ``live``): targets are read from the
deterministic fixture ``fixtures/dotnet_app_sample.json`` — parity with the other
connectors, so the whole pipeline runs without any credentials. Live: targets are
read from the ``DOTNET_APP_TARGETS`` env var (a JSON array of target configs,
secret-free) configured per deployment.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import get_live_connector, is_live
from .operational_config import (
    find_inline_secret_keys,
    resolve_target_secret,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dotnet_app_sample.json"

#: Env var (live mode) holding a JSON array of target configs for the deployment.
_TARGETS_ENV = "DOTNET_APP_TARGETS"

#: Default vault connector key used when a target does not name its own
#: ``credential_ref``. Mirrors the connector_id of the ingestor.
DEFAULT_CREDENTIAL_REF = "dotnet_app"


class DotNetAppConfigError(Exception):
    """Raised when the .NET application target configuration is invalid."""


@dataclass(frozen=True)
class DotNetAppTarget:
    """One configured .NET application AgentIQ is allowed to read (R17-A4 §3).

    A target is pure, non-secret configuration. The credential needed to read the
    application's health/diagnostics surface / logs is referenced by
    ``credential_ref`` and resolved from the vault at ingest time — it is never
    stored on the target.
    """

    app_id: str
    name: str
    diagnostics_url: str
    log_source: str
    environment: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    #: Vault connector key naming where this app's secret lives. None means the
    #: endpoint needs no credential (e.g. an internal unauthenticated surface).
    credential_ref: Optional[str] = DEFAULT_CREDENTIAL_REF

    def __post_init__(self) -> None:
        if not self.app_id or not isinstance(self.app_id, str):
            raise DotNetAppConfigError("DotNetAppTarget.app_id must be a non-empty string")
        # A target must expose at least one operational surface (diagnostics or
        # logs); neither is required individually, but a target with neither reads
        # nothing.
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
    """Build a :class:`DotNetAppTarget` from a raw config dict, enforcing AC4.

    Rejects any entry carrying an inline secret-looking field — a credential must
    be resolved from the vault via ``credential_ref``, never embedded in config.
    """
    if not isinstance(entry, dict):
        raise DotNetAppConfigError("each .NET app target must be a JSON object")

    inline_secrets = find_inline_secret_keys(entry)
    if inline_secrets:
        # Do NOT echo the values — only the offending key names — so a rejected
        # secret is never written to the log either (AC4).
        raise DotNetAppConfigError(
            f".NET app target '{entry.get('app_id', '?')}' contains inline "
            f"credential field(s) {inline_secrets}; store credentials in the vault "
            "and reference them via 'credential_ref' instead."
        )

    metadata = entry.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    # credential_ref is optional; default to the shared dotnet_app vault key. An
    # explicit null means the endpoint needs no credential.
    credential_ref = (
        entry["credential_ref"] if "credential_ref" in entry else DEFAULT_CREDENTIAL_REF
    )

    return DotNetAppTarget(
        app_id=str(entry.get("app_id", "")).strip(),
        name=str(entry.get("name", entry.get("app_id", ""))).strip(),
        diagnostics_url=str(entry.get("diagnostics_url", "")).strip(),
        log_source=str(entry.get("log_source", "")).strip(),
        environment=str(entry.get("environment", "")).strip(),
        metadata=metadata,
        credential_ref=credential_ref,
    )


def _raw_target_entries(org_id: str) -> List[Dict[str, Any]]:
    """Return the raw (dict) target entries for an org — config, never scanning.

    Offline: the deterministic fixture. Live: the ``DOTNET_APP_TARGETS`` env JSON
    array configured per deployment. Either source is explicit configuration; no
    network discovery is ever performed (R17-A4 §3).
    """
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
    """Return the configured .NET application targets for ``org_id``.

    The customer decides which applications AgentIQ may read; this returns exactly
    that configured set (no auto-discovery). A single malformed/insecure entry is
    skipped (logged by app_id / offending key, never by value) so one bad target
    does not block the rest — matching the project's "degrade, don't crash"
    ingestion rule.
    """
    targets: List[DotNetAppTarget] = []
    seen: set[str] = set()
    for entry in _raw_target_entries(org_id):
        try:
            target = _coerce_target(entry)
        except DotNetAppConfigError as exc:
            # Message names the app_id / offending key only — never a secret value.
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
    """Resolve a target's credential from the vault — vault only, fail-closed (AC4).

    Delegates to the shared :func:`operational_config.resolve_target_secret` (the
    identical vault-only, fail-closed resolution used by the Java ingestor), so the
    credential handling cannot diverge between platforms. There is **no env
    fallback** (R191-H1 / T1 — F1 fix): a target that declares a ``credential_ref``
    with no vault token raises
    :class:`operational_config.OperationalCredentialMissing` and the ingestor
    fail-closes for that target. Returns ``None`` only when the target needs no
    credential (``credential_ref`` is None). The resolved secret is handed straight
    to the HTTP/log client and is never attached to the target, logged, or echoed.

    ``env`` is accepted for backward-compatible call signatures but is intentionally
    **never read** — there is no environment credential path (R191-H1 / T1).
    """
    _ = env  # no environment credential path — accepted for signature compatibility
    return resolve_target_secret(
        org_id,
        app_id=target.app_id,
        credential_ref=target.credential_ref,
        connector_lookup=connector_lookup,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Safe failure reporting (AC4 — never log credentials / connection strings)
# ─────────────────────────────────────────────────────────────────────────────

def classify_endpoint_error(exc: BaseException) -> str:
    """Map an endpoint failure to a SAFE, credential-free error category.

    The category is derived by inspecting the exception type/message, but ONLY the
    category string is ever returned — the raw message (which can embed a
    connection string or credential) is never surfaced. Categories:
    ``timeout`` | ``connection_error`` | ``auth_error`` | ``tls_error`` |
    ``http_error`` | ``parse_error`` | ``unknown_error``.
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timeout" in msg or "timed out" in msg:
        return "timeout"
    if any(t in name for t in ("ssl", "certificate")) or "certificate" in msg or "tls" in msg:
        return "tls_error"
    if (
        "auth" in name
        or "unauthorized" in msg
        or "forbidden" in msg
        or " 401" in f" {msg}"
        or " 403" in f" {msg}"
    ):
        return "auth_error"
    if "connection" in name or "connection" in msg or "refused" in msg or "unreachable" in msg:
        return "connection_error"
    if "http" in name or any(code in msg for code in ("404", "500", "502", "503")):
        return "http_error"
    if any(t in name for t in ("json", "decode", "parse", "value")):
        return "parse_error"
    return "unknown_error"


def safe_endpoint_error(
    target: DotNetAppTarget,
    endpoint_type: str,
    exc: BaseException,
) -> Dict[str, str]:
    """Build a credential-free error record for a failed endpoint read (AC4).

    Reports only SAFE information — application id, endpoint type, error category,
    and the exception CLASS name — so a diagnostics/log failure can be logged and
    audited without leaking credentials or sensitive connection strings. The raw
    exception message is deliberately excluded (it commonly embeds a connection
    string or secret). ``endpoint_type`` is a caller-supplied label such as
    ``"diagnostics"`` or ``"logs"``.
    """
    return {
        "app_id": target.app_id,
        "endpoint_type": str(endpoint_type),
        "error_category": classify_endpoint_error(exc),
        "exception_type": type(exc).__name__,
        "environment": target.environment,
    }


def log_endpoint_failure(
    org_id: str,
    target: DotNetAppTarget,
    endpoint_type: str,
    exc: BaseException,
) -> Dict[str, str]:
    """Log a failed endpoint read using ONLY safe fields, and return the safe record.

    Convenience wrapper so collection code logs a credential-free line
    consistently. Returns the same dict :func:`safe_endpoint_error` builds so the
    caller can also attach it to a degraded-signal payload.
    """
    safe = safe_endpoint_error(target, endpoint_type, exc)
    logger.warning(
        "dotnet_app: endpoint read failed org=%s app_id=%s endpoint=%s category=%s (%s)",
        org_id,
        safe["app_id"],
        safe["endpoint_type"],
        safe["error_category"],
        safe["exception_type"],
    )
    return safe
