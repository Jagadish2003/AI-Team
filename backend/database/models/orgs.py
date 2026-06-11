"""DDL for the orgs table — AUTH-1 / AT-233.

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
"""

CREATE_ORGS_TABLE = """
CREATE TABLE IF NOT EXISTS orgs (
    id         VARCHAR(36)  NOT NULL PRIMARY KEY,
    name       VARCHAR(256) NOT NULL,
    created_at TIMESTAMP    NOT NULL
)
"""

ALL_ORGS_DDL: tuple[str, ...] = (CREATE_ORGS_TABLE,)
