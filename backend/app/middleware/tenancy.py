"""Tenancy middleware — AT-82 / T1-S10-B T1.

Extracts org_id on every request and stores it in a ContextVar so all
downstream DB helpers can enforce row-level isolation without every route
handler needing to thread org_id through manually.

Design decisions vs spec:
- The current auth layer (security.py) validates a static dev token; it does
  NOT issue real JWTs with claims. org_id is therefore taken from the
  X-Org-Id request header when present, falling back to "default".
  Real JWT claim extraction must replace this when a proper IDP is wired in.
- TenancyViolationError is raised as a plain Exception (not HTTPException) and
  caught by a registered FastAPI exception handler, so route-level try/except
  blocks cannot swallow it.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context variable — one per async task / request
# ---------------------------------------------------------------------------

_current_org_id: ContextVar[str | None] = ContextVar("current_org_id", default=None)

DEV_DEFAULT_ORG = "default"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TenancyViolationError(Exception):
    """Raised when a DB helper is called without tenancy context being set.

    Never raise HTTPException here — we want this to bypass route-level
    try/except and be caught only by the registered exception handler.
    """


def get_current_org_id() -> str:
    """Return the org_id set for the current request.

    Raises TenancyViolationError (HTTP 500) if no context has been set.
    This is a programming error, not a user error — it means a protected
    DB helper was called outside a request context.
    """
    org_id = _current_org_id.get()
    if org_id is None:
        logger.warning("TenancyViolationError: org_id context missing")
        raise TenancyViolationError("Tenancy context missing — org_id not set for this request")
    return org_id


def get_current_org_id_optional() -> str | None:
    """Return org_id or None — for use in middleware and tests."""
    return _current_org_id.get()


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class TenancyMiddleware(BaseHTTPMiddleware):
    """Extract org_id from X-Org-Id header (or fall back to 'default') and
    store it in the ContextVar for the duration of the request.

    Must be registered AFTER auth middleware so the JWT is validated first.
    In dev mode (static bearer token) there is no JWT to decode; org_id
    is taken exclusively from the X-Org-Id header.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        org_id = request.headers.get("X-Org-Id", DEV_DEFAULT_ORG).strip() or DEV_DEFAULT_ORG
        token = _current_org_id.set(org_id)
        try:
            response = await call_next(request)
        finally:
            _current_org_id.reset(token)
        return response


# ---------------------------------------------------------------------------
# Exception handler + registration helper
# ---------------------------------------------------------------------------


def tenancy_violation_handler(request: Request, exc: TenancyViolationError) -> JSONResponse:
    logger.warning("TenancyViolationError on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Tenancy context missing"})


def register_tenancy(app: FastAPI) -> None:
    """Add middleware and exception handler to the FastAPI app.

    Call this before any route registration.
    """
    app.add_middleware(TenancyMiddleware)
    app.add_exception_handler(TenancyViolationError, tenancy_violation_handler)
