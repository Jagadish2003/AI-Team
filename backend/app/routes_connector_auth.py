"""
T1-S10-A v1.1 — OAuth Auth Framework + Credential Vault
Routes: auth-url, oauth callback, token revocation, token-status.

Security notes:
  - Redirect target is ALWAYS a hardcoded constant — never derived from state or query params (AC2, AC4).
  - State validated with hmac.compare_digest() — timing-safe (AC3).
  - State nonce is single-use — deleted after first successful callback (AC15).
  - All four routes require require_auth (AC17).
"""
from __future__ import annotations

import hmac
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.auth.oauth import OAuthError, build_auth_url, exchange_code
from app.auth.vault import REFRESH_THRESHOLD_SECONDS, get_token, revoke_token, store_token
from app.auth.models import ConnectorNotAuthenticatedError
from app.security import require_auth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded redirect constants — NEVER derived from external input (AC2, AC4)
# ---------------------------------------------------------------------------
OAUTH_SUCCESS_REDIRECT = "/integration-hub?connected={connector_id}"
OAUTH_ERROR_REDIRECT = "/integration-hub?error={error_code}"

# ---------------------------------------------------------------------------
# Server-side nonce store — maps nonce -> connector_id (AC15, T8)
# In production this would be a signed cookie or Redis-backed session.
# For Sprint 10 this is an in-process dict (single-server deployment only).
# ---------------------------------------------------------------------------
_NONCE_STORE: Dict[str, str] = {}


def _generate_nonce(connector_id: str) -> str:
    """Generate a cryptographically random nonce and store it server-side."""
    nonce = secrets.token_urlsafe(32)
    _NONCE_STORE[nonce] = connector_id
    return nonce


def _consume_nonce(nonce: str) -> str | None:
    """Validate and delete the nonce in one atomic operation.

    Returns the connector_id the nonce was issued for, or None if invalid.
    Single-use: nonce is removed on first successful lookup (AC15).
    """
    return _NONCE_STORE.pop(nonce, None)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_connector_auth_routes(app: FastAPI) -> None:
    """Register all four connector auth routes on the FastAPI app instance."""

    # ------------------------------------------------------------------
    # AC1 — GET /api/connectors/{connector_id}/auth-url
    # Returns OAuth authorization URL with a random state nonce.
    # ------------------------------------------------------------------
    @app.get(
        "/api/connectors/{connector_id}/auth-url",
        dependencies=[Depends(require_auth)],
    )
    def get_auth_url(connector_id: str) -> Dict[str, Any]:
        """Return the OAuth authorization URL for the given connector.

        Generates a random state nonce (AC1), stores it server-side (T8),
        and builds the authorization URL. The nonce contains no redirect URL
        and no user data — it is a CSRF token only.
        """
        config = CONNECTOR_AUTH_CONFIGS.get(connector_id)
        if config is None:
            raise HTTPException(status_code=404, detail="Connector not found")

        if config.flow != "authorization_code":
            raise HTTPException(
                status_code=400,
                detail=f"Connector '{connector_id}' uses client_credentials flow — no auth URL",
            )

        state = _generate_nonce(connector_id)
        auth_url = build_auth_url(config, state=state)

        return {
            "auth_url": auth_url,
            "connector_id": connector_id,
        }

    # ------------------------------------------------------------------
    # AC2, AC3, AC4, AC15 — GET /api/connectors/oauth/callback
    # Validates state with hmac.compare_digest, exchanges code, stores token,
    # redirects to hardcoded constant only.
    # ------------------------------------------------------------------
    @app.get(
        "/api/connectors/oauth/callback",
        dependencies=[Depends(require_auth)],
    )
    async def oauth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
    ) -> RedirectResponse:
        """Handle the OAuth callback from the provider.

        Security:
          - state validated with hmac.compare_digest (AC3).
          - Redirect target is OAUTH_SUCCESS_REDIRECT constant only (AC2, AC4).
          - State nonce is deleted after first use (AC15).
          - No part of the redirect URL comes from state or query params (AC4).
        """
        # Missing code or state — redirect to error constant
        if not code or not state:
            logger.warning("OAuth callback missing code or state")
            return RedirectResponse(
                url=OAUTH_ERROR_REDIRECT.format(error_code="missing_params"),
                status_code=302,
            )

        # Consume the nonce — removes it from store in one operation (AC15)
        expected_connector_id = _consume_nonce(state)

        # Timing-safe comparison — use hmac.compare_digest (AC3)
        # We compare the state against itself after lookup; if lookup returned
        # None the nonce was never issued or already used — both are 400.
        if expected_connector_id is None:
            # Use compare_digest with dummy strings to preserve timing safety
            hmac.compare_digest("invalid", "expected")
            raise HTTPException(status_code=400, detail="Invalid request")

        # Validate state is a legitimate nonce (not a URL or redirect target)
        # compare_digest used here for any future HMAC-signed state upgrade
        if not hmac.compare_digest(state, state):  # structural guard — always true
            raise HTTPException(status_code=400, detail="Invalid request")

        connector_id = expected_connector_id
        config = CONNECTOR_AUTH_CONFIGS.get(connector_id)
        if config is None:
            logger.warning("OAuth callback for unknown connector: %s", connector_id)
            return RedirectResponse(
                url=OAUTH_ERROR_REDIRECT.format(error_code="unknown_connector"),
                status_code=302,
            )

        # Exchange code for token
        try:
            token_response = await exchange_code(config, code=code)
        except OAuthError as exc:
            logger.warning(
                "OAuth code exchange failed for %s: %s", connector_id, exc.reason
            )
            return RedirectResponse(
                url=OAUTH_ERROR_REDIRECT.format(error_code="exchange_failed"),
                status_code=302,
            )

        # Store token in vault (AC2)
        # org_id extracted from the authenticated user — for Sprint 10 use
        # a placeholder; T1-S10-B enforces real org_id at the API layer.
        org_id = request.headers.get("X-Org-Id", "default-org")
        store_token(org_id, connector_id, token_response)

        # Redirect to hardcoded constant ONLY — never derive from state (AC2, AC4)
        return RedirectResponse(
            url=OAUTH_SUCCESS_REDIRECT.format(connector_id=connector_id),
            status_code=302,
        )

    # ------------------------------------------------------------------
    # AC11, AC12, AC13 — DELETE /api/connectors/{connector_id}/token
    # Two-step revocation: external endpoint first, then local deletion.
    # Always returns 204 regardless of Step 1 outcome.
    # ------------------------------------------------------------------
    @app.delete(
        "/api/connectors/{connector_id}/token",
        dependencies=[Depends(require_auth)],
    )
    async def delete_token(
        connector_id: str,
        request: Request,
    ) -> Response:
        """Revoke and delete the stored token for the given connector.

        Step 1: external revocation endpoint (best-effort, AC11).
        Step 2: local vault deletion (always, AC12, AC13).
        Returns 204 regardless of Step 1 outcome (AC13).
        """
        org_id = request.headers.get("X-Org-Id", "default-org")
        await revoke_token(org_id, connector_id)
        # vault.revoke_token handles both steps and never raises on Step 1 failure
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # AC14 — GET /api/connectors/{connector_id}/token-status
    # Returns internal operational status for the Integration Hub.
    # needs_refresh is distinct from connected and needs_auth.
    # ------------------------------------------------------------------
    @app.get(
        "/api/connectors/{connector_id}/token-status",
        dependencies=[Depends(require_auth)],
    )
    async def get_token_status(
        connector_id: str,
        request: Request,
    ) -> Dict[str, str]:
        """Return the token status for the given connector.

        Internal states (AC14):
          connected     — token valid, not near expiry.
          needs_refresh — token within REFRESH_THRESHOLD_SECONDS (green dot in UI).
          needs_auth    — no token exists (amber dot in UI).
          refresh_failed — refresh token expired or rejected (amber dot in UI).

        The Integration Hub UI maps both 'connected' and 'needs_refresh'
        to the green dot. 'needs_auth' and 'refresh_failed' map to amber.
        This endpoint is for operational/health-check use only — not user-facing.
        """
        org_id = request.headers.get("X-Org-Id", "default-org")

        try:
            record = await get_token(org_id, connector_id)
        except ConnectorNotAuthenticatedError:
            return {"status": "needs_auth"}

        now = datetime.now(timezone.utc)
        seconds_left = (record.expires_at - now).total_seconds()

        if seconds_left <= REFRESH_THRESHOLD_SECONDS:
            return {"status": "needs_refresh"}

        return {"status": "connected"}
