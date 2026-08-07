"""Persisted per-org pack lifecycle state and its append-only transition history.

2.0-C1 T2 (AT-827) — safe disable state machine.

Two tables, mirroring the MSP-B5 runbook-decision shape: the current row is
convenient state, the append-only history is the audit trail.

Design notes
------------
* **Absence of a row means ``active``.** A pack is active by default, so
  provisioning these tables changes no behaviour until a customer disables
  something. There is no seed step and no backfill.
* **Org-scoped by primary key.** Every key and every query includes ``org_id``,
  so one tenant's disable can never affect another's runs.
* **No row is ever deleted at runtime.** Re-enabling a pack writes a new
  ``active`` state and a new history row; it does not delete the disable. That is
  what makes the transition history a real audit trail (2.0-C1 AC4). The only
  DELETE/DROP in this module is the migration ``downgrade()``, which is a schema
  operation, not an application code path.
* ``reason`` is operator-supplied free text explaining WHY a pack was turned off
  ("superseded by cloud_ops", "customer opted out"). It is never derived and
  never required.
"""

CREATE_PACK_STATES_TABLE = """
CREATE TABLE IF NOT EXISTS pack_states (
    org_id         VARCHAR(64)  NOT NULL,
    pack_id        VARCHAR(64)  NOT NULL,
    state          VARCHAR(16)  NOT NULL,
    revision       INTEGER      NOT NULL DEFAULT 0,
    reason         TEXT,
    updated_by     VARCHAR(128),
    created_at     TIMESTAMPTZ  NOT NULL,
    updated_at     TIMESTAMPTZ  NOT NULL,
    pinned_version VARCHAR(32),
    PRIMARY KEY (org_id, pack_id)
)
"""

CREATE_PACK_STATE_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS pack_state_history (
    id                VARCHAR(64)  PRIMARY KEY,
    org_id            VARCHAR(64)  NOT NULL,
    pack_id           VARCHAR(64)  NOT NULL,
    revision          INTEGER      NOT NULL,
    transition        VARCHAR(16)  NOT NULL,
    previous_state    VARCHAR(16)  NOT NULL,
    resulting_state   VARCHAR(16)  NOT NULL,
    reason            TEXT,
    actor_id          VARCHAR(128) NOT NULL,
    changed_at        TIMESTAMPTZ  NOT NULL,
    previous_version  VARCHAR(32),
    resulting_version VARCHAR(32),
    UNIQUE (org_id, pack_id, revision)
)
"""

# 2.0-C1 T3 (AT-828): columns added to the T2 tables for version rollback. Applied
# separately by migration 0043 so a deployment already carrying 0042's tables gains
# them without a table rebuild; ALL_PACK_STATE_DDL above already includes them for a
# fresh install, and IF NOT EXISTS makes both paths idempotent.
#
# Why these live on the T2 tables rather than a second table pair: a version pin and
# an enable/disable are two lifecycle facts about the SAME (org, pack) pair, and
# AT-830 has to surface them together. One row and one audit trail means "what has
# this org done to this pack" has a single answer, and `revision` counts every
# change regardless of kind.
ADD_PACK_STATES_PINNED_VERSION = """
ALTER TABLE pack_states
    ADD COLUMN IF NOT EXISTS pinned_version VARCHAR(32)
"""

ADD_PACK_STATE_HISTORY_PREVIOUS_VERSION = """
ALTER TABLE pack_state_history
    ADD COLUMN IF NOT EXISTS previous_version VARCHAR(32)
"""

ADD_PACK_STATE_HISTORY_RESULTING_VERSION = """
ALTER TABLE pack_state_history
    ADD COLUMN IF NOT EXISTS resulting_version VARCHAR(32)
"""

ALL_PACK_VERSION_DDL = (
    ADD_PACK_STATES_PINNED_VERSION,
    ADD_PACK_STATE_HISTORY_PREVIOUS_VERSION,
    ADD_PACK_STATE_HISTORY_RESULTING_VERSION,
)

DROP_PACK_VERSION_DDL = (
    "ALTER TABLE pack_state_history DROP COLUMN IF EXISTS resulting_version",
    "ALTER TABLE pack_state_history DROP COLUMN IF EXISTS previous_version",
    "ALTER TABLE pack_states DROP COLUMN IF EXISTS pinned_version",
)

# Newest-first history reads for one pack (the audit-trail query).
CREATE_PACK_STATE_HISTORY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_pack_state_history_org_pack
    ON pack_state_history (org_id, pack_id, revision DESC)
"""

# The hot read: "which packs are disabled for this org?" — consulted by the
# activation gate on every run and by the finding-display label.
CREATE_PACK_STATES_ORG_STATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_pack_states_org_state
    ON pack_states (org_id, state)
"""

ALL_PACK_STATE_DDL = (
    CREATE_PACK_STATES_TABLE,
    CREATE_PACK_STATE_HISTORY_TABLE,
    CREATE_PACK_STATE_HISTORY_INDEX,
    CREATE_PACK_STATES_ORG_STATE_INDEX,
)

DROP_PACK_STATE_DDL = (
    "DROP INDEX IF EXISTS idx_pack_states_org_state",
    "DROP INDEX IF EXISTS idx_pack_state_history_org_pack",
    "DROP TABLE IF EXISTS pack_state_history",
    "DROP TABLE IF EXISTS pack_states",
)
