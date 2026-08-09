"""Repair and enforce the A1/A2/A3 database contract.

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-09

This is deliberately idempotent.  It covers both supported deployment shapes:

* a normal database upgrading through 0031..0037 for the first time; and
* an older dev/provisioned database whose Alembic stamp advanced while one or
  more A2 indexes were absent.

No row is deleted or rewritten.  Missing tables/indexes are created, the known
0034/0035 movement columns are repaired with ``ADD COLUMN IF NOT EXISTS``, and
the application role loses destructive privileges on the closed-loop history.
"""

from __future__ import annotations

import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backend_on_path() -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)


def _schema_repair_ddl() -> "tuple[str, ...]":
    """Every A1/A2/A3 table/index in dependency-safe order."""

    _backend_on_path()
    from database.models.opportunity_baselines import ALL_OPPORTUNITY_BASELINES_DDL
    from database.models.opportunity_feedback import ALL_OPPORTUNITY_FEEDBACK_DDL
    from database.models.opportunity_instances import ALL_OPPORTUNITY_INSTANCES_DDL
    from database.models.opportunity_lifecycle import ALL_OPPORTUNITY_LIFECYCLE_DDL
    from database.models.opportunity_movements import (
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_CONFOUNDERS,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_DETECTOR,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_IDENTITY,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_PROJECTION_CONFIDENCE,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_PROJECTION_PACK,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_PROJECTION_VERDICT,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_RUN,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_VERDICT,
        CREATE_OPPORTUNITY_MOVEMENTS_TABLE,
    )
    from database.models.ranking_adjustments import ALL_RANKING_ADJUSTMENT_DDL

    # The ALTERs run before movement indexes: on a drifted database the table can
    # exist while one of the 0034/0035 columns does not, and an index that names a
    # missing column would abort the whole transaction.
    movement_repairs = (
        "ALTER TABLE opportunity_movements ADD COLUMN IF NOT EXISTS "
        "confounder_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE opportunity_movements ADD COLUMN IF NOT EXISTS "
        "confounder_material_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE opportunity_movements ADD COLUMN IF NOT EXISTS "
        "confounder_types TEXT",
        "ALTER TABLE opportunity_movements ADD COLUMN IF NOT EXISTS "
        "projection_validation_verdict VARCHAR(24) NOT NULL DEFAULT 'not_projected'",
        "ALTER TABLE opportunity_movements ADD COLUMN IF NOT EXISTS "
        "projection_pack_id VARCHAR(64)",
        "ALTER TABLE opportunity_movements ADD COLUMN IF NOT EXISTS "
        "projection_pack_version VARCHAR(32)",
        "ALTER TABLE opportunity_movements ADD COLUMN IF NOT EXISTS "
        "projection_confidence VARCHAR(16)",
    )
    movement_indexes = (
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_IDENTITY,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_RUN,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_VERDICT,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_CONFOUNDERS,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_PROJECTION_VERDICT,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_PROJECTION_PACK,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_DETECTOR,
        CREATE_OPPORTUNITY_MOVEMENTS_IDX_PROJECTION_CONFIDENCE,
    )

    return (
        *ALL_OPPORTUNITY_INSTANCES_DDL,
        *ALL_OPPORTUNITY_LIFECYCLE_DDL,
        *ALL_OPPORTUNITY_BASELINES_DDL,
        CREATE_OPPORTUNITY_MOVEMENTS_TABLE,
        *movement_repairs,
        *movement_indexes,
        *ALL_OPPORTUNITY_FEEDBACK_DDL,
        *ALL_RANKING_ADJUSTMENT_DDL,
    )


def _privilege_ddl(name: str) -> "tuple[str, ...]":
    _backend_on_path()
    from database.models import closed_loop_immutability, history_retention

    if hasattr(closed_loop_immutability, name):
        return getattr(closed_loop_immutability, name)
    return getattr(history_retention, name)


def upgrade() -> None:
    for statement in _schema_repair_ddl():
        op.execute(statement)

    # 0044 may already be stamped on an older database, so re-apply the expanded
    # protected set here.  This is a REVOKE-only, idempotent operation.
    for statement in _privilege_ddl("ALL_HISTORY_RETENTION_DDL"):
        op.execute(statement)
    for statement in _privilege_ddl("ALL_CLOSED_LOOP_IMMUTABILITY_DDL"):
        op.execute(statement)


def downgrade() -> None:
    # 0049 owns only the new privilege restrictions.  It repairs objects owned by
    # earlier migrations, so dropping those objects here would destroy valid 0048
    # schema/data.  audit_log stays immutable because migration 0038 still applies.
    for statement in _privilege_ddl("DROP_CLOSED_LOOP_IMMUTABILITY_DDL"):
        op.execute(statement)
