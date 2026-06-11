"""Tenancy middleware — AT-82 / T1-S10-B T1.

Extracts org_id on every request and stores it in a ContextVar so all
downstream DB helpers can enforce row-level isolation without every route
handler needing to thread org_id through manually.

Design decisions vs spec:
- org_id comes exclusively from the JWT org claim. The X-Org-Id header is
  NEVER used to set the org context. If an X-Org-Id header is supplied and it
  contradicts the JWT org claim, the request is rejected with HTTP 403
  (Section 4a — X-Org-Id impersonation guard). A matching X-Org-Id is silently
  ignored.
- The current auth layer (security.py) validates static dev tokens that carry
  no embedded claims. The dev token's org claim is therefore supplied
  out-of-band via the DEV_JWT_ORG environment variable (mirroring DEV_JWT_ROLE
  in security.py). When no claim is configured the token has no JWT org, and
  org_id falls back to the X-Org-Id header (dev behaviour), then "default".
  Real JWT claim extraction replaces _jwt_org_id() when a proper IDP is wired in.
- TenancyViolationError is raised as a plain Exception (not HTTPException) and
  caught by a registered FastAPI exception handler, so route-level try/except
  blocks cannot swallow it.
"""
from __future__ import annotations

import logging
import os
import time
from contextvars import ContextVar
from typing import Callable

import jwt as _pyjwt

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
# JWT org claim resolution
# ---------------------------------------------------------------------------


def _bearer_token(request: Request) -> str | None:
    """Extract the bearer token from the Authorization header, or None."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


_BLOCKLIST_PREFIX = "auth_blocklist"


def _jti_is_revoked(token: str) -> bool:
    """Return True if the JWT's jti is in the logout blocklist (AC9).

    Decodes without signature verification to extract jti — the route-level
    dependency owns full signature/expiry verification. Expired or malformed
    tokens that aren't real JWTs simply return False so dev static tokens
    continue to work unchanged.
    """
    try:
        payload = _pyjwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False},
            algorithms=["HS256"],
        )
    except Exception:
        return False

    jti = payload.get("jti")
    if not jti:
        return False

    from app import db

    entry = db.kv_get(f"{_BLOCKLIST_PREFIX}:{jti}")
    if not entry:
        return False
    exp = entry.get("exp")
    if exp is not None and int(exp) <= int(time.time()):
        return False  # blocklist entry itself has expired
    return True


def _jwt_org_id(token: str | None) -> str | None:
    """Return the org_id claim carried by the JWT, or None if it carries none.

    The dev auth layer uses static bearer tokens with no embedded claims, so
    the dev token's org claim is read from DEV_JWT_ORG (mirrors DEV_JWT_ROLE in
    security.py). When DEV_JWT_ORG is unset the token has no JWT org claim and
    callers fall back to the X-Org-Id header. A real IDP integration replaces
    this with JWT signature verification and claim decoding.
    """
    if token is None:
        return None
    dev_jwt = os.getenv("DEV_JWT", "dev-token-change-me")
    if token == dev_jwt:
        return os.getenv("DEV_JWT_ORG") or None
    # AUTH-1 JWTs carry the workspace in their org_id claim. Decoded without
    # signature verification (require_auth does the real verification before the
    # route runs); a forged org claim still fails require_auth, so the request is
    # rejected even though org context was set. Non-JWT static tokens (viewer/
    # analyst/admin) aren't decodable and fall through to None unchanged.
    try:
        payload = _pyjwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False},
            algorithms=["HS256"],
        )
    except Exception:
        return None
    return payload.get("org_id") or None


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
    """Resolve org_id from the JWT org claim and store it in the ContextVar
    for the duration of the request.

    X-Org-Id header handling (Section 4a — impersonation guard):
      * org_id is sourced from the JWT org claim, never from X-Org-Id.
      * If X-Org-Id is present and contradicts the JWT org claim → HTTP 403.
      * A matching X-Org-Id is silently ignored.
      * When the token carries no JWT org claim (dev tokens without
        DEV_JWT_ORG set), org_id falls back to the X-Org-Id header, then
        DEV_DEFAULT_ORG, preserving dev/test behaviour.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        raw_token = _bearer_token(request)

        # AC9: reject any request whose JWT jti has been added to the logout
        # blocklist. Only applies when a bearer token is present.
        if raw_token and _jti_is_revoked(raw_token):
            return JSONResponse(status_code=401, content={"detail": "Token has been revoked"})

        jwt_org_id = _jwt_org_id(raw_token)

        x_org_id = request.headers.get("X-Org-Id")
        if x_org_id is not None:
            x_org_id = x_org_id.strip() or None

        # Impersonation guard: a supplied X-Org-Id must not contradict the JWT.
        if x_org_id and jwt_org_id is not None and x_org_id != jwt_org_id:
            logger.warning(
                "X-Org-Id %r does not match JWT org_id %r — rejecting",
                x_org_id,
                jwt_org_id,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "X-Org-Id does not match authenticated workspace"},
            )

        # org_id comes from the JWT claim when present; X-Org-Id never overrides
        # it. Dev fallback (no JWT claim): X-Org-Id, then DEV_DEFAULT_ORG.
        if jwt_org_id is not None:
            org_id = jwt_org_id
        else:
            org_id = x_org_id or DEV_DEFAULT_ORG

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
