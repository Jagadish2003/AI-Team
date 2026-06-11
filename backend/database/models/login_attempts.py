"""DDL for the login_attempts table — AUTH-1 / AT-232.

Lightweight application-layer brute-force protection. One row per login
attempt. The rate-limiter (AT-233) counts failed attempts for an email OR an IP
within a 15-minute window and throttles the 6th attempt with 429 + Retry-After.

    Indexes cover the two windowed-count query shapes:
      WHERE email = ?       AND attempted_at >= ?  -> idx_login_attempts_email
      WHERE ip_address = ?  AND attempted_at >= ?  -> idx_login_attempts_ip

Deployment note: if the deployment sits behind an API gateway / reverse proxy
with rate limiting already configured, this table is redundant — see
deployment/README.md. Do not delete it unless that gateway protection is
confirmed.

No ORM — single source of truth for the schema; imported by
0004_create_users_and_login_attempts.py so migration and runtime never drift.

SQLite-compatible types. PostgreSQL deployment replaces VARCHAR(36) id -> UUID,
TIMESTAMP -> TIMESTAMP WITH TIME ZONE, BOOLEAN -> native BOOLEAN.
"""

CREATE_LOGIN_ATTEMPTS_TABLE = """
CREATE TABLE IF NOT EXISTS login_attempts (
    id           VARCHAR(36)  NOT NULL PRIMARY KEY,
    email        VARCHAR(256) NOT NULL,
    ip_address   VARCHAR(64)  NOT NULL,
    attempted_at TIMESTAMP    NOT NULL,
    succeeded    BOOLEAN      NOT NULL
)
"""

CREATE_LOGIN_ATTEMPTS_IDX_EMAIL = """
CREATE INDEX IF NOT EXISTS idx_login_attempts_email
    ON login_attempts (email, attempted_at)
"""

CREATE_LOGIN_ATTEMPTS_IDX_IP = """
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip
    ON login_attempts (ip_address, attempted_at)
"""

ALL_LOGIN_ATTEMPTS_DDL: tuple[str, ...] = (
    CREATE_LOGIN_ATTEMPTS_TABLE,
    CREATE_LOGIN_ATTEMPTS_IDX_EMAIL,
    CREATE_LOGIN_ATTEMPTS_IDX_IP,
)
