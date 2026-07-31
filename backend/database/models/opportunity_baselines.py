"""2.0-A2 T2 — the immutable baseline artifact table.

Keyed on ``(org_id, opportunity_identity)``: one baseline per real-world problem,
frozen at the moment its finding was first created.

**Write-once, enforced at the data layer.** AC1 requires the artifact be immutable
after creation, so there is deliberately no UPDATE path:

* the primary key makes a second write for the same identity a conflict, which the
  store turns into a no-op rather than an overwrite;
* the store issues no ``UPDATE`` or ``DELETE`` against these tables at all, and a
  contract test greps this module and the store to prove it;
* in production the grants below remove the capability entirely, mirroring the
  ``audit_log`` posture that 2.0-D4 T4 will later verify across the schema.

PostgreSQL deployment note — apply after CREATE TABLE::

    REVOKE UPDATE, DELETE ON opportunity_baselines FROM app_user;
    GRANT INSERT, SELECT ON opportunity_baselines TO app_user;

**Why not the run-scoped KV blob.** ``opps`` / ``evidence`` are rewritten wholesale
by materialization and by replay — ``replay.py`` already resets ``decision`` to
``UNREVIEWED`` on replay, which is exactly the hazard. A baseline a replay can
silently rewrite is not a baseline.

**Why not the first ``opportunity_instances`` row.** It is close — it carries
pack_id, pack_version, detector_id, score, confidence, evidence_ids — but it is
silent about the observation window and the underlying signal values, it has an
``is_deleted`` flag, and nothing stops an upsert on ``(opportunity_identity,
run_id)`` from rewriting it. This artifact REFERENCES that row instead of
replacing it: one is "how the finding scored on that run", the other is "the
measurement basis we will be judged against".

The artifact body is stored as JSON in ``artifact`` (the full frozen record), with
the fields T3's comparison and T4's confounder checks query on promoted to real
columns so neither has to parse JSON to filter.
"""

CREATE_OPPORTUNITY_BASELINES_TABLE = """
CREATE TABLE IF NOT EXISTS opportunity_baselines (
    org_id                VARCHAR(64)  NOT NULL,
    opportunity_identity  VARCHAR(64)  NOT NULL,
    -- The run that CREATED the finding. Never updated: a later run producing the
    -- same identity does not get to restate what the finding was born with.
    run_id                VARCHAR(64)  NOT NULL,
    detector_id           VARCHAR(128) NOT NULL,
    pack_id               VARCHAR(64),
    -- T4's confounder detection compares this against the pack version in force
    -- at measurement time to flag pack-logic drift. Capture it here or T4 has
    -- nothing to compare.
    pack_version          VARCHAR(32),
    opportunity_ref       VARCHAR(64),
    window_days           INTEGER,
    window_started_at     TIMESTAMPTZ,
    window_ended_at       TIMESTAMPTZ,
    window_derivation     VARCHAR(64)  NOT NULL,
    schema_version        VARCHAR(16)  NOT NULL,
    -- The whole frozen artifact: signals, values, baseline stats, window.
    artifact              TEXT         NOT NULL,
    captured_at           TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (org_id, opportunity_identity)
)
"""

# T3 reads a run's baselines when a post-action run lands.
CREATE_OPPORTUNITY_BASELINES_IDX_ORG_RUN = """
CREATE INDEX IF NOT EXISTS idx_opp_baselines_org_run
    ON opportunity_baselines (org_id, run_id)
"""

# T4's pack-version drift check filters by detector within an org.
CREATE_OPPORTUNITY_BASELINES_IDX_ORG_DETECTOR = """
CREATE INDEX IF NOT EXISTS idx_opp_baselines_org_detector
    ON opportunity_baselines (org_id, detector_id)
"""

ALL_OPPORTUNITY_BASELINES_DDL: tuple[str, ...] = (
    CREATE_OPPORTUNITY_BASELINES_TABLE,
    CREATE_OPPORTUNITY_BASELINES_IDX_ORG_RUN,
    CREATE_OPPORTUNITY_BASELINES_IDX_ORG_DETECTOR,
)

DROP_OPPORTUNITY_BASELINES_DDL: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_opp_baselines_org_detector",
    "DROP INDEX IF EXISTS idx_opp_baselines_org_run",
    "DROP TABLE IF EXISTS opportunity_baselines",
)

#: Column order shared by the writer and every reader, so they cannot disagree on
#: positional mapping.
OPPORTUNITY_BASELINE_COLUMNS: tuple[str, ...] = (
    "org_id",
    "opportunity_identity",
    "run_id",
    "detector_id",
    "pack_id",
    "pack_version",
    "opportunity_ref",
    "window_days",
    "window_started_at",
    "window_ended_at",
    "window_derivation",
    "schema_version",
    "artifact",
    "captured_at",
)

__all__ = [
    "ALL_OPPORTUNITY_BASELINES_DDL",
    "DROP_OPPORTUNITY_BASELINES_DDL",
    "OPPORTUNITY_BASELINE_COLUMNS",
    "CREATE_OPPORTUNITY_BASELINES_TABLE",
]
