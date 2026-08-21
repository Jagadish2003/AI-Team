"""HP-2.5 — model-provider posture on the health endpoint.

``GET /api/health`` reported ``healthy`` on database connectivity alone. A
deployment whose configured model provider was unreachable therefore looked
completely fine while producing findings with no AI narrative and no retrieval
evidence — the silent degradation HP-2 exists to remove, surviving right up to
the surface an operator actually watches.

HP-2.3 resolves the posture at startup and caches it; this shapes that cached
posture into the health payload.

Two rules govern what is reported.

**Never re-probe on a read.** The posture is whatever startup resolved. A health
endpoint that opened sockets per request would be a denial-of-service lever
pointed at the customer's own model server, and ``/api/health`` is *public*
(see ``_PUBLIC_ROUTES`` — no credential required), so anyone could pull it.

**Report no endpoint host.** For the same reason: the endpoint is unauthenticated,
and an in-boundary deployment's model host (``ollama.internal``) is internal
network topology. The payload carries the ROLE, the PROVIDER MODE and the STATUS —
enough to see that embeddings are down and which subsystem to look at — and the
full detail, including the host, stays in the startup log where it is already
written at WARNING.

Degrading the overall verdict
-----------------------------
Only a **reachability** failure flips the service to unhealthy. Every posture is
reported honestly in the check itself; what is narrow is which of them changes the
top-level verdict, and each exclusion has a reason:

``unknown``
    The posture was never established — probing disabled, or a provider that
    declares nothing to probe. Reporting a deployment as broken because nobody
    looked is the mirror image of the defect being fixed here, and it is the same
    rule HP-2.3 applies when deciding whether to refuse boot.

a missing credential, or an unconfigured endpoint
    Under ``customer_hosted`` these already **refuse boot** (HP-2.3), so a running
    deployment in the profile where the model is load-bearing cannot be in this
    state. Under ``saas`` they are a supported configuration: LLM enrichment is
    optional by design — the platform's deterministic fallbacks work without
    ``ANTHROPIC_API_KEY``, and the shipped dev/test setup has no key at all.
    Flipping every such deployment to unhealthy would make the signal noise, and a
    health endpoint that cries wolf is one nobody reads.

``reachability``
    Something the deployment was configured to talk to cannot be reached. That is
    an operational condition rather than a configuration choice, it is what story
    AC3 names, and it is the state in which findings quietly lose their AI
    narrative and their retrieval evidence.

The check's OWN status is always the honest one — the endpoint never claims a
provider is fine when it has not been measured, or when its credential is
missing. What it declines to do is convert a deliberate configuration into a
claim of failure.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from app import degradation
from app.model_gateway import probe

logger = logging.getLogger(__name__)

#: Statuses that represent a positive finding of a problem. ``unknown`` is
#: deliberately absent — see the module docstring.
_DEGRADING_STATUSES = frozenset(
    {
        degradation.STATUS_PARTIAL,
        degradation.STATUS_UNAVAILABLE,
        degradation.STATUS_FAILED,
    }
)

#: The only check whose failure flips the top-level verdict. See the module
#: docstring for why a credential or endpoint-configuration failure does not.
_DEGRADING_CHECK = probe.CHECK_REACHABILITY


def role_degrades_health(status: str, check: str) -> bool:
    """True when ONE role's posture should make ``/api/health`` unhealthy."""
    return status in _DEGRADING_STATUSES and check == _DEGRADING_CHECK


def degrades_overall_health(providers: Dict[str, Any]) -> bool:
    """True when the shaped provider check should make the service unhealthy.

    Takes the payload from :func:`model_provider_health` rather than a bare
    status, because the decision needs the per-role CHECK as well as its status —
    a rolled-up status alone cannot say whether the cause was reachability.
    """
    roles = providers.get("roles") or {}
    return any(
        role_degrades_health(r.get("status", ""), r.get("check", ""))
        for r in roles.values()
    )


def model_provider_health() -> Dict[str, Any]:
    """The ``checks.model_providers`` entry for ``GET /api/health``.

    Never raises: a health endpoint that 500s because a health check failed tells
    an operator nothing. An unreadable posture reports ``unknown``, which is
    honest and does not degrade the overall verdict.
    """
    try:
        from app.model_gateway import provider_posture

        posture = provider_posture()
    except Exception:  # noqa: BLE001 — a health check must not fail the endpoint
        logger.debug("health: model provider posture unavailable", exc_info=True)
        return {
            "status": degradation.STATUS_UNKNOWN,
            "reason": "posture_unavailable",
            "roles": {},
        }

    if posture is None:
        return {
            "status": degradation.STATUS_UNKNOWN,
            "reason": "not_evaluated",
            "roles": {},
        }

    roles: Dict[str, Any] = {}
    for role in posture.roles:
        # Deliberately NOT the endpoint host or the detail sentence — this
        # endpoint is public. See the module docstring.
        roles[role.role] = {
            "provider": role.provider,
            "status": role.status,
            "check": role.check,
            "probed": role.probed,
        }

    return {"status": posture.status, "roles": roles}


__all__ = [
    "degrades_overall_health",
    "model_provider_health",
    "role_degrades_health",
]
