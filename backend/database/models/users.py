"""DDL for the users table — AUTH-1 / AT-232.

users is IDENTITY ONLY. It answers "who you are", nothing about membership.

    org_id and role do NOT live here. They live in workspace_members, which is
    the single source of truth for which workspace a user belongs to and what
    they can do (AC1). Never add org_id or role back to this table — the login
    flow joins workspace_members at JWT-assembly time to source them.

No ORM — this project uses raw sqlite3 with Alembic raw-SQL migrations. This
module is the single source of truth for the schema; the migration
(0004_create_users_and_login_attempts.py) imports ALL_USERS_DDL so the CI gate
and any runtime CREATE-IF-NOT-EXISTS path can never drift apart. This mirrors
the pattern established by database/models/entities.py.

POC constraint — global email uniqueness (design debt):
    email is UNIQUE across the entire platform in this story. This is a
    deliberate POC simplification and is architecturally incompatible with a
    future multi-org model where the same person works across several customer
    workspaces. When multi-org identity is built, the unique constraint moves
    from `email` to `(email, org_id)`, the table gains a separate identity
    anchor, and idx_users_email_unique is migrated. Do not let this fossilise.
    Tracked as design debt in deployment/README.md, "AUTH-1 — Tracked Design
    Debt & Deferred Hardening" (issue #12).

invite_token_hash / invite_token_expires_at — RESERVED, currently UNUSED (issue #11):
    The live invite flow (routes_auth.py) is the CANONICAL store for invite
    tokens: it keeps the SHA-256 hash, org/role, expiry, and used flag in the KV
    store, NOT in these columns. These columns are reserved for a future
    DB-backed invite implementation and are never written or read today. Do not
    add a second write path here without first consolidating onto one store — two
    sources of truth for invite state is exactly the drift this note prevents.

last_login_at — LOGIN-EVENT-ONLY (issue #18):
    Set by user_auth.login() on each successful login and nowhere else. It is NOT
    a "last active" timestamp: GET /api/auth/me does not touch it, and there are
    no refresh tokens in this story. If silent re-auth / token refresh is added
    later, update this field there too (or rename it) so it does not silently
    under-report activity.

reset_token_hash / reset_token_expires_at — FORGOT-PASSWORD FLOW (CS-3):
    Back the forgot/reset-password flow. POST /api/auth/forgot-password stores
    the SHA-256 hash of a freshly generated reset token (NEVER the raw token, so a
    DB leak does not expose usable reset links) and a one-hour expiry here; POST
    /api/auth/reset-password validates the hash + expiry, rejecting an expired
    link with 400. Both columns are NULLABLE so existing accounts stay valid with
    no backfill (added by migration 0008_add_password_reset_fields). They are
    cleared (set back to NULL) once a reset is consumed. Unlike the reserved
    invite_token_* columns above, these ARE read and written by the live flow.

SQLite-compatible types (TEXT-backed). PostgreSQL deployment replaces:
    VARCHAR(36)  id                       -> UUID
    TIMESTAMP    *_at                      -> TIMESTAMP WITH TIME ZONE
    BOOLEAN      is_active                 -> BOOLEAN (native)
"""

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id                      VARCHAR(36)  NOT NULL PRIMARY KEY,
    email                   VARCHAR(256) NOT NULL,
    password_hash           VARCHAR(256) NOT NULL,
    is_active               BOOLEAN      NOT NULL,
    invite_token_hash       VARCHAR(256),
    invite_token_expires_at TIMESTAMP,
    created_at              TIMESTAMP    NOT NULL,
    last_login_at           TIMESTAMP,
    reset_token_hash        VARCHAR(256),
    reset_token_expires_at  TIMESTAMP
)
"""

# Password-reset columns (CS-3). Kept as standalone ADD COLUMN statements so the
# 0008 migration can apply them to a pre-existing users table, while the same two
# columns also appear in CREATE_USERS_TABLE above for fresh databases. Both
# nullable so existing rows need no backfill. SQLite appends added columns at the
# end of the table, matching their position in CREATE_USERS_TABLE — so the
# migrated schema and a freshly created schema have identical column order.
ADD_RESET_TOKEN_HASH_COLUMN = (
    "ALTER TABLE users ADD COLUMN reset_token_hash VARCHAR(256)"
)
ADD_RESET_TOKEN_EXPIRES_AT_COLUMN = (
    "ALTER TABLE users ADD COLUMN reset_token_expires_at TIMESTAMP"
)

# Ordered DDL the 0008 migration imports — single source of truth, no drift.
ADD_PASSWORD_RESET_COLUMNS_DDL: tuple[str, ...] = (
    ADD_RESET_TOKEN_HASH_COLUMN,
    ADD_RESET_TOKEN_EXPIRES_AT_COLUMN,
)

# Global unique index on email (POC constraint — see module docstring).
# email is stored lowercased and trimmed by the application layer (AT-233).
CREATE_USERS_EMAIL_UNIQUE_IDX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users (email)
"""

ALL_USERS_DDL: tuple[str, ...] = (
    CREATE_USERS_TABLE,
    CREATE_USERS_EMAIL_UNIQUE_IDX,
)
