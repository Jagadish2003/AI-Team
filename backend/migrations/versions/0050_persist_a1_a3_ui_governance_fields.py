"""Persist and enforce A2/A3 customer-entered governance fields.

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-09

The A2 action description was already preserved in append-only transition
history, but it was not carried on the current lifecycle row returned after a
page refresh. A3's Owner UI required a reset reason while the API and database
still allowed a reasonless reset. This migration closes both gaps without
discarding any existing history.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.opportunity_lifecycle "
        "ADD COLUMN IF NOT EXISTS action_note TEXT"
    )
    # Restore the current description from the latest action transition where
    # an older deployment already captured it in append-only history.
    op.execute(
        """
        UPDATE public.opportunity_lifecycle AS lifecycle
           SET action_note = (
               SELECT BTRIM(history.note)
                 FROM public.opportunity_lifecycle_history AS history
                WHERE history.org_id = lifecycle.org_id
                  AND history.opportunity_identity = lifecycle.opportunity_identity
                  AND history.to_state = 'actioned'
                  AND NULLIF(BTRIM(history.note), '') IS NOT NULL
                ORDER BY history.revision DESC
                LIMIT 1
           )
         WHERE lifecycle.action_note IS NULL
           AND lifecycle.action_date IS NOT NULL
           AND EXISTS (
               SELECT 1
                 FROM public.opportunity_lifecycle_history AS history
                WHERE history.org_id = lifecycle.org_id
                  AND history.opportunity_identity = lifecycle.opportunity_identity
                  AND history.to_state = 'actioned'
                  AND NULLIF(BTRIM(history.note), '') IS NOT NULL
           )
        """
    )
    op.execute(
        """
        DO
        $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'ck_opp_lifecycle_measurable_action_date'
                   AND conrelid = 'public.opportunity_lifecycle'::regclass
            ) THEN
                ALTER TABLE public.opportunity_lifecycle
                    ADD CONSTRAINT ck_opp_lifecycle_measurable_action_date
                    CHECK (
                        state NOT IN ('actioned', 'monitoring', 'measured', 'stalled')
                        OR action_date IS NOT NULL
                    ) NOT VALID;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM public.opportunity_lifecycle
                 WHERE state IN ('actioned', 'monitoring', 'measured', 'stalled')
                   AND action_date IS NULL
            ) THEN
                ALTER TABLE public.opportunity_lifecycle
                    VALIDATE CONSTRAINT ck_opp_lifecycle_measurable_action_date;
            END IF;
        END
        $$
        """
    )

    op.execute(
        "ALTER TABLE public.ranking_adjustment_history "
        "ADD COLUMN IF NOT EXISTS reset_reason TEXT"
    )
    op.execute(
        """
        UPDATE public.ranking_adjustment_history
           SET reset_reason = NULLIF(BTRIM(record ->> 'resetReason'), '')
         WHERE change_kind = 'reset'
           AND reset_reason IS NULL
           AND NULLIF(BTRIM(record ->> 'resetReason'), '') IS NOT NULL
        """
    )
    op.execute(
        """
        DO
        $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'ck_ranking_adjustment_reset_reason'
                   AND conrelid = 'public.ranking_adjustment_history'::regclass
            ) THEN
                ALTER TABLE public.ranking_adjustment_history
                    ADD CONSTRAINT ck_ranking_adjustment_reset_reason
                    CHECK (
                        change_kind <> 'reset'
                        OR (reset_reason IS NOT NULL AND BTRIM(reset_reason) <> '')
                    ) NOT VALID;
            END IF;

            -- NOT VALID still protects every new row. Validate it fully when
            -- this database has no legacy reasonless reset records.
            IF NOT EXISTS (
                SELECT 1 FROM public.ranking_adjustment_history
                 WHERE change_kind = 'reset'
                   AND NULLIF(BTRIM(reset_reason), '') IS NULL
            ) THEN
                ALTER TABLE public.ranking_adjustment_history
                    VALIDATE CONSTRAINT ck_ranking_adjustment_reset_reason;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.ranking_adjustment_history "
        "DROP CONSTRAINT IF EXISTS ck_ranking_adjustment_reset_reason"
    )
    op.execute(
        "ALTER TABLE public.ranking_adjustment_history "
        "DROP COLUMN IF EXISTS reset_reason"
    )
    op.execute(
        "ALTER TABLE public.opportunity_lifecycle "
        "DROP CONSTRAINT IF EXISTS ck_opp_lifecycle_measurable_action_date"
    )
    op.execute(
        "ALTER TABLE public.opportunity_lifecycle "
        "DROP COLUMN IF EXISTS action_note"
    )
