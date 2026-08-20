"""Store an installed pack's fixtures and its sandbox verdict.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-05

2.0-C3 T6 (AT-841): installing a pack runs its manifest through validation and
its fixtures through the harness *before activation*. Activation can be far later
than installation, and the platform moves in between, so activation re-runs the
whole check rather than trusting the install-time verdict — which means the
fixtures have to still be here. ``validation`` keeps the most recent verdict so
"why will this pack not activate" is answerable without re-uploading the bundle.

Additive and idempotent (``ADD COLUMN IF NOT EXISTS`` with defaults), and
behaviour-neutral for existing rows: a pack installed before this migration reads
as "no fixtures stored", and its re-validation says so explicitly rather than
reporting a full pass it did not perform.
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ddl(name: str) -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models import installed_packs

    return getattr(installed_packs, name)


def upgrade() -> None:
    for statement in _ddl("ADD_INSTALLED_PACKS_SANDBOX_COLUMNS"):
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE installed_packs DROP COLUMN IF EXISTS validation")
    op.execute("ALTER TABLE installed_packs DROP COLUMN IF EXISTS fixtures")
