"""Enforce audit_log immutability at the GRANT level — 2.0-D4 T2 (AC2).

``database/models/audit_log.py`` has documented the intended posture since AT-82:

    REVOKE UPDATE, DELETE ON audit_log FROM app_user;
    GRANT INSERT, SELECT ON audit_log TO app_user;

Documented grants and APPLIED grants are different things, and this deployment
proved it: ``provision.sql`` contained no REVOKE for ``audit_log`` at all, and the
application role held UPDATE, DELETE and TRUNCATE on the table. AC2 asks for a
data-layer test, and the honest reading of that is two halves — that the store
issues no UPDATE/DELETE (a source sweep, which catches a future code change) and
that the deployed role genuinely lacks the privilege (which catches a provisioning
script that never ran). This migration is what makes the second half true.

An important limitation, stated rather than discovered later
-----------------------------------------------------------
In PostgreSQL a table's OWNER retains the ability to re-grant itself anything, so
REVOKE against the owning role is advisory rather than binding. Grant-level
immutability only genuinely binds when the application role does **not** own
``audit_log``. That is a provisioning decision (create the table as a migration/DBA
role, grant INSERT+SELECT to the application role) and cannot be fixed from inside a
migration that runs AS the application role — so the migration applies what it can
and ``tests/unit/test_audit_log_immutability.py`` reports the ownership caveat
explicitly instead of letting a passing test imply protection that is not there.

Retention is deliberately NOT implemented here: an append-only table plus an
in-application deletion path is a contradiction. See
``docs/audit_export_and_retention.md`` for the retention design (how long, enforced
by what, run as whom) — the deletion path is outside the application by design.
"""
from __future__ import annotations

import logging
from typing import Sequence, Union

from alembic import op

logger = logging.getLogger(__name__)

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The privileges an append-only audit table must not grant to the application.
_FORBIDDEN = ("UPDATE", "DELETE", "TRUNCATE")


def _existing_roles(conn) -> set:
    """The subset of candidate role names that actually exist in this cluster.

    The application role name differs per deployment (``app_user`` in the documented
    posture, something else in practice), so the migration resolves what is really
    there instead of assuming. Checked UP FRONT rather than by attempting a REVOKE and
    catching the error: in PostgreSQL a failed statement aborts the surrounding
    transaction, so a swallowed exception leaves every later statement failing with
    "current transaction is aborted" — which is exactly how the first version of this
    migration broke the whole test-database build.
    """
    try:
        current = conn.exec_driver_sql("SELECT current_user").scalar()
    except Exception:  # pragma: no cover — non-PostgreSQL backend
        return set()
    candidates = {r for r in ("app_user", current) if r}
    if not candidates:
        return set()
    rows = conn.exec_driver_sql(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%(names)s)",
        {"names": list(candidates)},
    ).fetchall()
    return {row[0] for row in rows}


def _audit_log_exists(conn) -> bool:
    try:
        return bool(
            conn.exec_driver_sql(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'audit_log'"
            ).scalar()
        )
    except Exception:  # pragma: no cover — non-PostgreSQL backend
        return False


def upgrade() -> None:
    """Revoke mutating privileges on audit_log from the application role."""
    conn = op.get_bind()
    if not _audit_log_exists(conn):
        logger.warning("audit_log not present — skipping the immutability revoke")
        return

    # PUBLIC first: an implicit grant to PUBLIC would defeat any role-level revoke.
    # Safe unconditionally once the table exists.
    for privilege in _FORBIDDEN:
        conn.exec_driver_sql(f"REVOKE {privilege} ON audit_log FROM PUBLIC")

    for role in sorted(_existing_roles(conn)):
        for privilege in _FORBIDDEN:
            conn.exec_driver_sql(f'REVOKE {privilege} ON audit_log FROM "{role}"')
        # Re-assert what the application legitimately needs, so a revoke can never
        # leave it unable to append to or read its own audit trail.
        for privilege in ("INSERT", "SELECT"):
            conn.exec_driver_sql(f'GRANT {privilege} ON audit_log TO "{role}"')


def downgrade() -> None:
    """Restore mutating privileges.

    Provided for completeness only. Running this re-opens the immutability hole AC2
    exists to close, so it should never be part of a routine rollback.
    """
    conn = op.get_bind()
    if not _audit_log_exists(conn):
        return
    for role in sorted(_existing_roles(conn)):
        for privilege in _FORBIDDEN:
            conn.exec_driver_sql(f'GRANT {privilege} ON audit_log TO "{role}"')
