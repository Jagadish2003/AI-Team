"""Persisted MSP-B5 runbook-match lifecycle, history, and labelled feedback."""

CREATE_RUNBOOK_MATCHES_TABLE = """
CREATE TABLE IF NOT EXISTS runbook_matches (
    org_id          VARCHAR(64)  NOT NULL,
    recurrence_id   VARCHAR(128) NOT NULL,
    base_state      VARCHAR(16)  NOT NULL,
    current_state   VARCHAR(16)  NOT NULL,
    current_action  VARCHAR(16),
    match_payload   TEXT         NOT NULL,
    revision        INTEGER      NOT NULL DEFAULT 0,
    updated_by      VARCHAR(128),
    created_at      TIMESTAMPTZ  NOT NULL,
    updated_at      TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (org_id, recurrence_id)
)
"""

CREATE_RUNBOOK_MATCH_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS runbook_match_decision_history (
    id                  VARCHAR(64)  PRIMARY KEY,
    org_id              VARCHAR(64)  NOT NULL,
    recurrence_id       VARCHAR(128) NOT NULL,
    revision            INTEGER      NOT NULL,
    action              VARCHAR(16)  NOT NULL,
    previous_action     VARCHAR(16),
    previous_state      VARCHAR(16)  NOT NULL,
    resulting_state     VARCHAR(16)  NOT NULL,
    actor_id            VARCHAR(128) NOT NULL,
    decided_at          TIMESTAMPTZ  NOT NULL,
    UNIQUE (org_id, recurrence_id, revision)
)
"""

CREATE_RUNBOOK_MATCH_FEEDBACK_TABLE = """
CREATE TABLE IF NOT EXISTS runbook_match_feedback (
    id                  VARCHAR(64)  PRIMARY KEY,
    decision_history_id VARCHAR(64)  NOT NULL UNIQUE,
    org_id              VARCHAR(64)  NOT NULL,
    recurrence_id       VARCHAR(128) NOT NULL,
    feedback_label      VARCHAR(32)  NOT NULL,
    features_payload    TEXT         NOT NULL,
    actor_id            VARCHAR(128) NOT NULL,
    created_at          TIMESTAMPTZ  NOT NULL
)
"""

CREATE_RUNBOOK_MATCH_HISTORY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_runbook_match_history_org_recurrence
    ON runbook_match_decision_history (org_id, recurrence_id, revision DESC)
"""

CREATE_RUNBOOK_MATCH_FEEDBACK_INDEX = """
CREATE INDEX IF NOT EXISTS idx_runbook_match_feedback_org_created
    ON runbook_match_feedback (org_id, created_at DESC)
"""

ALL_RUNBOOK_MATCH_DDL = (
    CREATE_RUNBOOK_MATCHES_TABLE,
    CREATE_RUNBOOK_MATCH_HISTORY_TABLE,
    CREATE_RUNBOOK_MATCH_FEEDBACK_TABLE,
    CREATE_RUNBOOK_MATCH_HISTORY_INDEX,
    CREATE_RUNBOOK_MATCH_FEEDBACK_INDEX,
)

DROP_RUNBOOK_MATCH_DDL = (
    "DROP INDEX IF EXISTS idx_runbook_match_feedback_org_created",
    "DROP INDEX IF EXISTS idx_runbook_match_history_org_recurrence",
    "DROP TABLE IF EXISTS runbook_match_feedback",
    "DROP TABLE IF EXISTS runbook_match_decision_history",
    "DROP TABLE IF EXISTS runbook_matches",
)
