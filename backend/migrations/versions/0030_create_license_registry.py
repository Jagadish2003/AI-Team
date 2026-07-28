"""Create the vendor-side license registry — R-1.9.1-L3 (AT-715 / T1, AT-717 / T3).

Adds the CloudFulcrum-internal license issuance tables:

* ``license_registry`` — the authoritative record of every minted license.
* ``issuance_audit`` — the append-only issuance ledger, its append-only property
  enforced by two Postgres rewrite rules (ON UPDATE/DELETE DO INSTEAD NOTHING),
  mirroring the ``telemetry_events`` precedent.

The DDL is imported from database/models/license_registry.py (the single source
of truth shared with the runtime provisioner), never hardcoded here — same
pattern as 0003_create_entities.py importing ALL_ENTITIES_DDL, and
0015_create_org_licenses.py.

Idempotent: guarded by inspector.has_table, so re-running (or running against a
provision.sql-provisioned DB whose tables already exist) is a no-op. Rollback
drops both tables (their indexes and rules drop with them).

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-23

Merge note (R191-L3 ← dev): re-parented from 0025 to 0029 to linearise the
migration chain. The dev branch introduced its own 0026 (extend_cmdb_relationship_types)
→ 0027 → 0028 → 0029; this license-registry migration creates an independent table,
so running it after that chain is order-safe.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from database.models.license_registry import ALL_LICENSE_REGISTRY_DDL

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("license_registry") and inspector.has_table("issuance_audit"):
        return
    for ddl in ALL_LICENSE_REGISTRY_DDL:
        op.execute(ddl)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("issuance_audit"):
        op.drop_table("issuance_audit")
    if inspector.has_table("license_registry"):
        op.drop_table("license_registry")
