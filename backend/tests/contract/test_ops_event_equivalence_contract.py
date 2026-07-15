"""Real-DB bridge <-> native equivalence — MSP-B8 / T6 (AC2).

Runs the equivalence harness with the golden raw payloads STAGED in the actual
``ops_event_staging`` table and drained through the bridge on the read-only
:class:`DbStagingReader` — the true "staged database bridge ingestion" path —
then compares field by field against direct mapper invocation. Proves AWS and
Azure golden fixtures are detector-equivalent across both paths through the real
database, with only ``source_system`` differing and stable evidence resolution.
"""
import pytest

from discovery.ingest.ops_event_equivalence import (
    all_passed,
    format_report,
    load_golden_cases,
    run_equivalence,
)
from discovery.ingest.ops_event_staging_store import DbStagingReader, DbStagingSink


@pytest.fixture()
def conn():
    import sqlite3  # conftest routes this to PostgreSQL

    connection = sqlite3.connect("")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def results():
    return run_equivalence(
        load_golden_cases(),
        org_id="org_equiv_ac2",
        sink=DbStagingSink(),
        reader=DbStagingReader(),
    )


def test_all_golden_cases_equivalent_through_real_db(results):
    assert all_passed(results), format_report(results)


def test_both_providers_covered(results):
    providers = {r.provider for r in results}
    assert providers == {"aws", "azure"}


def test_only_source_system_differs_per_case(results):
    for r in results:
        assert r.bridge_record_found, r.message
        assert r.unexpected_diffs == [], format_report([r])
        assert r.source_system_bridge == f"bridge:{r.provider}"


def test_evidence_resolves_stably_through_real_db(results):
    for r in results:
        assert r.evidence_stable, r.message


def test_staged_rows_actually_landed_in_the_database(conn, results):
    # The bridge path genuinely went through the DB: the golden rows are present.
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM ops_event_staging WHERE org_id = %s",
        ("org_equiv_ac2",),
    ).fetchall()[0]["n"]
    assert n == len(load_golden_cases())
