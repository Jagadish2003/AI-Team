"""DDL for workspace_members — AT-82 T6."""

CREATE_WORKSPACE_MEMBERS_TABLE = """
CREATE TABLE IF NOT EXISTS workspace_members (
    org_id     TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('owner', 'analyst', 'viewer')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (org_id, user_id)
)
"""
