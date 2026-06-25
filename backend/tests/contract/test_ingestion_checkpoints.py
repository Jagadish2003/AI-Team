"""Contract tests for R16-A1 / AT-378 — ingestion_checkpoints repository.

These verify the checkpoint persistence LOGIC (`discovery/ingest/checkpoint_repository`)
and the write-only-on-full-success rule (AC2), the way the runner (AT-379) will
drive it.

Storage is exercised through an in-memory fake that stands in for the
``ingestion_checkpoints`` table — it implements exactly the two statements the
repository issues (the upsert and the keyed SELECT). This keeps the tests
hermetic: they run anywhere without a provisioned table or DB privileges, while
still exercising the real repository code. The physical table/DDL itself is
created and validated by the migration (0017) when CI runs ``alembic upgrade head``.
"""
from __future__ import annotations

import datetime

import pytest

from discovery.ingest.base import Checkpoint, DeltaBatch
from discovery.ingest import checkpoint_repository as repo


# --------------------------------------------------------------------------
# In-memory stand-in for the ingestion_checkpoints table.
# Models only what the repository's two queries need: an upsert keyed by
# (org_id, connector_id), and a keyed read of (value, captured_at).
# --------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, store: dict):
        self._store = store
        self._result = None

    def execute(self, sql: str, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("INSERT INTO ingestion_checkpoints"):
            org_id, connector_id, value, captured_at = params
            # dict assignment == ON CONFLICT DO UPDATE (one row per key).
            self._store[(org_id, connector_id)] = (value, captured_at)
            self._result = None
        elif s.startswith("SELECT value, captured_at FROM ingestion_checkpoints"):
            org_id, connector_id = params
            self._result = self._store.get((org_id, connector_id))  # tuple or None
        else:  # pragma: no cover - repository issues no other statements
            self._result = None

    def fetchone(self):
        return self._result

    def close(self):
        pass


class _FakeConn:
    def __init__(self, store: dict):
        self._store = store
        self.autocommit = False

    def cursor(self):
        return _FakeCursor(self._store)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def store(monkeypatch):
    """A fresh in-memory checkpoint store wired into the repository per test."""
    data: dict = {}
    monkeypatch.setattr(repo, "_connect", lambda: _FakeConn(data))
    return data


def _run_and_checkpoint(org_id: str, connector_id: str, batches) -> None:
    """Model the runner's checkpoint lifecycle (AT-379) over a delta stream.

    Persists a checkpoint ONLY after the final batch (``is_complete=True``) is
    processed. If iterating ``batches`` raises before that, save is never reached
    and the prior checkpoint is left untouched — the write-only-on-full-success
    rule the repository is built to support (AC2).
    """
    for batch in batches:
        if batch.is_complete:
            repo.save_checkpoint(Checkpoint.create(connector_id, org_id, batch.next_checkpoint))


# --------------------------------------------------------------------------
# First run: no row yet.
# --------------------------------------------------------------------------
def test_first_run_returns_none(store):
    assert repo.read_checkpoint("org-1", "slack") is None


# --------------------------------------------------------------------------
# Save then read round-trips the opaque value (and metadata).
# --------------------------------------------------------------------------
def test_save_then_read_roundtrips(store):
    cp = Checkpoint(
        connector_id="slack",
        org_id="org-1",
        value="2026-06-01T00:00:00+00:00",
        captured_at="2026-06-23T10:00:00+00:00",
    )
    repo.save_checkpoint(cp)

    got = repo.read_checkpoint("org-1", "slack")
    assert got is not None
    assert got.connector_id == "slack"
    assert got.org_id == "org-1"
    assert got.value == "2026-06-01T00:00:00+00:00"  # opaque value preserved verbatim
    assert datetime.datetime.fromisoformat(got.captured_at) == datetime.datetime.fromisoformat(cp.captured_at)


# --------------------------------------------------------------------------
# Second save upserts the same (org, connector) row — one row, advanced value.
# --------------------------------------------------------------------------
def test_save_is_upsert_not_duplicate(store):
    repo.save_checkpoint(Checkpoint.create("sqlserver", "org-1", "seq-100"))
    repo.save_checkpoint(Checkpoint.create("sqlserver", "org-1", "seq-250"))

    assert repo.read_checkpoint("org-1", "sqlserver").value == "seq-250"
    assert len(store) == 1  # upsert, not a duplicate row


# --------------------------------------------------------------------------
# AC2: a failed/partial run does NOT advance; a fully-consumed run does.
# --------------------------------------------------------------------------
def test_failure_mid_stream_leaves_checkpoint_unchanged(store):
    repo.save_checkpoint(Checkpoint.create("slack", "org-1", "cp1"))

    def failing_stream():
        yield DeltaBatch(records=[{"id": 1}], next_checkpoint="cp-partial", is_complete=False)
        raise RuntimeError("source connection dropped mid-stream")

    with pytest.raises(RuntimeError):
        _run_and_checkpoint("org-1", "slack", failing_stream())

    # The prior checkpoint must be untouched — next run re-reads from cp1.
    assert repo.read_checkpoint("org-1", "slack").value == "cp1"


def test_full_success_advances_checkpoint(store):
    repo.save_checkpoint(Checkpoint.create("slack", "org-1", "cp1"))

    def complete_stream():
        yield DeltaBatch(records=[{"id": 1}], next_checkpoint="cp-mid", is_complete=False)
        yield DeltaBatch(records=[], next_checkpoint="cp2", is_complete=True)

    _run_and_checkpoint("org-1", "slack", complete_stream())

    assert repo.read_checkpoint("org-1", "slack").value == "cp2"


# --------------------------------------------------------------------------
# AC5: the value is opaque — different connectors use different shapes, all
# round-trip verbatim through one storage path.
# --------------------------------------------------------------------------
def test_opaque_values_of_different_shapes_roundtrip(store):
    repo.save_checkpoint(Checkpoint.create("salesforce", "org-1", "2026-06-20T12:34:56Z"))  # timestamp
    repo.save_checkpoint(Checkpoint.create("git", "org-1", "9f2c1ab7e4d5"))                 # commit SHA
    repo.save_checkpoint(Checkpoint.create("sqlserver", "org-1", "0x00000000000A3F2D"))     # change seq

    assert repo.read_checkpoint("org-1", "salesforce").value == "2026-06-20T12:34:56Z"
    assert repo.read_checkpoint("org-1", "git").value == "9f2c1ab7e4d5"
    assert repo.read_checkpoint("org-1", "sqlserver").value == "0x00000000000A3F2D"


# --------------------------------------------------------------------------
# Composite primary key: checkpoints are isolated per (org_id, connector_id).
# --------------------------------------------------------------------------
def test_checkpoints_isolated_per_connector(store):
    repo.save_checkpoint(Checkpoint.create("slack", "org-1", "slack-pos"))
    repo.save_checkpoint(Checkpoint.create("jira", "org-1", "jira-pos"))

    assert repo.read_checkpoint("org-1", "slack").value == "slack-pos"
    assert repo.read_checkpoint("org-1", "jira").value == "jira-pos"


def test_checkpoints_isolated_per_org(store):
    repo.save_checkpoint(Checkpoint.create("slack", "org-1", "pos-A"))
    repo.save_checkpoint(Checkpoint.create("slack", "org-2", "pos-B"))

    assert repo.read_checkpoint("org-1", "slack").value == "pos-A"
    assert repo.read_checkpoint("org-2", "slack").value == "pos-B"
