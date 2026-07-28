"""DDL for the vendor-side license registry — R-1.9.1-L3 (AT-715 / T1, AT-717 / T3).

CloudFulcrum-INTERNAL. These two tables are the authoritative, upstream record of
every license key CloudFulcrum has *minted* — NOT the customer-side installed-key
store (``org_licenses`` / ``app/license_runtime.py``), which is one downstream copy
of a single installed key at one customer. Do not conflate the two.

* ``license_registry`` — one row per issued/renewed license: who it was issued to,
  under which contract, its terms, the signing ``kid``, its status, and the
  ``supersedes`` renewal linkage (AT-715 / T1). Indexes support customer/org
  lookup (AC3 lineage) and the expiring-within-N scan (AC4).
* ``issuance_audit`` — an APPEND-ONLY ledger of every issue/renew/regenerate write
  (who, when, what terms, which contract — AT-717 / T3). Append-only is enforced
  at the schema level by two Postgres rewrite rules that turn any UPDATE/DELETE
  into a no-op, mirroring the ``telemetry_events`` no-update/no-delete rules in
  ``database/provision/provision.sql`` — so no service path can alter or remove an
  audit entry (AC2).

Single source of truth for the schema, imported by BOTH the CI migration
(``0026_create_license_registry.py``) and the runtime provisioner
(``license/registry.py::ensure_registry_schema``) so they can never drift —
mirroring ``database/models/entities.py`` / ``org_licenses.py``. PostgreSQL is the
sole deployment target; ``provision.sql`` carries the pg_dump-style snapshot for
the psql-only provisioning path.

The status and audit-action vocabularies live here as the single source and are
mirrored into the table CHECK constraints, so the schema and the Python constants
can never diverge.
"""

# --- status vocabulary --------------------------------------------------------
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_REVOKED_AT_NEXT_ROTATION = "revoked_at_next_rotation"
REGISTRY_STATUSES = (STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_REVOKED_AT_NEXT_ROTATION)

# --- audit actions ------------------------------------------------------------
ACTION_ISSUE = "issue"
ACTION_RENEW = "renew"
ACTION_REGENERATE = "regenerate"
AUDIT_ACTIONS = (ACTION_ISSUE, ACTION_RENEW, ACTION_REGENERATE)


def _sql_in_list(values) -> str:
    """Render a tuple of strings as a SQL ``'a', 'b', 'c'`` list for a CHECK."""
    return ", ".join("'" + v + "'" for v in values)


CREATE_LICENSE_REGISTRY_TABLE = f"""
CREATE TABLE IF NOT EXISTS license_registry (
    license_id                   TEXT                     NOT NULL PRIMARY KEY,
    customer                     TEXT                     NOT NULL,
    org_id                       TEXT                     NOT NULL,
    contract_ref                 TEXT                     NOT NULL,
    deployment_type              TEXT                     NOT NULL,
    max_systems                  INTEGER,
    expires_at                   DATE                     NOT NULL,
    grace_days                   INTEGER                  NOT NULL DEFAULT 14,
    kid                          TEXT                     NOT NULL,
    issued_by                    TEXT                     NOT NULL,
    issued_at                    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    status                       TEXT                     NOT NULL DEFAULT '{STATUS_ACTIVE}'
                                   CHECK (status IN ({_sql_in_list(REGISTRY_STATUSES)})),
    supersedes                   TEXT,
    notes                        TEXT,
    deployment_fee_collected     BOOLEAN                  NOT NULL DEFAULT FALSE,
    deployment_fee_collected_at  TIMESTAMP WITH TIME ZONE,
    payload_version              INTEGER                  NOT NULL DEFAULT 2,
    license_key                  TEXT                     NOT NULL
)
"""

CREATE_LICENSE_REGISTRY_IDX_CUSTOMER = (
    "CREATE INDEX IF NOT EXISTS idx_license_registry_customer "
    "ON license_registry (customer)"
)
CREATE_LICENSE_REGISTRY_IDX_ORG = (
    "CREATE INDEX IF NOT EXISTS idx_license_registry_org "
    "ON license_registry (org_id)"
)
CREATE_LICENSE_REGISTRY_IDX_EXPIRY = (
    "CREATE INDEX IF NOT EXISTS idx_license_registry_expiry "
    "ON license_registry (status, expires_at)"
)
CREATE_LICENSE_REGISTRY_IDX_SUPERSEDES = (
    "CREATE INDEX IF NOT EXISTS idx_license_registry_supersedes "
    "ON license_registry (supersedes)"
)

CREATE_ISSUANCE_AUDIT_TABLE = f"""
CREATE TABLE IF NOT EXISTS issuance_audit (
    audit_id         TEXT                     NOT NULL PRIMARY KEY,
    license_id       TEXT                     NOT NULL,
    action           TEXT                     NOT NULL
                       CHECK (action IN ({_sql_in_list(AUDIT_ACTIONS)})),
    actor            TEXT                     NOT NULL,
    occurred_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    customer         TEXT,
    org_id           TEXT,
    contract_ref     TEXT,
    kid              TEXT,
    deployment_type  TEXT,
    terms            TEXT,
    supersedes       TEXT,
    notes            TEXT
)
"""

CREATE_ISSUANCE_AUDIT_IDX_LICENSE = (
    "CREATE INDEX IF NOT EXISTS idx_issuance_audit_license "
    "ON issuance_audit (license_id)"
)

# Append-only enforcement (AC2), schema-level. Postgres rewrite rules turn any
# UPDATE/DELETE against the audit table into a no-op — the row is neither changed
# nor removed. ``CREATE OR REPLACE RULE`` is idempotent so re-provisioning is
# safe. This mirrors the ``telemetry_events`` trg_telemetry_no_update /
# trg_telemetry_no_delete rules in provision.sql.
CREATE_ISSUANCE_AUDIT_NO_UPDATE_RULE = (
    "CREATE OR REPLACE RULE issuance_audit_no_update "
    "AS ON UPDATE TO issuance_audit DO INSTEAD NOTHING"
)
CREATE_ISSUANCE_AUDIT_NO_DELETE_RULE = (
    "CREATE OR REPLACE RULE issuance_audit_no_delete "
    "AS ON DELETE TO issuance_audit DO INSTEAD NOTHING"
)

ALL_LICENSE_REGISTRY_DDL: tuple = (
    CREATE_LICENSE_REGISTRY_TABLE,
    CREATE_LICENSE_REGISTRY_IDX_CUSTOMER,
    CREATE_LICENSE_REGISTRY_IDX_ORG,
    CREATE_LICENSE_REGISTRY_IDX_EXPIRY,
    CREATE_LICENSE_REGISTRY_IDX_SUPERSEDES,
    CREATE_ISSUANCE_AUDIT_TABLE,
    CREATE_ISSUANCE_AUDIT_IDX_LICENSE,
    CREATE_ISSUANCE_AUDIT_NO_UPDATE_RULE,
    CREATE_ISSUANCE_AUDIT_NO_DELETE_RULE,
)
