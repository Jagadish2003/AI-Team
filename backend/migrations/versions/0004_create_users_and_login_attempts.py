"""Create users (identity only) and login_attempts tables.

AUTH-1 / AT-232 — authentication foundation.

users carries identity ONLY (id, email, password_hash, is_active, invite token
fields, timestamps). It deliberately has NO org_id and NO role column — those
live in workspace_members, the single source of truth for membership (AC1).
A global unique index on email enforces the POC one-account-per-email
constraint (documented design debt — see database/models/users.py).

login_attempts backs lightweight application-layer rate limiting (5 failed
attempts per email or IP in 15 minutes -> 429).

The DDL lives in database/models/users.py (ALL_USERS_DDL) and
database/models/login_attempts.py (ALL_LOGIN_ATTEMPTS_DDL) and is imported here
so the migration (the CI gate) and any runtime CREATE-IF-NOT-EXISTS path can
never drift apart — the same pattern as 0003_create_entities.py. env.py
declares this project "raw-SQL migrations only"; importing tuples of raw SQL
strings does not introduce ORM metadata.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-09
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _auth_ddl() -> "tuple[str, ...]":
    """Return the locked users + login_attempts DDL from the single source."""
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models.users import ALL_USERS_DDL
    from database.models.login_attempts import ALL_LOGIN_ATTEMPTS_DDL

    return ALL_USERS_DDL + ALL_LOGIN_ATTEMPTS_DDL


def upgrade() -> None:
    for ddl in _auth_ddl():
        op.execute(ddl)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_login_attempts_ip")
    op.execute("DROP INDEX IF EXISTS idx_login_attempts_email")
    op.execute("DROP TABLE IF EXISTS login_attempts")
    op.execute("DROP INDEX IF EXISTS idx_users_email_unique")
    op.execute("DROP TABLE IF EXISTS users")
