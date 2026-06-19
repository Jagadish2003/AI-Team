"""Add org approval workflow columns to the orgs table — AUTH-2 / AT-352.

Adds five columns that power the pending-approval state machine introduced in
AUTH-2. Every new organisation registration starts in 'pending_approval' and
cannot log in until a CloudFulcrum admin approves via a signed email link.

Schema additions:
  approval_status          VARCHAR(32) NOT NULL DEFAULT 'pending_approval'
                           Valid values: 'pending_approval' | 'active' | 'rejected'.
                           Load-bearing: login() and register() branch on this field.
  approval_token_hash      VARCHAR(256) NULL
                           SHA-256 hex digest of the one-time approval token.
                           Cleared to NULL after first use (single-use enforcement).
  approval_token_expires_at TIMESTAMP NULL
                           UTC expiry; tokens older than 7 days are rejected.
  approved_at              TIMESTAMP NULL
                           Set when an admin clicks approve or reject.
  approved_by_action       VARCHAR(16) NULL
                           'approved' | 'rejected' — recorded alongside approved_at.

Rollback removes all five columns. SQLite supports ADD COLUMN but not DROP COLUMN
natively, so downgrade uses batch_alter_table for SQLite compatibility.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("orgs"):
        return

    existing_columns = {col["name"] for col in inspector.get_columns("orgs")}

    if "approval_status" not in existing_columns:
        op.add_column(
            "orgs",
            sa.Column(
                "approval_status",
                sa.String(32),
                nullable=False,
                server_default="pending_approval",
            ),
        )

    if "approval_token_hash" not in existing_columns:
        op.add_column(
            "orgs",
            sa.Column("approval_token_hash", sa.String(256), nullable=True),
        )

    if "approval_token_expires_at" not in existing_columns:
        op.add_column(
            "orgs",
            sa.Column("approval_token_expires_at", sa.DateTime(), nullable=True),
        )

    if "approved_at" not in existing_columns:
        op.add_column(
            "orgs",
            sa.Column("approved_at", sa.DateTime(), nullable=True),
        )

    if "approved_by_action" not in existing_columns:
        op.add_column(
            "orgs",
            sa.Column("approved_by_action", sa.String(16), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("orgs"):
        return

    existing_columns = {col["name"] for col in inspector.get_columns("orgs")}
    cols_to_drop = [
        "approval_status",
        "approval_token_hash",
        "approval_token_expires_at",
        "approved_at",
        "approved_by_action",
    ]
    cols_present = [c for c in cols_to_drop if c in existing_columns]

    if not cols_present:
        return

    with op.batch_alter_table("orgs") as batch_op:
        for col in cols_present:
            batch_op.drop_column(col)
