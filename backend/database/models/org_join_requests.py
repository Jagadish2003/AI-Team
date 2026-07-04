"""DDL for the org_join_requests table — org join-request approval flow.

A user's request to join an existing org, decided by an owner: it records the
requesting user, the target org, the requested role, and the pending/decided
state. workspace_members remains the source of truth for actual membership +
role; this table only tracks the *request* until it is approved or rejected.

Ownership note (migration 0023): this table previously lived ONLY in
provision.sql — a teammate added it to the pure-SQL dump but never created a
migration or model for it, so a database built via `alembic upgrade head` did
not have it. This module is now the single source of truth for the schema
(imported by 0023_create_org_join_requests_and_orgs_domain.py), mirroring the
entities/orgs/users pattern so the migration and the pure-SQL path can't drift.

SQLite-compatible types. PostgreSQL deployment replaces VARCHAR(36) -> UUID and
TIMESTAMP -> TIMESTAMP WITH TIME ZONE.
"""

CREATE_ORG_JOIN_REQUESTS_TABLE = """
CREATE TABLE IF NOT EXISTS org_join_requests (
    id           VARCHAR(36)  NOT NULL PRIMARY KEY,
    org_id       VARCHAR(36)  NOT NULL,
    user_id      VARCHAR(36)  NOT NULL,
    email        VARCHAR(256) NOT NULL,
    status       VARCHAR(16)  NOT NULL DEFAULT 'pending',
    role         VARCHAR(16),
    requested_at TIMESTAMP    NOT NULL,
    decided_at   TIMESTAMP,
    decided_by   VARCHAR(36)
)
"""

# Lookup index: an owner listing their org's requests by state.
CREATE_ORG_JOIN_REQUESTS_ORG_STATUS_IDX = """
CREATE INDEX IF NOT EXISTS idx_join_requests_org_status
ON org_join_requests (org_id, status)
"""

# One OPEN request per (org, user): a partial unique index over pending rows only,
# so a user can re-request after a prior request is decided (approved/rejected).
CREATE_ORG_JOIN_REQUESTS_PENDING_UNIQUE_IDX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_join_requests_pending_unique
ON org_join_requests (org_id, user_id) WHERE status = 'pending'
"""

# Ordered DDL the 0023 migration imports — single source of truth, no drift.
ALL_ORG_JOIN_REQUESTS_DDL: tuple[str, ...] = (
    CREATE_ORG_JOIN_REQUESTS_TABLE,
    CREATE_ORG_JOIN_REQUESTS_ORG_STATUS_IDX,
    CREATE_ORG_JOIN_REQUESTS_PENDING_UNIQUE_IDX,
)
