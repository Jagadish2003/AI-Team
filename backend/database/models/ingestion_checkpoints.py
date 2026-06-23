"""DDL for the ingestion_checkpoints table — R16-A1 / AT-378 (Change-Based Ingestion).

Stores one opaque per-source position marker per ``(org_id, connector_id)`` — the
checkpoint a connector reports after a successful run so the next run reads only
what changed since (R16-A1 §2). ``value`` is OPAQUE to the runner: each connector
encodes its own native change signal (an ISO timestamp, a commit SHA, a change
sequence id, …) and the runner persists/returns it verbatim, never interpreting
it. Mirrors the ``Checkpoint`` contract in ``discovery/ingest/base.py``.

Persistence rule (enforced by the repository + runner, not the schema): a row is
written ONLY after a run fully consumes the delta for that source — never
mid-batch — so a failed or partial run never advances the checkpoint and the next
run safely re-reads from the last known-good position.

No ORM and no foreign key — single source of truth for the schema, imported by
both 0017_create_ingestion_checkpoints.py (the CI migration) and the runtime
repository so they can never drift (same pattern as database/models/org_licenses.py
and entities.py). PostgreSQL is the sole deployment target; the IF NOT EXISTS
guard keeps it idempotent.
"""

CREATE_INGESTION_CHECKPOINTS_TABLE = """
CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
    org_id       VARCHAR(64)              NOT NULL,
    connector_id VARCHAR(64)              NOT NULL,
    value        TEXT                     NOT NULL,
    captured_at  TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, connector_id)
)
"""

ALL_INGESTION_CHECKPOINTS_DDL: tuple[str, ...] = (CREATE_INGESTION_CHECKPOINTS_TABLE,)
