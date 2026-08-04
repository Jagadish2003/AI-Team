"""Tenancy middleware — AT-82 / T1-S10-B T1.

Extracts org_id on every request and stores it in a ContextVar so all
downstream DB helpers can enforce row-level isolation without every route
handler needing to thread org_id through manually.

Design decisions vs spec:
- org_id comes exclusively from a SIGNATURE-VERIFIED JWT org claim. The org_id
  of a forged or tampered token is never used to set the org context (issue #3),
  because this ContextVar is read by DB helpers BEFORE route-level auth runs —
  trusting an unverified claim here would scope a request to an attacker-chosen
  org. The X-Org-Id header is NEVER used to override the JWT org claim. If an
  X-Org-Id header contradicts the verified JWT org claim, the request is
  rejected with HTTP 403 (Section 4a — X-Org-Id impersonation guard). A matching
  X-Org-Id is silently ignored.
- Static dev tokens (security.py) carry no embedded claims, so the dev token's
  org claim is supplied out-of-band via the DEV_JWT_ORG environment variable
  (mirroring DEV_JWT_ROLE in security.py). When no claim is configured the token
  has no JWT org, and org_id falls back to the X-Org-Id header (dev behaviour),
  then "default".
- TenancyViolationError is raised as a plain Exception (not HTTPException) and
  caught by a registered FastAPI exception handler, so route-level try/except
  blocks cannot swallow it.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
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
# JWT org claim resolution
# ---------------------------------------------------------------------------


def _bearer_token(request: Request) -> str | None:
    """Extract the bearer token from the Authorization header, or None."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


_BLOCKLIST_PREFIX = "auth_blocklist"


def _jti_in_blocklist(jti: str) -> bool:
    """Return True if a (already signature-verified) jti is in the logout blocklist."""
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


def _verified_jwt_payload(token: str | None) -> dict | None:
    """Return the payload of a signature-verified AUTH-1 JWT, else None.

    Delegates to user_auth.decode_signed, which verifies the HS256 signature and
    expiry. A forged, tampered, or expired token returns None — its claims are
    NEVER trusted here. This is the core of the issue #3 fix: org context must
    not be derived from an unverified claim, because the ContextVar is read by DB
    helpers BEFORE route-level auth runs, so trusting a forged org_id would let a
    request read/write another org's data for the duration of that request.
    """
    if not token:
        return None
    try:
        from app.auth.user_auth import decode_signed

        return decode_signed(token)
    except Exception:
        return None


def _x_org_header_trusted() -> bool:
    """Whether the X-Org-Id header may set org context (dev/test only — H2).

    X-Org-Id is a convenience for the static dev token, which carries no signed org
    claim. It is honoured ONLY outside production: in production every caller
    presents a signed JWT (whose org claim wins and is checked against any X-Org-Id
    by the impersonation guard), so trusting a bare header there would let a request
    set tenant context with no signed claim — the exact spoof R17-D3 review H2 flags
    (especially dangerous alongside OAUTH_CALLBACK_ALLOW_UNAUTH). Production callers
    without a JWT claim therefore fall through to DEV_DEFAULT_ORG, never the header.
    """
    return os.environ.get("ENVIRONMENT", "").strip().lower() != "production"


def _dev_token_org(token: str | None) -> str | None:
    """org claim for the static dev token (from DEV_JWT_ORG), else None.

    The dev/test static bearer tokens carry no embedded, signed claims, so their
    org is supplied out-of-band via DEV_JWT_ORG (mirrors DEV_JWT_ROLE in
    security.py). Non-dev / forged tokens get no trusted org from this path.
    """
    if token is None:
        return None
    dev_jwt = os.getenv("DEV_JWT", "dev-token-change-me")
    if token == dev_jwt:
        return os.getenv("DEV_JWT_ORG") or None
    return None


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


# Sentinel attributed to an audit/telemetry event when no org can be resolved.
# Deliberately NOT a real tenant org (e.g. "default"): attributing an unresolved
# event to a real org would silently misfile it under that tenant
# (R17-D3 / AT-450 T5-AC3). The leading-underscore "_unattributed" form is also
# deliberately distinct from any plausible real or user-supplied org value
# (R17-D3 review M3): an analyst querying WHERE org_id = '_unattributed' gets
# exactly the data-quality gaps, never a tenant literally named "unknown". It is
# inert — queried/alerted on, never served as a tenant's data.
UNATTRIBUTED_ORG = "_unattributed"

# Logging-only guard (not functional state): the unresolved-attribution message
# is always identical, and startup / background emitters fire it many times in a
# row, drowning real warnings. We keep the FIRST occurrence at WARNING so the
# data-quality gap stays observable, then log repeats at DEBUG to stop the spam.
# This flips a log level only — attribution behaviour and the returned sentinel
# are unchanged.
_unattributed_warned = False


def resolve_event_org_id(explicit_org_id: str | None = None) -> str:
    """Resolve the org an audit or telemetry event should be attributed to.

    Single source of truth for event org attribution (R17-D3 / AT-450), used by
    both ``telemetry.record_event`` and ``middleware.audit.log_event`` so the two
    trails can never disagree. Priority:

      1. The authenticated request org (tenancy ContextVar) — the source of
         truth whenever the event is emitted inside a request.
      2. An ``explicit_org_id`` supplied by a caller with no request context
         (background work: the discovery runner, DB ingestors, the token
         refresher and other jobs pass the run's / workspace's org directly).
      3. ``UNATTRIBUTED_ORG`` as a last resort, logged at WARNING so a call site
         that forgot to thread its org is observable — never a real tenant's org.

    Context wins over ``explicit_org_id`` on purpose: every in-request call site
    derives the explicit value from the same authenticated context anyway, and
    preferring context means a stale or mistaken explicit value can never
    misattribute an event to the wrong tenant.
    """
    ctx = _current_org_id.get()
    if ctx:
        return ctx
    if explicit_org_id:
        return explicit_org_id
    # Log the first unresolved event at WARNING (stays observable); repeats at
    # DEBUG so startup / background emitters don't flood the console. Logging
    # level only — the sentinel below is returned exactly as before.
    global _unattributed_warned
    log = logger.warning if not _unattributed_warned else logger.debug
    _unattributed_warned = True
    log(
        "event org attribution unresolved (no request context, no explicit "
        "org_id) — attributing to %r",
        UNATTRIBUTED_ORG,
    )
    return UNATTRIBUTED_ORG


@contextmanager
def event_org_context(org_id: str):
    """Temporarily attribute events emitted inside the block to ``org_id``.

    ``resolve_event_org_id`` normally lets the ambient request context win over a
    payload org_id. Some request handlers act for a specific org while the
    ambient context is different; OAuth callbacks are the billing case because
    the signed state names the connecting org, while the request can otherwise
    resolve to the dev/default org. This helper scopes event attribution only and
    restores the previous context after the block.
    """
    token = _current_org_id.set(org_id)
    try:
        yield
    finally:
        _current_org_id.reset(token)


def resolve_request_org_id(request: Request) -> str:
    """Resolve the org id for a request directly from its headers.

    Mirrors the precedence TenancyMiddleware uses (verified JWT org claim →
    dev-token DEV_JWT_ORG → X-Org-Id → DEV_DEFAULT_ORG) but reads the request
    rather than the ContextVar. Needed by middleware that runs OUTSIDE the
    tenancy middleware (e.g. the license gate, registered after tenancy and
    therefore executed before it), where the ContextVar is not yet set.

    Defensive by contract: never raises on a partial/minimal request object — a
    missing Authorization/X-Org-Id header simply yields ``DEV_DEFAULT_ORG`` — so a
    fail-closed caller can call it unconditionally. The X-Org-Id impersonation
    guard is intentionally NOT enforced here; that remains TenancyMiddleware's job
    (a contradicting X-Org-Id is still rejected once the request reaches it).
    """
    try:
        raw_token = _bearer_token(request)
    except Exception:
        raw_token = None

    verified = _verified_jwt_payload(raw_token)
    if verified is not None:
        jwt_org_id = verified.get("org_id") or None
    else:
        jwt_org_id = _dev_token_org(raw_token)

    if jwt_org_id is not None:
        return jwt_org_id

    # X-Org-Id is honoured only outside production (H2); see _x_org_header_trusted.
    if not _x_org_header_trusted():
        return DEV_DEFAULT_ORG

    try:
        x_org_id = request.headers.get("X-Org-Id")
        if x_org_id is not None:
            x_org_id = x_org_id.strip() or None
    except Exception:
        x_org_id = None

    return x_org_id or DEV_DEFAULT_ORG


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

        # Only a signature-verified token's claims are trusted. A forged/tampered
        # token yields no payload here, so its org_id is never used to set the
        # tenancy context (issue #3) and it cannot pass the jti revocation gate.
        verified = _verified_jwt_payload(raw_token)

        if verified is not None:
            # AC9: reject any request whose verified jti is in the logout
            # blocklist. Checked only for genuinely signed tokens.
            if _jti_in_blocklist(verified.get("jti")):
                return JSONResponse(
                    status_code=401, content={"detail": "Token has been revoked"}
                )
            jwt_org_id = verified.get("org_id") or None
        else:
            # Static dev token (no signed claims) → DEV_JWT_ORG. Anything else
            # (including a forged JWT) contributes no trusted org claim.
            jwt_org_id = _dev_token_org(raw_token)

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
        # it. Dev fallback (no JWT claim): X-Org-Id, then DEV_DEFAULT_ORG — but the
        # header is trusted only outside production (H2), so a prod request with no
        # signed org claim resolves to DEV_DEFAULT_ORG, never an attacker's header.
        if jwt_org_id is not None:
            org_id = jwt_org_id
        elif _x_org_header_trusted():
            org_id = x_org_id or DEV_DEFAULT_ORG
        else:
            org_id = DEV_DEFAULT_ORG

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
