"""Create the 2.0-C2 per-org pack certification activation policy.

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-31

2.0-C2 T4 (AT-834 / AC3): an org can restrict which certification levels may be
activated (e.g. federal deployments: Certified only), enforced at activation.

Additive and behaviour-neutral on its own: the absence of a row means "no
restriction", so no deployment changes until an owner sets a floor. Unlike 0034 this
table is NOT in the protected-history set — a policy is current configuration, not a
record of what the platform found, and lifting a restriction is a WRITE (of
``community``) rather than a delete, so nothing is lost either way.
"""
import os
import sys
from typing import Sequence, Union

from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ddl(name: str) -> "tuple[str, ...]":
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from database.models import pack_certification_policies

    return getattr(pack_certification_policies, name)


def upgrade() -> None:
    for statement in _ddl("ALL_PACK_CERTIFICATION_POLICY_DDL"):
        op.execute(statement)


def downgrade() -> None:
    for statement in _ddl("DROP_PACK_CERTIFICATION_POLICY_DDL"):
        op.execute(statement)
