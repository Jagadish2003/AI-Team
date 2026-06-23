"""Contract tests for R16-A1 / AT-378 — ingestion_checkpoints repository.

Exercise the checkpoint persistence layer (``discovery/ingest/checkpoint_repository``)
against the real PostgreSQL ``ingestion_checkpoints`` table created by migration
0017, plus the write-only-on-full-success persistence rule (AC2) modelled the way
the runner (AT-379) will drive it.

Each test uses a unique org id and best-effort-cleans its rows so the shared
session DB is unaffected (the least-privilege app role may lack DELETE — unique
ids keep tests isolated regardless).
"""
from __future__ import annotations

import datetime
import uuid

import pytest

from app import db
from discovery.ingest.base import Checkpoint, DeltaBatch
from discovery.ingest import checkpoint_repository as repo


def _delete_org(org_id: str) -> None:
    con = db.connect()
    try:
        con.autocommit = True
        con.cursor().execute("DELETE FROM ingestion_checkpoints WHERE org_id = %s", (org_id,))
    except Exception:
        pass
    finally:
        con.close()


@pytest.fixture
def org_id():
    oid = f"cp_org_{uuid.uuid4().hex[:12]}"
    _delete_org(oid)
    yield oid
    _delete_org(oid)


def _run_and_checkpoint(org_id: str, connector_id: str, batches) -> None:
    """Model the runner's checkpoint lifecycle (AT-379) over a delta stream.

    Persists a checkpoint ONLY after the final batch (``is_complete=True``) is
    processed. If iterating ``batches`` raises before that, save is never reached
    and the prior checkpoint is left untouched — the write-only-on-full-success
    rule the repository is built to support.
    """
    for batch in batches:
        # (a real runner would process batch.records here)
        if batch.is_complete:
            repo.save_checkpoint(Checkpoint.create(connector_id, org_id, batch.next_checkpoint))


# --------------------------------------------------------------------------
# First run: no row yet.
# --------------------------------------------------------------------------
def test_first_run_returns_none(org_id):
    assert repo.read_checkpoint(org_id, "slack") is None


# --------------------------------------------------------------------------
# Save then read round-trips the opaque value (and metadata).
# --------------------------------------------------------------------------
def test_save_then_read_roundtrips(org_id):
    cp = Checkpoint(
        connector_id="slack",
        org_id=org_id,
        value="2026-06-01T00:00:00+00:00",
        captured_at="2026-06-23T10:00:00+00:00",
    )
    repo.save_checkpoint(cp)

    got = repo.read_checkpoint(org_id, "slack")
    assert got is not None
    assert got.connector_id == "slack"
    assert got.org_id == org_id
    assert got.value == "2026-06-01T00:00:00+00:00"  # opaque value preserved verbatim
    # captured_at survives the TIMESTAMPTZ round-trip as the same instant.
    assert datetime.datetime.fromisoformat(got.captured_at) == datetime.datetime.fromisoformat(cp.captured_at)


# --------------------------------------------------------------------------
# Second save upserts the same (org, connector) row — one row, advanced value.
# --------------------------------------------------------------------------
def test_save_is_upsert_not_duplicate(org_id):
    repo.save_checkpoint(Checkpoint.create("sqlserver", org_id, "seq-100"))
    repo.save_checkpoint(Checkpoint.create("sqlserver", org_id, "seq-250"))

    got = repo.read_checkpoint(org_id, "sqlserver")
    assert got.value == "seq-250"


# --------------------------------------------------------------------------
# AC2: a failed/partial run does NOT advance; a fully-consumed run does.
# --------------------------------------------------------------------------
def test_failure_mid_stream_leaves_checkpoint_unchanged(org_id):
    repo.save_checkpoint(Checkpoint.create("slack", org_id, "cp1"))

    def failing_stream():
        yield DeltaBatch(records=[{"id": 1}], next_checkpoint="cp-partial", is_complete=False)
        raise RuntimeError("source connection dropped mid-stream")

    with pytest.raises(RuntimeError):
        _run_and_checkpoint(org_id, "slack", failing_stream())

    # The prior checkpoint must be untouched — next run re-reads from cp1.
    assert repo.read_checkpoint(org_id, "slack").value == "cp1"


def test_full_success_advances_checkpoint(org_id):
    repo.save_checkpoint(Checkpoint.create("slack", org_id, "cp1"))

    def complete_stream():
        yield DeltaBatch(records=[{"id": 1}], next_checkpoint="cp-mid", is_complete=False)
        yield DeltaBatch(records=[], next_checkpoint="cp2", is_complete=True)

    _run_and_checkpoint(org_id, "slack", complete_stream())

    assert repo.read_checkpoint(org_id, "slack").value == "cp2"


# --------------------------------------------------------------------------
# AC5: the value is opaque — different connectors use different shapes, all
# round-trip verbatim through one storage path.
# --------------------------------------------------------------------------
def test_opaque_values_of_different_shapes_roundtrip(org_id):
    repo.save_checkpoint(Checkpoint.create("salesforce", org_id, "2026-06-20T12:34:56Z"))  # timestamp shape
    repo.save_checkpoint(Checkpoint.create("git", org_id, "9f2c1ab7e4d5"))                  # commit-SHA shape
    repo.save_checkpoint(Checkpoint.create("sqlserver", org_id, "0x00000000000A3F2D"))      # change-seq shape

    assert repo.read_checkpoint(org_id, "salesforce").value == "2026-06-20T12:34:56Z"
    assert repo.read_checkpoint(org_id, "git").value == "9f2c1ab7e4d5"
    assert repo.read_checkpoint(org_id, "sqlserver").value == "0x00000000000A3F2D"


# --------------------------------------------------------------------------
# Composite primary key: checkpoints are isolated per (org_id, connector_id).
# --------------------------------------------------------------------------
def test_checkpoints_isolated_per_connector(org_id):
    repo.save_checkpoint(Checkpoint.create("slack", org_id, "slack-pos"))
    repo.save_checkpoint(Checkpoint.create("jira", org_id, "jira-pos"))

    assert repo.read_checkpoint(org_id, "slack").value == "slack-pos"
    assert repo.read_checkpoint(org_id, "jira").value == "jira-pos"


def test_checkpoints_isolated_per_org(org_id):
    other_org = f"cp_org_{uuid.uuid4().hex[:12]}"
    _delete_org(other_org)
    try:
        repo.save_checkpoint(Checkpoint.create("slack", org_id, "pos-A"))
        repo.save_checkpoint(Checkpoint.create("slack", other_org, "pos-B"))

        assert repo.read_checkpoint(org_id, "slack").value == "pos-A"
        assert repo.read_checkpoint(other_org, "slack").value == "pos-B"
    finally:
        _delete_org(other_org)
