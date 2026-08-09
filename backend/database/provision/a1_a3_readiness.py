"""Read-only readiness check for the complete A1 -> A2 -> A3 database loop.

The check is intentionally stricter than "the tables exist".  It verifies the
columns and keys that preserve cross-run identity, the indexes used by outcome
and learning reads, the Alembic head, and the application role's write policy.

Run from ``backend/`` without exposing connection strings::

    python database/provision/a1_a3_readiness.py --target all
    python database/provision/a1_a3_readiness.py --target dev
    python database/provision/a1_a3_readiness.py --target prod

The command only executes SELECT statements and exits non-zero on any gap.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


_BACKEND_DIR = Path(__file__).resolve().parents[2]


REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    # A1 serving copy + stable cross-run projection copy.
    "kv": frozenset({"key", "payload"}),
    "opportunity_instances": frozenset(
        {
            "opportunity_identity",
            "run_id",
            "org_id",
            "pack_id",
            "pack_version",
            "detector_id",
            "metadata",
            "created_at",
            "is_deleted",
        }
    ),
    # A2 measurement inputs and provenance.
    "runs": frozenset({"id", "payload", "seq"}),
    "signal_snapshots": frozenset(
        {
            "id",
            "org_id",
            "run_id",
            "pack_id",
            "detector_id",
            "signal_key",
            "metric_name",
            "metric_value",
            "captured_at",
        }
    ),
    "entities": frozenset(
        {"id", "org_id", "entity_type", "canonical_name", "first_seen_run_id"}
    ),
    "opportunity_lifecycle": frozenset(
        {
            "org_id",
            "opportunity_identity",
            "state",
            "action_date",
            "action_note",
            "actioned_by",
            "actioned_at",
            "revision",
            "first_seen_run_id",
            "last_run_id",
            "last_transition_at",
            "updated_by",
            "created_at",
            "updated_at",
        }
    ),
    "opportunity_lifecycle_history": frozenset(
        {
            "id",
            "org_id",
            "opportunity_identity",
            "revision",
            "from_state",
            "to_state",
            "actor",
            "actor_id",
            "action_date",
            "reason",
            "note",
            "run_id",
            "transitioned_at",
        }
    ),
    "opportunity_baselines": frozenset(
        {
            "org_id",
            "opportunity_identity",
            "run_id",
            "detector_id",
            "pack_id",
            "pack_version",
            "opportunity_ref",
            "window_days",
            "window_started_at",
            "window_ended_at",
            "window_derivation",
            "schema_version",
            "artifact",
            "captured_at",
        }
    ),
    "opportunity_movements": frozenset(
        {
            "org_id",
            "opportunity_identity",
            "current_run_id",
            "baseline_run_id",
            "detector_id",
            "action_date",
            "comparability_verdict",
            "baseline_pack_version",
            "current_pack_version",
            "primary_signal",
            "primary_baseline_value",
            "primary_current_value",
            "primary_delta",
            "primary_direction",
            "record",
            "measured_at",
            "created_at",
            "updated_at",
            "confounder_count",
            "confounder_material_count",
            "confounder_types",
            "projection_validation_verdict",
            "projection_pack_id",
            "projection_pack_version",
            "projection_confidence",
        }
    ),
    # A3 durable feedback, current bounded state, history, and audit.
    "opportunity_feedback": frozenset(
        {
            "feedback_id",
            "org_id",
            "opportunity_identity",
            "action",
            "reason_code",
            "reason_detail",
            "actor_id",
            "detector_id",
            "pack_id",
            "signal_concept",
            "run_id",
            "recorded_at",
            "record",
        }
    ),
    "ranking_adjustments": frozenset(
        {
            "org_id",
            "detector_id",
            "pack_id",
            "signal_concept",
            "net_weight",
            "outcome_weight",
            "decision_weight",
            "has_outcome_evidence",
            "signal_count",
            "learning_active",
            "contributing_refs",
            "config_version",
            "revision",
            "computed_at",
            "updated_at",
        }
    ),
    "ranking_adjustment_history": frozenset(
        {
            "history_id",
            "org_id",
            "detector_id",
            "pack_id",
            "change_kind",
            "previous_net_weight",
            "net_weight",
            "signal_count",
            "learning_active",
            "actor_id",
            "config_version",
            "revision",
            "reset_reason",
            "record",
            "recorded_at",
        }
    ),
    "audit_log": frozenset({"id", "org_id", "event_type", "payload", "timestamp"}),
}


REQUIRED_CHECK_CONSTRAINTS: Mapping[str, frozenset[str]] = {
    "opportunity_lifecycle": frozenset(
        {"ck_opp_lifecycle_measurable_action_date"}
    ),
    "ranking_adjustment_history": frozenset(
        {"ck_ranking_adjustment_reset_reason"}
    ),
}


REQUIRED_INDEXES = frozenset(
    {
        "idx_opp_instances_identity",
        "idx_opp_instances_org_run",
        "idx_ss_org_signal_time",
        "idx_ss_org_run",
        "idx_opp_lifecycle_org_state",
        "idx_opp_lifecycle_history_org_identity",
        "idx_opp_baselines_org_run",
        "idx_opp_baselines_org_detector",
        "idx_opp_movements_org_identity",
        "idx_opp_movements_org_run",
        "idx_opp_movements_org_verdict",
        "idx_opp_movements_org_confounders",
        "idx_opp_movements_org_projection_verdict",
        "idx_opp_movements_org_projection_pack",
        "idx_opp_movements_org_detector",
        "idx_opp_movements_org_projection_confidence",
        "idx_opportunity_feedback_identity",
        "idx_opportunity_feedback_similarity",
        "idx_opportunity_feedback_org_recorded",
        "idx_ranking_adjustments_org",
        "idx_ranking_adjustment_history_org",
        "idx_ranking_adjustment_history_group",
        "idx_audit_org_ts",
    }
)


# (constraint type, ordered columns).  The org-leading keys are the storage-level
# tenant boundary; the stable-identity keys are what let A2/A3 span runs.
REQUIRED_KEYS: Mapping[str, frozenset[tuple[str, tuple[str, ...]]]] = {
    "kv": frozenset({("PRIMARY KEY", ("key",))}),
    "opportunity_instances": frozenset(
        {("PRIMARY KEY", ("opportunity_identity", "run_id"))}
    ),
    "opportunity_lifecycle": frozenset(
        {("PRIMARY KEY", ("org_id", "opportunity_identity"))}
    ),
    "opportunity_lifecycle_history": frozenset(
        {
            ("PRIMARY KEY", ("id",)),
            ("UNIQUE", ("org_id", "opportunity_identity", "revision")),
        }
    ),
    "opportunity_baselines": frozenset(
        {("PRIMARY KEY", ("org_id", "opportunity_identity"))}
    ),
    "opportunity_movements": frozenset(
        {
            (
                "PRIMARY KEY",
                ("org_id", "opportunity_identity", "current_run_id"),
            )
        }
    ),
    "opportunity_feedback": frozenset({("PRIMARY KEY", ("feedback_id",))}),
    "ranking_adjustments": frozenset(
        {("PRIMARY KEY", ("org_id", "detector_id", "pack_id"))}
    ),
    "ranking_adjustment_history": frozenset(
        {("PRIMARY KEY", ("history_id",))}
    ),
    "audit_log": frozenset({("PRIMARY KEY", ("id",))}),
}


REQUIRED_PRIVILEGES: Mapping[str, frozenset[str]] = {
    "kv": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "runs": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "opportunity_instances": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "opportunity_lifecycle": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "opportunity_lifecycle_history": frozenset({"SELECT", "INSERT"}),
    "opportunity_baselines": frozenset({"SELECT", "INSERT"}),
    "opportunity_movements": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "opportunity_feedback": frozenset({"SELECT", "INSERT"}),
    "ranking_adjustments": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "ranking_adjustment_history": frozenset({"SELECT", "INSERT"}),
    "audit_log": frozenset({"SELECT", "INSERT"}),
}


FORBIDDEN_PRIVILEGES: Mapping[str, frozenset[str]] = {
    "kv": frozenset({"DELETE", "TRUNCATE"}),
    "runs": frozenset({"DELETE", "TRUNCATE"}),
    "opportunity_instances": frozenset({"DELETE", "TRUNCATE"}),
    "opportunity_lifecycle": frozenset({"DELETE", "TRUNCATE"}),
    "opportunity_lifecycle_history": frozenset({"UPDATE", "DELETE", "TRUNCATE"}),
    "opportunity_baselines": frozenset({"UPDATE", "DELETE", "TRUNCATE"}),
    "opportunity_movements": frozenset({"DELETE", "TRUNCATE"}),
    "opportunity_feedback": frozenset({"UPDATE", "DELETE", "TRUNCATE"}),
    "ranking_adjustments": frozenset({"DELETE", "TRUNCATE"}),
    "ranking_adjustment_history": frozenset({"UPDATE", "DELETE", "TRUNCATE"}),
    "audit_log": frozenset({"UPDATE", "DELETE", "TRUNCATE"}),
}


@dataclass(frozen=True)
class ReadinessReport:
    database: str
    role: str
    alembic_version: str | None
    expected_head: str
    issues: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.issues


def expected_alembic_head() -> str:
    versions = sorted(
        path.name.split("_", 1)[0]
        for path in (_BACKEND_DIR / "migrations" / "versions").glob("[0-9]*.py")
    )
    if not versions:
        raise RuntimeError("no Alembic revisions found")
    return versions[-1]


def inspect_connection(con: Any, *, check_privileges: bool = True) -> ReadinessReport:
    """Inspect one already-open psycopg2 connection using SELECTs only.

    ``check_privileges=False`` exists only for a schema migration test that runs
    as PostgreSQL's superuser.  A superuser bypasses table ACLs by definition, so
    ``has_table_privilege`` cannot prove the application-role policy there.  The
    CLI and provisioning runbook always keep the default ``True``.
    """

    issues: list[str] = []
    expected_head = expected_alembic_head()
    with con.cursor() as cur:
        cur.execute("SELECT current_database(), current_user")
        database, role = cur.fetchone()

        cur.execute("SELECT to_regclass('public.alembic_version')")
        if cur.fetchone()[0] is None:
            alembic_version = None
            issues.append("alembic_version table is missing")
        else:
            cur.execute("SELECT version_num FROM alembic_version")
            row = cur.fetchone()
            alembic_version = str(row[0]) if row else None
            if alembic_version != expected_head:
                issues.append(
                    f"Alembic is {alembic_version or 'unstamped'}; expected {expected_head}"
                )

        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
        actual_columns: dict[str, set[str]] = {}
        for table, column in cur.fetchall():
            actual_columns.setdefault(str(table), set()).add(str(column))

        for table, required in REQUIRED_COLUMNS.items():
            if table not in actual_columns:
                issues.append(f"missing table: {table}")
                continue
            missing = sorted(required - actual_columns[table])
            if missing:
                issues.append(f"{table} missing columns: {', '.join(missing)}")

        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        actual_indexes = {str(row[0]) for row in cur.fetchall()}
        missing_indexes = sorted(REQUIRED_INDEXES - actual_indexes)
        if missing_indexes:
            issues.append("missing indexes: " + ", ".join(missing_indexes))

        grouped: dict[tuple[str, str, str], list[str]] = {}
        # Include the physical constraint name in the grouping so two UNIQUE
        # constraints on one table are not accidentally merged together.
        cur.execute(
            "SELECT tc.table_name, tc.constraint_name, tc.constraint_type, "
            "       kcu.column_name, kcu.ordinal_position "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            " AND tc.constraint_schema = kcu.constraint_schema "
            "WHERE tc.table_schema = 'public' "
            "  AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE') "
            "ORDER BY tc.table_name, tc.constraint_name, kcu.ordinal_position"
        )
        for table, name, kind, column, _ordinal in cur.fetchall():
            grouped.setdefault((str(table), str(name), str(kind)), []).append(str(column))
        actual_keys: dict[str, set[tuple[str, tuple[str, ...]]]] = {}
        for (table, _name, kind), columns in grouped.items():
            actual_keys.setdefault(table, set()).add((kind, tuple(columns)))

        for table, required in REQUIRED_KEYS.items():
            if table not in actual_columns:
                continue
            missing = sorted(required - actual_keys.get(table, set()))
            for kind, columns in missing:
                issues.append(f"{table} missing {kind}: ({', '.join(columns)})")

        cur.execute(
            "SELECT relation.relname, constraint_row.conname, "
            "       constraint_row.convalidated "
            "FROM pg_constraint AS constraint_row "
            "JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = 'public' AND constraint_row.contype = 'c'"
        )
        actual_checks: dict[str, dict[str, bool]] = {}
        for table, name, validated in cur.fetchall():
            actual_checks.setdefault(str(table), {})[str(name)] = bool(validated)
        for table, required in REQUIRED_CHECK_CONSTRAINTS.items():
            for name in sorted(required):
                if name not in actual_checks.get(table, {}):
                    issues.append(f"{table} missing CHECK constraint: {name}")
                elif not actual_checks[table][name]:
                    issues.append(f"{table} CHECK constraint is not validated: {name}")

        # These data-level checks catch a write path that silently discarded a
        # UI field even though the destination column exists.
        if "opportunity_lifecycle" in actual_columns:
            cur.execute(
                "SELECT COUNT(*) FROM opportunity_lifecycle "
                "WHERE state IN ('actioned', 'monitoring', 'measured', 'stalled') "
                "AND action_date IS NULL"
            )
            if int(cur.fetchone()[0]):
                issues.append("measurable lifecycle rows exist without an action date")
            if "action_note" in actual_columns["opportunity_lifecycle"]:
                cur.execute(
                    "SELECT COUNT(*) FROM opportunity_lifecycle AS lifecycle "
                    "WHERE lifecycle.action_date IS NOT NULL "
                    "AND EXISTS ("
                    "  SELECT 1 FROM opportunity_lifecycle_history AS history "
                    "  WHERE history.org_id = lifecycle.org_id "
                    "    AND history.opportunity_identity = lifecycle.opportunity_identity "
                    "    AND history.to_state = 'actioned' "
                    "    AND NULLIF(BTRIM(history.note), '') IS NOT NULL"
                    ") AND lifecycle.action_note IS DISTINCT FROM ("
                    "  SELECT BTRIM(history.note) "
                    "  FROM opportunity_lifecycle_history AS history "
                    "  WHERE history.org_id = lifecycle.org_id "
                    "    AND history.opportunity_identity = lifecycle.opportunity_identity "
                    "    AND history.to_state = 'actioned' "
                    "    AND NULLIF(BTRIM(history.note), '') IS NOT NULL "
                    "  ORDER BY history.revision DESC LIMIT 1"
                    ")"
                )
                if int(cur.fetchone()[0]):
                    issues.append(
                        "current lifecycle action descriptions disagree with history"
                    )

        if "opportunity_feedback" in actual_columns:
            cur.execute(
                "SELECT COUNT(*) FROM opportunity_feedback WHERE "
                "COALESCE(record ->> 'opportunityIdentity', '') <> opportunity_identity "
                "OR COALESCE(record ->> 'action', '') <> action "
                "OR COALESCE(record ->> 'reasonCode', '') <> COALESCE(reason_code, '') "
                "OR COALESCE(record ->> 'reasonDetail', '') <> COALESCE(reason_detail, '') "
                "OR COALESCE(record ->> 'actorId', '') <> actor_id"
            )
            if int(cur.fetchone()[0]):
                issues.append("feedback UI fields disagree with their stored record")

        if "reset_reason" in actual_columns.get("ranking_adjustment_history", set()):
            cur.execute(
                "SELECT COUNT(*) FROM ranking_adjustment_history "
                "WHERE change_kind = 'reset' "
                "AND NULLIF(BTRIM(reset_reason), '') IS NULL"
            )
            if int(cur.fetchone()[0]):
                issues.append("ranking reset history contains a reset without a reason")

        if check_privileges:
            all_privilege_tables = set(REQUIRED_PRIVILEGES) | set(FORBIDDEN_PRIVILEGES)
            for table in sorted(all_privilege_tables):
                if table not in actual_columns:
                    continue
                for privilege in sorted(REQUIRED_PRIVILEGES.get(table, ())):
                    cur.execute(
                        "SELECT has_table_privilege(current_user, %s, %s)",
                        (f"public.{table}", privilege),
                    )
                    if not bool(cur.fetchone()[0]):
                        issues.append(f"{table} does not grant required {privilege}")
                for privilege in sorted(FORBIDDEN_PRIVILEGES.get(table, ())):
                    cur.execute(
                        "SELECT has_table_privilege(current_user, %s, %s)",
                        (f"public.{table}", privilege),
                    )
                    if bool(cur.fetchone()[0]):
                        issues.append(f"{table} still grants forbidden {privilege}")

    return ReadinessReport(
        database=str(database),
        role=str(role),
        alembic_version=alembic_version,
        expected_head=expected_head,
        issues=tuple(issues),
    )


def _targets(name: str, values: Mapping[str, str | None]) -> Sequence[tuple[str, str]]:
    keys = {
        "dev": "DEV_DATABASE_URL",
        "prod": "PROD_DATABASE_URL",
        "database": "DATABASE_URL",
    }
    selected = ("dev", "prod") if name == "all" else (name,)
    out = []
    for label in selected:
        key = keys[label]
        url = os.getenv(key) or values.get(key)
        if not url:
            raise RuntimeError(f"{key} is not configured")
        out.append((label, str(url)))
    return tuple(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=("dev", "prod", "database", "all"),
        default="all",
        help="database URL selector from backend/.env (default: all)",
    )
    args = parser.parse_args(argv)

    from dotenv import dotenv_values
    import psycopg2

    values = dotenv_values(_BACKEND_DIR / ".env")
    failed = False
    for label, url in _targets(args.target, values):
        try:
            con = psycopg2.connect(url, connect_timeout=15)
            con.set_session(readonly=True)
            try:
                report = inspect_connection(con)
            finally:
                con.rollback()
                con.close()
        except Exception as exc:  # connection details are intentionally not printed
            failed = True
            print(f"{label}: NOT READY (connection/check failed: {type(exc).__name__})")
            continue

        if report.ready:
            print(
                f"{label}: READY (database={report.database}, role={report.role}, "
                f"alembic={report.alembic_version})"
            )
            continue
        failed = True
        print(
            f"{label}: NOT READY (database={report.database}, role={report.role}, "
            f"alembic={report.alembic_version or 'missing'})"
        )
        for issue in report.issues:
            print(f"  - {issue}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
