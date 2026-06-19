"""LIC-1 / T5 (AT-346) — license gate for discovery-run endpoints.

Blocks the discovery-run *mutating* endpoints when the license status is
``readonly`` or ``invalid``; leaves every read endpoint, every auth/login
route, and all ``valid`` / ``grace`` traffic untouched. This is the behavioural
restriction of the scheme — graceful, never a hard lockout (story §5, AC5/AC6).

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
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..license_runtime import get_current_license_status
from ..licensing import LicenseStatus

logger = logging.getLogger(__name__)

# Discovery-run mutating endpoints, matched on method POST + path. Kept as an
# explicit allow-list of *blocked* paths so the gate never over-blocks.
_GATED_POST_PATHS = (
    re.compile(r"^/api/runs/start/?$"),
    re.compile(r"^/api/runs/[^/]+/compute/?$"),
    re.compile(r"^/api/stack-builder/launch/?$"),
)

# Statuses under which discovery runs are blocked. valid/grace never restrict.
_BLOCKED_STATUSES = frozenset({LicenseStatus.READONLY, LicenseStatus.INVALID})


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


class LicenseGateMiddleware(BaseHTTPMiddleware):
    """Block run-trigger endpoints when the license is read-only/invalid."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if _is_gated_run_request(request.method, request.url.path):
            try:
                status = get_current_license_status().get("status")
            except Exception:
                # Fail CLOSED for runs: a status-check error means we cannot
                # confirm a live license, so do not let a discovery run proceed.
                logger.exception(
                    "license gate: status check failed on %s — blocking (fail-closed)",
                    request.url.path,
                )
                return _blocked_response(LicenseStatus.INVALID)
            if status in _BLOCKED_STATUSES:
                return _blocked_response(status)
        # Everything else (reads, auth/login, valid/grace runs) passes through.
        return await call_next(request)


def register_license_gate(app: FastAPI) -> None:
    """Register the license gate. Call next to the existing middleware block."""
    app.add_middleware(LicenseGateMiddleware)
