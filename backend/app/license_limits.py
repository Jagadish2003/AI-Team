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
import os
from typing import Any, Dict, List, Optional

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

# MSP-B13 / T4 (AT-746): "approaching-cap" margin — how many systems may remain
# before the Integration Hub shows the approaching-capacity notice. Configurable
# (the AC's "configured warning"); default 1 so the LAST licensable seat always
# warns before it is taken. 0 disables the approaching notice (only the at-cap
# hard stop remains).
_DEFAULT_APPROACHING_MARGIN = 1


def _approaching_margin() -> int:
    """Configured systems-remaining threshold for the approaching-cap notice."""
    raw = os.environ.get("LICENSE_APPROACHING_CAP_MARGIN")
    if raw is None or not raw.strip():
        return _DEFAULT_APPROACHING_MARGIN
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "license limits: non-integer LICENSE_APPROACHING_CAP_MARGIN %r — using %d",
            raw, _DEFAULT_APPROACHING_MARGIN,
        )
        return _DEFAULT_APPROACHING_MARGIN


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


def _is_multi_scope(record: Dict[str, Any]) -> bool:
    """Whether a connector record is a multi-scope cloud connector (MSP-B13).

    A multi-scope connector (AWS/Azure Events) is one-connection-MANY-scopes: each
    pinned account/subscription is a billable system, and the connection row itself
    is not. Detected from the catalog ``multiScope`` flag (merged in by
    ``org_connectors_list``), with the presence of a ``scopes`` list as a fallback
    signal so a per-org override that dropped the flag is still counted correctly.
    """
    return bool(record.get("multiScope")) or isinstance(record.get("scopes"), list)


def _pinned_scope_count(record: Dict[str, Any]) -> int:
    """Number of PINNED scopes on a multi-scope connector record.

    Only pinned scopes live on ``scopes`` (candidates live on ``candidate_scopes``
    and are never counted — forward-only activation, MSP-B13 AC4), so the length of
    the list is exactly the connector's billable system count.
    """
    scopes = record.get("scopes")
    if not isinstance(scopes, list):
        return 0
    return sum(1 for s in scopes if isinstance(s, dict) and s.get("scope_id"))


def count_connected_systems(org_id: str) -> int:
    """Number of systems currently connected for the org.

    A "system" is one connected Integration-Hub entity — the pricing definition
    (Addendum A §1, AC14). For a single-scope connector that is the connector when
    its per-org ``status == "connected"``; for a MULTI-SCOPE cloud connector
    (MSP-B13: AWS/Azure Events) it is each PINNED account/subscription, so the
    connection contributes its pinned-scope count, not one (the connection row
    itself is not a billable system). ``db.org_connectors_list`` merges the shared
    catalog with this org's own state, so the count the customer sees in the hub is
    exactly the count enforced here.
    """
    total = 0
    for c in db.org_connectors_list(org_id):
        if _is_multi_scope(c):
            total += _pinned_scope_count(c)
        elif c.get("status") == CONNECTED_STATUS:
            total += 1
    return total


def _build_limit_state(used: int, max_systems: Optional[int]) -> dict:
    """Pure derivation of the Integration-Hub limit state from its two inputs.

    Split out from :func:`get_limit_state` so the used-vs-licensed maths (and the
    unlimited / approaching / at-cap derivation) is unit-testable without a DB or a
    license.

    ``max_systems`` of ``None`` means unlimited, so ``systemsLicensed`` is ``None``
    and ``canConnectMore`` is always ``True``. Otherwise ``canConnectMore`` mirrors
    the aggregate half of ``can_connect_new_system`` (``used < max_systems``) — it
    is the hub-wide "is there headroom" signal, NOT a per-connector verdict: a
    reconnect of an already-connected system is always allowed regardless
    (forward-only), which the connect-time gate handles per connector.

    MSP-B13 / T4 (AT-746) adds the approaching-cap notice + at-cap hard stop the
    Integration Hub / cloud-connector cards render (AC2/AC5):

      * ``atCap`` — at or over the licensed limit (``used >= max_systems``); carries
        the hard-stop ``notice`` (:func:`limit_message`).
      * ``approachingCap`` — under the cap but within the configured margin of it
        (``0 < remaining <= LICENSE_APPROACHING_CAP_MARGIN``); carries the
        approaching ``notice`` (:func:`approaching_cap_message`).

    Both are additive to the T10 shape; ``notice`` is ``None`` when there is nothing
    to warn about (comfortably under the cap, or unlimited).
    """
    unlimited = max_systems is None
    can_connect = unlimited or used < max_systems
    at_cap = (not unlimited) and used >= max_systems
    approaching = False
    notice: Optional[str] = None
    if at_cap:
        notice = limit_message(max_systems)
    elif not unlimited:
        remaining = max_systems - used
        margin = _approaching_margin()
        if margin > 0 and 0 < remaining <= margin:
            approaching = True
            notice = approaching_cap_message(used, max_systems)
    return {
        "systemsUsed": used,
        "systemsLicensed": max_systems,  # None => unlimited license
        "unlimited": unlimited,
        "canConnectMore": can_connect,
        "approachingCap": approaching,
        "atCap": at_cap,
        "notice": notice,
    }


def get_limit_state(org_id: str) -> dict:
    """The org's Integration-Hub license-limit state — systems used vs licensed.

    R17-D4 Addendum A / T10 (AT-505): exposes current usage against the
    entitlement so the Integration Hub can show it (AC14). Both numbers come from
    the SAME helpers the connect-time gate enforces with —
    :func:`count_connected_systems` and :func:`get_max_systems` — so the count the
    customer sees is exactly the count that is enforced (Addendum A §1 / AC14).

    Returns ``{systemsUsed, systemsLicensed, unlimited, canConnectMore}`` where
    ``systemsLicensed``/``unlimited`` reflect ``max_systems`` (``None`` => unlimited,
    including pre-addendum keys and no-license/invalid states, per AC13).
    """
    return _build_limit_state(count_connected_systems(org_id), get_max_systems(org_id))


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


def approaching_cap_message(used: int, max_systems: int) -> str:
    """The approaching-capacity notice wording (MSP-B13 / T4, AC2/AC5).

    Shown before the cap is reached so a customer is warned honestly at connection
    time, not surprised at the cap. ``remaining`` is always positive here (the
    at-cap case uses :func:`limit_message` instead).
    """
    remaining = max_systems - used
    seat = "system" if remaining == 1 else "systems"
    return (
        f"You are approaching your licence limit: {used} of {max_systems} systems "
        f"connected ({remaining} {seat} remaining). Contact CloudFulcrum to add more."
    )


# ---------------------------------------------------------------------------
# Per-scope activation gate — MSP-B13 / T4 (AT-746)
#
# A multi-scope cloud connector (AWS/Azure Events) bills PER PINNED SCOPE, so the
# licence gate must fire when an Owner PINS a scope, not when the connection is
# created. These helpers mirror can_connect_new_system / enforce_can_connect but
# key idempotency on the (connector, scope) pair rather than the connector.
# ---------------------------------------------------------------------------


def _pinned_scope_ids(org_id: str, connector_id: str) -> List[str]:
    """The scope ids already pinned on this org's connector, or []."""
    record = db.org_connector_get(org_id, connector_id) or {}
    scopes = record.get("scopes")
    if not isinstance(scopes, list):
        return []
    return [
        str(s.get("scope_id"))
        for s in scopes
        if isinstance(s, dict) and s.get("scope_id")
    ]


def can_pin_new_scope(
    org_id: str, connector_id: str, scope_id: Optional[str] = None
) -> bool:
    """Whether the org may PIN another scope under its licence (MSP-B13 AC5).

    Each pinned scope is one system, so a new pin is a new system and is subject to
    ``max_systems`` exactly like a connector connect. Re-pinning an ALREADY-pinned
    scope is idempotent (not a new system) and is never blocked — forward-only,
    mirroring :func:`can_connect_new_system`'s reconnect refinement.
    """
    max_systems = get_max_systems(org_id)
    if max_systems is None:
        return True  # unlimited licence (or no enforceable limit)
    if scope_id is not None and str(scope_id) in _pinned_scope_ids(org_id, connector_id):
        return True  # idempotent re-pin of an existing scope — not a new system
    return count_connected_systems(org_id) < max_systems


def enforce_can_pin_scope(
    org_id: str, connector_id: str, scope_id: Optional[str] = None
) -> None:
    """Raise HTTP 402 if pinning ``scope_id`` would exceed ``max_systems`` (AC3).

    No-op when the licence is unlimited, the org is under its limit, or the scope is
    already pinned (idempotent). On a block it raises ``HTTPException(402)`` with the
    SAME structured detail as :func:`enforce_can_connect` (message + reason +
    used/licensed counts), so the cloud-connector card renders the identical
    hard-stop wording the connector connect gate does.
    """
    if can_pin_new_scope(org_id, connector_id, scope_id):
        return
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
