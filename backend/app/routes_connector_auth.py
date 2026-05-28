"""OAuth connector auth routes — AT-77 (T1-S10-A A5): OAuth callback and state security.

Four routes (all require Bearer auth — spec AC17):
  GET  /api/connectors/oauth/callback           — OAuth callback (Bearer + state nonce)
  GET  /api/connectors/{connector_id}/auth-url  — Generate one-time auth URL
  DELETE /api/connectors/{connector_id}/token   — Revoke token
  GET  /api/connectors/{connector_id}/token-status — Token status

State nonce storage: SQLite table oauth_nonces (matching existing raw-sqlite3 pattern in db.py).
No session/cookie mechanism exists in this codebase; nonces are stored server-side in the DB
with a 10-minute TTL and are deleted on first use (single-use guarantee).
"""
from __future__ import annotations

import hmac
import logging
import secrets as _secrets_mod
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import RedirectResponse

from app import db
from app.auth import build_auth_url, exchange_code, revoke_token, store_token
from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.auth.vault import REFRESH_THRESHOLD_SECONDS
from app.middleware.audit import log_event
from app.rbac import _get_user_id_from_token
from app.security import require_auth
from database.models.credentials import (
    ALTER_CREDENTIALS_ADD_REFRESH_FAILED,
    CREATE_CREDENTIALS_IDX_CONNECTOR,
    CREATE_CREDENTIALS_IDX_ORG,
    CREATE_CREDENTIALS_TABLE,
)

logger = logging.getLogger(__name__)

# Hardcoded redirect targets — never constructed from external input
OAUTH_SUCCESS_REDIRECT = "/integration-hub?connected={connector_id}"
OAUTH_ERROR_REDIRECT = "/integration-hub?error={error_code}"

_NONCE_TTL_SECONDS = 600  # 10-minute window for state nonce validity
_DEFAULT_ORG_ID = "default"  # Single-tenant dev; org isolation is a T1-S11 concern

_CREATE_NONCES_TABLE = """
CREATE TABLE IF NOT EXISTS oauth_nonces (
    nonce        TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    expires_at   TEXT NOT NULL
)
"""


# ---------------------------------------------------------------------------
# Table initialisation (called lazily on first use)
# ---------------------------------------------------------------------------


def _ensure_tables() -> None:
    import sqlite3 as _sqlite3
    con = db.connect()
    try:
        con.execute(CREATE_CREDENTIALS_TABLE)
        con.execute(CREATE_CREDENTIALS_IDX_ORG)
        con.execute(CREATE_CREDENTIALS_IDX_CONNECTOR)
        try:
            con.execute(ALTER_CREDENTIALS_ADD_REFRESH_FAILED)
        except _sqlite3.OperationalError:
            pass  # Column already exists
        con.execute(_CREATE_NONCES_TABLE)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Nonce store (server-side, single-use, TTL-bounded)
# ---------------------------------------------------------------------------


def _store_nonce(nonce: str, connector_id: str) -> None:
    _ensure_tables()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=_NONCE_TTL_SECONDS)
    ).isoformat()
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO oauth_nonces (nonce, connector_id, expires_at) VALUES (?, ?, ?)",
            (nonce, connector_id, expires_at),
        )
        con.commit()
    finally:
        con.close()


def _consume_nonce(state: str) -> Optional[str]:
    """Delete and validate a nonce in one step. Returns connector_id or None.

    The nonce is deleted immediately on lookup — whether or not the comparison
    succeeds — so replay of any state value (valid or invalid) always returns None
    on the second attempt.
    """
    _ensure_tables()

    con = db.connect()
    try:
        cur = con.execute(
            "SELECT nonce, connector_id, expires_at FROM oauth_nonces WHERE nonce = ?",
            (state,),
        )
        row = cur.fetchone()
        if row is not None:
            con.execute("DELETE FROM oauth_nonces WHERE nonce = ?", (state,))
            con.commit()
    finally:
        con.close()

    if row is None:
        return None

    stored_nonce, connector_id, expires_at_str = row

    # Reject expired nonces (already deleted above)
    expires_at = datetime.fromisoformat(expires_at_str)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        return None

    # Timing-safe comparison (spec requirement: use hmac.compare_digest, not ==)
    if not hmac.compare_digest(stored_nonce, state):
        return None

    return connector_id


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_connector_auth_routes(app: FastAPI) -> None:
    # Register /oauth/callback BEFORE /{connector_id}/... so the literal
    # path segment "oauth" is not captured by the path parameter.

    @app.get(
        "/api/connectors/oauth/callback",
        include_in_schema=True,
    )
    async def oauth_callback(
        code: Optional[str] = Query(default=None),
        state: Optional[str] = Query(default=None),
        error: Optional[str] = Query(default=None),
        token: str = Depends(require_auth),
    ) -> RedirectResponse:
        """Handle OAuth callback. Requires Bearer auth (called by frontend after provider redirect).

        On state mismatch: HTTP 400 with generic message (spec AC3).
        On exchange/storage failure: redirect to OAUTH_ERROR_REDIRECT.
        Redirect targets are hardcoded constants; no part derives from request input.
        """
        if error or not code or not state:
            return RedirectResponse(
                OAUTH_ERROR_REDIRECT.format(error_code="oauth_error"),
                status_code=302,
            )

        connector_id = _consume_nonce(state)
        if connector_id is None:
            # AC3: HTTP 400 on state mismatch, generic message, no detail
            raise HTTPException(status_code=400, detail="Invalid request")

        config = CONNECTOR_AUTH_CONFIGS.get(connector_id)
        if config is None:
            return RedirectResponse(
                OAUTH_ERROR_REDIRECT.format(error_code="unknown_connector"),
                status_code=302,
            )

        try:
            token_response = await exchange_code(config, code)
            store_token(_DEFAULT_ORG_ID, connector_id, token_response)
        except Exception:
            logger.exception("Token exchange failed for connector %s", connector_id)
            return RedirectResponse(
                OAUTH_ERROR_REDIRECT.format(error_code="exchange_failed"),
                status_code=302,
            )

        log_event(
            "connector_connected",
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
            scopes_granted=config.scopes,
        )
        # AC2/AC4: redirect target is a hardcoded constant; connector_id comes
        # from the server-side nonce store, not from state or query params.
        return RedirectResponse(
            OAUTH_SUCCESS_REDIRECT.format(connector_id=connector_id),
            status_code=302,
        )

    @app.get(
        "/api/connectors/{connector_id}/auth-url",
        dependencies=[Depends(require_auth)],
    )
    def get_auth_url(connector_id: str) -> Dict[str, str]:
        """Generate a one-time authorization URL for the given connector.

        State nonce is cryptographically random (secrets.token_urlsafe(32)),
        stored server-side, and contains no redirect URL or user data (AC1).
        """
        config = CONNECTOR_AUTH_CONFIGS.get(connector_id)
        if config is None:
            raise HTTPException(status_code=404, detail="Unknown connector")

        if config.flow != "authorization_code":
            raise HTTPException(
                status_code=400,
                detail="Connector does not use authorization_code flow",
            )

        state = _secrets_mod.token_urlsafe(32)
        _store_nonce(state, connector_id)
        auth_url = build_auth_url(config, state)
        return {"auth_url": auth_url, "connector_id": connector_id}

    @app.delete(
        "/api/connectors/{connector_id}/token",
    )
    async def delete_token(connector_id: str, token: str = Depends(require_auth)) -> Response:
        """Revoke and delete the stored token for the given connector (AC11/AC12/AC13)."""
        await revoke_token(_DEFAULT_ORG_ID, connector_id)
        log_event(
            "connector_disconnected",
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
        )
        return Response(status_code=204)

    @app.get(
        "/api/connectors/{connector_id}/token-status",
        dependencies=[Depends(require_auth)],
    )
    async def get_token_status(connector_id: str) -> Dict[str, str]:
        """Return token status: connected | needs_refresh | needs_auth | refresh_failed (AC14)."""
        _ensure_tables()

        con = db.connect()
        try:
            cur = con.execute(
                "SELECT expires_at, refresh_failed FROM credentials "
                "WHERE org_id = ? AND connector_id = ?",
                (_DEFAULT_ORG_ID, connector_id),
            )
            row = cur.fetchone()
        finally:
            con.close()

        if row is None:
            return {"status": "needs_auth"}

        expires_at_str, refresh_failed_flag = row[0], row[1]
        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        seconds_left = (expires_at - now).total_seconds()

        if seconds_left <= 0:
            return {"status": "needs_auth"}
        if seconds_left <= REFRESH_THRESHOLD_SECONDS:
            if refresh_failed_flag:
                return {"status": "refresh_failed"}
            return {"status": "needs_refresh"}
        return {"status": "connected"}
 