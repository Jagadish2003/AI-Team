"""Structural guards for the A1/A2/A3 database-readiness contract."""

from __future__ import annotations

from pathlib import Path

from app.history_retention import PROTECTED_TABLES
from database.models.closed_loop_immutability import (
    ALL_CLOSED_LOOP_IMMUTABILITY_DDL,
    APPEND_ONLY_TABLES,
    CLOSED_LOOP_PROTECTED_TABLES,
    MUTABLE_CLOSED_LOOP_TABLES,
)
from database.provision.a1_a3_readiness import (
    REQUIRED_CHECK_CONSTRAINTS,
    FORBIDDEN_PRIVILEGES,
    REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    REQUIRED_KEYS,
    expected_alembic_head,
)


BACKEND = Path(__file__).resolve().parents[2]


def test_every_closed_loop_table_is_protected_from_deletion():
    assert set(CLOSED_LOOP_PROTECTED_TABLES) <= set(PROTECTED_TABLES)
    for table in CLOSED_LOOP_PROTECTED_TABLES:
        assert {"DELETE", "TRUNCATE"} <= set(FORBIDDEN_PRIVILEGES[table])


def test_append_only_tables_revoke_update_in_applied_ddl():
    ddl = "\n".join(ALL_CLOSED_LOOP_IMMUTABILITY_DDL)
    assert "REVOKE UPDATE ON TABLE" in ddl
    for table in APPEND_ONLY_TABLES:
        assert f"'{table}'" in ddl
        assert "UPDATE" in FORBIDDEN_PRIVILEGES[table]
    assert "audit_log" in ddl
    assert "REVOKE UPDATE, DELETE, TRUNCATE" in ddl
    assert "GRANT SELECT, INSERT ON TABLE" in ddl
    assert "GRANT UPDATE ON TABLE" in ddl
    assert set(MUTABLE_CLOSED_LOOP_TABLES).isdisjoint(APPEND_ONLY_TABLES)


def test_readiness_contract_covers_a1_a2_a3_storage_and_identity_keys():
    expected_tables = {
        "kv",
        "opportunity_instances",
        "opportunity_lifecycle",
        "opportunity_lifecycle_history",
        "opportunity_baselines",
        "opportunity_movements",
        "opportunity_feedback",
        "ranking_adjustments",
        "ranking_adjustment_history",
        "audit_log",
    }
    assert expected_tables <= set(REQUIRED_COLUMNS)
    assert expected_tables - {"runs", "signal_snapshots", "entities"} <= (
        set(REQUIRED_KEYS) | {"runs", "signal_snapshots", "entities"}
    )
    assert "idx_opp_movements_org_projection_verdict" in REQUIRED_INDEXES
    assert "idx_opportunity_feedback_similarity" in REQUIRED_INDEXES
    assert "idx_ranking_adjustment_history_group" in REQUIRED_INDEXES
    assert "action_note" in REQUIRED_COLUMNS["opportunity_lifecycle"]
    assert "note" in REQUIRED_COLUMNS["opportunity_lifecycle_history"]
    assert "reason_detail" in REQUIRED_COLUMNS["opportunity_feedback"]
    assert "reset_reason" in REQUIRED_COLUMNS["ranking_adjustment_history"]
    assert "ck_opp_lifecycle_measurable_action_date" in REQUIRED_CHECK_CONSTRAINTS[
        "opportunity_lifecycle"
    ]
    assert "ck_ranking_adjustment_reset_reason" in REQUIRED_CHECK_CONSTRAINTS[
        "ranking_adjustment_history"
    ]


def test_0049_repairs_before_restricting():
    migration_0049 = (
        BACKEND
        / "migrations"
        / "versions"
        / "0049_repair_a1_a3_database_readiness.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "0049"' in migration_0049
    assert 'down_revision: Union[str, None] = "0048"' in migration_0049
    assert migration_0049.index("_schema_repair_ddl()") < migration_0049.index(
        'ALL_CLOSED_LOOP_IMMUTABILITY_DDL'
    )


def test_0050_is_the_linear_head_and_persists_ui_governance_fields():
    migration = (
        BACKEND
        / "migrations"
        / "versions"
        / "0050_persist_a1_a3_ui_governance_fields.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "0050"' in migration
    assert 'down_revision: Union[str, None] = "0049"' in migration
    assert "action_note" in migration
    assert "reset_reason" in migration
    assert expected_alembic_head() == "0050"


def test_pure_sql_bundle_matches_head_and_applies_revoke_after_grant_all():
    sql = (BACKEND / "database" / "provision" / "provision.sql").read_text(
        encoding="utf-8"
    )
    assert "VALUES ('0050')" in sql
    assert '"action_note" "text"' in sql
    assert '"reset_reason" "text"' in sql
    grant_at = sql.index("GRANT ALL PRIVILEGES ON ALL TABLES")
    closed_loop_at = sql.index("A1/A2/A3 closed-loop immutability")
    assert closed_loop_at > grant_at
    for table in APPEND_ONLY_TABLES:
        assert f"'{table}'" in sql[closed_loop_at:]


def test_core_table_ensure_is_read_only_when_schema_is_already_complete():
    """A least-privilege app role must not ALTER tables it does not own."""

    from database import seed_loader

    class ExistingSchemaCursor:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append(" ".join(str(sql).split()))

        def fetchone(self):
            return (1,)

    class ExistingSchemaConnection:
        def __init__(self):
            self.cur = ExistingSchemaCursor()
            self.committed = False

        def cursor(self):
            return self.cur

        def commit(self):
            self.committed = True

    con = ExistingSchemaConnection()
    seed_loader.ensure_db(con)

    assert con.committed is True
    assert con.cur.statements
    assert all(statement.startswith("SELECT 1 FROM") for statement in con.cur.statements)
