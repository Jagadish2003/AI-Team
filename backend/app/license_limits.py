"""R17-D4 Addendum A / T9 — Scoped Activation: enforce ``limits.max_systems``.

Connection-time enforcement of the LIC-1 payload's ``limits.max_systems``
entitlement. The field was reserved (and left unenforced) in the signed payload
from LIC-1 day one precisely so scope could be enforced later WITHOUT a
key-format change — this module is that "later" (Addendum A §1, "Design-ahead
pays off").

Principles this module encodes (Addendum A §1 / AC10–AC13):

  * A "system" = one connected entity in the Integration Hub — the same
    definition as the pricing sheet. The count the customer sees is the count
    that is enforced. Here that is: connectors whose per-org state is
    ``status == "connected"`` (see ``db.org_connectors_list``).

  * Forward-only, never destructive. The limit gates NEW connections only; it
    never disconnects an existing one. A renewed key carrying a LOWER limit than
    the number of currently-connected systems keeps every live connector running
    and merely blocks further connections until the org is back under the limit
    (AC12). This mirrors LIC-1's never-a-cold-stop posture — commercial pressure
    comes from blocking growth, not breaking production.

  * ``max_systems`` of ``None`` (or absent) behaves as UNLIMITED, so keys issued
    before this addendum remain valid and unconstrained (AC13). Enforcement is
    opt-in per key.

This module is deliberately the single source of the counting + entitlement
logic; the connect-time gates (``main.connect_connector``,
``routes_connector_auth.get_auth_url`` / ``oauth_callback``) call
``enforce_can_connect`` so the rule lives in exactly one place. T10 (systems
used / systems licensed state) and T11 (frontend) build on the same helpers.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException

from . import db
from .license_runtime import get_current_license_status

logger = logging.getLogger(__name__)

# The connector per-org state value that marks a "connected" system. Kept as one
# constant so the count and the "already connected?" check can never drift.
CONNECTED_STATUS = "connected"

# Structured block reason, surfaced to the SPA so it can show the renewal path
# (T11). Distinct from the license gate's "license_inactive" so the UI can tell a
# scope block (buy more systems) from a term block (renew the key).
BLOCK_REASON = "system_limit_reached"


def get_max_systems(org_id: str) -> Optional[int]:
    """The org's licensed ``max_systems``, or ``None`` for an unlimited license.

    Reads the org's live-validated license via
    ``license_runtime.get_current_license_status`` (the side-effect-free path the
    run gate also uses) and returns ``payload.limits.max_systems``.

    ``None`` means "do not enforce a system cap" and is returned for every case
    where no numeric limit applies: an unlimited/pre-addendum key
    (``max_systems`` null or absent), or no verifiable payload at all
    (no_license / invalid — those states are handled by LIC-1's existing
    read-only behaviour, not by this scope limit). Never raises.
    """
    try:
        result = get_current_license_status(org_id=org_id)
    except Exception:  # pragma: no cover — defensive; a status read must not break connects
        logger.exception("license limits: status read failed for org %s", org_id)
        return None

    payload = result.get("payload") or {}
    limits = payload.get("limits") or {}
    max_systems = limits.get("max_systems")
    if max_systems is None:
        return None
    try:
        return int(max_systems)
    except (TypeError, ValueError):
        # A malformed limit is treated as unlimited rather than blocking every
        # connect — forward-only never over-blocks on bad data.
        logger.warning(
            "license limits: non-integer max_systems %r for org %s — treating as unlimited",
            max_systems,
            org_id,
        )
        return None


def count_connected_systems(org_id: str) -> int:
    """Number of systems currently connected for the org.

    A "system" is one connected Integration-Hub entity: a connector whose per-org
    state is ``status == "connected"`` (``db.org_connectors_list`` merges the
    shared catalog with this org's own connection state). This is exactly the
    count the customer sees in the hub, so the enforced count matches the pricing
    definition (Addendum A §1, AC14).
    """
    connectors = db.org_connectors_list(org_id)
    return sum(1 for c in connectors if c.get("status") == CONNECTED_STATUS)


def _is_connected(org_id: str, connector_id: str) -> bool:
    """Whether this specific connector is already connected for the org.

    Re-connecting / re-authorising an already-connected system is idempotent —
    it does not add a NEW system, so the limit must never block it (forward-only).
    """
    record = db.org_connector_get(org_id, connector_id)
    return bool(record) and record.get("status") == CONNECTED_STATUS


def can_connect_new_system(org_id: str, connector_id: Optional[str] = None) -> bool:
    """Whether the org may connect another system under its license.

    Mirrors the Addendum A §1 reference logic::

        max_systems = license.limits.get('max_systems')
        if max_systems is None:
            return True                       # unlimited license
        return count_connected_systems(org_id) < max_systems

    with one forward-only refinement: when ``connector_id`` is supplied and that
    connector is ALREADY connected, this returns ``True`` — reconnecting an
    existing system is not a new connection and must never be blocked (so a key
    whose limit is below the current count still lets existing systems
    re-authorise; it only blocks genuinely new ones — AC12).
    """
    max_systems = get_max_systems(org_id)
    if max_systems is None:
        return True  # unlimited license (or no enforceable limit)
    if connector_id is not None and _is_connected(org_id, connector_id):
        return True  # idempotent reconnect of an existing system — not a new one
    return count_connected_systems(org_id) < max_systems


def limit_message(max_systems: int) -> str:
    """The customer-facing block message (Addendum A §1)."""
    return (
        f"Your license covers {max_systems} systems. "
        "Contact CloudFulcrum to add more."
    )


def enforce_can_connect(org_id: str, connector_id: Optional[str] = None) -> None:
    """Raise HTTP 402 if connecting ``connector_id`` would exceed ``max_systems``.

    No-op when the license is unlimited, when the org is under its limit, or when
    the connector is already connected (idempotent reconnect). On a block it
    raises ``HTTPException(402)`` with a structured detail carrying the clear
    message and a request path, plus the used/licensed counts the Integration Hub
    surfaces (T10/T11).

    402 Payment Required (the same code LIC-1's run gate uses) cleanly separates a
    license/entitlement block from an auth (401) or RBAC (403) failure.
    """
    if can_connect_new_system(org_id, connector_id):
        return

    # Only reachable when a numeric limit applies and it is exceeded.
    max_systems = get_max_systems(org_id)
    used = count_connected_systems(org_id)
    raise HTTPException(
        status_code=402,
        detail={
            "detail": limit_message(max_systems),
            "reason": BLOCK_REASON,
            "systemsUsed": used,
            "systemsLicensed": max_systems,
        },
    )
