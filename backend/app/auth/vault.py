from __future__ import annotations

import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import httpx
import psycopg2
from cryptography.fernet import Fernet

from app import db
from app.auth import oauth as _oauth
from app.auth.auth_modes import (
    AUTH_MODE_CLIENT_CREDENTIALS,
    connector_supports_mode,
    resolve_auth_mode,
)
from app.auth.configs import CONNECTOR_AUTH_CONFIGS
from app.middleware.audit import log_event
from app.auth.models import (
    ConnectorAuthConfig,
    ConnectorNotAuthenticatedError,
    StaticCredentialRecord,
    TokenRecord,
)
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
    org_id: Optional[str] = None,
) -> None:
    """Store a state nonce with connector context and a 10-minute expiry.

    Called by the OAuth initiation route when generating the state parameter.
    The nonce key is prefixed with 'nonce:' to namespace it in the store.
    `code_verifier`, when provided, is the PKCE verifier bound to this state;
    it is returned by consume_nonce() so the callback can complete the exchange.
    `org_id`, when provided, captures the tenancy context of the (authenticated)
    initiation request so the callback — which runs on an unauthenticated browser
    redirect and has no JWT/org context — can store the token and connection state
    under the correct org. It is server-side state, never trusted from the callback.
    """
    _init_nonce_table()

    now = datetime.now(timezone.utc)
    data = json.dumps({
        "nonce":         nonce,
        "connector_id":  connector_id,
        "code_verifier": code_verifier,
        "org_id":        org_id,
        "created_at":    now.isoformat(),
        "expires_at":    (now + timedelta(minutes=_NONCE_TTL_MINUTES)).isoformat(),
    })

    key = f"nonce:{nonce}"
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO nonces (key, data) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET data=EXCLUDED.data, is_deleted=FALSE",
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
        # Step 1: Read (only an active, not-yet-consumed nonce)
        cur.execute(
            "SELECT data FROM nonces WHERE key = %s AND is_deleted = FALSE",
            (key,),
        )
        row = cur.fetchone()

        if row is None:
            return None  # Already used or never issued

        # Step 2: Soft-delete immediately — before any further processing. The app
        # role has no DELETE; marking is_deleted preserves the single-use guarantee
        # (the read above filters it out on a second call).
        cur.execute("UPDATE nonces SET is_deleted = TRUE WHERE key = %s", (key,))
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
# Customer-tenant model credential (R17-D2 T2)
#
# Customer-Tenant Model Mode authenticates into the customer's own cloud tenant
# (e.g. Azure's managed model service) using a credential the customer provides
# and controls.
# Those credentials get the SAME vault-grade handling as connector OAuth tokens:
# Fernet-encrypted at rest under CREDENTIAL_VAULT_KEY, stored in the SAME
# `credentials` table (reused — no schema change), never written in plaintext,
# never logged, and revocable by the customer at any time (R17-D2 §2, AC2).
#
# The tenant credential is a STATIC key, not an OAuth token: there is no refresh
# flow and no natural expiry, so it is vaulted under a reserved connector_id and
# read back synchronously without the OAuth auto-refresh path get_token() runs.
# Only the model gateway package resolves this credential — see
# app/model_gateway/customer_tenant_vault.py.
# ---------------------------------------------------------------------------

#: Reserved connector_id under which the customer-tenant model credential is
#: vaulted in the shared `credentials` table. Not a real OAuth connector.
CUSTOMER_TENANT_CONNECTOR_ID = "customer_tenant"

#: A static credential has no natural expiry, but expires_at is NOT NULL. A
#: far-future sentinel documents "no expiry" without special-casing the schema.
#:
#: Dependency (do not break): the background token_refresher.py job only selects
#: rows WHERE refresh_token IS NOT NULL. Customer-tenant rows store
#: refresh_token = NULL (a static key has no OAuth refresh path), so this
#: sentinel date is never evaluated by the refresher. Do NOT replace it with
#: None — the expires_at column is NOT NULL and the INSERT would fail.
_STATIC_CREDENTIAL_EXPIRY_ISO = datetime(9999, 12, 31, tzinfo=timezone.utc).isoformat()


def store_customer_tenant_credential(
    org_id: str,
    api_key: str,
    *,
    connector_id: str = CUSTOMER_TENANT_CONNECTOR_ID,
) -> None:
    """Fernet-encrypt and upsert the customer-tenant model credential for an org.

    Reuses the encrypted `credentials` table and CREDENTIAL_VAULT_KEY exactly as
    connector OAuth tokens do — no schema change. The plaintext key is encrypted
    before it touches the DB and is never logged. Calling again with a new value
    ROTATES the credential in place (single row per org+connector), and a
    previously revoked (soft-deleted) row is reactivated.

    Raises ValueError for an empty key and MissingSecretError when
    CREDENTIAL_VAULT_KEY is absent (encryption is impossible) — both are operator
    configuration errors surfaced at store time, off the model-call path.
    """
    if not api_key or not api_key.strip():
        raise ValueError("customer-tenant credential must be a non-empty value")

    _init_credentials_table()

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    enc_access = _encrypt(api_key)  # raises MissingSecretError if key unset
    record_id = str(uuid.uuid4())

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO credentials
                (id, org_id, connector_id, access_token, refresh_token, expires_at, scopes, created_at, updated_at, refresh_failed)
            VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, 0)
            ON CONFLICT (org_id, connector_id) DO UPDATE SET
                access_token   = EXCLUDED.access_token,
                refresh_token  = NULL,
                expires_at     = EXCLUDED.expires_at,
                updated_at     = EXCLUDED.updated_at,
                refresh_failed = 0,
                is_deleted     = FALSE
            """,
            (
                record_id, org_id, connector_id, enc_access,
                _STATIC_CREDENTIAL_EXPIRY_ISO, "[]", now_iso, now_iso,
            ),
        )
        con.commit()
    finally:
        con.close()


def get_customer_tenant_credential(
    org_id: str,
    *,
    connector_id: str = CUSTOMER_TENANT_CONNECTOR_ID,
) -> Optional[str]:
    """Return the decrypted customer-tenant credential for an org, or None.

    Returns None — never raises and never logs the value — when:
      * no active row exists (never stored, or revoked/soft-deleted), or
      * the stored ciphertext cannot be decrypted (missing/rotated vault key,
        corrupted or tampered value).

    None means "no usable credential", so the model gateway degrades to a
    graceful auth failure rather than crashing the run (R17-D2 §2, AC5). This is
    the READ path the model call depends on, so it is defensively total.
    """
    try:
        _init_credentials_table()
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT access_token FROM credentials "
                "WHERE org_id = %s AND connector_id = %s AND is_deleted = FALSE",
                (org_id, connector_id),
            )
            row = cur.fetchone()
        finally:
            con.close()
    except Exception:
        logger.warning(
            "customer-tenant credential lookup failed for org %s (returning none)",
            org_id,
        )
        return None

    if row is None or row[0] is None:
        return None

    try:
        return _decrypt(row[0])
    except Exception:
        # Undecryptable ciphertext (wrong/rotated key, corruption, tampering).
        # Treat as no usable credential; never surface the raw value.
        logger.warning(
            "customer-tenant credential for org %s could not be decrypted "
            "(treating as unavailable)",
            org_id,
        )
        return None


def revoke_customer_tenant_credential(
    org_id: str,
    *,
    connector_id: str = CUSTOMER_TENANT_CONNECTOR_ID,
) -> None:
    """Soft-delete the customer-tenant credential for an org (customer revocation).

    Mirrors revoke_token's local deletion: the app DB role has no DELETE, so the
    row is marked is_deleted = TRUE. A subsequent get returns None → the gateway
    fails gracefully; a later store reactivates the row. Idempotent when no
    credential exists.
    """
    _init_credentials_table()
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE credentials SET is_deleted = TRUE "
            "WHERE org_id = %s AND connector_id = %s",
            (org_id, connector_id),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Static connector credentials (R17-D3 Addendum A, T10)
#
# Second vault record type alongside OAuth token records. Jira API tokens,
# ServiceNow username/password, and native DB (SQL Server / Oracle /
# PostgreSQL) connection credentials are STATIC: entered once by an admin, no
# OAuth dance, no refresh flow, no natural expiry. They get the SAME vault
# hygiene as token records — Fernet-encrypted at rest under
# CREDENTIAL_VAULT_KEY, keyed per (org_id, connector_id) in the SAME
# `credentials` table, decrypted at use only, never logged (AC10).
#
# The `kind` column discriminates the two record types on the shared table.
# The existing UNIQUE(org_id, connector_id) constraint therefore enforces ONE
# credential per connector per org across both kinds: storing a static
# credential replaces a previous OAuth token for that connector and vice
# versa (ServiceNow supports either flow — the org holds one or the other,
# never both). Static rows keep refresh_token = NULL so the background
# token-refresher job never touches them, and reuse the far-future
# _STATIC_CREDENTIAL_EXPIRY_ISO sentinel because expires_at is NOT NULL.
# ---------------------------------------------------------------------------

#: `kind` column values discriminating the two vault record types.
OAUTH_CREDENTIAL_KIND = "oauth"
STATIC_CREDENTIAL_KIND = "static"


def _parse_stored_utc(value: str) -> datetime:
    """Parse a stored ISO datetime string, defaulting naive values to UTC."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def store_static_credential(
    org_id: str,
    connector_id: str,
    *,
    username: str,
    secret: str,
    base_url: str,
) -> StaticCredentialRecord:
    """Fernet-encrypt and upsert a static credential for an org+connector pair.

    username and secret are encrypted before they touch the DB and are never
    logged; base_url (the Jira/ServiceNow instance URL or DB host) is not a
    secret and is stored as-is. Calling again ROTATES the credential in place
    (single row per org+connector), reactivates a previously revoked row, and
    replaces an existing OAuth token record for the connector (the row's kind
    flips to 'static' and its token fields are neutralised).

    Raises ValueError for an empty secret and MissingSecretError when
    CREDENTIAL_VAULT_KEY is absent — operator configuration errors surfaced
    at store time. username/base_url may be empty where a connector needs
    only a secret; requiring specific fields per connector is the entry
    route's concern (T12), not the vault's.
    """
    if not secret or not secret.strip():
        raise ValueError("static credential secret must be a non-empty value")

    _init_credentials_table()

    now_iso = datetime.now(timezone.utc).isoformat()
    enc_username = _encrypt(username)
    enc_secret = _encrypt(secret)
    # access_token is NOT NULL but a static record has no token. Store
    # encrypted-empty so the column only ever holds valid ciphertext and the
    # OAuth revocation path decrypts it to '' (falsy → skipped) rather than
    # erroring on a non-Fernet sentinel.
    enc_access_sentinel = _encrypt("")
    record_id = str(uuid.uuid4())

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO credentials
                (id, org_id, connector_id, kind, access_token, refresh_token,
                 expires_at, scopes, enc_username, enc_secret, base_url,
                 created_at, updated_at, refresh_failed)
            VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, 0)
            ON CONFLICT (org_id, connector_id) DO UPDATE SET
                kind           = EXCLUDED.kind,
                access_token   = EXCLUDED.access_token,
                refresh_token  = NULL,
                expires_at     = EXCLUDED.expires_at,
                scopes         = EXCLUDED.scopes,
                enc_username   = EXCLUDED.enc_username,
                enc_secret     = EXCLUDED.enc_secret,
                base_url       = EXCLUDED.base_url,
                updated_at     = EXCLUDED.updated_at,
                refresh_failed = 0,
                is_deleted     = FALSE
            RETURNING created_at, updated_at
            """,
            (
                record_id, org_id, connector_id, STATIC_CREDENTIAL_KIND,
                enc_access_sentinel, _STATIC_CREDENTIAL_EXPIRY_ISO, "[]",
                enc_username, enc_secret, base_url, now_iso, now_iso,
            ),
        )
        created_str, updated_str = cur.fetchone()
        con.commit()
    finally:
        con.close()

    return StaticCredentialRecord(
        org_id=org_id,
        connector_id=connector_id,
        username=username,
        secret=secret,
        base_url=base_url,
        created_at=_parse_stored_utc(created_str),
        updated_at=_parse_stored_utc(updated_str),
    )


def get_static_credential(
    org_id: str, connector_id: str
) -> Optional[StaticCredentialRecord]:
    """Return the decrypted static credential for org+connector, or None.

    None when no active static record exists: never stored, revoked
    (soft-deleted), or the connector currently holds an OAuth token record
    instead (kind='oauth' rows are invisible here, exactly as static rows are
    invisible to get_token). Decryption problems propagate loudly — the same
    posture as the token read path — so a corrupted value or missing vault
    key is never silently reported as 'not configured'. Decrypted values are
    returned to the caller only, never logged.
    """
    _init_credentials_table()

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT enc_username, enc_secret, base_url, created_at, updated_at
            FROM credentials
            WHERE org_id = %s AND connector_id = %s
              AND kind = %s AND is_deleted = FALSE
            """,
            (org_id, connector_id, STATIC_CREDENTIAL_KIND),
        )
        row = cur.fetchone()
    finally:
        con.close()

    if row is None:
        return None

    enc_username, enc_secret, base_url, created_str, updated_str = row
    return StaticCredentialRecord(
        org_id=org_id,
        connector_id=connector_id,
        username=_decrypt(enc_username) if enc_username else "",
        secret=_decrypt(enc_secret) if enc_secret else "",
        base_url=base_url or "",
        created_at=_parse_stored_utc(created_str),
        updated_at=_parse_stored_utc(updated_str),
    )


def get_static_credential_metadata(
    org_id: str, connector_id: str
) -> Optional[dict]:
    """Return NON-SECRET metadata for an active static credential, or None.

    Companion to get_static_credential for the credential-status / write-only UI
    surface (R17-D3 Addendum A, T12): it reports whether a credential exists plus
    its non-secret base_url and timestamps WITHOUT decrypting enc_username /
    enc_secret. The status path therefore never touches the plaintext at all, so
    it cannot leak it and does not even need CREDENTIAL_VAULT_KEY (AC10: values
    are write-only — never readable back through the UI or logs). ``has_username``
    reflects presence only, never the value. Scoped to kind='static' and
    is_deleted=FALSE exactly like get_static_credential, so OAuth and revoked rows
    are invisible.
    """
    _init_credentials_table()

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT base_url, enc_username, created_at, updated_at
            FROM credentials
            WHERE org_id = %s AND connector_id = %s
              AND kind = %s AND is_deleted = FALSE
            """,
            (org_id, connector_id, STATIC_CREDENTIAL_KIND),
        )
        row = cur.fetchone()
    finally:
        con.close()

    if row is None:
        return None

    base_url, enc_username, created_str, updated_str = row
    return {
        "base_url": base_url or "",
        "has_username": bool(enc_username),
        "created_at": _parse_stored_utc(created_str),
        "updated_at": _parse_stored_utc(updated_str),
    }


def revoke_static_credential(org_id: str, connector_id: str) -> None:
    """Soft-delete the static credential for an org+connector pair.

    Static credentials have no external revocation endpoint, so revocation is
    purely local, mirroring revoke_customer_tenant_credential: the app DB
    role has no DELETE, so the row is marked is_deleted = TRUE. Scoped to
    kind='static' so revoking a static credential can never remove an OAuth
    token row. Idempotent when nothing is stored; a later store reactivates
    the row.
    """
    _init_credentials_table()
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE credentials SET is_deleted = TRUE "
            "WHERE org_id = %s AND connector_id = %s AND kind = %s",
            (org_id, connector_id, STATIC_CREDENTIAL_KIND),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# JWT bearer signing material (R18-A3 T2 / AT-555)
#
# The Salesforce connected-app JWT bearer flow authenticates with a signed
# assertion rather than a stored access token: the durable secret is the cert
# PRIVATE KEY (plus the Salesforce username the assertion runs as, and the login
# host). That material is STATIC — no OAuth dance, no refresh — so it reuses the
# static-credential record type and all its hygiene (Fernet-encrypted at rest,
# masked in repr, write-only, never logged — AC5):
#
#     secret   → the PEM private key
#     username → the Salesforce username (the assertion `sub`)
#     base_url → the login/instance host (the assertion `aud` + token endpoint)
#
# It is keyed under a RESERVED connector id (``{connector_id}:jwt``) so it lives
# in its OWN row, distinct from the ``{connector_id}`` OAuth row where the MINTED
# access token is cached. This sidesteps the shared UNIQUE(org_id, connector_id)
# constraint (one row would otherwise hold either the key or the token, not both)
# and keeps get_credential(connector_id) returning the token, never the key — so
# ingestion stays mode-agnostic (the private key is never mis-resolved as a
# credential). get_token() mints/re-mints the access token from this material.
# ---------------------------------------------------------------------------

#: Suffix marking the reserved connector id that holds a connector's JWT bearer
#: signing material (kept separate from the minted-token row).
_JWT_MATERIAL_SUFFIX = ":jwt"


def jwt_material_connector_id(connector_id: str) -> str:
    """Return the reserved vault id holding ``connector_id``'s JWT signing material."""
    return f"{connector_id}{_JWT_MATERIAL_SUFFIX}"


def store_jwt_bearer_credential(
    org_id: str,
    connector_id: str,
    *,
    private_key: str,
    subject: str,
    base_url: str,
) -> StaticCredentialRecord:
    """Vault the JWT bearer signing material for an org+connector (AC5).

    ``private_key`` (PEM) is the secret, ``subject`` the Salesforce username the
    assertion runs as, ``base_url`` the login/instance host. Encrypted at rest and
    write-only exactly like any static credential; rotating in place replaces the
    prior key. Stored under the reserved ``{connector_id}:jwt`` id so it never
    collides with the minted-token row.
    """
    return store_static_credential(
        org_id,
        jwt_material_connector_id(connector_id),
        username=subject,
        secret=private_key,
        base_url=base_url,
    )


def get_jwt_bearer_credential(
    org_id: str, connector_id: str
) -> Optional[StaticCredentialRecord]:
    """Return the decrypted JWT bearer signing material, or None if not configured."""
    return get_static_credential(org_id, jwt_material_connector_id(connector_id))


def get_jwt_bearer_credential_metadata(
    org_id: str, connector_id: str
) -> Optional[dict]:
    """Return NON-SECRET metadata for the JWT material (never decrypts the key)."""
    return get_static_credential_metadata(org_id, jwt_material_connector_id(connector_id))


def revoke_jwt_bearer_credential(org_id: str, connector_id: str) -> None:
    """Soft-delete the JWT bearer signing material for an org+connector."""
    revoke_static_credential(org_id, jwt_material_connector_id(connector_id))


async def _mint_jwt_bearer_token(
    org_id: str, connector_id: str
) -> Optional[TokenRecord]:
    """Mint (or re-mint) an access token from vaulted JWT bearer material.

    Returns a freshly-minted, cached :class:`TokenRecord` when the connector holds
    JWT bearer material for this org, or ``None`` when it does not (so the caller
    falls back to its normal not-authenticated handling). This is the whole of
    "refresh handled by re-assertion": a new signed assertion is exchanged
    outbound for a new access token, which is then stored as the connector's OAuth
    token row so downstream resolution is identical to any other mode (AC3).

    Never raises — a mint failure (unreachable host, rejected assertion) resolves
    to ``None`` so get_token() surfaces it as not-authenticated rather than
    crashing the run.
    """
    material = get_jwt_bearer_credential(org_id, connector_id)
    if material is None:
        return None

    config = CONNECTOR_AUTH_CONFIGS.get(connector_id)
    if config is None:
        return None

    base_url = (material.base_url or "").rstrip("/")
    token_url = f"{base_url}/services/oauth2/token" if base_url else None

    try:
        token_response = await _oauth.get_jwt_bearer_token(
            config,
            private_key=material.secret,
            subject=material.username,
            audience=base_url or None,
            token_url=token_url,
        )
    except _oauth.OAuthError as exc:
        logger.warning(
            "JWT bearer token mint failed for %s/%s: %s",
            org_id,
            connector_id,
            exc.reason,
        )
        return None

    record = store_token(org_id, connector_id, token_response)

    # Salesforce returns instance_url in the JWT bearer response, exactly as it
    # does for authorization_code. Persist it so live ingest resolves the URL
    # without any SF_INSTANCE_URL env config (mirrors the OAuth connect capture).
    instance_url = token_response.get("instance_url")
    if instance_url:
        try:
            from app.live_ingest_credentials import store_connector_instance_url

            store_connector_instance_url(org_id, connector_id, instance_url.rstrip("/"))
        except Exception:
            logger.exception(
                "Failed to persist instance URL from JWT bearer for %s/%s",
                org_id,
                connector_id,
            )

    return record


# ---------------------------------------------------------------------------
# Client-credentials minting (R18-A3 T3 / AT-556)
#
# Microsoft Graph (Teams / SharePoint) — and any client_credentials connector —
# authenticates outbound under a SERVICE identity: the app's own client_id +
# client_secret are exchanged for an access token with no browser redirect and no
# inbound callback (AC2). The grant issues NO refresh token, so — exactly like JWT
# bearer's re-assertion — get_token re-mints from the deployment credential when
# the cached token is missing or expired. The minted token lands in the SAME OAuth
# token row as any other mode, so ingestion resolves it identically (AC3).
# ---------------------------------------------------------------------------


async def _mint_client_credentials_token(
    org_id: str, connector_id: str
) -> Optional[TokenRecord]:
    """Mint (or re-mint) an access token via the client-credentials grant.

    Returns a freshly-minted, cached :class:`TokenRecord` when the connector is
    configured for ``client_credentials`` mode for this org, or ``None`` otherwise
    (so :func:`get_token` falls back to its normal not-authenticated handling). The
    outbound exchange never touches an inbound callback (AC2), and the result is
    stored as the connector's OAuth token row so downstream resolution is identical
    to any other mode (AC3).

    Gated on the org's RESOLVED mode being ``client_credentials`` (its own default
    for SAP/D365; a per-org selection for Teams/SharePoint), so an authorization_code
    connector — whose expired token requires a user reconnect — is never silently
    re-minted here.

    Never raises — a mint failure (unreachable host, rejected credentials, or an
    unconfigured client secret) resolves to ``None`` so get_token surfaces it as
    not-authenticated rather than crashing the run.
    """
    config = CONNECTOR_AUTH_CONFIGS.get(connector_id)
    if config is None:
        return None
    if not connector_supports_mode(connector_id, AUTH_MODE_CLIENT_CREDENTIALS):
        return None
    if resolve_auth_mode(org_id, connector_id) != AUTH_MODE_CLIENT_CREDENTIALS:
        return None

    try:
        token_response = await _oauth.get_client_credentials_token(config)
    except _oauth.OAuthError as exc:
        logger.warning(
            "client_credentials token mint failed for %s/%s: %s",
            org_id,
            connector_id,
            exc.reason,
        )
        return None
    except MissingSecretError:
        # The deployment's connector client secret is not configured. Surface as
        # not-authenticated (never log the missing value); the operator sets the
        # {CONNECTOR}_CLIENT_SECRET env var to enable the mode.
        logger.warning(
            "client_credentials token mint skipped for %s/%s: connector secret not configured",
            org_id,
            connector_id,
        )
        return None

    return store_token(org_id, connector_id, token_response)


async def _mint_outbound_token(
    org_id: str, connector_id: str
) -> Optional[TokenRecord]:
    """Try each outbound-only mint path in turn: JWT bearer, then client-credentials.

    Returns the first minted :class:`TokenRecord`, or ``None`` when the connector
    holds no outbound-only material/mode for this org. Each inner mint is gated on
    its own mode/material, so at most one applies — the order is just a cheap probe
    (the JWT-bearer probe returns immediately when no signing material is vaulted).
    """
    minted = await _mint_jwt_bearer_token(org_id, connector_id)
    if minted is not None:
        return minted
    return await _mint_client_credentials_token(org_id, connector_id)


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
                (id, org_id, connector_id, kind, access_token, refresh_token, expires_at, scopes, created_at, updated_at, refresh_failed)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)
            ON CONFLICT (org_id, connector_id) DO UPDATE SET
                kind           = EXCLUDED.kind,
                access_token   = EXCLUDED.access_token,
                refresh_token  = EXCLUDED.refresh_token,
                expires_at     = EXCLUDED.expires_at,
                scopes         = EXCLUDED.scopes,
                enc_username   = NULL,
                enc_secret     = NULL,
                base_url       = NULL,
                updated_at     = EXCLUDED.updated_at,
                refresh_failed = 0,
                is_deleted     = FALSE
            """,
            (record_id, org_id, connector_id, OAUTH_CREDENTIAL_KIND, enc_access, enc_refresh, expires_iso, scopes_json, now_iso, now_iso),
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


async def get_token(
    org_id: str,
    connector_id: str,
    *,
    min_validity_seconds: int = REFRESH_THRESHOLD_SECONDS,
) -> TokenRecord:
    """Return a valid, decrypted TokenRecord for the given org+connector.

    Auto-refreshes if the token has ``min_validity_seconds`` or less left before
    expiry (default ``REFRESH_THRESHOLD_SECONDS``). The proactive token-refresher
    background job passes a larger lookahead so tokens are renewed *before* they
    lapse, rather than only on the read that happens to fall inside the window.
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
              AND kind = %s AND is_deleted = FALSE
            """,
            (org_id, connector_id, OAUTH_CREDENTIAL_KIND),
        )
        row = cur.fetchone()
    finally:
        con.close()

    if row is None:
        # No cached OAuth token. An outbound-only connector has no token to start
        # with — mint one outbound (JWT bearer re-assertion, or a client-credentials
        # exchange for a service identity — AC1/AC2). This is the outbound "connect".
        # Falls through to not-authenticated when no outbound material/mode applies
        # (e.g. a static-credential connector, or one that was never authenticated).
        minted = await _mint_outbound_token(org_id, connector_id)
        if minted is not None:
            return minted
        raise ConnectorNotAuthenticatedError(org_id, connector_id)

    record = _row_to_token_record(row)

    now = datetime.now(timezone.utc)
    seconds_left = (record.expires_at - now).total_seconds()

    if seconds_left <= min_validity_seconds:
        if record.refresh_token is None:
            # No refresh token. For an outbound-only connector this is expected —
            # "refresh" is a fresh signed assertion (JWT bearer) or a fresh
            # client-credentials exchange, not an OAuth refresh grant — so re-mint
            # outbound rather than failing (AC1/AC2). Any other connector with no
            # refresh token stays not-authenticated.
            minted = await _mint_outbound_token(org_id, connector_id)
            if minted is not None:
                return minted
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

        # Preserve the existing refresh token when the provider does NOT return a
        # new one on refresh. Salesforce (and ServiceNow without token rotation)
        # keep the same long-lived refresh token and omit it from the refresh
        # response; only rotating providers (Atlassian/Jira) return a fresh one.
        # Without this, store_token would overwrite refresh_token with NULL and the
        # NEXT refresh would have nothing to present — dropping the connector to
        # needs_auth after a single refresh. Carrying the old token forward keeps
        # auto-refresh working indefinitely for the life of the refresh token.
        if not new_response.get("refresh_token"):
            new_response["refresh_token"] = record.refresh_token

        record = store_token(org_id, connector_id, new_response)

    return record


def get_credential(
    org_id: str, connector_id: str
) -> Optional[Union[TokenRecord, StaticCredentialRecord]]:
    """Synchronous, non-refreshing read of a stored credential for org+connector.

    Returns the decrypted vault record when an active credential exists — a
    ``TokenRecord`` for an OAuth connector (``kind='oauth'``) or a
    ``StaticCredentialRecord`` for a static one (``kind='static'``, e.g. a Jira
    API token or ServiceNow user/password) — or ``None`` when there is none
    (never stored, or revoked/soft-deleted). This is the low-level read primitive
    that the single credential-resolution layer (``app/auth/credentials.py``)
    wraps; it dispatches on the ``kind`` column so a static credential is never
    mis-read as an OAuth token with an empty access token, and vice versa.

    Unlike :func:`get_token`, this deliberately does NOT auto-refresh and does NOT
    raise: it returns exactly what the per-org encrypted vault holds, so the
    resolution layer can decide whether a missing credential is a
    ``CredentialsNotConfigured`` state. Callers needing a guaranteed-valid OAuth
    token with the refresh path should still use :func:`get_token`.
    """
    _init_credentials_table()

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT kind FROM credentials "
            "WHERE org_id = %s AND connector_id = %s AND is_deleted = FALSE",
            (org_id, connector_id),
        )
        row = cur.fetchone()
    finally:
        con.close()

    if row is None:
        return None

    # kind is NOT NULL DEFAULT 'oauth', so a legacy/OAuth row resolves through the
    # token path; only an explicit 'static' row uses the static-credential reader.
    if row[0] == STATIC_CREDENTIAL_KIND:
        return get_static_credential(org_id, connector_id)
    return _read_oauth_credential(org_id, connector_id)


def _read_oauth_credential(org_id: str, connector_id: str) -> Optional[TokenRecord]:
    """Read and decrypt the OAuth token row for org+connector (no refresh)."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, org_id, connector_id, access_token, refresh_token,
                   expires_at, scopes, created_at, updated_at
            FROM credentials
            WHERE org_id = %s AND connector_id = %s
              AND kind = %s AND is_deleted = FALSE
            """,
            (org_id, connector_id, OAUTH_CREDENTIAL_KIND),
        )
        row = cur.fetchone()
    finally:
        con.close()

    if row is None:
        return None

    return _row_to_token_record(row)


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
            "SELECT access_token FROM credentials "
            "WHERE org_id = %s AND connector_id = %s AND is_deleted = FALSE",
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

    # --- Step 2: local soft delete (always executes regardless of Step 1 outcome) ---
    # The app role has no DELETE; mark inactive. get_token/get_token_status filter
    # is_deleted, and store_token reactivates the row on reconnect.
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE credentials SET is_deleted = TRUE "
            "WHERE org_id = %s AND connector_id = %s",
            (org_id, connector_id),
        )
        con.commit()
    finally:
        con.close()
