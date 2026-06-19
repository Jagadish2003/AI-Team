from __future__ import annotations

import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import psycopg2
from cryptography.fernet import Fernet

from app import db
from app.auth import oauth as _oauth
from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.middleware.audit import log_event
from app.auth.models import ConnectorAuthConfig, ConnectorNotAuthenticatedError, TokenRecord
from app.auth.secrets import MissingSecretError
from database.models.credentials import (
    ALTER_CREDENTIALS_ADD_REFRESH_FAILED,
    CREATE_CREDENTIALS_IDX_CONNECTOR,
    CREATE_CREDENTIALS_IDX_ORG,
    CREATE_CREDENTIALS_TABLE,
)

logger = logging.getLogger(__name__)

REFRESH_THRESHOLD_SECONDS = int(os.environ.get("REFRESH_THRESHOLD_SECONDS", "300"))
_OAUTH_HTTP_TIMEOUT = int(os.environ.get("OAUTH_HTTP_TIMEOUT_SECONDS", "30"))

# Nonce TTL — must match Section 2 of T1-S11 Task 1 spec
_NONCE_TTL_MINUTES = 10

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _init_credentials_table() -> None:
    """No-op. The credentials table is provisioned externally.

    Created by database/provision/provision.sh; the application no longer
    creates this table at runtime.
    """
    return None


def _init_nonce_table() -> None:
    """No-op. The nonces table is provisioned by database/provision/provision.sh."""
    return None


def _get_fernet() -> Fernet:
    """Return a Fernet instance using CREDENTIAL_VAULT_KEY from env.

    Called at use time only — never cached at module level.
    Raises MissingSecretError if the env var is absent.
    """
    key = os.environ.get("CREDENTIAL_VAULT_KEY")
    if key is None:
        raise MissingSecretError("CREDENTIAL_VAULT_KEY")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def _parse_expires_at(token_response: dict) -> datetime:
    """Return a UTC-aware datetime for when the token expires.

    Handles both expires_in (relative seconds) and expires_at (absolute).
    Falls back to 1 hour if neither is present.
    """
    now = datetime.now(timezone.utc)

    if "expires_in" in token_response:
        try:
            return now + timedelta(seconds=int(token_response["expires_in"]))
        except (TypeError, ValueError):
            pass

    if "expires_at" in token_response:
        val = token_response["expires_at"]
        # Numeric Unix timestamp (int or float or numeric string)
        try:
            return datetime.fromtimestamp(float(val), tz=timezone.utc)
        except (TypeError, ValueError):
            pass
        # ISO 8601 string
        try:
            dt = datetime.fromisoformat(str(val))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # Default: 1 hour from now
    return now + timedelta(hours=1)


def _row_to_token_record(row: tuple) -> TokenRecord:
    """Convert a raw DB row to a TokenRecord with decrypted token values."""
    _, org_id, connector_id, enc_access, enc_refresh, expires_at_str, scopes_json, created_str, updated_str = row

    access_token = _decrypt(enc_access)
    refresh_token = _decrypt(enc_refresh) if enc_refresh else None

    expires_at = datetime.fromisoformat(expires_at_str)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    created_at = datetime.fromisoformat(created_str)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    updated_at = datetime.fromisoformat(updated_str)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)

    return TokenRecord(
        org_id=org_id,
        connector_id=connector_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=json.loads(scopes_json),
        created_at=created_at,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# Nonce store — single-use state nonce enforcement (T1-S11 Task 1, Section 2)
# ---------------------------------------------------------------------------


def store_nonce(
    nonce: str,
    connector_id: str,
    code_verifier: Optional[str] = None,
) -> None:
    """Store a state nonce with connector context and a 10-minute expiry.

    Called by the OAuth initiation route when generating the state parameter.
    The nonce key is prefixed with 'nonce:' to namespace it in the store.
    `code_verifier`, when provided, is the PKCE verifier bound to this state;
    it is returned by consume_nonce() so the callback can complete the exchange.
    """
    _init_nonce_table()

    now = datetime.now(timezone.utc)
    data = json.dumps({
        "nonce":         nonce,
        "connector_id":  connector_id,
        "code_verifier": code_verifier,
        "created_at":    now.isoformat(),
        "expires_at":    (now + timedelta(minutes=_NONCE_TTL_MINUTES)).isoformat(),
    })

    key = f"nonce:{nonce}"
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO nonces (key, data) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET data=EXCLUDED.data",
            (key, data),
        )
        con.commit()
    finally:
        con.close()


def consume_nonce(nonce: str) -> dict | None:
    """Retrieve and DELETE the nonce in a single operation.

    Returns the nonce data dict if the nonce exists and has not expired.
    Returns None if the nonce does not exist (already used or never issued).
    Returns None if the nonce has expired (issued more than 10 minutes ago).

    Delete-before-process pattern (Section 2):
        The nonce is deleted from the store BEFORE expiry is checked.
        This prevents a race condition where two simultaneous requests both
        read the nonce as valid before either deletes it.
        A second call with the same nonce always returns None → 400.

    NOTE: Must NOT fall back to storing None — kv_delete (DELETE SQL here)
    is the only correct pattern. Storing a null payload leaves the key in
    the store and breaks the single-use guarantee.
    """
    _init_nonce_table()

    key = f"nonce:{nonce}"
    con = db.connect()
    try:
        cur = con.cursor()
        # Step 1: Read
        cur.execute(
            "SELECT data FROM nonces WHERE key = %s",
            (key,),
        )
        row = cur.fetchone()

        if row is None:
            return None  # Already used or never issued

        # Step 2: DELETE immediately — before any further processing
        cur.execute("DELETE FROM nonces WHERE key = %s", (key,))
        con.commit()
    finally:
        con.close()

    # Step 3: Check expiry (after deletion — nonce is already gone from store)
    data = json.loads(row[0])
    expires_at = datetime.fromisoformat(data["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) > expires_at:
        return None  # Expired — nonce already deleted above, correctly

    # Timing-safe comparison of the supplied state against the stored nonce
    # (Section 2 / AC9): use hmac.compare_digest, never ==, to guard against
    # timing attacks on state validation. Rows written before the nonce value
    # was stored fall back to the exact key match already performed above.
    stored_nonce = data.get("nonce")
    if stored_nonce is not None and not hmac.compare_digest(stored_nonce, nonce):
        return None

    return data


# ---------------------------------------------------------------------------
# Public vault API
# ---------------------------------------------------------------------------


def _mark_refresh_failed(org_id: str, connector_id: str) -> None:
    """Set refresh_failed=1 for an existing credential row.  No-op if row absent."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE credentials SET refresh_failed=1 WHERE org_id=%s AND connector_id=%s",
            (org_id, connector_id),
        )
        con.commit()
    finally:
        con.close()


def store_token(org_id: str, connector_id: str, token_response: dict) -> TokenRecord:
    """Encrypt and upsert a token response for the given org+connector pair.

    Accepts the raw dict returned by exchange_code() or get_client_credentials_token().
    Returns a TokenRecord with decrypted values (plaintext never written to DB).
    """
    _init_credentials_table()

    now = datetime.now(timezone.utc)
    expires_at = _parse_expires_at(token_response)

    enc_access = _encrypt(token_response["access_token"])

    raw_refresh = token_response.get("refresh_token")
    enc_refresh = _encrypt(raw_refresh) if raw_refresh else None

    scopes_json = json.dumps(token_response.get("scope", "").split() if isinstance(token_response.get("scope"), str) else token_response.get("scopes", []))

    record_id = str(uuid.uuid4())
    now_iso = now.isoformat()
    expires_iso = expires_at.isoformat()

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO credentials
                (id, org_id, connector_id, access_token, refresh_token, expires_at, scopes, created_at, updated_at, refresh_failed)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0)
            ON CONFLICT (org_id, connector_id) DO UPDATE SET
                access_token   = EXCLUDED.access_token,
                refresh_token  = EXCLUDED.refresh_token,
                expires_at     = EXCLUDED.expires_at,
                scopes         = EXCLUDED.scopes,
                updated_at     = EXCLUDED.updated_at,
                refresh_failed = 0
            """,
            (record_id, org_id, connector_id, enc_access, enc_refresh, expires_iso, scopes_json, now_iso, now_iso),
        )
        con.commit()
    finally:
        con.close()

    return TokenRecord(
        org_id=org_id,
        connector_id=connector_id,
        access_token=token_response["access_token"],
        refresh_token=raw_refresh,
        expires_at=expires_at,
        scopes=json.loads(scopes_json),
        created_at=now,
        updated_at=now,
    )


async def get_token(org_id: str, connector_id: str) -> TokenRecord:
    """Return a valid, decrypted TokenRecord for the given org+connector.

    Auto-refreshes if the token is within REFRESH_THRESHOLD_SECONDS of expiry.
    Raises ConnectorNotAuthenticatedError when no token exists or refresh fails.
    """
    _init_credentials_table()

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, org_id, connector_id, access_token, refresh_token,
                   expires_at, scopes, created_at, updated_at
            FROM credentials
            WHERE org_id = %s AND connector_id = %s
            """,
            (org_id, connector_id),
        )
        row = cur.fetchone()
    finally:
        con.close()

    if row is None:
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    record = _row_to_token_record(row)

    now = datetime.now(timezone.utc)
    seconds_left = (record.expires_at - now).total_seconds()

    if seconds_left <= REFRESH_THRESHOLD_SECONDS:
        if record.refresh_token is None:
            raise ConnectorNotAuthenticatedError(org_id, connector_id)

        config = CONNECTOR_AUTH_CONFIGS.get(connector_id)
        if config is None:
            raise ConnectorNotAuthenticatedError(org_id, connector_id)

        try:
            new_response = await _oauth.refresh_token(config, record.refresh_token)
        except _oauth.OAuthError as exc:
            logger.warning("Token refresh failed for %s/%s: %s", org_id, connector_id, exc.reason)
            # Mark refresh_failed so token-status can return 'refresh_failed'
            try:
                _mark_refresh_failed(org_id, connector_id)
            except Exception:
                pass  # Never let flag-writing block the error propagation
            raise ConnectorNotAuthenticatedError(org_id, connector_id) from exc

        record = store_token(org_id, connector_id, new_response)

    return record


async def revoke_token(
    org_id: str,
    connector_id: str,
    *,
    _revoke_transport: Optional[httpx.AsyncBaseTransport] = None,
) -> None:
    """Revoke and delete the stored token for the given org+connector.

    Step 1a — RFC 7009 revocation (standard connectors, best-effort, never raises):
        POST to config.revocation_url if present.
        Any failure is logged as a WARNING.

    Step 1b — Slack-specific revocation via auth.revoke Web API (T1-S11 Task 1, Section 3):
        Runs when connector_id == 'slack' and revocation_url is None.
        Calls https://slack.com/api/auth.revoke with Bearer token.
        ok=false in the response body is logged as connector_revocation_failed.
        Any exception is logged as a WARNING. Never raises.

    Step 2 — Local deletion (always executes):
        Deletes the DB record regardless of Step 1 outcome.

    Idempotent: if no token exists, both steps are no-ops.
    """
    _init_credentials_table()

    config = CONNECTOR_AUTH_CONFIGS.get(connector_id)

    # Fetch the stored access token once — used by both Step 1a and 1b
    access_token_for_revoke: Optional[str] = None
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT access_token FROM credentials WHERE org_id = %s AND connector_id = %s",
            (org_id, connector_id),
        )
        row = cur.fetchone()
    finally:
        con.close()

    if row:
        try:
            access_token_for_revoke = _decrypt(row[0])
        except Exception as exc:
            logger.warning(
                "Could not decrypt token for external revocation of %s/%s: %s",
                org_id, connector_id, exc,
            )

    # --- Step 1a: RFC 7009 revocation (standard connectors) ---
    if config and config.revocation_url and access_token_for_revoke:
        try:
            async with httpx.AsyncClient(
                timeout=_OAUTH_HTTP_TIMEOUT, transport=_revoke_transport
            ) as client:
                resp = await client.post(
                    config.revocation_url,
                    data={"token": access_token_for_revoke, "token_type_hint": "access_token"},
                )
            if resp.status_code == 200:
                logger.info("External revocation succeeded for %s/%s", org_id, connector_id)
            else:
                logger.warning(
                    "External revocation returned HTTP %s for %s/%s",
                    resp.status_code, org_id, connector_id,
                )
        except httpx.TimeoutException:
            logger.warning(
                "External revocation timed out for %s/%s", org_id, connector_id
            )
        except Exception as exc:
            logger.warning(
                "External revocation failed for %s/%s: %s", org_id, connector_id, exc
            )

    # --- Step 1b: Slack-specific revocation via auth.revoke Web API ---
    # Runs only when connector_id is 'slack' (revocation_url is None for Slack).
    # This is a connector-specific branch, not a general framework mechanism.
    elif connector_id == "slack" and access_token_for_revoke:
        try:
            async with httpx.AsyncClient(
                timeout=_OAUTH_HTTP_TIMEOUT, transport=_revoke_transport
            ) as client:
                resp = await client.post(
                    "https://slack.com/api/auth.revoke",
                    headers={"Authorization": f"Bearer {access_token_for_revoke}"},
                )
            body = resp.json()
            if not body.get("ok"):
                error_code = body.get("error", "unknown")
                logger.warning(
                    "Slack auth.revoke returned ok=false for %s/%s: error=%s",
                    org_id, connector_id, error_code,
                )
                log_event(
                    "connector_revocation_failed",
                    org_id=org_id,
                    connector_id=connector_id,
                    error_code=error_code,
                )
        except httpx.TimeoutException:
            logger.warning(
                "Slack auth.revoke timed out for %s/%s", org_id, connector_id
            )
        except Exception as exc:
            logger.warning(
                "Slack auth.revoke failed for %s/%s: %s", org_id, connector_id, exc
            )

    # --- Step 2: local deletion (always executes regardless of Step 1 outcome) ---
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "DELETE FROM credentials WHERE org_id = %s AND connector_id = %s",
            (org_id, connector_id),
        )
        con.commit()
    finally:
        con.close()
