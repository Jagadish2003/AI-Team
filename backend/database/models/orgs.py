"""DDL for the orgs table — AUTH-1 / AT-233, AUTH-2 / AT-352.

Persists the workspace (organization) created at registration. org_id is the
opaque tenant identifier used everywhere else in the platform (workspace_members,
tenancy middleware, the per-org KV/data isolation layer); this table simply gives
that id a human-readable name and a creation timestamp.

Registration (register_org_and_owner) inserts orgs + users + workspace_members in
a single transaction. Membership and role still live in workspace_members — this
table holds organization identity only, never user identity or role.

No ORM — single source of truth for the schema, imported by
0005_create_orgs.py and the runtime ensure_auth_tables() lazy-init so the
migration (CI gate) and runtime can never drift. Same pattern as
database/models/users.py and entities.py.

SQLite-compatible types. PostgreSQL deployment replaces VARCHAR(36) id -> UUID,
TIMESTAMP -> TIMESTAMP WITH TIME ZONE.

AUTH-2 approval columns (added by migration 0013):
  approval_status          — state machine: pending_approval | active | rejected.
                             NOT NULL DEFAULT 'pending_approval'. Every org starts
                             pending; transitions are one-directional.
  approval_token_hash      — SHA-256 hex digest of the signed approval token.
                             NULL after the token is consumed (single-use).
  approval_token_expires_at — UTC expiry for the token (7 days from registration).
  approved_at              — UTC timestamp set when approve/reject action is taken.
  approved_by_action       — 'approved' | 'rejected', set alongside approved_at.
"""

CREATE_ORGS_TABLE = """
CREATE TABLE IF NOT EXISTS orgs (
    id         VARCHAR(36)  NOT NULL PRIMARY KEY,
    name       VARCHAR(256) NOT NULL,
    created_at TIMESTAMP    NOT NULL
)
"""

ADD_APPROVAL_STATUS_COLUMN = """
ALTER TABLE orgs
ADD COLUMN approval_status VARCHAR(32) NOT NULL DEFAULT 'pending_approval'
"""

ADD_APPROVAL_TOKEN_HASH_COLUMN = """
ALTER TABLE orgs
ADD COLUMN approval_token_hash VARCHAR(256) NULL
"""

ADD_APPROVAL_TOKEN_EXPIRES_AT_COLUMN = """
ALTER TABLE orgs
ADD COLUMN approval_token_expires_at TIMESTAMP NULL
"""

ADD_APPROVED_AT_COLUMN = """
ALTER TABLE orgs
ADD COLUMN approved_at TIMESTAMP NULL
"""

ADD_APPROVED_BY_ACTION_COLUMN = """
ALTER TABLE orgs
ADD COLUMN approved_by_action VARCHAR(16) NULL
"""

# Org-name deduplication (migration 0020). A normalised (trimmed + lowercased)
# copy of the org name plus a UNIQUE index on it, so two registrations of the
# same company name resolve to ONE org_id instead of fragmenting into duplicate
# workspaces. name_normalised is written ONLY by register_org_and_owner via
# user_auth.normalise_org_name() — never from a raw user value.
#
# Kept as standalone ADD COLUMN / index statements (NOT part of CREATE_ORGS_TABLE)
# so the fresh-migration chain stays consistent: 0005 creates the base table,
# 0013 adds the approval columns, 0020 adds this column. Same SSOT pattern as
# ALL_ORG_APPROVAL_DDL. provision.sql carries the fully-expanded column + index
# for the pure-SQL provisioning path.
ADD_ORG_NAME_NORMALISED_COLUMN = """
ALTER TABLE orgs
ADD COLUMN IF NOT EXISTS name_normalised VARCHAR(256) NOT NULL DEFAULT ''
"""

BACKFILL_ORG_NAME_NORMALISED = """
UPDATE orgs SET name_normalised = LOWER(name) WHERE name_normalised = ''
"""

CREATE_ORGS_NAME_NORMALISED_UNIQUE_IDX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_orgs_name_normalised_unique
ON orgs (name_normalised)
"""

ALL_ORGS_DDL: tuple[str, ...] = (CREATE_ORGS_TABLE,)

# Ordered DDL the 0020 migration imports — single source of truth, no drift. The
# UNIQUE index is created AFTER the backfill so pre-existing rows are normalised
# before the constraint is enforced.
ALL_ORG_NAME_NORMALISED_DDL: tuple[str, ...] = (
    ADD_ORG_NAME_NORMALISED_COLUMN,
    BACKFILL_ORG_NAME_NORMALISED,
    CREATE_ORGS_NAME_NORMALISED_UNIQUE_IDX,
)

ALL_ORG_APPROVAL_DDL: tuple[str, ...] = (
    ADD_APPROVAL_STATUS_COLUMN,
    ADD_APPROVAL_TOKEN_HASH_COLUMN,
    ADD_APPROVAL_TOKEN_EXPIRES_AT_COLUMN,
    ADD_APPROVED_AT_COLUMN,
    ADD_APPROVED_BY_ACTION_COLUMN,
)
