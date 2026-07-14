"""Extend graph relationship vocabulary for observed ServiceNow CMDB edges.

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-14
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ALL_TYPES = (
    "connects_to",
    "depends_on",
    "escalates_to",
    "member_of",
    "owns",
    "routes_to",
    "runs_on",
    "used_by",
)
_PREVIOUS_TYPES = (
    "depends_on",
    "escalates_to",
    "member_of",
    "owns",
    "routes_to",
)


def _replace_constraint(types: tuple[str, ...]) -> None:
    values = ", ".join(f"'{value}'" for value in types)
    op.execute(
        "ALTER TABLE entity_relationships "
        "DROP CONSTRAINT IF EXISTS entity_relationships_relationship_type_check"
    )
    op.execute(
        "ALTER TABLE entity_relationships "
        "ADD CONSTRAINT entity_relationships_relationship_type_check "
        f"CHECK (relationship_type IN ({values}))"
    )


def upgrade() -> None:
    _replace_constraint(_ALL_TYPES)


def downgrade() -> None:
    op.execute(
        "DELETE FROM entity_relationships "
        "WHERE relationship_type IN ('connects_to', 'runs_on', 'used_by')"
    )
    _replace_constraint(_PREVIOUS_TYPES)
