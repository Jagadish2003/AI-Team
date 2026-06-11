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
    last_login_at           TIMESTAMP
)
"""

# Global unique index on email (POC constraint — see module docstring).
# email is stored lowercased and trimmed by the application layer (AT-233).
CREATE_USERS_EMAIL_UNIQUE_IDX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users (email)
"""

ALL_USERS_DDL: tuple[str, ...] = (
    CREATE_USERS_TABLE,
    CREATE_USERS_EMAIL_UNIQUE_IDX,
)
