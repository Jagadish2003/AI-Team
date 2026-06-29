"""OAuth connector auth routes — AT-77 (T1-S10-A A5): OAuth callback and state security.

Four routes (all require Bearer auth — spec AC17):
  GET  /api/connectors/oauth/callback           — OAuth callback (Bearer + state nonce)
  GET  /api/connectors/{connector_id}/auth-url  — Generate one-time auth URL
  DELETE /api/connectors/{connector_id}/token   — Revoke token
  GET  /api/connectors/{connector_id}/token-status — Token status

State nonce storage: oauth_nonces table (matching the existing raw-SQL pattern in db.py).
No session/cookie mechanism exists in this codebase; nonces are stored server-side in the DB
with a 10-minute TTL and are deleted on first use (single-use guarantee).
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets as _secrets_mod
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import psycopg2

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials

from app import db
from app.auth import (
    build_auth_url,
    exchange_code,
    generate_pkce_pair,
    revoke_token,
    store_token,
)
from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.auth.vault import REFRESH_THRESHOLD_SECONDS, consume_nonce, store_nonce
from app.middleware.audit import log_event
from app.middleware.tenancy import get_current_org_id
from app.rbac import _get_user_id_from_token
from app.security import bearer, require_auth
from database.models.credentials import (
    ALTER_CREDENTIALS_ADD_REFRESH_FAILED,
    CREATE_CREDENTIALS_IDX_CONNECTOR,
    CREATE_CREDENTIALS_IDX_ORG,
    CREATE_CREDENTIALS_TABLE,
)

logger = logging.getLogger(__name__)

# Frontend OAuth callback target (CS-2 / AT-326 T4; FE route added in AT-325 T3).
#
# The provider redirects the browser to the backend callback below; the backend
# then redirects to the frontend /oauth/callback page (handled by
# OAuthCallbackPage), which reads ?status, ?connected and ?code, then routes the
# user back to Integration Hub. The success/error query formats below are the
# AT-326 T4 contract and must stay in lock-step with OAuthCallbackPage.
#
# The base URL is SERVER-CONTROLLED config (env var), never derived from request
# input — this preserves the open-redirect protection from T1-S10-A. It defaults
# to a relative path so same-origin / reverse-proxied deployments need no config;
# set OAUTH_FRONTEND_BASE_URL (e.g. https://app.example.com) when the frontend is
# served from a different origin than the backend.
_FRONTEND_BASE_URL = os.environ.get("OAUTH_FRONTEND_BASE_URL", "").rstrip("/")
_FRONTEND_CALLBACK_PATH = "/oauth/callback"

# Hardcoded redirect templates — only {connector_id} / {error_code} are filled,
# both from server-side state (the nonce store / fixed error codes), never from
# request input. status= is a literal so OAuthCallbackPage can branch on it.
OAUTH_SUCCESS_REDIRECT = (
    _FRONTEND_BASE_URL + _FRONTEND_CALLBACK_PATH + "?connected={connector_id}&status=success"
)
OAUTH_ERROR_REDIRECT = (
    _FRONTEND_BASE_URL + _FRONTEND_CALLBACK_PATH + "?status=error&code={error_code}"
)

_NONCE_TTL_SECONDS = 600  # 10-minute window for state nonce validity

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
    """No-op. The credentials and oauth_nonces tables are provisioned externally.

    Created by database/provision/provision.sh; the application no longer
    creates these tables at runtime.
    """
    return None


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
        cur = con.cursor()
        cur.execute(
            "INSERT INTO oauth_nonces (nonce, connector_id, expires_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (nonce) DO UPDATE SET "
            "connector_id = EXCLUDED.connector_id, expires_at = EXCLUDED.expires_at, "
            "is_deleted = FALSE",
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
        cur = con.cursor()
        cur.execute(
            "SELECT nonce, connector_id, expires_at FROM oauth_nonces "
            "WHERE nonce = %s AND is_deleted = FALSE",
            (state,),
        )
        row = cur.fetchone()
        if row is not None:
            # Soft delete (app role has no DELETE): the filtered read above makes a
            # replay of the same state return None on the second attempt.
            cur.execute(
                "UPDATE oauth_nonces SET is_deleted = TRUE WHERE nonce = %s", (state,)
            )
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
        raise HTTPException(status_code=400, detail='Invalid state')

    return connector_id


# ---------------------------------------------------------------------------
# Callback auth (dev-gated)
# ---------------------------------------------------------------------------


def _callback_allows_unauth() -> bool:
    """Whether the OAuth callback may complete without a Bearer header.

    A provider's top-level browser redirect cannot carry an Authorization
    header, so the live browser flow can only complete locally if the callback
    accepts an unauthenticated request. This is OFF by default — production
    behaviour (Bearer required, AC17) is unchanged. Set OAUTH_CALLBACK_ALLOW_UNAUTH=1
    in a local .env to enable. The route stays protected by the single-use,
    TTL-bounded state nonce (consume_nonce), which is the real CSRF defence here.
    """
    return os.environ.get("OAUTH_CALLBACK_ALLOW_UNAUTH", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _callback_auth(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> Optional[str]:
    """Require a Bearer token unless OAUTH_CALLBACK_ALLOW_UNAUTH is set.

    When a token is present it is always validated (so a bad token is still
    rejected). When absent, it is allowed only in the dev-gated case.
    """
    if creds is not None and creds.scheme.lower() == "bearer":
        return require_auth(creds)
    if _callback_allows_unauth():
        return None
    raise HTTPException(status_code=401, detail="Unauthorized")


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
        token: Optional[str] = Depends(_callback_auth),
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

        nonce_data = consume_nonce(state)
        if nonce_data is None:
            # AC3: HTTP 400 on state mismatch, generic message, no detail
            raise HTTPException(status_code=400, detail="Invalid request")
        connector_id = nonce_data["connector_id"]
        code_verifier = nonce_data.get("code_verifier")
        # Org carried from initiation: the callback runs on an unauthenticated
        # browser redirect with no JWT/org context of its own, so the authenticated
        # org is captured in get_auth_url and threaded through the server-side state
        # nonce, then read back here. There is NO hardcoded 'default' fallback
        # (R17-D3 §1): a nonce with no org cannot be safely bound to a tenant, so the
        # flow fails closed rather than storing the token under the wrong org.
        org_id = nonce_data.get("org_id")
        if not org_id:
            logger.error(
                "OAuth callback for connector %s has no authenticated org in state "
                "nonce — refusing to store token under a default org",
                connector_id,
            )
            return RedirectResponse(
                OAUTH_ERROR_REDIRECT.format(error_code="no_org"),
                status_code=302,
            )

        config = CONNECTOR_AUTH_CONFIGS.get(connector_id)
        if config is None:
            return RedirectResponse(
                OAUTH_ERROR_REDIRECT.format(error_code="unknown_connector"),
                status_code=302,
            )

        try:
            token_response = await exchange_code(config, code, code_verifier=code_verifier)
            store_token(org_id, connector_id, token_response)
        except Exception:
            logger.exception("Token exchange failed for connector %s", connector_id)
            return RedirectResponse(
                OAUTH_ERROR_REDIRECT.format(error_code="exchange_failed"),
                status_code=302,
            )

        # Reflect the connection in this org's connector state so GET /api/connectors
        # reports "connected" and the Integration Hub tile updates (CS-2 AC6). The old
        # POST /api/connectors/{id}/connect set this; the OAuth flow replaced that call
        # (CS-2 AC8), so the success path must set it here. Display-state only — token
        # storage above is the source of truth, so a failure here must not fail the
        # flow (the token is already saved).
        try:
            record = db.org_connector_get(org_id, connector_id) or {}
            record["status"] = "connected"
            record["lastSynced"] = record.get("lastSynced", "—")
            db.org_connector_set(org_id, connector_id, record)
        except Exception:
            logger.exception("Failed to mark connector %s connected", connector_id)

        # CS-2 live ingest: capture the instance/site URL discovered during OAuth
        # so discovery runs can ingest live without separate env config.
        # Salesforce returns instance_url in the token response; ServiceNow's host
        # comes from its connector config. Best-effort — never fail the flow.
        try:
            from app.live_ingest_credentials import (
                capture_instance_url,
                fetch_jira_gateway_base,
                store_connector_instance_url,
            )

            instance_url = capture_instance_url(
                connector_id, token_response, config.token_url
            )
            if instance_url is None and connector_id == "jira":
                # Jira Cloud OAuth: discover the cloudId and build the
                # api.atlassian.com gateway base so discovery can call /rest/...
                # through it with the Bearer token.
                instance_url = await fetch_jira_gateway_base(
                    token_response.get("access_token", "")
                )
            if instance_url:
                store_connector_instance_url(org_id, connector_id, instance_url)
        except Exception:
            logger.exception("Failed to capture instance URL for connector %s", connector_id)

        log_event(
            "connector_connected",
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token) if token else None,
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
    def get_auth_url(connector_id: str) -> Dict[str, object]:
        """Generate a one-time authorization URL for the given connector.

        State nonce is cryptographically random (secrets.token_urlsafe(32)),
        stored server-side, and contains no redirect URL or user data (AC1).

        The response also echoes the exact OAuth ``scopes`` being requested so the
        admin can be shown what permissions are about to be granted *before* the
        consent redirect (R16-A2 §3 / AT-420: "surface the requested scopes to
        the admin during the consent step"). For Slack these are the minimal,
        public-channels-only read scopes.
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
        # PKCE (RFC 7636): bind a per-request verifier to the state nonce and
        # send its S256 challenge in the authorize URL. Required by providers
        # that enforce PKCE (e.g. Salesforce "Require PKCE").
        code_verifier, code_challenge = generate_pkce_pair()
        # Capture the AUTHENTICATED org now (this route is Bearer-protected, so the
        # tenancy middleware has resolved a real org) and thread it through the
        # server-side state nonce. The unauthenticated callback reads it back to
        # persist the token + connection state under the right tenant — never a
        # hardcoded 'default' (R17-D3 §1). Sourced server-side, never from callback
        # input.
        store_nonce(
            state,
            connector_id,
            code_verifier=code_verifier,
            org_id=get_current_org_id(),
        )
        auth_url = build_auth_url(config, state, code_challenge=code_challenge)
        return {
            "auth_url": auth_url,
            "connector_id": connector_id,
            "scopes": list(config.scopes),
        }

    @app.delete(
        "/api/connectors/{connector_id}/token",
    )
    async def delete_token(connector_id: str, token: str = Depends(require_auth)) -> Response:
        """Revoke and delete the stored token for the given connector (AC11/AC12/AC13)."""
        org_id = get_current_org_id()
        await revoke_token(org_id, connector_id)
        # Mirror the connect path: clear this org's connection state so the tile
        # returns to disconnected after revoke.
        try:
            record = db.org_connector_get(org_id, connector_id)
            if record is not None and record.get("org_id") == org_id:
                record["status"] = "disconnected"
                db.org_connector_set(org_id, connector_id, record)
        except Exception:
            logger.exception("Failed to clear connector %s connection state", connector_id)
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

        # Scope to the caller's org — the OAuth callback stores the credential under
        # the org carried in the state nonce (the authenticated org), and disconnect
        # reads with get_current_org_id() too. Using a hardcoded "default" here looked
        # up the wrong org for a real workspace, found no row, and returned needs_auth
        # → the tile showed "Token expired" / "Reconnect" right after a successful
        # connect.
        org_id = get_current_org_id()

        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT expires_at, refresh_failed, refresh_token FROM credentials "
                "WHERE org_id = %s AND connector_id = %s AND is_deleted = FALSE",
                (org_id, connector_id),
            )
            row = cur.fetchone()
        finally:
            con.close()

        if row is None:
            return {"status": "needs_auth"}

        expires_at_str, refresh_failed_flag, refresh_token_enc = row[0], row[1], row[2]
        # A stored refresh token means the vault can silently mint a new access
        # token on the next use (get_token auto-refreshes within/after the expiry
        # window). So an expired access token is NOT a re-auth prompt as long as a
        # refresh token is held and the last refresh did not fail — otherwise the
        # user would be forced to re-run the OAuth flow every time the short-lived
        # access token lapses (ServiceNow ~30 min, Salesforce/Jira ~1 h).
        has_refresh_token = refresh_token_enc is not None and str(refresh_token_enc) != ""
        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        seconds_left = (expires_at - now).total_seconds()

        # Near-expiry OR already expired: refreshable unless the refresh token is
        # gone or a prior refresh failed. 'needs_refresh' keeps the connector shown
        # as connected (the tile only prompts re-auth on needs_auth/refresh_failed).
        if seconds_left <= REFRESH_THRESHOLD_SECONDS:
            if refresh_failed_flag:
                return {"status": "refresh_failed"}
            if has_refresh_token:
                return {"status": "needs_refresh"}
            # No refresh token to fall back on — the user must reconnect.
            return {"status": "needs_auth"}
        return {"status": "connected"}
 
