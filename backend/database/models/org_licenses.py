"""DDL for the org_licenses table — LIC-1 per-org licensing.

Stores the installed CloudFulcrum license key **per organisation** instead of a
single installation-global slot, so each tenant's license is independent: a newly
registered org has no row and therefore no valid license until its Owner pastes a
key, while another org's key never leaks across the tenant boundary.

One row per org (org_id PRIMARY KEY); a row exists only once a key is installed.
``last_seen_date`` / ``last_status`` carry the per-org clock-rollback baseline and
the cached status used for transition telemetry — the same signals that previously
lived in the app-global ``license:*`` KV slots, now scoped per org.

No ORM and no foreign key to ``orgs`` — single source of truth for the schema,
imported by both 0015_create_org_licenses.py (the CI migration) and the runtime
so they can never drift, mirroring database/models/orgs.py / entities.py. The FK
is intentionally omitted to match the other loosely-coupled tables (kv,
credentials) and to keep migration ordering risk-free.

SQLite-compatible types. PostgreSQL is the sole deployment target (see
database/provision); ``updated_at`` uses TIMESTAMP WITH TIME ZONE there.
"""

CREATE_ORG_LICENSES_TABLE = """
CREATE TABLE IF NOT EXISTS org_licenses (
    org_id         VARCHAR(36)              NOT NULL PRIMARY KEY,
    license_key    TEXT                     NOT NULL,
    last_seen_date VARCHAR(32),
    last_status    VARCHAR(32),
    updated_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
)
"""

ALL_ORG_LICENSES_DDL: tuple[str, ...] = (CREATE_ORG_LICENSES_TABLE,)
