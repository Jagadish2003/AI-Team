"""2.0-A2 T3 — stored movement records (post-action measurement).

Keyed on ``(org_id, opportunity_identity, current_run_id)``: one record per
comparison, so re-running a measurement for the same comparison run is idempotent
rather than accumulating duplicates.

**A stored artifact, not a computed-at-read-time view.** This is the point of the
table. If movement were recomputed on read, a later pack change — or a later
recalculation of the rolling temporal baseline — could retroactively alter a
measurement that had already been reported to a customer. A number that quietly
changes after it was quoted is worse than no number.

Both run ids are real columns rather than JSON fields, because AC7 requires every
outcome number to resolve back to the runs that produced both sides of the
comparison, and 2.0-B1's evidence trace joins on them.

``comparability_verdict`` is ``NOT NULL``: the definition of done requires
comparability always be populated, and a null would be read as "fine" by anything
rendering it. The full assessment (reasons, window lengths, elapsed time, cadence
gap, seasonal overlap, boundary handling) lives in the ``record`` JSON alongside
the per-signal movements.

Unlike the T2 baseline this record is REPLACEABLE for a given comparison run — a
re-measurement of the same run should correct itself rather than duplicate — so
the conflict clause is DO UPDATE. That is deliberate and is the one difference
from the baseline's write-once posture: the baseline is what we were judged
against and must never move; a movement record is a derived measurement of a
specific run pair and re-deriving it for that same pair is idempotent, not
revisionist.
"""

CREATE_OPPORTUNITY_MOVEMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS opportunity_movements (
    org_id                 VARCHAR(64)  NOT NULL,
    opportunity_identity   VARCHAR(64)  NOT NULL,
    -- The run that produced the CURRENT side of the comparison. Part of the key
    -- so one comparison run yields exactly one record per opportunity.
    current_run_id         VARCHAR(64)  NOT NULL,
    -- The run that produced the BASELINE side. A real column, not a JSON field:
    -- AC7's "resolves to the runs that produced both measurements".
    baseline_run_id        VARCHAR(64)  NOT NULL,
    detector_id            VARCHAR(128) NOT NULL,
    -- The pivot every post-action observation is measured against.
    action_date            DATE         NOT NULL,
    -- Never null: comparability must always be populated.
    comparability_verdict  VARCHAR(24)  NOT NULL,
    baseline_pack_version  VARCHAR(32),
    current_pack_version   VARCHAR(32),
    -- The primary signal's movement, promoted for portfolio aggregation (T6)
    -- without parsing JSON. The full per-signal set lives in `record`.
    primary_signal         VARCHAR(128),
    primary_baseline_value DOUBLE PRECISION,
    primary_current_value  DOUBLE PRECISION,
    primary_delta          DOUBLE PRECISION,
    primary_direction      VARCHAR(16),
    -- The whole trace-friendly record: per-signal movements, both windows, the
    -- full comparability assessment, post-action run ids.
    record                 TEXT         NOT NULL,
    measured_at            TIMESTAMPTZ  NOT NULL,
    created_at             TIMESTAMPTZ  NOT NULL,
    updated_at             TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (org_id, opportunity_identity, current_run_id)
)
"""

# The per-opportunity movement series, newest first — the outcome view (T6).
CREATE_OPPORTUNITY_MOVEMENTS_IDX_IDENTITY = """
CREATE INDEX IF NOT EXISTS idx_opp_movements_org_identity
    ON opportunity_movements (org_id, opportunity_identity, measured_at DESC)
"""

# Every movement a given run produced — the post-run reporting read.
CREATE_OPPORTUNITY_MOVEMENTS_IDX_RUN = """
CREATE INDEX IF NOT EXISTS idx_opp_movements_org_run
    ON opportunity_movements (org_id, current_run_id)
"""

# T6's portfolio aggregate counts caveated measurements, so it filters on verdict.
CREATE_OPPORTUNITY_MOVEMENTS_IDX_VERDICT = """
CREATE INDEX IF NOT EXISTS idx_opp_movements_org_verdict
    ON opportunity_movements (org_id, comparability_verdict)
"""

ALL_OPPORTUNITY_MOVEMENTS_DDL: tuple[str, ...] = (
    CREATE_OPPORTUNITY_MOVEMENTS_TABLE,
    CREATE_OPPORTUNITY_MOVEMENTS_IDX_IDENTITY,
    CREATE_OPPORTUNITY_MOVEMENTS_IDX_RUN,
    CREATE_OPPORTUNITY_MOVEMENTS_IDX_VERDICT,
)

DROP_OPPORTUNITY_MOVEMENTS_DDL: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_opp_movements_org_verdict",
    "DROP INDEX IF EXISTS idx_opp_movements_org_run",
    "DROP INDEX IF EXISTS idx_opp_movements_org_identity",
    "DROP TABLE IF EXISTS opportunity_movements",
)

OPPORTUNITY_MOVEMENT_COLUMNS: tuple[str, ...] = (
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
)

__all__ = [
    "ALL_OPPORTUNITY_MOVEMENTS_DDL",
    "DROP_OPPORTUNITY_MOVEMENTS_DDL",
    "OPPORTUNITY_MOVEMENT_COLUMNS",
    "CREATE_OPPORTUNITY_MOVEMENTS_TABLE",
]
