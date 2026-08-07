"""Create the 2.0-C3 installed authored-pack registry.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-05

2.0-C3 T4 (AT-839): a signed pack bundle installs into an org, gated by C1
compatibility and C2 certification policy. This table records what was installed,
which bundle bytes it came from, and which publisher key vouched for them.

Additive and behaviour-neutral on its own: with no rows, nothing changes for any
deployment. Like ``pack_certification_policies`` (0035) and unlike the 0033/0034
protected set, this table holds current CONFIGURATION rather than run history, so
it is not privilege-protected against deletion — withdrawal is a status write, and
nothing here records what the platform found.
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ddl(name: str) -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models import installed_packs

    return getattr(installed_packs, name)


def upgrade() -> None:
    for statement in _ddl("ALL_INSTALLED_PACK_DDL"):
        op.execute(statement)


def downgrade() -> None:
    for statement in _ddl("DROP_INSTALLED_PACK_DDL"):
        op.execute(statement)
