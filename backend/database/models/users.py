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
