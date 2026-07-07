from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import jwt

from app.auth.models import ConnectorAuthConfig
from app.auth.secrets import resolve_secret

logger = logging.getLogger(__name__)

OAUTH_HTTP_TIMEOUT = int(os.environ.get("OAUTH_HTTP_TIMEOUT_SECONDS", "30"))

# RFC 7523 grant type for the JWT bearer flow (Salesforce connected-app
# server-to-server integration — R18-A3 T2 / AT-555). Outbound-only: a signed
# assertion is POSTed for an access token, so NO redirect URI / inbound callback
# is ever involved (AC1).
JWT_BEARER_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# Assertion lifetime. Salesforce rejects a JWT whose ``exp`` is more than 5
# minutes out; 3 minutes leaves comfortable clock-skew headroom. The assertion is
# minted fresh on every token request, so it need only outlive the round-trip.
_JWT_ASSERTION_TTL_SECONDS = 180

# Ask the token endpoint for a JSON response. GitHub's token endpoint
# (https://github.com/login/oauth/access_token) returns
# application/x-www-form-urlencoded BY DEFAULT and only switches to JSON when the
# request sends this header — without it ``response.json()`` raises on the form
# body and the whole connect flow fails as "exchange_failed". Every other
# provider already returns JSON and honours this header, so sending it is safe
# and correct for all of them.
_JSON_ACCEPT = {"Accept": "application/json"}


def _raise_for_token_error(config: ConnectorAuthConfig, response: httpx.Response) -> None:
    """Log the provider's OAuth error code, then raise :class:`OAuthError`.

    A failed token request otherwise surfaces only as a bare status code (e.g.
    "401"), which is undiagnosable. OAuth error RESPONSE bodies carry an
    ``error`` / ``error_description`` (Microsoft: ``AADSTS...``; GitHub:
    ``bad_verification_code``; etc.) that pinpoints the cause — and they NEVER
    contain the client secret or token, so logging just those two fields is safe.
    The request body (which does hold the secret) is never logged. The first line
    of ``error_description`` is capped so a long Microsoft trace is not dumped.
    """
    err = desc = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            err = str(body.get("error") or "")
            raw_desc = body.get("error_description") or ""
            desc = str(raw_desc).splitlines()[0][:300] if raw_desc else ""
    except Exception:  # noqa: BLE001 — non-JSON/empty error body; log status only.
        pass
    logger.warning(
        "OAuth token request failed: connector=%s status=%s error=%r description=%r",
        config.connector_id,
        response.status_code,
        err,
        desc,
    )
    # Surface the provider error code (no secret) in the exception too, so it
    # appears in the callback traceback — e.g. "401 (invalid_client: AADSTS...)".
    detail = ": ".join(p for p in (err, desc) if p) or None
    raise OAuthError(config.connector_id, response.status_code, detail=detail)


def _parse_token_response(response: httpx.Response) -> dict:
    """Parse an OAuth token response body into a dict.

    Prefers JSON (what every provider returns once asked via ``Accept:
    application/json``), but falls back to decoding an
    ``application/x-www-form-urlencoded`` body so a token is never dropped if a
    provider ignores the Accept header (GitHub's historical default). Returning
    a partial/empty dict here is fine — the caller's ``store_token`` validates
    that an ``access_token`` is present.
    """
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type.lower():
        return response.json()
    try:
        return response.json()
    except Exception:
        return dict(parse_qsl(response.text))


class OAuthError(Exception):
    """Raised when an OAuth HTTP call fails.

    ``reason`` is the HTTP status (or "timeout"). ``detail`` is the provider's
    own OAuth error code / description (e.g. Microsoft ``invalid_client:
    AADSTS7000215 ...``), which pinpoints the cause and is SAFE to surface — OAuth
    error bodies never contain the client secret or token. It is included in the
    message so it appears in the callback traceback, not just a separate log line.
    The upstream request body (which holds the secret) is never included.
    """

    def __init__(
        self, connector_id: str, reason: str | int, detail: Optional[str] = None
    ) -> None:
        self.connector_id = connector_id
        self.reason = reason
        self.detail = detail
        msg = f"OAuth error for connector '{connector_id}': {reason}"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for a PKCE S256 exchange (RFC 7636).

    The verifier is a high-entropy URL-safe string kept server-side (tied to the
    state nonce). The challenge is base64url(sha256(verifier)) with padding
    stripped, and is the value sent in the authorization request. Required by
    providers that enforce PKCE (e.g. Salesforce Connected Apps with "Require
    PKCE" enabled) and mandated for all authorization-code flows under OAuth 2.1.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_auth_url(
    config: ConnectorAuthConfig,
    state: str,
    code_challenge: Optional[str] = None,
) -> str:
    """Build the authorization URL to redirect the user to the OAuth provider.

    `state` is a CSRF nonce generated by the caller (T8).
    `code_challenge`, when provided, adds the PKCE parameters
    (code_challenge + code_challenge_method=S256) — required by providers that
    enforce PKCE and harmless for those that don't.
    Does not call resolve_secret — client_secret is not needed at this step.
    Raises ValueError if the config has no authorization_url (e.g. client_credentials flow).
    """
    if not config.authorization_url:
        raise ValueError(
            f"Connector '{config.connector_id}' has no authorization_url "
            "(client_credentials flows do not use a browser redirect)"
        )
    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": " ".join(config.scopes),
        "state": state,
        "response_type": "code",
    }
    # Provider-specific params that make a long-lived refresh token reliably
    # issued (Atlassian audience + prompt=consent, Salesforce prompt=consent), so
    # access tokens can be auto-refreshed rather than expiring permanently.
    if config.authorize_params:
        params.update(config.authorize_params)
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{config.authorization_url}?{urlencode(params)}"


async def exchange_code(
    config: ConnectorAuthConfig,
    code: str,
    *,
    code_verifier: Optional[str] = None,
    _transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict:
    """Exchange an authorization code for an access token.

    `code_verifier`, when provided, completes a PKCE exchange — it must be the
    verifier whose challenge was sent in build_auth_url. Required by providers
    that enforce PKCE.
    _transport is injected only in tests; callers omit it.
    Secret is resolved inline and discarded when the async-with block exits.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": config.client_id,
        "client_secret": resolve_secret(config.secret_key),
        "redirect_uri": config.redirect_uri,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    async with httpx.AsyncClient(timeout=OAUTH_HTTP_TIMEOUT, transport=_transport) as client:
        try:
            response = await client.post(config.token_url, data=data, headers=_JSON_ACCEPT)
        except httpx.TimeoutException:
            raise OAuthError(config.connector_id, "timeout")
    if response.status_code != 200:
        _raise_for_token_error(config, response)
    return _parse_token_response(response)


async def refresh_token(
    config: ConnectorAuthConfig,
    refresh_token_value: str,
    *,
    _transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict:
    """Exchange a refresh token for a new access token.

    _transport is injected only in tests; callers omit it.
    Secret is resolved inline and discarded when the async-with block exits.
    """
    async with httpx.AsyncClient(timeout=OAUTH_HTTP_TIMEOUT, transport=_transport) as client:
        try:
            response = await client.post(
                config.token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token_value,
                    "client_id": config.client_id,
                    "client_secret": resolve_secret(config.secret_key),
                },
                headers=_JSON_ACCEPT,
            )
        except httpx.TimeoutException:
            raise OAuthError(config.connector_id, "timeout")
    if response.status_code != 200:
        _raise_for_token_error(config, response)
    return _parse_token_response(response)


async def get_client_credentials_token(
    config: ConnectorAuthConfig,
    *,
    _transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict:
    """Fetch a token using the client_credentials flow (SAP, D365).

    _transport is injected only in tests; callers omit it.
    Secret is resolved inline and discarded when the async-with block exits.
    """
    async with httpx.AsyncClient(timeout=OAUTH_HTTP_TIMEOUT, transport=_transport) as client:
        try:
            response = await client.post(
                config.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": config.client_id,
                    "client_secret": resolve_secret(config.secret_key),
                    "scope": " ".join(config.scopes),
                },
                headers=_JSON_ACCEPT,
            )
        except httpx.TimeoutException:
            raise OAuthError(config.connector_id, "timeout")
    if response.status_code != 200:
        _raise_for_token_error(config, response)
    return _parse_token_response(response)


def _authorization_base(url: str) -> str:
    """Return the ``scheme://host`` origin of a token URL.

    Used as the JWT ``aud`` (audience) and to derive the token endpoint when the
    caller passes only a login host. e.g.
    ``https://login.salesforce.com/services/oauth2/token`` → ``https://login.salesforce.com``.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url.rstrip("/")


def build_jwt_bearer_assertion(
    *,
    issuer: str,
    subject: str,
    audience: str,
    private_key: str,
    ttl_seconds: int = _JWT_ASSERTION_TTL_SECONDS,
) -> str:
    """Build and RS256-sign the JWT assertion for the JWT bearer flow (RFC 7523).

    ``issuer`` is the connected-app consumer key (``iss``), ``subject`` the
    Salesforce username to run as (``sub``), ``audience`` the login/token host
    (``aud``, e.g. ``https://login.salesforce.com``), and ``private_key`` the PEM
    private key whose public cert is uploaded to the connected app. The assertion
    is short-lived (``ttl_seconds``) and minted fresh per token request.

    The private key is passed to the signer only and is NEVER logged. A signing
    failure (malformed/unusable key) raises :class:`OAuthError` with a generic
    reason so the key material cannot leak into a traceback or log line.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    try:
        return jwt.encode(payload, private_key, algorithm="RS256")
    except Exception as exc:  # noqa: BLE001 — never surface the key; generic reason only.
        logger.warning(
            "JWT bearer assertion signing failed (issuer=%s): %s",
            issuer,
            type(exc).__name__,
        )
        raise OAuthError(issuer, "jwt_signing_failed", detail="invalid private key")


async def get_jwt_bearer_token(
    config: ConnectorAuthConfig,
    *,
    private_key: str,
    subject: str,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
    token_url: Optional[str] = None,
    _transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict:
    """Exchange a signed JWT assertion for an access token (RFC 7523, outbound-only).

    Salesforce's connected-app JWT bearer flow (AT-555): a signed assertion is
    POSTed to the token endpoint and exchanged for an access token — there is NO
    client secret (the assertion signature is the credential), NO redirect URI and
    NO inbound callback (AC1). ``instance_url`` rides in the Salesforce response
    exactly as it does for the authorization-code flow.

    ``issuer`` defaults to ``config.client_id`` (the connected-app consumer key —
    a non-secret client id, per the secret_key convention). ``audience`` and
    ``token_url`` default to the connector's configured host so a caller need only
    supply the per-org ``private_key`` and ``subject``; passing a per-org
    ``audience``/``token_url`` (derived from the stored login host) targets that
    org's Salesforce instance. ``_transport`` is injected only in tests.
    """
    turl = token_url or config.token_url
    aud = audience or _authorization_base(turl)
    iss = issuer or config.client_id

    assertion = build_jwt_bearer_assertion(
        issuer=iss, subject=subject, audience=aud, private_key=private_key
    )

    async with httpx.AsyncClient(timeout=OAUTH_HTTP_TIMEOUT, transport=_transport) as client:
        try:
            response = await client.post(
                turl,
                data={"grant_type": JWT_BEARER_GRANT_TYPE, "assertion": assertion},
                headers=_JSON_ACCEPT,
            )
        except httpx.TimeoutException:
            raise OAuthError(config.connector_id, "timeout")
    if response.status_code != 200:
        _raise_for_token_error(config, response)
    return _parse_token_response(response)
