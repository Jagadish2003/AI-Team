"""DB-free lock + parity tests for the MSP-B8 staging schema (T1).

These run without a database, so the staging contract is guarded even where the
contract test DB is unreachable. They pin three artifacts to one shape so they can
never silently drift:

  * the single source of truth — ``database/models/ops_event_staging.py``
  * the partner PostgreSQL artifact — ``database/staging/ops_event_staging_postgresql.sql``
  * the partner SQL Server artifact — ``database/staging/ops_event_staging_sqlserver.sql``

and check the partner guide (``docs/MSP-B8_STAGING_SCHEMA.md``) carries the
versioned contract. The live-schema lock (that the migration actually builds this)
is ``tests/contract/test_ops_event_staging_schema.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from database.models import ops_event_staging as m

_BACKEND = Path(__file__).resolve().parents[2]
_REPO = _BACKEND.parent
_PG_SQL = _BACKEND / "database" / "staging" / "ops_event_staging_postgresql.sql"
_MSSQL_SQL = _BACKEND / "database" / "staging" / "ops_event_staging_sqlserver.sql"
_DOC = _REPO / "docs" / "MSP-B8_STAGING_SCHEMA.md"

# The locked column set. Every artifact must mention every one of these.
STAGING_COLUMNS = [
    "row_id",
    "org_id",
    "provider",
    "source_format",
    "batch_id",
    "provider_event_id",
    "raw",
    "loaded_at",
]
BATCH_COLUMNS = [
    "org_id",
    "batch_id",
    "provider",
    "source_format",
    "source_reference",
    "record_count",
    "skipped_count",
    "loaded_at",
]

# Constraints / indexes that carry the acceptance criteria.
REQUIRED_OBJECTS = [
    "uq_ops_event_staging_provider_event",   # AC3 duplicate prevention
    "idx_ops_event_staging_org_row",         # AC4 row-id paging + AC6 org scope
    "idx_ops_event_staging_org_batch",       # batch lookup
    "idx_ops_event_staging_org_format",      # provider/format filtering
]


@pytest.fixture(scope="module")
def pg_sql() -> str:
    return _PG_SQL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def mssql_sql() -> str:
    return _MSSQL_SQL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def model_ddl() -> str:
    return "\n".join(m.ALL_OPS_EVENT_STAGING_DDL)


# --- columns present in all three artifacts -------------------------------

@pytest.mark.parametrize("col", STAGING_COLUMNS)
def test_staging_columns_in_every_artifact(col, model_ddl, pg_sql, mssql_sql):
    assert col in model_ddl, f"{col} missing from model DDL"
    assert col in pg_sql, f"{col} missing from PostgreSQL artifact"
    assert col in mssql_sql, f"{col} missing from SQL Server artifact"


@pytest.mark.parametrize("col", BATCH_COLUMNS)
def test_batch_registry_columns_in_every_artifact(col, model_ddl, pg_sql, mssql_sql):
    assert col in model_ddl, f"{col} missing from model DDL"
    assert col in pg_sql, f"{col} missing from PostgreSQL artifact"
    assert col in mssql_sql, f"{col} missing from SQL Server artifact"


@pytest.mark.parametrize("obj", REQUIRED_OBJECTS)
def test_constraints_and_indexes_in_every_artifact(obj, model_ddl, pg_sql, mssql_sql):
    assert obj in model_ddl, f"{obj} missing from model DDL"
    assert obj in pg_sql, f"{obj} missing from PostgreSQL artifact"
    assert obj in mssql_sql, f"{obj} missing from SQL Server artifact"


# --- dialect-specific expectations ----------------------------------------

def test_postgres_uses_jsonb_and_identity(pg_sql):
    assert "JSONB" in pg_sql
    assert "GENERATED ALWAYS AS IDENTITY" in pg_sql


def test_sqlserver_uses_nvarchar_identity_and_json_check(mssql_sql):
    assert "IDENTITY(1,1)" in mssql_sql
    assert "NVARCHAR(MAX)" in mssql_sql
    assert "ISJSON(raw)" in mssql_sql
    # Both tables must exist in the SQL Server artifact.
    assert "ops_event_staging" in mssql_sql
    assert "ops_event_load_batches" in mssql_sql


def test_provider_column_is_open_not_check_constrained(model_ddl):
    # Open column (retrieval.py stance): no CHECK on provider — future providers
    # must load without a schema migration.
    assert "CHECK" not in model_ddl.split("provider", 1)[1].split(",", 1)[0]


# --- version stamped everywhere -------------------------------------------

def test_schema_version_stamped_in_all_artifacts(pg_sql, mssql_sql):
    v = m.STAGING_SCHEMA_VERSION
    assert v in pg_sql
    assert v in mssql_sql
    assert v in _DOC.read_text(encoding="utf-8")


def test_partner_doc_covers_the_load_contract():
    doc = _DOC.read_text(encoding="utf-8")
    # AC8: a partner engineer can apply the DDL and understand the contract.
    for needle in (
        "provider_event_id",   # idempotency key explained
        "row_id",              # checkpoint / incremental reads explained
        "org_id",              # org scoping explained
        "batch_id",            # batch identification explained
        "read-only",           # fail-closed read posture
        "psql",                # how to apply PostgreSQL
        "sqlcmd",              # how to apply SQL Server
    ):
        assert needle in doc, f"partner doc does not cover {needle!r}"


# --- record model validation ----------------------------------------------

def test_staging_row_requires_core_fields():
    with pytest.raises(ValueError):
        m.OpsEventStagingRow(
            org_id="",
            provider="aws",
            source_format="cloudtrail",
            batch_id="b1",
            provider_event_id="e1",
            raw={"k": "v"},
        )


def test_staging_row_requires_raw_payload():
    with pytest.raises(ValueError):
        m.OpsEventStagingRow(
            org_id="default",
            provider="aws",
            source_format="cloudtrail",
            batch_id="b1",
            provider_event_id="e1",
            raw=None,  # type: ignore[arg-type]
        )


def test_staging_row_accepts_valid_record():
    row = m.OpsEventStagingRow(
        org_id="default",
        provider="aws",
        source_format="cloudtrail",
        batch_id="aws:cloudtrail:2026-06",
        provider_event_id="EXAMPLE-EVENT-ID",
        raw={"eventID": "EXAMPLE-EVENT-ID"},
    )
    # Store-owned fields are unset on the load-side record.
    assert row.row_id is None
    assert row.loaded_at is None


def test_known_providers_and_formats_documented():
    assert m.KNOWN_PROVIDERS == {"aws", "azure"}
    assert "cloudtrail" in m.KNOWN_SOURCE_FORMATS
    assert "azure_activity_log" in m.KNOWN_SOURCE_FORMATS
