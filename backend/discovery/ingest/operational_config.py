"""
R17-A4 / T3 — Shared per-deployment operational-target configuration primitives.

Phase one of the Java/.NET enterprise-application scope is **configured, not
auto-discovered**: each customer deployment explicitly declares which applications
are in scope, and the credentials to read them live in the vault, never in config
(R17-A3 §2 / R17-A4 §3, AC4). The rules that enforce "no secret in config" and the
**vault-only, fail-closed** credential resolution are identical for Java (Actuator)
and .NET (health/diagnostics) targets, so they live here ONCE and each platform's
config module (:mod:`discovery.ingest.java_app_config` /
:mod:`discovery.ingest.dotnet_app_config`) reuses them — only the target dataclass
(which endpoint fields it declares) and the fixture/env source differ.

Keeping the credential handling shared is deliberately security-motivated: a
divergence between the two platforms' secret-rejection or secret-resolution logic
would be a security defect, not just a maintenance smell.

Fail-closed on a vault miss (R191-H1 / T1 — F1 fix)
---------------------------------------------------
The credential for an operational-app target is resolved from the vault ONLY.
There is **no env-variable fallback** — the ``os.environ`` fallback that R17-D3
Addendum A eliminated everywhere else lived on here (the 1.8 verification's
critical F1 finding) and is now gone. When a target declares a ``credential_ref``
but the vault has no token for it, :func:`resolve_target_secret` **fail-closes**:
it raises :class:`OperationalCredentialMissing` rather than reaching into the
process environment. The ingestor for that target does not run, the run continues
for other targets, and the failure surfaces in connector health with an actionable
message naming which org, which target, and which credential ref failed — the
identical posture every other connector already has. A target that declares NO
``credential_ref`` still resolves to ``None`` (an internal unauthenticated
endpoint), unchanged.
"""

from __future__ import annotations

import logging
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


class OperationalCredentialMissing(Exception):
    """Raised when a target declares a ``credential_ref`` but the vault has no token.

    This is the **fail-closed** signal (R191-H1 / T1 — F1 fix): a vault miss never
    falls back to the process environment. The caller (the platform ingestor)
    catches it, skips that one target, and surfaces the failure in connector health.

    The exception carries ONLY safe, credential-free identifiers — ``org_id``,
    ``app_id``, ``credential_ref`` — so it can be logged and reported without ever
    exposing a secret value. Its ``str`` is deliberately an actionable operator
    message that names the org, the target, and the credential ref that must be
    connected.
    """

    def __init__(self, *, org_id: str, app_id: str, credential_ref: str):
        self.org_id = org_id
        self.app_id = app_id
        self.credential_ref = credential_ref
        super().__init__(
            f"No vault credential for operational-app target '{app_id}' "
            f"(org='{org_id}', credential_ref='{credential_ref}'). The target was "
            "skipped — connect the credential for this connector to enable it. "
            "(No environment fallback is used.)"
        )


def resolve_target_secret(
    org_id: str,
    *,
    app_id: str,
    credential_ref: Optional[str],
    connector_lookup: Callable[[str], Optional[Dict[str, str]]],
) -> Optional[str]:
    """Resolve a target's credential from the vault — vault only, fail-closed (AC4/AC1).

    Resolution mirrors the SaaS connectors: the credential comes from the per-run
    credential context ONLY (a DB-sourced vault token, isolated per org/run via
    ``contextvars``). There is **no environment fallback** — the ``os.environ``
    fallback that R17-D3 Addendum A eliminated everywhere else, and that the 1.8
    verification flagged as the critical F1 regression, is gone (R191-H1 / T1).

    Behaviour:

      * ``credential_ref`` is falsy → returns ``None`` (the target declared it needs
        no credential — e.g. an internal unauthenticated Actuator). Unchanged.
      * a vault token is present → returns the stripped token.
      * a ``credential_ref`` is declared but the vault has no token (miss, empty
        token, or a lookup error) → **raises** :class:`OperationalCredentialMissing`.
        The ingestor fail-closes for that target: it does not run, the run continues
        for other targets, and the miss surfaces in connector health (AC1).

    The returned secret is handed straight to the HTTP/log client and is never
    attached to the target, logged, or echoed. ``connector_lookup`` is injectable so
    tests can exercise resolution without a live vault. There is no
    ``env`` / ``default_env_token_key`` / ``default_credential_ref`` parameter any
    more — there is no environment credential path.
    """
    if not credential_ref:
        return None  # endpoint declared as needing no credential

    cred = None
    try:
        cred = connector_lookup(credential_ref)
    except Exception:  # noqa: BLE001 — a lookup error is a miss, not an env fallback.
        # Do NOT echo the exception (a live client repr can embed a Bearer token):
        # only the safe identifiers. A lookup failure is treated as a vault miss and
        # fails closed exactly like an absent credential.
        logger.warning(
            "operational ingest: credential lookup failed for target '%s' (org=%s, "
            "credential_ref=%s) — failing closed (no environment fallback)",
            app_id,
            org_id,
            credential_ref,
        )
    if cred and cred.get("token"):
        return str(cred["token"]).strip()

    # Vault miss — fail closed. No os.environ read.
    raise OperationalCredentialMissing(
        org_id=org_id, app_id=app_id, credential_ref=credential_ref
    )


def credential_missing_health(
    *,
    system: str,
    exc: "OperationalCredentialMissing",
) -> Dict[str, Any]:
    """Build a connector-health record for a fail-closed operational-app target (AC1).

    Shapes the miss into the same ``{system, status, message, latencyMs, isLive}``
    dict shape the SaaS connector-health checks emit (see
    :class:`discovery.ingest.connector_health.ConnectorHealth`), so the run's
    ``connector_health`` KV — and the connector-health API the UI reads — reports
    the failed target with an actionable reason that names the org, the target, and
    the credential ref. Carries no secret value.

    ``system`` is the human-facing connector name (e.g. ``"Java Application"`` /
    ``".NET Application"``); ``exc.app_id`` disambiguates which configured target.
    """
    return {
        "system": system,
        "status": "error",
        "message": (
            f"Credential missing for target '{exc.app_id}' "
            f"(credential_ref='{exc.credential_ref}') — target skipped. "
            "Connect this connector's credential in the Integration Hub to enable it."
        ),
        "latencyMs": None,
        "isLive": False,
        "appId": exc.app_id,
        "credentialRef": exc.credential_ref,
        "orgId": exc.org_id,
    }
