"""2.0-B2 T5 — unmerge suppression + the dependent-finding re-evaluation work list.

AC4 is "unmerge restores constituents and flags dependent findings for
re-evaluation". Restoring costs no schema at all: T2's merge never deleted
anything (the absorbed row, its identity and its edges all survive; a merge is a
``merge_provenance`` block on the survivor plus a ``merged_into`` pointer on the
constituent), so reversing one is removing those two marks. These two tables exist
for the parts that are NOT reversible by removing a mark.

``entity_unmerges`` — why a "reversal" needs its own storage
------------------------------------------------------------
Because the appliers are idempotent and run repeatedly. ``apply_org_merges`` walks
the auto-merge tiers and every human-confirmed pair on each pass, so a pair whose
merge was just reversed is re-merged by the very next pass — the source data still
carries the cross-reference, and the confirmed proposal is still confirmed (T4 made
that answer durable on purpose). Without a recorded suppression, "unmerge" means
"unmerged until something runs", which is not a reversal at all.

So an unmerge records a BLOCK, and ``entity_merge.apply_merge`` consults it and
refuses a blocked pair with a named reason.

The block is keyed on a *pair key*, and each unmerge records TWO of them, because
the two available ways to name a pair fail in opposite directions:

* the **row-id pair** — exact, and the only key that is right when two entities
  share a name; breaks when the entity rows churn (the T4 lesson: a source that
  starts supplying record ids makes ``upsert_source_entity`` insert a NEW row).
* the **source-identity pair** (``source_system`` + canonical name per side) —
  survives that churn; breaks if a side is renamed.

Recording both and matching on either covers each failure mode with the other. The
residual gap is stated rather than hidden: if the row ids AND a name change
together, the pair is no longer recognisable as the one that was unmerged, and it
may merge again. That is a genuinely new pair by every identity the platform has.

One row per ``(org_id, pair_key)`` — so an unmerge writes two rows sharing an
``unmerge_id`` — which makes the hot read ("is this pair blocked?") a single
indexed lookup rather than a containment scan over a JSON column.

``status`` is ``blocked`` | ``released``. Releasing is deliberate and separate: it
is how an Owner says "I was wrong, let it merge again", and it is its own audit
event. Nothing is ever deleted, so the record of the unmerge survives its release.

``finding_reevaluation_flags`` — the other half of AC4
------------------------------------------------------
Keyed on ``(org_id, opportunity_identity)``, following 2.0-A2's lifecycle store
for the same reason: a finding's need for re-evaluation is a property of the
PROBLEM, not of the one run that happened to observe it. Run-scoped KV
structurally cannot answer "which findings are still awaiting re-evaluation?"
across runs, and "on the next run" is exactly a cross-run question.

``status`` is ``pending`` | ``cleared``. A later run that re-observes the identity
clears it and records WHICH run did so — that is what makes "re-evaluated on the
next run" an observable fact rather than an intention. Re-flagging an already-
pending finding keeps its original ``flagged_at`` (the wait started then) while
moving the trigger, so a second unmerge cannot reset the clock on a finding that
has been waiting.
"""

CREATE_ENTITY_UNMERGES_TABLE = """
CREATE TABLE IF NOT EXISTS entity_unmerges (
    org_id                VARCHAR(64)  NOT NULL,
    -- One of the two keys naming the pair this block covers (see module docstring).
    pair_key              VARCHAR(80)  NOT NULL,
    -- Groups the rows written by ONE unmerge, so the log reads as one action.
    unmerge_id            VARCHAR(64)  NOT NULL,
    pair_key_kind         VARCHAR(16)  NOT NULL,
    status                VARCHAR(16)  NOT NULL,
    -- The entity the constituent was detached FROM, and the constituent itself.
    survivor_entity_id    VARCHAR(36)  NOT NULL,
    detached_entity_id    VARCHAR(36)  NOT NULL,
    entity_type           VARCHAR(32)  NOT NULL,
    -- The rule that had performed the merge being reversed (T2's provenance), so
    -- the log answers "what kind of decision was undone?".
    previous_rule         VARCHAR(32),
    -- Every entity id the unmerge handed back, including the detached entity's own
    -- sub-constituents when a chain of merges was split.
    restored_entity_ids   TEXT         NOT NULL,
    -- What the unmerge did about dependent findings, kept with the action itself.
    flagged_finding_count INTEGER      NOT NULL DEFAULT 0,
    unlinked_finding_count INTEGER     NOT NULL DEFAULT 0,
    reason                TEXT,
    actor_id              VARCHAR(128) NOT NULL,
    created_at            TIMESTAMPTZ  NOT NULL,
    released_by           VARCHAR(128),
    released_at           TIMESTAMPTZ,
    release_reason        TEXT,
    PRIMARY KEY (org_id, pair_key)
)
"""

# The hot read, on every merge attempt: "is this pair blocked?" Both candidate keys
# are looked up in one indexed query.
CREATE_ENTITY_UNMERGES_IDX_ORG_STATUS = """
CREATE INDEX IF NOT EXISTS idx_entity_unmerges_org_status
    ON entity_unmerges (org_id, status, pair_key)
"""

# The log read: one org's unmerges, newest first; and the per-action grouping.
CREATE_ENTITY_UNMERGES_IDX_ORG_CREATED = """
CREATE INDEX IF NOT EXISTS idx_entity_unmerges_org_created
    ON entity_unmerges (org_id, created_at DESC)
"""

CREATE_ENTITY_UNMERGES_IDX_UNMERGE_ID = """
CREATE INDEX IF NOT EXISTS idx_entity_unmerges_org_unmerge
    ON entity_unmerges (org_id, unmerge_id)
"""

CREATE_FINDING_REEVALUATION_FLAGS_TABLE = """
CREATE TABLE IF NOT EXISTS finding_reevaluation_flags (
    org_id                VARCHAR(64)  NOT NULL,
    -- The STABLE cross-run identity of the finding, not a run-scoped opp id: the
    -- flag has to outlive the run that was current when it was raised.
    opportunity_identity  VARCHAR(64)  NOT NULL,
    status                VARCHAR(16)  NOT NULL,
    -- Why re-evaluation is needed, and what triggered it. 'entity_unmerge' is the
    -- only producer today; the column exists because it will not be the last.
    reason                VARCHAR(64)  NOT NULL,
    trigger_kind          VARCHAR(32)  NOT NULL,
    trigger_ref           VARCHAR(64),
    -- The entity ids whose identity changed under this finding, so a reviewer can
    -- see WHAT changed rather than only that something did.
    entity_ids            TEXT         NOT NULL,
    -- The run the finding was last observed in when it was flagged: the "before"
    -- side of any comparison a re-evaluation makes.
    flagged_run_id        VARCHAR(64),
    flagged_by            VARCHAR(128) NOT NULL,
    flagged_at            TIMESTAMPTZ  NOT NULL,
    updated_at            TIMESTAMPTZ  NOT NULL,
    -- Set by the run that re-observed the finding. Recording the run id is what
    -- turns "will be re-evaluated" into "was re-evaluated, by this run".
    cleared_run_id        VARCHAR(64),
    cleared_at            TIMESTAMPTZ,
    PRIMARY KEY (org_id, opportunity_identity)
)
"""

# The run-time read: "which of this org's findings are awaiting re-evaluation?"
CREATE_FINDING_REEVALUATION_FLAGS_IDX_ORG_STATUS = """
CREATE INDEX IF NOT EXISTS idx_finding_reeval_flags_org_status
    ON finding_reevaluation_flags (org_id, status, flagged_at DESC)
"""

ALL_ENTITY_UNMERGE_DDL: tuple[str, ...] = (
    CREATE_ENTITY_UNMERGES_TABLE,
    CREATE_ENTITY_UNMERGES_IDX_ORG_STATUS,
    CREATE_ENTITY_UNMERGES_IDX_ORG_CREATED,
    CREATE_ENTITY_UNMERGES_IDX_UNMERGE_ID,
    CREATE_FINDING_REEVALUATION_FLAGS_TABLE,
    CREATE_FINDING_REEVALUATION_FLAGS_IDX_ORG_STATUS,
)

DROP_ENTITY_UNMERGE_DDL: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_finding_reeval_flags_org_status",
    "DROP TABLE IF EXISTS finding_reevaluation_flags",
    "DROP INDEX IF EXISTS idx_entity_unmerges_org_unmerge",
    "DROP INDEX IF EXISTS idx_entity_unmerges_org_created",
    "DROP INDEX IF EXISTS idx_entity_unmerges_org_status",
    "DROP TABLE IF EXISTS entity_unmerges",
)

#: Column order shared by the writer and every reader, so the two cannot disagree.
ENTITY_UNMERGE_COLUMNS: tuple[str, ...] = (
    "org_id",
    "pair_key",
    "unmerge_id",
    "pair_key_kind",
    "status",
    "survivor_entity_id",
    "detached_entity_id",
    "entity_type",
    "previous_rule",
    "restored_entity_ids",
    "flagged_finding_count",
    "unlinked_finding_count",
    "reason",
    "actor_id",
    "created_at",
    "released_by",
    "released_at",
    "release_reason",
)

FINDING_REEVALUATION_FLAG_COLUMNS: tuple[str, ...] = (
    "org_id",
    "opportunity_identity",
    "status",
    "reason",
    "trigger_kind",
    "trigger_ref",
    "entity_ids",
    "flagged_run_id",
    "flagged_by",
    "flagged_at",
    "updated_at",
    "cleared_run_id",
    "cleared_at",
)
