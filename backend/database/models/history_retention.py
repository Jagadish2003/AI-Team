"""Privilege-level enforcement that run history is never deleted.

2.0-C1 T4 (AT-829) — the DDL half of AC4's "enforced at the data layer".

``provision.sql`` grants each application login role ``ALL PRIVILEGES ON ALL
TABLES``, which includes DELETE and TRUNCATE. This module generates the REVOKEs
that claw those two back on the tables holding findings, evidence, run records, and
the pack lifecycle audit trail — so no application code path, intended or buggy, can
remove run history. The database refuses it.

The protected-table set is NOT duplicated here: it is imported from
``app.history_retention``, which is the single source of truth shared with the
runtime guard and the CI sweep.

Idempotent and safe to re-run: REVOKE on an already-revoked privilege is a no-op, and
each role is guarded by an existence check (the same pattern as the GRANT block in
``provision.sql``), so a deployment whose app role list differs is unaffected.

Ordering matters: these REVOKEs must run AFTER the ``GRANT ALL PRIVILEGES`` block,
or the grant would hand DELETE straight back.
"""

from __future__ import annotations

import os
import sys

# Import the protected set from the app layer rather than restating it. The path
# nudge mirrors the migration modules, which run outside the app's import context.
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.history_retention import PROTECTED_TABLES  # noqa: E402

#: App login roles this deployment grants. Kept in step with ``provision.sql``'s
#: ``app_roles`` array — add a production role in BOTH places.
APP_ROLES = ("agentiq", "aiqdevusr")


def _revoke_block(roles: "tuple[str, ...]" = APP_ROLES) -> str:
    """A single idempotent DO block revoking DELETE/TRUNCATE for every role.

    Emitted as one statement so it can be executed by both alembic and the plain-SQL
    provisioning path. Table and role names come from module constants, never user
    input.
    """
    role_array = ", ".join(f"'{role}'" for role in roles)
    table_array = ", ".join(f"'{table}'" for table in sorted(PROTECTED_TABLES))
    return f"""
DO
$$
DECLARE
    r text;
    t text;
    app_roles text[] := ARRAY[{role_array}];
    protected_tables text[] := ARRAY[{table_array}];
BEGIN
    FOREACH r IN ARRAY app_roles LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            FOREACH t IN ARRAY protected_tables LOOP
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = t
                ) THEN
                    EXECUTE format(
                        'REVOKE DELETE, TRUNCATE ON TABLE public.%I FROM %I', t, r
                    );
                END IF;
            END LOOP;
        END IF;
    END LOOP;
END
$$
"""


def _grant_block(roles: "tuple[str, ...]" = APP_ROLES) -> str:
    """The inverse, for ``downgrade()`` — restores DELETE/TRUNCATE.

    Present only so the migration is reversible. Running it re-opens the ability to
    delete run history, which is why nothing calls it outside ``downgrade()``.
    """
    role_array = ", ".join(f"'{role}'" for role in roles)
    table_array = ", ".join(f"'{table}'" for table in sorted(PROTECTED_TABLES))
    return f"""
DO
$$
DECLARE
    r text;
    t text;
    app_roles text[] := ARRAY[{role_array}];
    protected_tables text[] := ARRAY[{table_array}];
BEGIN
    FOREACH r IN ARRAY app_roles LOOP
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
        END IF;
    END LOOP;
END
$$
"""


ALL_HISTORY_RETENTION_DDL = (_revoke_block(),)
DROP_HISTORY_RETENTION_DDL = (_grant_block(),)
