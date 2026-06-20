"""Make org approval timestamps timezone-aware — AUTH-2 review #6.

0013 added approval_token_expires_at and approved_at as naive TIMESTAMP
(WITHOUT TIME ZONE). A naive expiry column carries the same UTC fragility fixed
in PR #178: the value is interpreted in whatever the session timezone happens
to be, so an expiry written as UTC can be read back skewed if a connection runs
in a non-UTC timezone — silently shifting the 7-day approval window.

This converts both columns to TIMESTAMP WITH TIME ZONE. Existing values were
written as UTC (register_org_and_owner uses datetime.now(timezone.utc)), so the
conversion interprets them with ``USING <col> AT TIME ZONE 'UTC'`` to preserve
the instant. PostgreSQL only (the project's sole target).

Idempotent: each column is converted only if it exists and is not already
timezone-aware, so re-running (or running against a fresh DB whose 0013 already
produced tz-aware columns) is a no-op.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TZ_COLUMNS = ("approval_token_expires_at", "approved_at")


def _orgs_columns() -> "dict[str, object]":
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("orgs"):
        return {}
    return {col["name"]: col["type"] for col in inspector.get_columns("orgs")}


def upgrade() -> None:
    columns = _orgs_columns()
    for name in _TZ_COLUMNS:
        col_type = columns.get(name)
        if col_type is None:
            continue
        # Skip if already timezone-aware (fresh DB / re-run).
        if getattr(col_type, "timezone", False):
            continue
        op.execute(
            f"ALTER TABLE orgs ALTER COLUMN {name} "
            f"TYPE TIMESTAMP WITH TIME ZONE USING {name} AT TIME ZONE 'UTC'"
        )


def downgrade() -> None:
    columns = _orgs_columns()
    for name in _TZ_COLUMNS:
        col_type = columns.get(name)
        if col_type is None:
            continue
        if not getattr(col_type, "timezone", False):
            continue
        op.execute(
            f"ALTER TABLE orgs ALTER COLUMN {name} "
            f"TYPE TIMESTAMP WITHOUT TIME ZONE USING {name} AT TIME ZONE 'UTC'"
        )
