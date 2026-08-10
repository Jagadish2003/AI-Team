"""2.0-A2 T1 — opportunity lifecycle state + append-only transition history.

Keyed on ``(org_id, opportunity_identity)``, NOT on a run.

That key is the single most important design decision in this subtask.
``opportunity_identity`` (R16-B1 §2, ``discovery/opportunity_identity.py``) is
computed only from run-invariant inputs — org, pack, signal key, resolved primary
entity keys — and deliberately EXCLUDES score, confidence, run timestamp and
evidence ids, precisely so the same real-world problem keeps one id run after run.

Lifecycle is a property of the PROBLEM, not of one observation of it. Storing it
on the run-scoped ``opps`` KV blob would reset it every time a run re-surfaced
the finding, which is exactly the failure this table exists to prevent. The
per-run observations already live in ``opportunity_instances`` keyed
``(opportunity_identity, run_id)``; this is its sibling, keyed one level up.

Two tables, for two different jobs:

* ``opportunity_lifecycle`` — the CURRENT state. Convenient to read, and the row
  a transition updates.
* ``opportunity_lifecycle_history`` — APPEND-ONLY. Every transition ever made,
  including an analyst unwinding their own mistake. History is never rewritten:
  an unwind is a new forward row, so the record of the mistake survives.

``revision`` increments per transition and is unique per identity in history, so
the series is orderable and a concurrent double-transition collides rather than
interleaving silently.
"""

CREATE_OPPORTUNITY_LIFECYCLE_TABLE = """
CREATE TABLE IF NOT EXISTS opportunity_lifecycle (
    org_id                VARCHAR(64)  NOT NULL,
    opportunity_identity  VARCHAR(64)  NOT NULL,
    state                 VARCHAR(16)  NOT NULL,
    -- The human-supplied date the change was deployed. NULL in every state that
    -- has no recorded action; never defaulted (see opportunity_lifecycle_states).
    action_date           DATE,
    -- The customer-entered description of the agent/process that was deployed.
    -- Kept on current state for the normal UI read and copied into append-only
    -- history so reopening can clear current state without erasing the record.
    action_note           TEXT,
    actioned_by           VARCHAR(128),
    actioned_at           TIMESTAMPTZ,
    revision              INTEGER      NOT NULL DEFAULT 0,
    -- The run that first surfaced this opportunity, for provenance only. The
    -- lifecycle itself is not run-scoped.
    first_seen_run_id     VARCHAR(64),
    last_run_id           VARCHAR(64),
    last_transition_at    TIMESTAMPTZ,
    updated_by            VARCHAR(128),
    created_at            TIMESTAMPTZ  NOT NULL,
    updated_at            TIMESTAMPTZ  NOT NULL,
    CONSTRAINT ck_opp_lifecycle_measurable_action_date CHECK (
        state NOT IN ('actioned', 'monitoring', 'measured', 'stalled')
        OR action_date IS NOT NULL
    ),
    PRIMARY KEY (org_id, opportunity_identity)
)
"""

CREATE_OPPORTUNITY_LIFECYCLE_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS opportunity_lifecycle_history (
    id                    VARCHAR(64)  PRIMARY KEY,
    org_id                VARCHAR(64)  NOT NULL,
    opportunity_identity  VARCHAR(64)  NOT NULL,
    revision              INTEGER      NOT NULL,
    from_state            VARCHAR(16)  NOT NULL,
    to_state              VARCHAR(16)  NOT NULL,
    actor                 VARCHAR(16)  NOT NULL,
    actor_id              VARCHAR(128) NOT NULL,
    -- The action date as it stood AFTER this transition, so a reader can see an
    -- unwind clearing it without consulting the current row.
    action_date           DATE,
    reason                TEXT         NOT NULL,
    note                  TEXT,
    run_id                VARCHAR(64),
    transitioned_at       TIMESTAMPTZ  NOT NULL,
    UNIQUE (org_id, opportunity_identity, revision)
)
"""

# The portfolio read (2.0-A2 T6): every actioned opportunity in one org, by state.
CREATE_OPPORTUNITY_LIFECYCLE_IDX_ORG_STATE = """
CREATE INDEX IF NOT EXISTS idx_opp_lifecycle_org_state
    ON opportunity_lifecycle (org_id, state)
"""

# The per-opportunity history read, newest first.
CREATE_OPPORTUNITY_LIFECYCLE_HISTORY_IDX = """
CREATE INDEX IF NOT EXISTS idx_opp_lifecycle_history_org_identity
    ON opportunity_lifecycle_history (org_id, opportunity_identity, revision DESC)
"""

ALL_OPPORTUNITY_LIFECYCLE_DDL: tuple[str, ...] = (
    CREATE_OPPORTUNITY_LIFECYCLE_TABLE,
    CREATE_OPPORTUNITY_LIFECYCLE_HISTORY_TABLE,
    CREATE_OPPORTUNITY_LIFECYCLE_IDX_ORG_STATE,
    CREATE_OPPORTUNITY_LIFECYCLE_HISTORY_IDX,
)

DROP_OPPORTUNITY_LIFECYCLE_DDL: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_opp_lifecycle_history_org_identity",
    "DROP INDEX IF EXISTS idx_opp_lifecycle_org_state",
    "DROP TABLE IF EXISTS opportunity_lifecycle_history",
    "DROP TABLE IF EXISTS opportunity_lifecycle",
)

#: Column order for the current-state row — the one ordering the writer and every
#: reader share, so they cannot disagree on positional mapping.
OPPORTUNITY_LIFECYCLE_COLUMNS: tuple[str, ...] = (
    "org_id",
    "opportunity_identity",
    "state",
    "action_date",
    "action_note",
    "actioned_by",
    "actioned_at",
    "revision",
    "first_seen_run_id",
    "last_run_id",
    "last_transition_at",
    "updated_by",
    "created_at",
    "updated_at",
)

OPPORTUNITY_LIFECYCLE_HISTORY_COLUMNS: tuple[str, ...] = (
    "id",
    "org_id",
    "opportunity_identity",
    "revision",
    "from_state",
    "to_state",
    "actor",
    "actor_id",
    "action_date",
    "reason",
    "note",
    "run_id",
    "transitioned_at",
)

__all__ = [
    "ALL_OPPORTUNITY_LIFECYCLE_DDL",
    "DROP_OPPORTUNITY_LIFECYCLE_DDL",
    "OPPORTUNITY_LIFECYCLE_COLUMNS",
    "OPPORTUNITY_LIFECYCLE_HISTORY_COLUMNS",
    "CREATE_OPPORTUNITY_LIFECYCLE_TABLE",
    "CREATE_OPPORTUNITY_LIFECYCLE_HISTORY_TABLE",
]
