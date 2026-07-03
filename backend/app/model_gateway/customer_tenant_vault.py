"""R17-D2 T2 - Customer-tenant credential resolution (gateway-owned bridge).

This module is the ONLY place that turns a stored/configured customer-tenant
credential into a usable secret for a model call. Keeping it inside the model
gateway package upholds the R16-D1 rule that no code outside the gateway ever
handles a resolved model credential.

Resolution order (secure first, dev fallback second)
-----------------------------------------------------
1. The Fernet-encrypted credential vault — ``app.auth.vault`` stores the
   customer-tenant credential in the same encrypted ``credentials`` table as
   connector OAuth tokens, keyed by org. This is the secure production path and
   the source of truth for rotation and revocation (R17-D2 §2, AC2).
2. The ``CUSTOMER_TENANT_API_KEY`` environment variable — a dev/standalone
   fallback only, the same role ``.env`` plays for other connectors. In
   production the credential lives ONLY in the vault (the env var is unset), so
   revoking the vault credential fully revokes access.

Contract
--------
- Returns "" when neither source yields a credential. The provider then degrades
  to a graceful auth failure (generation ``ok=False`` / embedding ``[]``) rather
  than crashing (R17-D2 §2, AC5).
- Never raises: every failure in the vault path (missing vault key, DB error,
  undecryptable/ revoked value) is swallowed and falls through to the env
  fallback, then to "".
- Never logs the credential value. Only the org and a presence flag are logged.
- Re-resolved live on every call (no caching), so a customer's rotation or
  revocation takes effect without a process restart.
"""
from __future__ import annotations

import logging
import os

from app.model_gateway.customer_tenant_config import CONFIG_KEY_API_KEY

logger = logging.getLogger(__name__)


def _resolve_org_id() -> str:
    """Best-effort current org for the model call.

    Uses the tenancy ContextVar when a request/run has set one, otherwise the
    dev default org. Model calls also run in background contexts (materialization,
    jobs) where no request context exists, so this must never raise — it falls
    back to the default org rather than failing the call.
    """
    try:
        from app.middleware.tenancy import DEV_DEFAULT_ORG, get_current_org_id_optional

        return get_current_org_id_optional() or DEV_DEFAULT_ORG
    except Exception:
        return "default"


def resolve_customer_tenant_api_key() -> str:
    """Return the live customer-tenant credential: vault first, env fallback.

    Never raises and never logs the value. Returns "" when no credential is
    available anywhere, which the provider treats as a graceful auth failure.
    """
    # 1) Secure path: the encrypted credential vault, scoped to the current org.
    try:
        from app.auth.vault import get_customer_tenant_credential

        vaulted = get_customer_tenant_credential(_resolve_org_id())
        if vaulted:
            return vaulted
    except Exception:
        # Vault subsystem unavailable (e.g. no DB / no vault key in a dev run).
        # Fall through to the env fallback — never fail the model call here.
        logger.debug(
            "customer_tenant vault resolution unavailable; trying env fallback",
            exc_info=True,
        )

    # 2) Dev/standalone fallback: the environment variable.
    #
    # Suppressed in production. When REQUIRE_CONNECTOR_SECRETS=1 (the production
    # posture the rest of the auth framework enforces), the credential MUST come
    # from the vault so that customer rotation/revocation is authoritative. If we
    # fell back to CUSTOMER_TENANT_API_KEY here, a revoked vault credential could
    # be silently overridden by a stale env var — bypassing the vault entirely.
    # In that case return "" so the provider degrades gracefully (ok=False / [])
    # exactly as it does for any missing credential.
    if os.getenv("REQUIRE_CONNECTOR_SECRETS") == "1":
        logger.debug(
            "customer_tenant: REQUIRE_CONNECTOR_SECRETS=1 — env fallback disabled; "
            "credential must come from the vault"
        )
        return ""
    return os.getenv(CONFIG_KEY_API_KEY, "")
