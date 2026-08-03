"""2.0-A3 T2 — the persisted per-org ranking adjustment state.

**Why this is stored rather than derived at read time.**

Computing the adjustment on the fly from decision history whenever a list is
served would be cheaper to write and would need no table at all. It would also
mean a customer's ranking changed silently as history accrued, with no record of
what was applied when — so "why did this move last Tuesday?" would have no
answer, and T4's audit and reset would be impossible to satisfy honestly. A
reset in particular has nothing to reset if the state is an expression rather
than a value.

So the state is a VALUE: computed deliberately from T1's signal set, written
here with a revision, and read unchanged until the next recomputation.

**Two tables, for two jobs** — the same split ``opportunity_lifecycle`` uses:

* ``ranking_adjustments`` — the CURRENT adjustment per similarity group. The row
  the serving path reads and a recomputation updates.
* ``ranking_adjustment_history`` — APPEND-ONLY. Every value this group's
  adjustment has ever held, including the ones a reset replaced. T4 renders this;
  it exists now because history cannot be reconstructed retroactively, and a
  table added later would start with a hole exactly where the first questions
  will be asked.

**Keyed on the similarity group, not on a finding.** The learned adjustment is a
statement about a finding TYPE — "your team accepted four of these" — so the key
is ``(org_id, detector_id, pack_id)``, matching T1's ``SimilarityGroup``. Keying
per opportunity would make the state grow without bound and would relearn
nothing when the same problem re-surfaced on the next run.

``org_id`` leads every key and every index, mirroring ``opportunity_lifecycle``,
``opportunity_baselines`` and ``opportunity_movements``, so per-org scoping is a
property of the storage shape rather than of the queries written against it.
"""

CREATE_RANKING_ADJUSTMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS ranking_adjustments (
    org_id                VARCHAR(64)  NOT NULL,
    -- The T1 similarity group. A NULL pack is meaningful (a finding whose pack
    -- is unknown), so both columns are coalesced to '' in the key below rather
    -- than left nullable — a NULL in a primary key would let duplicate rows
    -- accumulate for the same real group.
    detector_id           VARCHAR(128) NOT NULL DEFAULT '',
    pack_id               VARCHAR(64)  NOT NULL DEFAULT '',
    signal_concept        VARCHAR(160),
    -- The signed learned weight. Positive favours the finding type.
    net_weight            DOUBLE PRECISION NOT NULL DEFAULT 0,
    outcome_weight        DOUBLE PRECISION NOT NULL DEFAULT 0,
    decision_weight       DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- Whether any MEASURED outcome contributed, as opposed to opinion alone.
    -- Promoted to a column because an explanation reads differently when it can
    -- say "and one delivered measured improvement".
    has_outcome_evidence  BOOLEAN      NOT NULL DEFAULT FALSE,
    signal_count          INTEGER      NOT NULL DEFAULT 0,
    -- Whether the layer was ACTIVE for this org when the value was computed.
    -- Stored so a zero adjustment during cold start is distinguishable from a
    -- zero adjustment that learning actually arrived at.
    learning_active       BOOLEAN      NOT NULL DEFAULT FALSE,
    -- The AC2 links: the decisions and measurements behind this value.
    contributing_refs     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    -- Which weighting produced it. A value computed under different weights is
    -- not comparable with one computed under these.
    config_version        VARCHAR(32),
    revision              INTEGER      NOT NULL DEFAULT 1,
    computed_at           TIMESTAMPTZ  NOT NULL,
    updated_at            TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (org_id, detector_id, pack_id)
)
"""

#: The serving path reads every adjustment for one org at once.
CREATE_RANKING_ADJUSTMENTS_ORG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_ranking_adjustments_org
    ON ranking_adjustments (org_id, updated_at DESC)
"""

CREATE_RANKING_ADJUSTMENT_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS ranking_adjustment_history (
    history_id            VARCHAR(64)  NOT NULL PRIMARY KEY,
    org_id                VARCHAR(64)  NOT NULL,
    detector_id           VARCHAR(128) NOT NULL DEFAULT '',
    pack_id               VARCHAR(64)  NOT NULL DEFAULT '',
    -- 'recomputed' | 'reset'. The vocabulary lives in Python; the column is
    -- deliberately not an enum so T4 can add a case without a migration.
    change_kind           VARCHAR(32)  NOT NULL,
    previous_net_weight   DOUBLE PRECISION,
    net_weight            DOUBLE PRECISION NOT NULL DEFAULT 0,
    signal_count          INTEGER      NOT NULL DEFAULT 0,
    learning_active       BOOLEAN      NOT NULL DEFAULT FALSE,
    -- Who or what caused the change. A recomputation is the system; a reset is
    -- an Owner, and T4 needs to tell them apart.
    actor_id              VARCHAR(128),
    config_version        VARCHAR(32),
    revision              INTEGER      NOT NULL DEFAULT 1,
    record                JSONB        NOT NULL,
    recorded_at           TIMESTAMPTZ  NOT NULL
)
"""

CREATE_RANKING_ADJUSTMENT_HISTORY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_ranking_adjustment_history_org
    ON ranking_adjustment_history (org_id, recorded_at DESC)
"""

CREATE_RANKING_ADJUSTMENT_HISTORY_GROUP_INDEX = """
CREATE INDEX IF NOT EXISTS idx_ranking_adjustment_history_group
    ON ranking_adjustment_history (org_id, detector_id, pack_id, recorded_at DESC)
"""

#: History is append-only in production, like the feedback record. The current
#: table is deliberately NOT — it is a cache of the latest computed value and is
#: meant to be updated in place; its history lives next door.
RANKING_ADJUSTMENT_GRANTS = """
-- Run once per deployment, after the tables exist:
--   GRANT INSERT, SELECT, UPDATE, DELETE ON ranking_adjustments TO agentiq_app;
--   GRANT INSERT, SELECT ON ranking_adjustment_history TO agentiq_app;
--   REVOKE UPDATE, DELETE ON ranking_adjustment_history FROM agentiq_app;
"""

ALL_RANKING_ADJUSTMENT_DDL = (
    CREATE_RANKING_ADJUSTMENTS_TABLE,
    CREATE_RANKING_ADJUSTMENTS_ORG_INDEX,
    CREATE_RANKING_ADJUSTMENT_HISTORY_TABLE,
    CREATE_RANKING_ADJUSTMENT_HISTORY_INDEX,
    CREATE_RANKING_ADJUSTMENT_HISTORY_GROUP_INDEX,
)

__all__ = [
    "ALL_RANKING_ADJUSTMENT_DDL",
    "CREATE_RANKING_ADJUSTMENTS_TABLE",
    "CREATE_RANKING_ADJUSTMENTS_ORG_INDEX",
    "CREATE_RANKING_ADJUSTMENT_HISTORY_TABLE",
    "CREATE_RANKING_ADJUSTMENT_HISTORY_INDEX",
    "CREATE_RANKING_ADJUSTMENT_HISTORY_GROUP_INDEX",
    "RANKING_ADJUSTMENT_GRANTS",
]
