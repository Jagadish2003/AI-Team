"""LIC-1 / T5 (AT-346) — license gate for discovery-run endpoints.

Blocks the discovery-run *mutating* endpoints when the license status is
``readonly`` or ``invalid``; leaves every read endpoint, every auth/login
route, and all ``valid`` / ``grace`` traffic untouched. This is the behavioural
restriction of the scheme — graceful, never a hard lockout (story §5, AC5/AC6).

Seat-overage gate (R17-D4 Addendum A follow-up): the connect-time gate
(``license_limits.enforce_can_connect``) is FORWARD-ONLY — it blocks *new*
connections past ``max_systems`` but never disconnects existing ones, so an org
that connected systems while unlicensed (unlimited) and then installed a smaller
key ends up with more systems connected than it is licensed for. Because the
value of a license is consumed at RUN time, this gate additionally blocks a
discovery run while ``connected_systems > max_systems``, with an actionable "you
have N of M — disconnect the extra" message. It runs ONLY after the status check
passes (a healthy license), only when the license carries a numeric cap
(unlimited licenses are never seat-gated), and FAILS OPEN on a count error so a
valid run is never wrongly blocked by a transient DB hiccup (the status gate
above remains the fail-closed license-validity check).

Why middleware (not a per-route dependency): the gate is a single
cross-cutting policy over a small, well-known set of run-trigger paths. A
middleware keeps the logic in one module behind a one-line
``register_license_gate(app)`` registration and avoids editing each run
route's signature (the ticket's preferred shape).

Gated endpoints (POST only):
  * ``POST /api/runs/start``                 — starts a discovery run
  * ``POST /api/runs/{run_id}/compute``      — triggers Track B materialization
  * ``POST /api/stack-builder/launch``       — creates a discovery run

Deliberately NOT gated: reads (status / findings / reports / graph), all
``/api/auth/*`` routes, and ``POST /api/runs/{run_id}/replay`` (a read-only
re-serve of persisted artifacts — no new run).

Failure policy (ticket): a status-check error fails CLOSED for these run
endpoints (we cannot confirm a live license), and reads are never gated so they
are inherently fail-open.
"""
from __future__ import annotations

import logging
import re
from typing import Callable, Optional, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..license_runtime import get_current_license_status
from ..licensing import LicenseStatus
from .tenancy import resolve_request_org_id

logger = logging.getLogger(__name__)

# Discovery-run mutating endpoints, matched on method POST + path. Kept as an
# explicit allow-list of *blocked* paths so the gate never over-blocks.
_GATED_POST_PATHS = (
    re.compile(r"^/api/runs/start/?$"),
    re.compile(r"^/api/runs/[^/]+/compute/?$"),
    re.compile(r"^/api/stack-builder/launch/?$"),
)

# Allow-list, not block-list: a discovery run proceeds ONLY for an explicitly
# healthy license — valid (within term) or grace (expired, still fully
# functional). Every other value — readonly, invalid, no_license,
# clock_rollback, or any unrecognised/future status — fails closed and is
# blocked. Modelling it as an allow-list means a newly added status can never
# silently open the gate (it would have to be added here deliberately).
_RUN_ALLOWED_STATUSES = frozenset({LicenseStatus.VALID, LicenseStatus.GRACE})


def _is_gated_run_request(method: str, path: str) -> bool:
    return method == "POST" and any(p.match(path) for p in _GATED_POST_PATHS)


def _blocked_response(status: str) -> JSONResponse:
    """Clear, structured 402 for a license-blocked discovery run.

    402 Payment Required cleanly distinguishes a license block from an auth
    (401) or RBAC (403) failure, so the SPA can surface the renewal banner.
    """
    return JSONResponse(
        status_code=402,
        content={
            "detail": (
                "AgentIQ license is not active — discovery runs are disabled. "
                "Existing findings, reports, and the knowledge graph remain "
                "available, and login still works. Contact CloudFulcrum to renew."
            ),
            "reason": "license_inactive",
            "licenseStatus": status,
        },
    )


def _seat_overage(org_id: str) -> Optional[Tuple[int, int]]:
    """Return ``(used, licensed)`` when the org has MORE systems connected than
    its license covers, else ``None``.

    Uses the SAME single-source-of-truth helpers the connect gate and the
    Integration Hub counter use (``license_limits``), so the count can never
    drift. Returns ``None`` — i.e. do NOT seat-gate — for an unlimited license
    (no numeric cap) and, deliberately, for ANY error: this check is additive to
    the fail-closed status gate above and must never over-block a valid-license
    run because a count query hiccuped (fail OPEN on the overage dimension only).
    """
    try:
        from ..license_limits import count_connected_systems, get_max_systems

        licensed = get_max_systems(org_id)
        if licensed is None:
            return None  # unlimited — no seat cap to enforce
        used = count_connected_systems(org_id)
        return (used, licensed) if used > licensed else None
    except Exception:  # noqa: BLE001 — never block a valid run on a count error
        logger.warning(
            "license gate: seat-count check failed for org %s — not seat-gating",
            org_id,
            exc_info=True,
        )
        return None


def _overage_blocked_response(used: int, licensed: int) -> JSONResponse:
    """Clear, structured 402 for a run blocked by the connected-system seat cap.

    Distinct ``reason`` and the used/licensed counts let the SPA show an
    actionable "disconnect N systems" message rather than a generic renewal banner.
    """
    excess = max(0, used - licensed)
    return JSONResponse(
        status_code=402,
        content={
            "detail": (
                f"You have {used} systems connected but your license covers "
                f"{licensed}. Disconnect {excess} system"
                f"{'s' if excess != 1 else ''} in the Integration Hub to run "
                "discovery, or contact CloudFulcrum to increase your license."
            ),
            "reason": "license_over_limit",
            "systemsUsed": used,
            "systemsLicensed": licensed,
        },
    )


class LicenseGateMiddleware(BaseHTTPMiddleware):
    """Block run-trigger endpoints unless the license is live-validated healthy.

    Status is computed LIVE per request for the request's org via
    get_current_license_status(org_id=...) (which re-validates that org's stored
    key against the current clock); the gate never trusts a cached status, so a
    failed/stale periodic check cannot open it. The org is resolved straight from
    the request (resolve_request_org_id) because this middleware runs OUTSIDE the
    tenancy middleware, so the tenancy ContextVar is not yet set. A status-check
    error fails closed.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if _is_gated_run_request(request.method, request.url.path):
            try:
                org_id = resolve_request_org_id(request)
                status = get_current_license_status(org_id=org_id).get("status")
            except Exception:
                # Fail CLOSED for runs: a status-check error means we cannot
                # confirm a live license, so do not let a discovery run proceed.
                logger.exception(
                    "license gate: status check failed on %s — blocking (fail-closed)",
                    request.url.path,
                )
                return _blocked_response(LicenseStatus.INVALID)
            # Fail closed: block unless the live status is explicitly valid/grace.
            if status not in _RUN_ALLOWED_STATUSES:
                return _blocked_response(status or LicenseStatus.INVALID)
            # License is healthy — now enforce the SEAT COUNT: a valid license
            # still cannot run discovery while more systems are connected than it
            # covers (forward-only connect gate leaves such overage in place).
            overage = _seat_overage(org_id)
            if overage is not None:
                return _overage_blocked_response(*overage)
        # Everything else (reads, auth/login, in-limit valid/grace runs) passes through.
        return await call_next(request)


def register_license_gate(app: FastAPI) -> None:
    """Register the license gate. Call next to the existing middleware block."""
    app.add_middleware(LicenseGateMiddleware)
