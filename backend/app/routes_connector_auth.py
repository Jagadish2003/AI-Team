"""OAuth connector auth routes — AT-77 (T1-S10-A A5): OAuth callback and state security.

Four OAuth routes (all require Bearer auth — spec AC17):
  GET  /api/connectors/oauth/callback           — OAuth callback (Bearer + state nonce)
  GET  /api/connectors/{connector_id}/auth-url  — Generate one-time auth URL
  DELETE /api/connectors/{connector_id}/token   — Revoke token
  GET  /api/connectors/{connector_id}/token-status — Token status

Plus the unified per-tile disconnect used by the Integration Hub (R18-C0 P4 / AT-566):
  DELETE /api/connectors/{connector_id}         — analyst+: revoke WHICHEVER credential
                                                  kind the org holds (OAuth token and/or
                                                  static credential) and mark the tile
                                                  disconnected. Idempotent.

Plus the static-credential entry surface for connectors that authenticate with a
URL + username + token/password rather than OAuth (R17-D3 Addendum A, T12 / AC10 —
Jira, ServiceNow, native DB connectors):
  POST   /api/connectors/{connector_id}/credentials — Owner-only: encrypt into the vault
  GET    /api/connectors/{connector_id}/credentials — status only (never the secret)
  DELETE /api/connectors/{connector_id}/credentials — Owner-only: revoke

Values are WRITE-ONLY: the POST stores them Fernet-encrypted in the per-org vault
(via store_static_credential), and neither the GET nor the POST response ever
returns the username or secret — an Owner can replace a credential but never read
one back through the UI, and the action is audited without the values (AC10).

State nonce storage is owned by app.auth.vault and its provisioned ``nonces``
table. No session/cookie mechanism exists in this codebase; nonces are stored
server-side with a 10-minute TTL and are soft-deleted on first use.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets as _secrets_mod
from datetime import datetime, timezone
from typing import Dict, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from app import db
from app.auth import (
    OAuthError,
    build_auth_url,
    exchange_code,
    generate_pkce_pair,
    get_client_credentials_token,
    revoke_token,
    store_token,
)
from app import license_limits
from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.auth.oauth_state import decode_state, encode_state
from app.auth.secrets import MissingSecretError
from app.auth.auth_modes import (
    all_known_connector_ids,
    connector_supports_mode,
    get_connector_auth_capability,
    resolve_auth_mode,
    set_auth_mode,
)
from app import network_profile
from app.auth.vault import (
    REFRESH_THRESHOLD_SECONDS,
    consume_nonce,
    get_jwt_bearer_credential_metadata,
    get_static_credential_metadata,
    revoke_jwt_bearer_credential,
    revoke_static_credential,
    store_jwt_bearer_credential,
    store_nonce,
    store_static_credential,
)
from app.middleware.audit import log_event
from app.middleware.tenancy import get_current_org_id
from app.rbac import _get_user_id_from_token, require_role
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

# ---------------------------------------------------------------------------
# Callback auth (dev-gated)
# ---------------------------------------------------------------------------


def _is_production() -> bool:
    """True when ENVIRONMENT names a production deployment.

    Mirrors the check in app.auth.user_auth so dangerous dev-only flags share one
    notion of "production" across the codebase.
    """
    return os.environ.get("ENVIRONMENT", "").strip().lower() == "production"


def _callback_allows_unauth() -> bool:
    """Whether the OAuth callback may complete without a Bearer header.

    A provider's top-level browser redirect cannot carry an Authorization
    header, so the live browser flow can only complete locally if the callback
    accepts an unauthenticated request. This is OFF by default — production
    behaviour (Bearer required, AC17) is unchanged. Set OAUTH_CALLBACK_ALLOW_UNAUTH=1
    in a local .env to enable. The route stays protected by the single-use,
    TTL-bounded, HMAC-signed state nonce, which is the real CSRF/tenant defence.

    SECURITY BOUNDARY (R17-D3 review M1/H2): this flag effectively disables the
    callback's tenant-binding auth, so it is honoured ONLY outside production. When
    ENVIRONMENT=production it is force-ignored (and warned about) even if the env
    var is set, so a staging/prod misconfiguration cannot open the unauthenticated
    path.
    """
    # WARNING: never set OAUTH_CALLBACK_ALLOW_UNAUTH in a shared/staging/production
    # deployment — it is a local-dev convenience only and is force-disabled in prod.
    enabled = os.environ.get("OAUTH_CALLBACK_ALLOW_UNAUTH", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if enabled and _is_production():
        logger.warning(
            "OAUTH_CALLBACK_ALLOW_UNAUTH is set but ENVIRONMENT=production — "
            "ignoring it; the OAuth callback still requires a Bearer token."
        )
        return False
    return enabled


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
# Static (non-OAuth) connector credentials — R17-D3 Addendum A, T12 / AC10
# ---------------------------------------------------------------------------

# Connectors that authenticate with STATIC credentials (URL + username +
# token/password) entered by an Owner, rather than the OAuth flow above. Jira and
# ServiceNow ALSO support OAuth (handled by the Connect flow); this static path is
# the addendum's "credential form" alternative for them and the primary path for
# the native DB connectors. Keyed to the ids the Integration Hub / connector
# catalog use ('sql_server'); 'sqlserver' is accepted as an alias so a caller
# using the DB-driver id resolves too. The vault keys purely on the string passed,
# so this set is also the guard against pointing the static path at an OAuth-only
# connector (e.g. salesforce), which is rejected with a 400.
STATIC_CREDENTIAL_CONNECTORS = frozenset(
    {"jira", "servicenow", "postgresql", "sql_server", "sqlserver", "oracle_db"}
)


class StaticCredentialRequest(BaseModel):
    """Body for POST /api/connectors/{id}/credentials.

    All three are entered by an Owner. base_url is the non-secret instance
    location; username + secret are WRITE-ONLY — encrypted into the vault and
    never returned. Fields default to empty so the handler can return one clear
    400 naming the missing field(s) instead of a raw 422.
    """

    base_url: str = ""
    username: str = ""
    secret: str = ""


class StaticCredentialStatus(BaseModel):
    """Response for the credential POST/GET — metadata ONLY (AC10).

    Never carries the username or secret. base_url is non-secret and is echoed so
    the admin can confirm which instance is wired; has_username reports presence
    only, so the UI can show "a credential is set" without revealing the value.
    """

    connector_id: str
    configured: bool
    base_url: Optional[str] = None
    has_username: bool = False
    updated_at: Optional[str] = None


class JwtBearerCredentialRequest(BaseModel):
    """Body for POST /api/connectors/{id}/jwt-credentials (R18-A3 T2 / AT-555).

    The Salesforce connected-app JWT bearer material an Owner enters once:
      login_url    — the login/instance host (e.g. https://login.salesforce.com),
                     used as the assertion audience + token endpoint (non-secret).
      username     — the Salesforce username the assertion runs as (the `sub`).
      private_key  — the PEM cert private key (WRITE-ONLY: encrypted into the
                     vault, never returned, never logged).
    Fields default to empty so the handler returns one clear 400 naming the
    missing field(s) rather than a raw 422.
    """

    login_url: str = ""
    username: str = ""
    private_key: str = ""


class ClientCredentialsConnectStatus(BaseModel):
    """Response for POST /api/connectors/{id}/client-credentials (R18-A3 T3 / AT-556).

    The client-credentials connect takes NO body — the credential is the
    deployment's app client secret (the ``{CONNECTOR}_CLIENT_SECRET`` env var), not
    a per-user entry — so the response just confirms the outbound connect landed:
    the connector is authenticated, and its per-org auth mode is now
    ``client_credentials``. Carries no token or secret (AC5).
    """

    connector_id: str
    connected: bool
    auth_mode: str


class ConnectorAuthCapability(BaseModel):
    """Per-connector auth capability for the Integration Hub (R18-A3 T5 / AT-558).

    Pairs with the deployment ``network_profile`` so the UI can decide, per tile,
    whether to offer the authorization-code connect flow or route the customer to
    the outbound setup path. ``has_outbound_only_mode`` is the load-bearing flag:
    in ``no_public_inbound`` the UI hides the authorization-code Connect button
    exactly for connectors where this is true (AC4).
    """

    connector_id: str
    supported_auth_modes: list[str]
    outbound_only_modes: list[str]
    has_outbound_only_mode: bool
    default_auth_mode: Optional[str] = None


class NetworkProfileResponse(BaseModel):
    """Response for GET /api/network-profile (R18-A3 T5 / AT-558).

    Carries the deployment's inbound-network posture and the per-connector auth
    capability map the UI uses to gate connect flows. No secrets — capability and
    profile only.
    """

    network_profile: str
    no_public_inbound: bool
    connectors: Dict[str, ConnectorAuthCapability]


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_connector_auth_routes(app: FastAPI) -> None:

    @app.get(
        "/api/network-profile",
        response_model=NetworkProfileResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
    )
    def get_network_profile_info() -> NetworkProfileResponse:
        """Return the deployment network profile + per-connector auth capability.

        R18-A3 T5 (AT-558). The frontend pairs ``network_profile`` with each
        connector's ``has_outbound_only_mode`` to decide whether to offer the
        authorization-code connect flow or the outbound setup path — in a
        ``no_public_inbound`` deployment the authorization-code Connect button is
        hidden wherever an outbound-only mode exists, so the customer can never
        start a flow that cannot complete (AC4). Read-only; carries no secrets.
        """
        profile = network_profile.get_network_profile()
        capabilities = {
            connector_id: ConnectorAuthCapability(**get_connector_auth_capability(connector_id))
            for connector_id in all_known_connector_ids()
        }
        return NetworkProfileResponse(
            network_profile=profile,
            no_public_inbound=network_profile.is_no_public_inbound(),
            connectors=capabilities,
        )

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

        # T2 (AT-447): the initiating org is carried in the signed `state` param.
        # Verify the HMAC signature FIRST — a tampered or forged state (e.g. one
        # whose org_id was swapped to another tenant) fails here and is rejected
        # before any nonce lookup or token exchange.
        decoded = decode_state(state)
        if decoded is None:
            # AC3: HTTP 400 on state mismatch/tamper, generic message, no detail.
            # Diagnostic (server-side only; the response stays generic): the state
            # failed HMAC/format verification. Causes: a tampered/forged state, or
            # the state was signed with a different OAUTH_STATE_SECRET/JWT_SECRET
            # than this process uses (e.g. the secret was rotated, or differs
            # between the instance that issued the auth-url and the one handling
            # the callback). Never log the state value itself.
            logger.warning(
                "OAuth callback rejected (400): state failed signature/format "
                "verification. Ensure OAUTH_STATE_SECRET (or JWT_SECRET) is set and "
                "identical across the instances issuing and receiving the flow."
            )
            raise HTTPException(status_code=400, detail="Invalid request")
        state_org_id = decoded["org_id"]
        nonce = decoded["nonce"]

        # Consume the single-use, server-side nonce the state points at. This both
        # enforces single-use (replay → None) and yields the connector_id, PKCE
        # verifier, and the org bound to the flow at initiation.
        nonce_data = consume_nonce(nonce)
        if nonce_data is None:
            # Diagnostic (server-side only; response stays generic): the state's
            # signature was valid but its single-use nonce is gone. Causes, in
            # order of likelihood: (1) the authorize URL was reused/refreshed or
            # the browser navigated back — the nonce is single-use and was already
            # consumed by an earlier callback; (2) the flow took longer than the
            # 10-minute nonce window (common when the provider requires an admin
            # CONSENT/approval step, e.g. SharePoint's Sites.Read.All) so the nonce
            # expired; (3) the nonce was never stored. Start a FRESH Connect
            # (don't reuse/refresh the consent tab) and complete consent promptly.
            logger.warning(
                "OAuth callback rejected (400) for org %s: state signature OK but "
                "its single-use nonce was not found/expired/already-used. Likely a "
                "reused or refreshed authorize URL, or the consent/approval step "
                "exceeded the 10-minute nonce window. Start a fresh Connect.",
                state_org_id,
            )
            raise HTTPException(status_code=400, detail="Invalid request")
        connector_id = nonce_data["connector_id"]
        code_verifier = nonce_data.get("code_verifier")
        # The callback runs on an unauthenticated browser redirect with no JWT/org
        # context of its own. The authenticated org was captured at initiation
        # (get_auth_url) and stored server-side AND signed into the state. There is
        # NO hardcoded 'default' fallback (R17-D3 §1): a flow with no bound org
        # fails closed rather than storing the token under the wrong tenant.
        stored_org_id = nonce_data.get("org_id")
        if not stored_org_id:
            logger.error(
                "OAuth callback for connector %s has no authenticated org bound to "
                "its state nonce — refusing to store token under a default org",
                connector_id,
            )
            return RedirectResponse(
                OAUTH_ERROR_REDIRECT.format(error_code="no_org"),
                status_code=302,
            )
        # T2-AC2/AC3: the org carried in the (signature-verified) state must match
        # the org the server bound to this nonce when the flow started. A mismatch
        # means the callback is being associated with a different tenant than the
        # one that initiated the authorization — refuse it (generic 400, no detail).
        if not hmac.compare_digest(state_org_id, stored_org_id):
            logger.error(
                "OAuth callback tenant mismatch for connector %s: state org and "
                "nonce org disagree — refusing to bind the flow to the wrong tenant",
                connector_id,
            )
            raise HTTPException(status_code=400, detail="Invalid request")
        org_id = stored_org_id

        config = CONNECTOR_AUTH_CONFIGS.get(connector_id)
        if config is None:
            return RedirectResponse(
                OAUTH_ERROR_REDIRECT.format(error_code="unknown_connector"),
                status_code=302,
            )

        # R17-D4 Addendum A / T9: authoritative connection-time limit check. The
        # get_auth_url gate stops most over-limit flows up front, but re-check
        # here (the real connection point) before exchanging/storing a token so a
        # flow that slipped past — or a limit reached mid-consent — never adds a
        # system beyond the licensed max_systems. Forward-only: an already
        # connected connector re-authorising is not a new system and passes.
        # Redirect (don't raise) — the browser is completing a provider redirect.
        if not license_limits.can_connect_new_system(org_id, connector_id):
            logger.warning(
                "OAuth callback for connector %s blocked: org %s is at its "
                "licensed max_systems limit — not connecting a new system",
                connector_id,
                org_id,
            )
            return RedirectResponse(
                OAUTH_ERROR_REDIRECT.format(error_code="system_limit"),
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
            # Connecting a source now makes it immediately usable — there is no
            # separate "Configure & Sync" step. Mark it configured (discovery-ready)
            # right here so the Integration Hub tile goes straight from Connect to
            # "View data" (the /configure flag-flip is now automatic on connect).
            record["configured"] = True
            record["lastSynced"] = "Just now"
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
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
    )
    def get_auth_url(connector_id: str) -> Dict[str, object]:
        """Generate a one-time authorization URL for the given connector.

        The state parameter is built from a cryptographically random, single-use
        nonce (secrets.token_urlsafe(32)) stored server-side, plus the initiating
        org carried HMAC-signed alongside it (R17-D3 / AT-447 T2). It contains no
        redirect URL and no user PII (AC1) — the org_id is a non-secret internal
        id, and the signature makes the carried tenant tamper-evident.

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

        # R18-A3 T5 / AC4: in a no-public-inbound deployment the authorization-code
        # callback can never arrive, so an authorization_code flow cannot complete.
        # Where the connector has an outbound-only mode (JWT bearer / client-
        # credentials / static), refuse to START the flow server-side — the UI
        # already hides the Connect button (NetworkProfileContext), and this closes
        # the same gap for direct API callers so a user can never begin a flow that
        # cannot finish. The response points at the outbound setup path.
        if network_profile.is_no_public_inbound():
            capability = get_connector_auth_capability(connector_id)
            if capability.get("has_outbound_only_mode"):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "authorization_code flow is unavailable in a "
                        "no_public_inbound deployment for a connector with an "
                        "outbound-only auth mode; use the outbound setup path"
                    ),
                )

        # R17-D4 Addendum A / T9: block STARTING an OAuth flow for a NEW system
        # once the org is at its licensed max_systems, so the user is stopped
        # before the provider consent step rather than after exchanging a token.
        # Re-authorising an already-connected connector is not a new system and
        # is never blocked (forward-only). Raises HTTP 402 at the limit.
        license_limits.enforce_can_connect(get_current_org_id(), connector_id)

        nonce = _secrets_mod.token_urlsafe(32)
        # PKCE (RFC 7636): bind a per-request verifier to the state nonce and
        # send its S256 challenge in the authorize URL. Required by providers
        # that enforce PKCE (e.g. Salesforce "Require PKCE").
        code_verifier, code_challenge = generate_pkce_pair()
        # Capture the AUTHENTICATED org now (this route is Bearer-protected, so the
        # tenancy middleware has resolved a real org). It is bound server-side to
        # the single-use nonce AND carried — HMAC-signed — through the OAuth state
        # parameter (R17-D3 §1 / AT-447 T2). The unauthenticated callback reads the
        # org back from both and verifies they agree, so the flow can only ever be
        # completed for the tenant that started it — never a hardcoded 'default',
        # and never another tenant. Sourced server-side, never from callback input.
        org_id = get_current_org_id()
        store_nonce(
            nonce,
            connector_id,
            code_verifier=code_verifier,
            org_id=org_id,
        )
        # The state the provider echoes back carries the org + nonce, signed so the
        # callback can detect any tampering with the bound tenant (T2-AC1).
        state = encode_state(org_id, nonce)
        auth_url = build_auth_url(config, state, code_challenge=code_challenge)
        return {
            "auth_url": auth_url,
            "connector_id": connector_id,
            "scopes": list(config.scopes),
        }

    @app.delete(
        "/api/connectors/{connector_id}/token",
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
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

    @app.delete(
        "/api/connectors/{connector_id}",
        dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
    )
    async def disconnect_connector(
        connector_id: str, token: str = Depends(require_auth)
    ) -> Response:
        """Disconnect a connector for the caller's org (R18-C0 P4 / AT-566).

        The single action behind each connected Integration Hub tile's Disconnect
        control. It clears WHICHEVER credential kind the org holds for this
        connector and returns the tile to its unconnected state, so the frontend
        needs one call regardless of how the connector was authenticated:

          * OAuth connectors — ``revoke_token`` does best-effort external
            revocation (RFC 7009 / Slack ``auth.revoke``) and soft-deletes the
            vault row (its local delete is not kind-scoped, so it clears the row
            whatever its kind).
          * Static-credential connectors (Jira/ServiceNow token, native DBs) —
            ``revoke_static_credential`` soft-deletes the static vault row too, so
            the static path is cleared explicitly and stays correct even if
            ``revoke_token``'s local delete is ever narrowed to ``kind='oauth'``.

        Both revokes are org+connector scoped (org from the tenancy context, never
        the request — one org can never disconnect another's connector) and
        idempotent, so disconnecting a connector that was never connected or is
        already disconnected still returns 204 with no error. analyst+, matching
        the connect (auth-url) and revoke (token) routes.
        """
        org_id = get_current_org_id()

        await revoke_token(org_id, connector_id)
        revoke_static_credential(org_id, connector_id)

        # Mirror delete_token / delete_static_credentials: flip this org's
        # connection state to disconnected so GET /api/connectors reports the tile
        # unconnected. Display-state only — the vault revoke above is the source of
        # truth, so a failure here must not fail the disconnect. The org_id guard
        # (as in delete_token) avoids writing a spurious override for a connector
        # this org never connected.
        try:
            record = db.org_connector_get(org_id, connector_id)
            if record is not None and record.get("org_id") == org_id:
                record["status"] = "disconnected"
                db.org_connector_set(org_id, connector_id, record)
        except Exception:
            logger.exception(
                "Failed to clear connector %s connection state on disconnect",
                connector_id,
            )

        log_event(
            "connector_disconnected",
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
        )
        return Response(status_code=204)

    @app.get(
        "/api/connectors/{connector_id}/token-status",
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
    )
    async def get_token_status(connector_id: str) -> Dict[str, str]:
        """Return token status: connected | needs_refresh | needs_auth | refresh_failed (AC14)."""
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

        # A recorded failure means the connection needs re-auth REGARDLESS of the
        # stored expiry. This covers a server-side session invalidation flagged
        # during ingestion (e.g. Salesforce INVALID_SESSION_ID / HTTP 401), where
        # the access token was revoked BEFORE its stored expires_at — so a pure
        # expiry check would still read "connected" while every live call 401s.
        if refresh_failed_flag:
            return {"status": "refresh_failed"}

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

    # -----------------------------------------------------------------------
    # Static (non-OAuth) connector credentials — R17-D3 Addendum A, T12 / AC10
    # -----------------------------------------------------------------------

    @app.post(
        "/api/connectors/{connector_id}/credentials",
        response_model=StaticCredentialStatus,
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    )
    def set_static_credentials(
        connector_id: str,
        body: StaticCredentialRequest,
        token: str = Depends(require_auth),
    ) -> StaticCredentialStatus:
        """Store a static credential for the caller's org, Fernet-encrypted (AC10).

        Owner-only (require_role('owner')). URL + username + token/password are
        encrypted into the per-org vault via store_static_credential — the same
        encryption and per-(org_id, connector_id) keying as OAuth tokens. The org
        is taken from the tenancy context, NEVER the request body, so one org can
        never write another org's credential. Values are write-only: the response
        carries only non-secret metadata (never the username or secret), and the
        audit event records the action and actor without the values.
        """
        if connector_id not in STATIC_CREDENTIAL_CONNECTORS:
            # An OAuth connector (e.g. salesforce) or an unknown id — the static
            # path does not apply. 400 rather than a silent store under a stray id.
            raise HTTPException(
                status_code=400,
                detail=(
                    "Connector does not use static credentials — connect it "
                    "through the OAuth flow instead."
                ),
            )

        base_url = (body.base_url or "").strip()
        username = (body.username or "").strip()
        secret = body.secret or ""
        # All static connectors here need URL + username + secret. Report every
        # missing field at once as a plain-string detail the UI can show inline.
        missing = [
            label
            for label, value in (
                ("URL", base_url),
                ("username", username),
                ("token/password", secret.strip()),
            )
            if not value
        ]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required field(s): {', '.join(missing)}.",
            )

        org_id = get_current_org_id()
        try:
            record = store_static_credential(
                org_id,
                connector_id,
                username=username,
                secret=secret,
                base_url=base_url,
            )
        except ValueError:
            # Defensive: the vault also rejects an empty/whitespace secret.
            raise HTTPException(
                status_code=400, detail="A non-empty token/password is required."
            )
        except MissingSecretError:
            # CREDENTIAL_VAULT_KEY is not configured — an operator/server error,
            # not a client one. Never echo the key or the attempted values.
            logger.error(
                "Cannot store static credential for connector %s: the credential "
                "vault key (CREDENTIAL_VAULT_KEY) is not configured",
                connector_id,
            )
            raise HTTPException(
                status_code=500, detail="Credential vault is not configured."
            )

        # Mirror the OAuth success path: reflect the connection in this org's
        # connector state so the Integration Hub tile shows the source connected.
        # Display-state only — the vault write above is the source of truth, so a
        # failure here must never fail the request.
        try:
            rec = db.org_connector_get(org_id, connector_id) or {}
            rec["status"] = "connected"
            rec["lastSynced"] = rec.get("lastSynced", "—")
            db.org_connector_set(org_id, connector_id, rec)
        except Exception:
            logger.exception(
                "Failed to mark connector %s connected after credential entry",
                connector_id,
            )

        # Audit the ACTION and actor only — never the URL/username/secret (AC10).
        log_event(
            "connector_credentials_set",
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
        )
        return StaticCredentialStatus(
            connector_id=connector_id,
            configured=True,
            base_url=record.base_url or None,
            has_username=bool(record.username),
            updated_at=record.updated_at.isoformat(),
        )

    @app.get(
        "/api/connectors/{connector_id}/credentials",
        response_model=StaticCredentialStatus,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
    )
    def get_static_credential_status(connector_id: str) -> StaticCredentialStatus:
        """Report whether a static credential is configured — metadata only (AC10).

        Reads NON-SECRET metadata via get_static_credential_metadata, which never
        decrypts enc_username/enc_secret, so the secret cannot leak through this
        path and it does not even need the vault key. base_url is a non-secret
        instance location and is returned; the username value and secret never are.
        """
        if connector_id not in STATIC_CREDENTIAL_CONNECTORS:
            raise HTTPException(
                status_code=400, detail="Connector does not use static credentials."
            )
        org_id = get_current_org_id()
        meta = get_static_credential_metadata(org_id, connector_id)
        if meta is None:
            return StaticCredentialStatus(connector_id=connector_id, configured=False)
        return StaticCredentialStatus(
            connector_id=connector_id,
            configured=True,
            base_url=meta["base_url"] or None,
            has_username=meta["has_username"],
            updated_at=meta["updated_at"].isoformat(),
        )

    @app.delete(
        "/api/connectors/{connector_id}/credentials",
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    )
    def delete_static_credentials(
        connector_id: str, token: str = Depends(require_auth)
    ) -> Response:
        """Revoke (soft-delete) the org's static credential for this connector.

        Owner-only. Scoped to kind='static' in the vault so it can never remove an
        OAuth token row. Idempotent — a 204 even when nothing was stored.
        """
        if connector_id not in STATIC_CREDENTIAL_CONNECTORS:
            raise HTTPException(
                status_code=400, detail="Connector does not use static credentials."
            )
        org_id = get_current_org_id()
        revoke_static_credential(org_id, connector_id)
        # Mirror delete_token: clear this org's connection state after revoke.
        try:
            rec = db.org_connector_get(org_id, connector_id)
            if rec is not None and rec.get("org_id") == org_id:
                rec["status"] = "disconnected"
                db.org_connector_set(org_id, connector_id, rec)
        except Exception:
            logger.exception(
                "Failed to clear connector %s connection state after credential revoke",
                connector_id,
            )
        log_event(
            "connector_credentials_revoked",
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
        )
        return Response(status_code=204)

    # -----------------------------------------------------------------------
    # JWT bearer credential entry — R18-A3 T2 / AT-555 (AC1/AC5)
    #
    # Outbound-only Salesforce connect: an Owner enters the connected-app cert
    # private key once. It is vaulted (encrypted, write-only, never logged — AC5);
    # the access token is minted outbound from it on first ingest and re-minted by
    # re-assertion on expiry (get_token), so no inbound callback is ever needed.
    # -----------------------------------------------------------------------

    @app.post(
        "/api/connectors/{connector_id}/jwt-credentials",
        response_model=StaticCredentialStatus,
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    )
    def set_jwt_bearer_credentials(
        connector_id: str,
        body: JwtBearerCredentialRequest,
        token: str = Depends(require_auth),
    ) -> StaticCredentialStatus:
        """Store the JWT bearer signing material for the caller's org (AC5).

        Owner-only. The PEM private key + Salesforce username + login host are
        encrypted into the per-org vault via store_jwt_bearer_credential (same
        keying/encryption as any credential). The org is taken from the tenancy
        context, never the body. Values are write-only: the response is non-secret
        metadata only, and the audit event records the action/actor without them.
        Selecting a connector that has no jwt_bearer mode is a 400.
        """
        if not connector_supports_mode(connector_id, "jwt_bearer"):
            raise HTTPException(
                status_code=400,
                detail="Connector does not support the JWT bearer auth mode.",
            )

        login_url = (body.login_url or "").strip()
        username = (body.username or "").strip()
        private_key = body.private_key or ""
        missing = [
            label
            for label, value in (
                ("login URL", login_url),
                ("username", username),
                ("private key", private_key.strip()),
            )
            if not value
        ]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required field(s): {', '.join(missing)}.",
            )

        # Normalise the Salesforce login host so the outbound token URL
        # ({base}/services/oauth2/token) and the JWT `aud` claim are always
        # well-formed (R18-A3 T2 hardening). A bare host defaults to https; only
        # an http(s) URL with a host is accepted, so a misconfig fails loudly here
        # at setup rather than silently at first mint.
        if "://" not in login_url:
            login_url = "https://" + login_url
        parsed = urlparse(login_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(
                status_code=400,
                detail=(
                    "login URL must be an http(s) URL, e.g. "
                    "https://login.salesforce.com"
                ),
            )
        login_url = login_url.rstrip("/")

        org_id = get_current_org_id()
        try:
            record = store_jwt_bearer_credential(
                org_id,
                connector_id,
                private_key=private_key,
                subject=username,
                base_url=login_url,
            )
        except ValueError:
            raise HTTPException(
                status_code=400, detail="A non-empty private key is required."
            )
        except MissingSecretError:
            logger.error(
                "Cannot store JWT bearer credential for connector %s: the credential "
                "vault key (CREDENTIAL_VAULT_KEY) is not configured",
                connector_id,
            )
            raise HTTPException(
                status_code=500, detail="Credential vault is not configured."
            )

        # Record the per-org auth-mode selection so resolve_auth_mode reflects
        # reality, and reflect the connection in this org's connector state (the
        # access token mints on first ingest). Display state only — the vault write
        # above is the source of truth, so a failure here must not fail the request.
        try:
            set_auth_mode(org_id, connector_id, "jwt_bearer")
        except Exception:
            logger.exception(
                "Failed to record jwt_bearer auth mode for connector %s", connector_id
            )
        try:
            rec = db.org_connector_get(org_id, connector_id) or {}
            rec["status"] = "connected"
            rec["lastSynced"] = rec.get("lastSynced", "—")
            db.org_connector_set(org_id, connector_id, rec)
        except Exception:
            logger.exception(
                "Failed to mark connector %s connected after JWT credential entry",
                connector_id,
            )

        log_event(
            "connector_credentials_set",
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
        )
        return StaticCredentialStatus(
            connector_id=connector_id,
            configured=True,
            base_url=record.base_url or None,
            has_username=bool(record.username),
            updated_at=record.updated_at.isoformat(),
        )

    @app.get(
        "/api/connectors/{connector_id}/jwt-credentials",
        response_model=StaticCredentialStatus,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
    )
    def get_jwt_bearer_credential_status(connector_id: str) -> StaticCredentialStatus:
        """Report whether JWT bearer material is configured — metadata only (AC5).

        Reads NON-SECRET metadata (never decrypts the private key), so the key
        cannot leak through this path and it does not even need the vault key.
        """
        if not connector_supports_mode(connector_id, "jwt_bearer"):
            raise HTTPException(
                status_code=400,
                detail="Connector does not support the JWT bearer auth mode.",
            )
        org_id = get_current_org_id()
        meta = get_jwt_bearer_credential_metadata(org_id, connector_id)
        if meta is None:
            return StaticCredentialStatus(connector_id=connector_id, configured=False)
        return StaticCredentialStatus(
            connector_id=connector_id,
            configured=True,
            base_url=meta["base_url"] or None,
            has_username=meta["has_username"],
            updated_at=meta["updated_at"].isoformat(),
        )

    @app.delete(
        "/api/connectors/{connector_id}/jwt-credentials",
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    )
    def delete_jwt_bearer_credentials(
        connector_id: str, token: str = Depends(require_auth)
    ) -> Response:
        """Revoke (soft-delete) the org's JWT bearer signing material.

        Owner-only. Scoped to the reserved ``{connector_id}:jwt`` static row, so it
        never touches the connector's cached OAuth token row. Idempotent.
        """
        if not connector_supports_mode(connector_id, "jwt_bearer"):
            raise HTTPException(
                status_code=400,
                detail="Connector does not support the JWT bearer auth mode.",
            )
        org_id = get_current_org_id()
        revoke_jwt_bearer_credential(org_id, connector_id)
        try:
            rec = db.org_connector_get(org_id, connector_id)
            if rec is not None and rec.get("org_id") == org_id:
                rec["status"] = "disconnected"
                db.org_connector_set(org_id, connector_id, rec)
        except Exception:
            logger.exception(
                "Failed to clear connector %s connection state after JWT credential revoke",
                connector_id,
            )
        log_event(
            "connector_credentials_revoked",
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
        )
        return Response(status_code=204)

    # -----------------------------------------------------------------------
    # Client-credentials connect — R18-A3 T3 / AT-556 (AC2/AC5)
    #
    # Outbound-only Microsoft Graph (Teams / SharePoint) connect: no browser
    # redirect, no inbound callback. The credential is the deployment's app client
    # secret (the {CONNECTOR}_CLIENT_SECRET env var against an admin-consented app
    # registration — see docs/INTEGRATE_GRAPH_CLIENT_CREDENTIALS.md), so this takes
    # NO body. It exchanges the app credentials outbound for an access token, vaults
    # it (encrypted, never logged — AC5), records the per-org client_credentials mode
    # selection, and marks the connector connected. The token re-mints automatically
    # on expiry via get_token (client-credentials issues no refresh token), so
    # ingestion continues without any further user action (AC2).
    # -----------------------------------------------------------------------

    @app.post(
        "/api/connectors/{connector_id}/client-credentials",
        response_model=ClientCredentialsConnectStatus,
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    )
    async def connect_client_credentials(
        connector_id: str, token: str = Depends(require_auth)
    ) -> ClientCredentialsConnectStatus:
        """Connect a connector via the outbound-only client-credentials grant (AC2).

        Owner-only. Selecting a connector that has no client_credentials mode is a
        400. Acquires an access token outbound (no callback), stores it per-org in
        the encrypted vault (AC5), sets the org's auth mode to client_credentials,
        and reflects the connection in the org's connector state. A missing app
        secret is a 500 (operator config); an upstream token failure is a 502.
        """
        if not connector_supports_mode(connector_id, "client_credentials"):
            raise HTTPException(
                status_code=400,
                detail="Connector does not support the client-credentials auth mode.",
            )
        config = CONNECTOR_AUTH_CONFIGS.get(connector_id)
        if config is None:
            raise HTTPException(status_code=404, detail="Unknown connector")

        org_id = get_current_org_id()

        # Block connecting a NEW system once the org is at its licensed max_systems;
        # re-connecting an already-connected connector is not a new system (R17-D4
        # Addendum A / T9). Raises HTTP 402 at the limit.
        license_limits.enforce_can_connect(org_id, connector_id)

        # Outbound token acquisition — the client secret is resolved inside
        # get_client_credentials_token and never logged (AC5).
        try:
            token_response = await get_client_credentials_token(config)
        except MissingSecretError:
            logger.error(
                "Cannot connect connector %s via client-credentials: the connector "
                "client secret (%s) is not configured",
                connector_id,
                config.secret_key,
            )
            raise HTTPException(
                status_code=500,
                detail="Connector client secret is not configured.",
            )
        except OAuthError as exc:
            # Provider rejected the credentials / unreachable. exc carries only the
            # provider's OAuth error code (never the secret), safe to surface.
            logger.warning(
                "client-credentials connect failed for connector %s: %s",
                connector_id,
                exc.reason,
            )
            raise HTTPException(
                status_code=502,
                detail="Could not acquire a token from the provider.",
            )

        store_token(org_id, connector_id, token_response)

        # Record the per-org auth-mode selection so resolve_auth_mode reflects
        # reality and get_token re-mints in client_credentials mode on expiry.
        # Display/selection state — the vault write above is the source of truth,
        # so a failure here must not fail the request.
        try:
            set_auth_mode(org_id, connector_id, "client_credentials")
        except Exception:
            logger.exception(
                "Failed to record client_credentials auth mode for connector %s",
                connector_id,
            )
        try:
            rec = db.org_connector_get(org_id, connector_id) or {}
            rec["status"] = "connected"
            rec["lastSynced"] = rec.get("lastSynced", "—")
            db.org_connector_set(org_id, connector_id, rec)
        except Exception:
            logger.exception(
                "Failed to mark connector %s connected after client-credentials connect",
                connector_id,
            )

        log_event(
            "connector_connected",
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
            scopes_granted=config.client_credentials_scopes or config.scopes,
        )
        return ClientCredentialsConnectStatus(
            connector_id=connector_id,
            connected=True,
            auth_mode=resolve_auth_mode(org_id, connector_id),
        )

