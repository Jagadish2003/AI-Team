"""The migrated PostgreSQL database can support the complete A1/A2/A3 loop."""

from __future__ import annotations

import os

import psycopg2
import pytest

from database.provision.a1_a3_readiness import inspect_connection


def test_migrated_application_database_is_a1_a2_a3_ready():
    con = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        con.set_session(readonly=True)
        # The disposable test database is reached as PostgreSQL's superuser,
        # which bypasses ACLs even after a REVOKE.  Validate the full physical
        # schema here; unit tests pin the privilege DDL, while the real dev/prod
        # readiness command checks privileges as their non-superuser app role.
        report = inspect_connection(con, check_privileges=False)
    finally:
        con.rollback()
        con.close()

    assert report.ready, "\n".join(report.issues)


def test_migration_role_grants_safe_closed_loop_access_to_application_role():
    """A DBA-owned schema remains usable (and immutable) by ``aiqdevusr``."""

    con = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        con.set_session(readonly=True)
        with con.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'aiqdevusr'")
            if cur.fetchone() is None:
                pytest.skip("deployment application role is not present in this cluster")

            policies = {
                "opportunity_lifecycle": ({"SELECT", "INSERT", "UPDATE"}, {"DELETE", "TRUNCATE"}),
                "opportunity_lifecycle_history": ({"SELECT", "INSERT"}, {"UPDATE", "DELETE", "TRUNCATE"}),
                "opportunity_baselines": ({"SELECT", "INSERT"}, {"UPDATE", "DELETE", "TRUNCATE"}),
                "opportunity_movements": ({"SELECT", "INSERT", "UPDATE"}, {"DELETE", "TRUNCATE"}),
                "opportunity_feedback": ({"SELECT", "INSERT"}, {"UPDATE", "DELETE", "TRUNCATE"}),
                "ranking_adjustments": ({"SELECT", "INSERT", "UPDATE"}, {"DELETE", "TRUNCATE"}),
                "ranking_adjustment_history": ({"SELECT", "INSERT"}, {"UPDATE", "DELETE", "TRUNCATE"}),
                "audit_log": ({"SELECT", "INSERT"}, {"UPDATE", "DELETE", "TRUNCATE"}),
            }
            failures = []
            for table, (required, forbidden) in policies.items():
                for privilege in required:
                    cur.execute(
                        "SELECT has_table_privilege(%s, %s, %s)",
                        ("aiqdevusr", f"public.{table}", privilege),
                    )
                    if not cur.fetchone()[0]:
                        failures.append(f"{table} lacks {privilege}")
                for privilege in forbidden:
                    cur.execute(
                        "SELECT has_table_privilege(%s, %s, %s)",
                        ("aiqdevusr", f"public.{table}", privilege),
                    )
                    if cur.fetchone()[0]:
                        failures.append(f"{table} grants {privilege}")
    finally:
        con.rollback()
        con.close()

    assert not failures, "\n".join(failures)
