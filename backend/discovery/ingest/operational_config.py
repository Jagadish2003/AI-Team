"""
R17-A4 / T3 — Shared per-deployment operational-target configuration primitives.

Phase one of the Java/.NET enterprise-application scope is **configured, not
auto-discovered**: each customer deployment explicitly declares which applications
are in scope, and the credentials to read them live in the vault, never in config
(R17-A3 §2 / R17-A4 §3, AC4). The rules that enforce "no secret in config" and the
"vault first, env fallback" credential resolution are identical for Java (Actuator)
and .NET (health/diagnostics) targets, so they live here ONCE and each platform's
config module (:mod:`discovery.ingest.java_app_config` /
:mod:`discovery.ingest.dotnet_app_config`) reuses them — only the target dataclass
(which endpoint fields it declares) and the fixture/env source differ.

Keeping the credential handling shared is deliberately security-motivated: a
divergence between the two platforms' secret-rejection or secret-resolution logic
would be a security defect, not just a maintenance smell.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Field names that must NEVER appear in a target config entry — a credential
#: belongs in the vault, not in deployment config (AC4). A target carrying any of
#: these is rejected by the platform loader so a pasted secret cannot slip through
#: into config files or logs. Note the *_ref forms (``credential_ref``,
#: ``certificate_ref``) are references, NOT secrets, and are deliberately allowed.
FORBIDDEN_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "username",
        "user",
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
        "connection_string",
        "connectionstring",
        "certificate",
        "cert",
        "private_key",
        "pfx",
        "pfx_password",
        "sas_token",
    }
)


def find_inline_secret_keys(entry: Dict[str, Any]) -> List[str]:
    """Return the sorted secret-looking key names present in a raw target entry.

    A non-empty result means the entry embeds a credential inline and must be
    rejected. Only the offending KEY names are returned (never the values) so a
    caller can name them in a log without ever writing a secret value (AC4).
    """
    return sorted(
        k for k in entry.keys() if str(k).strip().lower() in FORBIDDEN_SECRET_KEYS
    )


def env_token_key(
    credential_ref: Optional[str],
    default_credential_ref: str,
    default_env_token_key: str,
) -> str:
    """Return the env var name that holds a target's CLI/standalone token.

    The default credential ref uses the connector's canonical env var
    (``JAVA_APP_TOKEN`` / ``DOTNET_APP_TOKEN``); a custom ref namespaces its own
    (``{REF}_TOKEN``) so a deployment can point different targets at different
    vault keys.
    """
    if not credential_ref or credential_ref == default_credential_ref:
        return default_env_token_key
    return f"{credential_ref.upper()}_TOKEN"


def resolve_target_secret(
    org_id: str,
    *,
    app_id: str,
    credential_ref: Optional[str],
    default_credential_ref: str,
    default_env_token_key: str,
    connector_lookup: Callable[[str], Optional[Dict[str, str]]],
    env: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Resolve a target's credential from the vault — never from config or logs (AC4).

    Resolution mirrors the SaaS connectors: the per-run credential context first
    (a DB-sourced vault token, isolated per org/run via ``contextvars``), then an
    env var as a CLI/standalone fallback. Returns ``None`` when the target needs
    no credential (``credential_ref`` is None) or none is configured — the caller
    decides whether that is acceptable (an internal unauthenticated endpoint) or a
    skip.

    The returned secret is handed straight to the HTTP/log client and is never
    attached to the target, logged, or echoed. ``connector_lookup``/``env`` are
    injectable so tests can exercise resolution without a live vault.
    """
    if not credential_ref:
        return None  # endpoint declared as needing no credential

    cred = None
    try:
        cred = connector_lookup(credential_ref)
    except Exception:  # noqa: BLE001 — credential lookup must not break the run.
        logger.warning(
            "operational ingest: credential lookup failed for target '%s' (org=%s); "
            "trying env fallback",
            app_id,
            org_id,
        )
    if cred and cred.get("token"):
        return str(cred["token"]).strip()

    environ = env if env is not None else os.environ
    key = env_token_key(credential_ref, default_credential_ref, default_env_token_key)
    token = environ.get(key)
    return token.strip() if token else None
