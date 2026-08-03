"""2.0-A3 T1 — the durable analyst-decision record the learning layer reads.

**Why this table exists at all, when a `decision` field already does.**

``opps[i]["decision"]`` (``APPROVED`` / ``REJECTED`` / ``UNREVIEWED``) is the
REVIEW decision: "is this finding real?", answered per run, for one run's list.
It is stored inside the run-scoped ``opps`` KV blob, which materialization
rewrites wholesale on every run and which ``app/replay.py`` resets to
``UNREVIEWED`` when ``REPLAY_RESETS_DECISIONS`` is set.

A learning signal a replay can erase is not a signal. That is the same argument
2.0-A2 T2 made for the baseline artifact, and it applies here for the same
reason: the learning layer must accumulate evidence ACROSS runs, while the review
decision is per-run by design and correctly so.

Three further reasons the existing field cannot carry this job:

* **Wrong key.** Decisions are addressed by ``(run_id, opp_id)`` — a per-run id.
  The learning layer joins on ``opportunity_identity``, the stable cross-run key.
* **No per-decision identity.** AC2 requires a rank-adjusted opportunity to LINK
  to the contributing decisions. A single mutable enum field has no id, no
  actor, and no timestamp to link to.
* **``defer`` does not belong in that enum.** The same literal tuple is validated
  for EVIDENCE decisions (``main.py`` ``set_evidence_decision``), where deferring
  is meaningless. Widening it would add a state that is invalid at one of its
  two call sites.

So this is an ADDITIVE record, not a replacement. The analyst review flow is
untouched; the opportunity-decision route additionally mirrors its decision here
so the existing UI feeds learning with no frontend change, and ``defer`` — which
has no home in the review enum — gets an explicit route of its own.

**Append-only, like the lifecycle history.** An analyst who changes their mind
appends a new row; the earlier judgement is never rewritten. What the team
thought at the time is itself part of the learning record, and a store that
edits history cannot answer "why was this ranked higher last month?".
"""

CREATE_OPPORTUNITY_FEEDBACK_TABLE = """
CREATE TABLE IF NOT EXISTS opportunity_feedback (
    -- Stable per-decision id. This is what AC2's "links to the contributing
    -- decisions" resolves against, so it must never be reused or rewritten.
    feedback_id           VARCHAR(64)  NOT NULL PRIMARY KEY,
    org_id                VARCHAR(64)  NOT NULL,
    -- The stable cross-run key. Not a run-scoped opportunity id: the whole
    -- point is that a decision made on one run informs ranking on the next.
    opportunity_identity  VARCHAR(64)  NOT NULL,
    -- accept / dismiss / defer. Deliberately NOT the review enum's vocabulary:
    -- these are learning actions, and conflating them with APPROVED/REJECTED
    -- would re-introduce the coupling this table exists to avoid.
    action                VARCHAR(16)  NOT NULL,
    -- Structured, from a closed vocabulary — never free text. A reason the
    -- learning layer cannot group on teaches it nothing, and free text in a
    -- learning input is an unbounded PII surface.
    reason_code           VARCHAR(48),
    -- Optional analyst elaboration. Carried for the explainability surface and
    -- for audit; never parsed, never grouped on, never a learning input.
    reason_detail         TEXT,
    actor_id              VARCHAR(128) NOT NULL,
    -- Similarity dimensions, denormalised at write time so the signal set can
    -- group without re-reading every run's opportunity blob. Frozen as of the
    -- decision: if a later pack version renames a detector, what the analyst
    -- actually judged is still recorded truthfully.
    detector_id           VARCHAR(128),
    pack_id               VARCHAR(64),
    signal_concept        VARCHAR(160),
    -- Provenance only. The decision belongs to the opportunity, not the run.
    run_id                VARCHAR(64),
    recorded_at           TIMESTAMPTZ  NOT NULL,
    -- Full record as served, so the read path never reconstructs from columns.
    record                JSONB        NOT NULL
)
"""

#: Reading is always "this org's decisions about this identity, newest first".
CREATE_OPPORTUNITY_FEEDBACK_IDENTITY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_opportunity_feedback_identity
    ON opportunity_feedback (org_id, opportunity_identity, recorded_at DESC)
"""

#: The signal set groups by similarity dimension across a whole org.
CREATE_OPPORTUNITY_FEEDBACK_SIMILARITY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_opportunity_feedback_similarity
    ON opportunity_feedback (org_id, detector_id, pack_id, recorded_at DESC)
"""

#: Cold-start counting (AC4) and the org-wide signal sweep.
CREATE_OPPORTUNITY_FEEDBACK_ORG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_opportunity_feedback_org_recorded
    ON opportunity_feedback (org_id, recorded_at DESC)
"""

#: Production removes the capability, not just the code path — the same posture
#: the baseline artifact takes. An append-only table that the application can
#: still UPDATE is append-only by convention, which is not a guarantee.
FEEDBACK_GRANTS = """
-- Run once per deployment, after the table exists:
--   GRANT INSERT, SELECT ON opportunity_feedback TO agentiq_app;
--   REVOKE UPDATE, DELETE ON opportunity_feedback FROM agentiq_app;
"""

ALL_OPPORTUNITY_FEEDBACK_DDL = (
    CREATE_OPPORTUNITY_FEEDBACK_TABLE,
    CREATE_OPPORTUNITY_FEEDBACK_IDENTITY_INDEX,
    CREATE_OPPORTUNITY_FEEDBACK_SIMILARITY_INDEX,
    CREATE_OPPORTUNITY_FEEDBACK_ORG_INDEX,
)

__all__ = [
    "ALL_OPPORTUNITY_FEEDBACK_DDL",
    "CREATE_OPPORTUNITY_FEEDBACK_TABLE",
    "CREATE_OPPORTUNITY_FEEDBACK_IDENTITY_INDEX",
    "CREATE_OPPORTUNITY_FEEDBACK_SIMILARITY_INDEX",
    "CREATE_OPPORTUNITY_FEEDBACK_ORG_INDEX",
    "FEEDBACK_GRANTS",
]
