"""2.0-B2 T3 — proposed cross-source entity matches + append-only decision history.

The ranked resolution engine (``app/cross_source_resolution.py``, T1) auto-merges
only the two tiers where identity is STATED — an explicit cross-reference, or the
org's alias table. A name-similarity match is never merged; it is a question for a
person. These two tables are where that question is parked so an Owner/Analyst can
answer it, and where the answer is kept.

Keyed on ``(org_id, proposal_id)``, where ``proposal_id`` is derived from the
ORDER-INDEPENDENT entity pair (see ``app.entity_match_proposals.proposal_id_for``).
That matters twice over: the engine proposes symmetrically — A→B and B→A are two
decisions about ONE question — so an order-dependent key would file the same
question twice and let an analyst answer it inconsistently; and because the id is
deterministic, re-running the engine UPSERTS the same row rather than growing a
duplicate queue.

Two tables, two jobs (the ``opportunity_lifecycle`` / ``runbook_matches`` pattern):

* ``entity_match_proposals`` — the CURRENT state of each proposed pair, including
  the evidence the reviewer needs to decide. This is what the review surface lists.
* ``entity_match_proposal_history`` — APPEND-ONLY. Every decision ever recorded,
  including an analyst reversing their own. History is never rewritten: a reversal
  is a new forward row, so the original decision — and who made it — survives.

``status`` is ``pending`` | ``confirmed`` | ``rejected``. Only a ``pending`` row is
ever refreshed by a later engine pass: once a human has answered, re-proposing the
same pair would ask the same question forever and quietly discard the answer.
``revision`` increments per decision and is unique per proposal in history, so the
series is orderable and two concurrent decisions collide rather than interleaving.

``evidence_payload`` carries the whole proposal snapshot (both entities' source
identities, the tier, and the corroborating relationships) so the review surface
can show WHY without re-running the engine, and so a decision made months ago can
still be explained against the evidence that existed when it was made.
"""

CREATE_ENTITY_MATCH_PROPOSALS_TABLE = """
CREATE TABLE IF NOT EXISTS entity_match_proposals (
    org_id              VARCHAR(64)  NOT NULL,
    proposal_id         VARCHAR(64)  NOT NULL,
    entity_type         VARCHAR(32)  NOT NULL,
    -- The pair, stored in sorted order so (A,B) and (B,A) are ONE row.
    left_entity_id      VARCHAR(36)  NOT NULL,
    right_entity_id     VARCHAR(36)  NOT NULL,
    -- Which resolution tier proposed it (always a propose-only tier — an
    -- auto-merge tier never reaches this table).
    tier                VARCHAR(32)  NOT NULL,
    confidence          FLOAT        NOT NULL,
    status              VARCHAR(16)  NOT NULL,
    -- The full proposal snapshot the reviewer sees: both entities' display names
    -- and source identities, the reason, and the corroborating relationships.
    evidence_payload    TEXT         NOT NULL,
    revision            INTEGER      NOT NULL DEFAULT 0,
    decided_by          VARCHAR(128),
    decided_at          TIMESTAMPTZ,
    note                TEXT,
    first_proposed_at   TIMESTAMPTZ  NOT NULL,
    last_proposed_at    TIMESTAMPTZ  NOT NULL,
    created_at          TIMESTAMPTZ  NOT NULL,
    updated_at          TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (org_id, proposal_id)
)
"""

CREATE_ENTITY_MATCH_PROPOSAL_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS entity_match_proposal_history (
    id                  VARCHAR(64)  PRIMARY KEY,
    org_id              VARCHAR(64)  NOT NULL,
    proposal_id         VARCHAR(64)  NOT NULL,
    revision            INTEGER      NOT NULL,
    action              VARCHAR(16)  NOT NULL,
    previous_status     VARCHAR(16)  NOT NULL,
    resulting_status    VARCHAR(16)  NOT NULL,
    actor_id            VARCHAR(128) NOT NULL,
    note                TEXT,
    decided_at          TIMESTAMPTZ  NOT NULL,
    UNIQUE (org_id, proposal_id, revision)
)
"""

# The review surface's primary read: one org's queue, filtered by status, newest
# proposal first.
CREATE_ENTITY_MATCH_PROPOSALS_IDX_ORG_STATUS = """
CREATE INDEX IF NOT EXISTS idx_entity_match_proposals_org_status
    ON entity_match_proposals (org_id, status, last_proposed_at DESC)
"""

# The per-proposal history read, newest first.
CREATE_ENTITY_MATCH_PROPOSAL_HISTORY_IDX = """
CREATE INDEX IF NOT EXISTS idx_entity_match_proposal_history_org_proposal
    ON entity_match_proposal_history (org_id, proposal_id, revision DESC)
"""

ALL_ENTITY_MATCH_PROPOSAL_DDL: tuple[str, ...] = (
    CREATE_ENTITY_MATCH_PROPOSALS_TABLE,
    CREATE_ENTITY_MATCH_PROPOSAL_HISTORY_TABLE,
    CREATE_ENTITY_MATCH_PROPOSALS_IDX_ORG_STATUS,
    CREATE_ENTITY_MATCH_PROPOSAL_HISTORY_IDX,
)

DROP_ENTITY_MATCH_PROPOSAL_DDL: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_entity_match_proposal_history_org_proposal",
    "DROP INDEX IF EXISTS idx_entity_match_proposals_org_status",
    "DROP TABLE IF EXISTS entity_match_proposal_history",
    "DROP TABLE IF EXISTS entity_match_proposals",
)

#: Column order shared by the writer and every reader, so the two cannot disagree.
ENTITY_MATCH_PROPOSAL_COLUMNS: tuple[str, ...] = (
    "org_id",
    "proposal_id",
    "entity_type",
    "left_entity_id",
    "right_entity_id",
    "tier",
    "confidence",
    "status",
    "evidence_payload",
    "revision",
    "decided_by",
    "decided_at",
    "note",
    "first_proposed_at",
    "last_proposed_at",
    "created_at",
    "updated_at",
)

ENTITY_MATCH_PROPOSAL_HISTORY_COLUMNS: tuple[str, ...] = (
    "id",
    "org_id",
    "proposal_id",
    "revision",
    "action",
    "previous_status",
    "resulting_status",
    "actor_id",
    "note",
    "decided_at",
)

__all__ = [
    "ALL_ENTITY_MATCH_PROPOSAL_DDL",
    "DROP_ENTITY_MATCH_PROPOSAL_DDL",
    "ENTITY_MATCH_PROPOSAL_COLUMNS",
    "ENTITY_MATCH_PROPOSAL_HISTORY_COLUMNS",
    "CREATE_ENTITY_MATCH_PROPOSALS_TABLE",
    "CREATE_ENTITY_MATCH_PROPOSAL_HISTORY_TABLE",
]
