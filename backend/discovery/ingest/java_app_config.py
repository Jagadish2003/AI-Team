"""
R17-A3 / T3 — Per-deployment configuration of Java application targets + vault
credential resolution.

Phase one of the Java/.NET enterprise-application scope is **configured, not
auto-discovered** (R17-A3 §2, Architectural Note "Configured, not
auto-discovered"). AgentIQ does NOT scan the network to find Java apps; each
customer deployment explicitly declares which applications are in scope. This
module owns that declaration and the safe resolution of the credentials needed
to read them.

What a target declares
----------------------
Each :class:`JavaAppTarget` names exactly one running Java application AgentIQ is
allowed to read:

  * ``app_id``        — stable identity used as the artifact-id prefix and the
                        per-app checkpoint key (so a signal traces back to the
                        right service — R16-B1).
  * ``actuator_url``  — base URL of the framework health/diagnostics endpoint
                        (Spring Boot Actuator: ``/health``, ``/metrics``,
                        ``/info``). Operational surface only (R17-A3 §1).
  * ``log_source``    — where the application's logs are read from (a file path
                        or a log endpoint).
  * ``metadata``      — non-secret service metadata (service name, environment,
                        owning team) used to connect the signal back to the right
                        service for cross-system corroboration (R17-A3 §3).
  * ``credential_ref``— a *reference* (a vault connector key), NOT a credential.
                        The actual secret is resolved at ingest time from the
                        credential vault, never stored here.

The credential rule (AC3) — secrets live in the vault, never in config
----------------------------------------------------------------------
Passwords, tokens, API keys, and basic-auth values MUST NOT appear in the target
configuration, in logs, or in code. The configuration carries only a
``credential_ref`` naming where the secret lives; :func:`resolve_secret` reads
the decrypted value from the per-run credential context (DB-sourced vault token,
isolated per org/run — the same mechanism the SaaS connectors use, see
``discovery.ingest.get_live_connector``). Resolution is **vault-only and
fail-closed** (R191-H1 / T1): a target whose credential is missing from the vault
raises rather than reading the process environment. The resolved secret is
returned to the caller and is never attached to the target, logged, or echoed in
a ``repr``.

To make the "no secrets in config" rule enforceable rather than merely
documented, :func:`load_targets` REJECTS any target entry that carries an inline
secret-looking field (``password``/``token``/``secret``/``api_key``/
``basic_auth``/...). A misconfigured deployment that pastes a credential into the
target config surfaces as a rejected target rather than silently persisting a
plaintext secret.

Offline vs live
---------------
Offline (default, ``INGEST_MODE`` != ``live``): targets are read from the
deterministic fixture ``fixtures/java_app_sample.json`` — parity with the other
connectors, so the whole pipeline runs without any credentials. Live: targets
are read from the ``JAVA_APP_TARGETS`` env var (a JSON array of target configs,
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
    classify_endpoint_error,
    find_inline_secret_keys,
    log_endpoint_failure,
    resolve_target_secret,
    safe_endpoint_error,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "java_app_sample.json"

#: Env var (live mode) holding a JSON array of target configs for the deployment.
_TARGETS_ENV = "JAVA_APP_TARGETS"

#: Default vault connector key used when a target does not name its own
#: ``credential_ref``. Mirrors the connector_id of the ingestor.
DEFAULT_CREDENTIAL_REF = "java_app"

__all__ = [
    "DEFAULT_CREDENTIAL_REF",
    "JavaAppConfigError",
    "JavaAppTarget",
    "classify_endpoint_error",
    "load_targets",
    "log_endpoint_failure",
    "resolve_secret",
    "safe_endpoint_error",
]


class JavaAppConfigError(Exception):
    """Raised when the Java application target configuration is invalid."""


@dataclass(frozen=True)
class JavaAppTarget:
    """One configured Java application AgentIQ is allowed to read (R17-A3 §2).

    A target is pure, non-secret configuration. The credential needed to read the
    application's Actuator endpoint / logs is referenced by ``credential_ref`` and
    resolved from the vault at ingest time — it is never stored on the target.
    """

    app_id: str
    name: str
    actuator_url: str
    log_source: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    #: Vault connector key naming where this app's secret lives. None means the
    #: endpoint needs no credential (e.g. an internal unauthenticated Actuator).
    credential_ref: Optional[str] = DEFAULT_CREDENTIAL_REF

    def __post_init__(self) -> None:
        if not self.app_id or not isinstance(self.app_id, str):
            raise JavaAppConfigError("JavaAppTarget.app_id must be a non-empty string")
        # A target must expose at least one operational surface (Actuator or logs);
        # neither is required individually, but a target with neither reads nothing.
        if not (self.actuator_url or self.log_source):
            raise JavaAppConfigError(
                f"JavaAppTarget '{self.app_id}' declares neither an actuator_url "
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


def _coerce_target(entry: Dict[str, Any]) -> JavaAppTarget:
    """Build a :class:`JavaAppTarget` from a raw config dict, enforcing AC3.

    Rejects any entry carrying an inline secret-looking field — a credential must
    be resolved from the vault via ``credential_ref``, never embedded in config.
    """
    if not isinstance(entry, dict):
        raise JavaAppConfigError("each Java app target must be a JSON object")

    inline_secrets = find_inline_secret_keys(entry)
    if inline_secrets:
        # Do NOT echo the values — only the offending key names — so a rejected
        # secret is never written to the log either (AC3).
        raise JavaAppConfigError(
            f"Java app target '{entry.get('app_id', '?')}' contains inline "
            f"credential field(s) {inline_secrets}; store credentials in the vault "
            "and reference them via 'credential_ref' instead."
        )

    metadata = entry.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    # credential_ref is optional; default to the shared java_app vault key. An
    # explicit null means the endpoint needs no credential.
    credential_ref = (
        entry["credential_ref"] if "credential_ref" in entry else DEFAULT_CREDENTIAL_REF
    )

    return JavaAppTarget(
        app_id=str(entry.get("app_id", "")).strip(),
        name=str(entry.get("name", entry.get("app_id", ""))).strip(),
        actuator_url=str(entry.get("actuator_url", "")).strip(),
        log_source=str(entry.get("log_source", "")).strip(),
        metadata=metadata,
        credential_ref=credential_ref,
    )


def _raw_target_entries(org_id: str) -> List[Dict[str, Any]]:
    """Return the raw (dict) target entries for an org — config, never scanning.

    Offline: the deterministic fixture. Live: the ``JAVA_APP_TARGETS`` env JSON
    array configured per deployment. Either source is explicit configuration; no
    network discovery is ever performed (R17-A3 §2).
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
        raise JavaAppConfigError(
            f"{_TARGETS_ENV} is not valid JSON: {type(exc).__name__}"
        ) from exc
    if not isinstance(parsed, list):
        raise JavaAppConfigError(f"{_TARGETS_ENV} must be a JSON array of targets")
    return [e for e in parsed if isinstance(e, dict)]


def load_targets(org_id: str) -> List[JavaAppTarget]:
    """Return the configured Java application targets for ``org_id``.

    The customer decides which applications AgentIQ may read; this returns exactly
    that configured set (no auto-discovery). A single malformed/insecure entry is
    skipped (logged by app_id, never by value) so one bad target does not block
    the rest — matching the project's "degrade, don't crash" ingestion rule.
    """
    targets: List[JavaAppTarget] = []
    seen: set[str] = set()
    for entry in _raw_target_entries(org_id):
        try:
            target = _coerce_target(entry)
        except JavaAppConfigError as exc:
            # Message names the app_id / offending key only — never a secret value.
            logger.warning("java_app: skipping invalid target (org=%s): %s", org_id, exc)
            continue
        if target.app_id in seen:
            logger.warning(
                "java_app: duplicate target app_id '%s' (org=%s) — keeping the first",
                target.app_id,
                org_id,
            )
            continue
        seen.add(target.app_id)
        targets.append(target)
    return targets


def resolve_secret(
    org_id: str,
    target: JavaAppTarget,
    *,
    connector_lookup: Callable[[str], Optional[Dict[str, str]]] = get_live_connector,
    env: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Resolve a target's credential from the vault — vault only, fail-closed (AC3).

    Resolution mirrors the SaaS connectors: the credential comes from the per-run
    credential context ONLY (a DB-sourced vault token, isolated per org/run via
    ``contextvars``). There is **no env fallback** (R191-H1 / T1 — F1 fix): a target
    that declares a ``credential_ref`` with no vault token raises
    :class:`operational_config.OperationalCredentialMissing`, and the ingestor
    fail-closes for that target (it does not run; the run continues for other
    targets; the miss surfaces in connector health). Returns ``None`` only when the
    target needs no credential (``credential_ref`` is None — an internal
    unauthenticated Actuator).

    The returned secret is handed straight to the HTTP/log client and is never
    attached to the target, logged, or echoed. ``connector_lookup`` is injectable so
    tests can exercise resolution without a live vault. The actual vault-only,
    fail-closed resolution is the shared
    :func:`operational_config.resolve_target_secret` (identical for Java and .NET).

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
