"""Database privilege guards for the A1 -> A2 -> A3 closed loop.

The application deliberately updates the *current* lifecycle and ranking rows,
and it may correct a movement record for the same run pair.  It must never erase
those records, however, and four stores are stricter still: they are append-only
or write-once and must not be updated either.

This module is the applied counterpart to the promises documented by the A2/A3
models.  It is shared by migration 0049 and the pure-SQL provisioning bundle so
an upgraded database and a clean production install receive the same policy.

``audit_log`` is included in the UPDATE revoke because A3 reset/recompute events
are part of the learning history.  Its DELETE/TRUNCATE protection remains owned
by migration 0038; 0049 simply re-asserts the full policy after any broad grants.
"""

from __future__ import annotations

from database.models.history_retention import APP_ROLES


# A2/A3 tables that an application can read/append/update as appropriate, but
# can never DELETE or TRUNCATE.  A1's ``kv`` and ``opportunity_instances`` are
# already covered by the shared history-retention set.
CLOSED_LOOP_PROTECTED_TABLES: tuple[str, ...] = (
    "opportunity_baselines",
    "opportunity_feedback",
    "opportunity_lifecycle",
    "opportunity_lifecycle_history",
    "opportunity_movements",
    "ranking_adjustment_history",
    "ranking_adjustments",
)

# These are stricter than the current-state/derived tables above.  They are
# append-only (or write-once for the baseline), so UPDATE is forbidden too.
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "opportunity_baselines",
    "opportunity_feedback",
    "opportunity_lifecycle_history",
    "ranking_adjustment_history",
)

# Current/derived rows the application legitimately updates in place.  Every
# other closed-loop table is append-only or write-once.
MUTABLE_CLOSED_LOOP_TABLES: tuple[str, ...] = (
    "opportunity_lifecycle",
    "opportunity_movements",
    "ranking_adjustments",
)


def _array(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _revoke_block() -> str:
    """Revoke destructive A2/A3 access from known roles and the migration role.

    Including ``current_user`` makes the migration work when a deployment uses a
    role name other than the two names shipped in the provisioning runbook.  The
    role array is de-duplicated in SQL before use.
    """

    roles = _array(APP_ROLES)
    protected = _array(CLOSED_LOOP_PROTECTED_TABLES)
    append_only = _array(APPEND_ONLY_TABLES)
    mutable = _array(MUTABLE_CLOSED_LOOP_TABLES)
    return f"""
DO
$$
DECLARE
    r text;
    t text;
    app_roles text[] := ARRAY[{roles}, current_user];
    protected_tables text[] := ARRAY[{protected}];
    append_only_tables text[] := ARRAY[{append_only}];
    mutable_tables text[] := ARRAY[{mutable}];
BEGIN
    FOREACH t IN ARRAY protected_tables LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = t
        ) THEN
            EXECUTE format(
                'REVOKE DELETE, TRUNCATE ON TABLE public.%I FROM PUBLIC', t
            );
        END IF;
    END LOOP;

    FOREACH t IN ARRAY append_only_tables LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = t
        ) THEN
            EXECUTE format('REVOKE UPDATE ON TABLE public.%I FROM PUBLIC', t);
        END IF;
    END LOOP;

    FOR r IN SELECT DISTINCT unnest(app_roles) LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            FOREACH t IN ARRAY protected_tables LOOP
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = t
                ) THEN
                    EXECUTE format(
                        'REVOKE DELETE, TRUNCATE ON TABLE public.%I FROM %I', t, r
                    );
                    -- A migration/DBA role can own the tables while the backend
                    -- connects as a separate app role. Re-assert the legitimate
                    -- DML as well as the forbidden DML so both deployment shapes
                    -- converge on the same usable policy.
                    EXECUTE format(
                        'GRANT SELECT, INSERT ON TABLE public.%I TO %I', t, r
                    );
                END IF;
            END LOOP;

            FOREACH t IN ARRAY mutable_tables LOOP
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = t
                ) THEN
                    EXECUTE format('GRANT UPDATE ON TABLE public.%I TO %I', t, r);
                END IF;
            END LOOP;

            FOREACH t IN ARRAY append_only_tables LOOP
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = t
                ) THEN
                    EXECUTE format('REVOKE UPDATE ON TABLE public.%I FROM %I', t, r);
                    EXECUTE format(
                        'GRANT SELECT, INSERT ON TABLE public.%I TO %I', t, r
                    );
                END IF;
            END LOOP;
        END IF;
    END LOOP;
END
$$
"""


def _audit_revoke_block() -> str:
    """Re-assert audit immutability after broad provisioning grants.

    Migration 0038 established this policy.  Repeating it here is idempotent and
    fixes the pure-SQL path, whose ``GRANT ALL`` necessarily runs after the table
    is created.
    """

    roles = _array(APP_ROLES)
    return f"""
DO
$$
DECLARE
    r text;
    app_roles text[] := ARRAY[{roles}, current_user];
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'audit_log'
    ) THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.audit_log FROM PUBLIC;
        FOR r IN SELECT DISTINCT unnest(app_roles) LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                EXECUTE format(
                    'REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.audit_log FROM %I',
                    r
                );
                EXECUTE format(
                    'GRANT SELECT, INSERT ON TABLE public.audit_log TO %I', r
                );
            END IF;
        END LOOP;
    END IF;
END
$$
"""


def _restore_block() -> str:
    """Restore the privileges 0049 newly removed when downgrading to 0048.

    ``audit_log`` is intentionally absent: migration 0038 still owns its policy
    at revision 0048, so a 0049 downgrade must not undo it.
    """

    roles = _array(APP_ROLES)
    protected = _array(CLOSED_LOOP_PROTECTED_TABLES)
    append_only = _array(APPEND_ONLY_TABLES)
    return f"""
DO
$$
DECLARE
    r text;
    t text;
    app_roles text[] := ARRAY[{roles}, current_user];
    protected_tables text[] := ARRAY[{protected}];
    append_only_tables text[] := ARRAY[{append_only}];
BEGIN
    FOR r IN SELECT DISTINCT unnest(app_roles) LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            FOREACH t IN ARRAY protected_tables LOOP
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = t
                ) THEN
                    EXECUTE format(
                        'GRANT DELETE, TRUNCATE ON TABLE public.%I TO %I', t, r
                    );
                END IF;
            END LOOP;
            FOREACH t IN ARRAY append_only_tables LOOP
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = t
                ) THEN
                    EXECUTE format('GRANT UPDATE ON TABLE public.%I TO %I', t, r);
                END IF;
            END LOOP;
        END IF;
    END LOOP;
END
$$
"""


ALL_CLOSED_LOOP_IMMUTABILITY_DDL: tuple[str, ...] = (
    _revoke_block(),
    _audit_revoke_block(),
)

DROP_CLOSED_LOOP_IMMUTABILITY_DDL: tuple[str, ...] = (_restore_block(),)


__all__ = [
    "ALL_CLOSED_LOOP_IMMUTABILITY_DDL",
    "APPEND_ONLY_TABLES",
    "CLOSED_LOOP_PROTECTED_TABLES",
    "DROP_CLOSED_LOOP_IMMUTABILITY_DDL",
    "MUTABLE_CLOSED_LOOP_TABLES",
]
