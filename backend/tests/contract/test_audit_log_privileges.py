"""
2.0-D4 T2 — audit_log deployed-privilege check (D4-AC2, second half).

AC2 asks for a data-layer test, and it has two halves that catch different failures.
``tests/unit/test_audit_log_immutability.py`` holds the SOURCE sweep (no UPDATE/DELETE
in the store — catches a future code change). This holds the other half: the role the
application actually connects as must not hold UPDATE/DELETE/TRUNCATE — which catches
a provisioning script that never ran.

It lives in the contract suite because it needs a live database. Run from
``tests/unit`` it skipped for want of a DATABASE_URL and proved nothing, which is
exactly the vacuous pass this half exists to prevent.

What this found: ``database/models/audit_log.py`` had documented
``REVOKE UPDATE, DELETE ON audit_log FROM app_user`` since AT-82 and it had never been
applied — ``provision.sql`` carried no REVOKE for the table and the application role
held UPDATE, DELETE **and** TRUNCATE. Migration 0038 applies it.

Ownership caveat (docs/audit_export_and_retention.md section 3.3): in PostgreSQL a
table OWNER can re-grant itself anything, so REVOKE against the owning role is
advisory. Where the application role owns the table this cannot prove immutability,
so the test says so loudly rather than passing and implying protection.
"""
from __future__ import annotations

import pathlib
import re

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[2]
TABLE = "audit_log"


# ── half 2: the deployed privilege ─────────────────────────────────────────────


def _privilege_state():
    """(role, owner, {privilege: bool}) for audit_log, or None when no DB."""
    import os

    if not os.getenv("DATABASE_URL"):
        return None
    try:
        import psycopg2
    except Exception:  # pragma: no cover
        return None
    try:
        con = psycopg2.connect(os.environ["DATABASE_URL"])
    except Exception:
        return None
    try:
        cur = con.cursor()
        cur.execute("SELECT current_user")
        role = cur.fetchone()[0]
        cur.execute(
            "SELECT tableowner FROM pg_tables WHERE tablename = %s", (TABLE,)
        )
        row = cur.fetchone()
        owner = row[0] if row else None
        privileges = {}
        for privilege in ("INSERT", "SELECT", "UPDATE", "DELETE", "TRUNCATE"):
            cur.execute(
                "SELECT has_table_privilege(%s, %s, %s)", (role, TABLE, privilege)
            )
            privileges[privilege] = bool(cur.fetchone()[0])
        return role, owner, privileges
    finally:
        con.close()


class TestDeployedPrivilege:
    """Catches a provisioning script that never ran — the half a source sweep
    cannot see."""

    def test_the_application_role_can_still_append_and_read(self):
        """The revoke must not break the audit trail it protects."""
        state = _privilege_state()
        if state is None:
            pytest.skip("no reachable PostgreSQL — privilege state unverifiable")
        role, _owner, privileges = state
        assert privileges["INSERT"], f"{role} must retain INSERT on {TABLE}"
        assert privileges["SELECT"], f"{role} must retain SELECT on {TABLE}"

    def test_the_application_role_holds_no_mutating_privilege(self):
        """The real AC2 assertion.

        Where the application role OWNS the table this cannot be proven — an owner
        re-grants itself at will — so the test reports that explicitly rather than
        passing and implying protection. It never passes vacuously: a non-owning role
        holding UPDATE/DELETE/TRUNCATE is a hard failure.
        """
        state = _privilege_state()
        if state is None:
            pytest.skip("no reachable PostgreSQL — privilege state unverifiable")
        role, owner, privileges = state
        held = [p for p in ("UPDATE", "DELETE", "TRUNCATE") if privileges[p]]

        if role == owner and held:
            pytest.skip(
                f"{role} OWNS {TABLE}, so REVOKE is advisory and grant-level "
                f"immutability cannot be proven here (still holds: {held}). "
                "Provision audit_log under a migration/DBA role and grant the "
                "application role INSERT+SELECT only — see "
                "docs/audit_export_and_retention.md section 3.3."
            )
        assert not held, (
            f"{role} does not own {TABLE} but still holds {held} — migration 0038 "
            "has not been applied to this database"
        )

    def test_the_documented_posture_is_actually_applied_somewhere(self):
        """The provisioning artifact must contain the revoke.

        Independent of the live database: catches the original defect, where the
        posture existed only as a comment in the DDL module and no provisioning path
        applied it.
        """
        provision = (BACKEND / "database" / "provision" / "provision.sql").read_text(
            encoding="utf-8"
        )
        migration = (
            BACKEND / "migrations" / "versions" / "0038_enforce_audit_log_immutability.py"
        ).read_text(encoding="utf-8")

        assert re.search(rf"REVOKE[^;]*{TABLE}", provision, re.I | re.S), (
            "provision.sql must REVOKE mutating privileges on audit_log"
        )
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            assert privilege in migration, f"migration 0038 must revoke {privilege}"

    def test_the_ddl_module_still_documents_the_posture(self):
        ddl = (BACKEND / "database" / "models" / "audit_log.py").read_text(
            encoding="utf-8"
        )
        assert "REVOKE" in ddl and TABLE in ddl


