"""Contract tests for R16-A1 / AT-379 — runner integration / checkpoint lifecycle.

Verifies the runner driver in ``discovery/ingest/change_runner.py`` against the
acceptance criteria assigned to this subtask:

  AC2 — The runner persists a connector's next_checkpoint ONLY after the full
        delta is processed. A simulated failure mid-batch leaves the prior
        checkpoint unchanged, and the next run re-reads from it.
  AC5 — The checkpoint value is opaque to the runner — no runner code interprets
        its contents. Verified by two connectors using different checkpoint
        shapes (an ISO timestamp and a sequence id) driven by the same runner.

The checkpoint store is an in-memory dict wired in through the runner's injectable
read/save seam, so the tests exercise the real runner logic hermetically. One
test additionally drives through the actual AT-378 repository (with a fake DB
connection) to prove the runner + persistence layer integrate.
"""
from __future__ import annotations

import pytest

from discovery.ingest.base import Checkpoint, DeltaBatch, ChangeBasedIngestor
from discovery.ingest import change_runner
from discovery.ingest import checkpoint_repository as repo


# ─────────────────────────────────────────────────────────────────────────────
# In-memory checkpoint store wired through the runner's injectable seam.
# ─────────────────────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


# ─────────────────────────────────────────────────────────────────────────────
# Two connectors with DIFFERENT opaque checkpoint shapes (AC5).
# Each records the `since` it was handed so we can assert read-before-run.
# ─────────────────────────────────────────────────────────────────────────────
class TimestampConnector(ChangeBasedIngestor):
    """Opaque value is an ISO timestamp (Salesforce SystemModstamp-style)."""

    connector_id = "ts_source"

    def __init__(self, records):
        self._records = records
        self.seen_since = "UNSET"

    def ingest_changes(self, org_id, since):
        self.seen_since = since
        if since is None:
            changed = list(self._records)
        else:
            changed = [r for r in self._records if r["updated_at"] > since.value]
        max_ts = max((r["updated_at"] for r in self._records), default="")
        nxt = max_ts or (since.value if since else "")
        yield DeltaBatch(records=changed, next_checkpoint=nxt, is_complete=True)


class SequenceConnector(ChangeBasedIngestor):
    """Opaque value is a stringified change-sequence id (DB change-tracking-style)."""

    connector_id = "seq_source"

    def __init__(self, records):
        self._records = records
        self.seen_since = "UNSET"

    def ingest_changes(self, org_id, since):
        self.seen_since = since
        if since is None:
            changed = list(self._records)
        else:
            cutoff = int(since.value)
            changed = [r for r in self._records if r["seq"] > cutoff]
        max_seq = max((r["seq"] for r in self._records), default=0)
        nxt = str(max_seq) if self._records else (since.value if since else "0")
        yield DeltaBatch(records=changed, next_checkpoint=nxt, is_complete=True)


class PagedConnector(ChangeBasedIngestor):
    """Yields several pages; only the final batch is is_complete=True."""

    connector_id = "paged_source"

    def __init__(self, pages, fail_at=None):
        # pages: list of (records, next_checkpoint, is_complete)
        self._pages = pages
        self._fail_at = fail_at  # 0-based page index to raise at, or None

    def ingest_changes(self, org_id, since):
        for i, (records, nxt, complete) in enumerate(self._pages):
            if self._fail_at is not None and i == self._fail_at:
                raise RuntimeError("source connection dropped mid-stream")
            yield DeltaBatch(records=records, next_checkpoint=nxt, is_complete=complete)


def _drive(ingestor, org_id, store, **kw):
    return change_runner.ingest_with_checkpoint(
        ingestor, org_id,
        read_checkpoint=store.read, save_checkpoint=store.save, **kw,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Read-before-run + write-only-on-full-success (the happy path).
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_reads_none_then_advances():
    store = Store()
    conn = TimestampConnector([
        {"id": "a", "updated_at": "2026-06-01T00:00:00+00:00"},
        {"id": "b", "updated_at": "2026-06-10T00:00:00+00:00"},
    ])
    res = _drive(conn, "org-1", store)

    assert conn.seen_since is None                     # read-before-run: first run → None
    assert res.records == 2 and res.batches == 1
    assert res.checkpoint_advanced is True
    assert res.new_checkpoint.value == "2026-06-10T00:00:00+00:00"
    # persisted for the next run
    assert store.read("org-1", "ts_source").value == "2026-06-10T00:00:00+00:00"


def test_second_run_passes_prior_checkpoint_as_since():
    store = Store()
    conn = TimestampConnector([
        {"id": "a", "updated_at": "2026-06-01T00:00:00+00:00"},
        {"id": "b", "updated_at": "2026-06-10T00:00:00+00:00"},
    ])
    _drive(conn, "org-1", store)                       # first run advances to 06-10

    conn2 = TimestampConnector([
        {"id": "a", "updated_at": "2026-06-01T00:00:00+00:00"},
        {"id": "b", "updated_at": "2026-06-10T00:00:00+00:00"},
        {"id": "c", "updated_at": "2026-06-20T00:00:00+00:00"},
    ])
    res = _drive(conn2, "org-1", store)

    # The runner read the persisted checkpoint and handed it back as `since`.
    assert conn2.seen_since is not None
    assert conn2.seen_since.value == "2026-06-10T00:00:00+00:00"
    assert res.records == 1                             # only the new record 'c'
    assert store.read("org-1", "ts_source").value == "2026-06-20T00:00:00+00:00"


def test_multi_page_advances_only_to_terminal_checkpoint():
    store = Store()
    conn = PagedConnector(pages=[
        ([{"id": 1}], "cp-mid", False),                 # not complete
        ([{"id": 2}], "cp-final", True),                # terminal
    ])
    res = _drive(conn, "org-1", store)
    assert res.batches == 2 and res.records == 2
    assert res.checkpoint_advanced is True
    assert store.read("org-1", "paged_source").value == "cp-final"  # terminal, not cp-mid


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — failure mid-stream leaves the prior checkpoint unchanged.
# ─────────────────────────────────────────────────────────────────────────────
def test_ac2_stream_failure_midbatch_does_not_advance():
    store = Store()
    store.save(Checkpoint.create("paged_source", "org-1", "cp1"))   # known-good prior

    conn = PagedConnector(
        pages=[([{"id": 1}], "cp-partial", False), ([{"id": 2}], "cp-final", True)],
        fail_at=1,                                                  # blow up on page 2
    )
    res = _drive(conn, "org-1", store)

    assert res.ok is False and isinstance(res.error, RuntimeError)
    assert res.checkpoint_advanced is False
    # prior checkpoint untouched → next run re-reads from cp1
    assert store.read("org-1", "paged_source").value == "cp1"


def test_ac2_process_batch_failure_does_not_advance():
    store = Store()
    store.save(Checkpoint.create("ts_source", "org-1", "2026-01-01T00:00:00+00:00"))

    conn = TimestampConnector([{"id": "a", "updated_at": "2026-06-10T00:00:00+00:00"}])

    def boom(_batch):
        raise ValueError("downstream processing failed")

    res = _drive(conn, "org-1", store, process_batch=boom)

    assert res.ok is False and isinstance(res.error, ValueError)
    assert res.checkpoint_advanced is False
    assert store.read("org-1", "ts_source").value == "2026-01-01T00:00:00+00:00"


def test_ac2_next_run_after_failure_rereads_and_then_succeeds():
    store = Store()
    store.save(Checkpoint.create("paged_source", "org-1", "cp1"))

    # Run 1 fails mid-stream → no advance.
    failing = PagedConnector(
        pages=[([{"id": 1}], "cp-partial", False), ([{"id": 2}], "cp-final", True)],
        fail_at=1,
    )
    _drive(failing, "org-1", store)
    assert store.read("org-1", "paged_source").value == "cp1"

    # Run 2 (source recovered) re-reads cp1 and completes → advances.
    recovered = PagedConnector(pages=[([{"id": 1}, {"id": 2}], "cp-final", True)])
    res = _drive(recovered, "org-1", store)
    assert res.checkpoint_advanced is True
    assert store.read("org-1", "paged_source").value == "cp-final"


def test_partial_stream_without_terminal_batch_does_not_advance():
    store = Store()
    store.save(Checkpoint.create("paged_source", "org-1", "cp1"))
    # Stream ends without ever reporting is_complete=True (e.g. generator exhausted early).
    conn = PagedConnector(pages=[([{"id": 1}], "cp-mid", False)])
    res = _drive(conn, "org-1", store)
    assert res.ok is True                       # no exception, but…
    assert res.checkpoint_advanced is False     # …no terminal batch → no advance
    assert store.read("org-1", "paged_source").value == "cp1"


# ─────────────────────────────────────────────────────────────────────────────
# AC1-adjacent: an unchanged source leaves the checkpoint unchanged.
# ─────────────────────────────────────────────────────────────────────────────
def test_unchanged_source_does_not_regress_checkpoint():
    store = Store()
    conn = SequenceConnector([{"id": "x", "seq": 5}])
    _drive(conn, "org-1", store)                        # advance to "5"
    assert store.read("org-1", "seq_source").value == "5"

    # Re-run with no new records: empty delta, checkpoint echoes the same value.
    conn2 = SequenceConnector([{"id": "x", "seq": 5}])
    res = _drive(conn2, "org-1", store)
    assert res.records == 0
    assert store.read("org-1", "seq_source").value == "5"   # no regression


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — the runner treats the checkpoint value as opaque across shapes.
# ─────────────────────────────────────────────────────────────────────────────
def test_ac5_same_runner_drives_two_different_checkpoint_shapes():
    store = Store()
    ts = TimestampConnector([
        {"id": "a", "updated_at": "2026-06-01T00:00:00+00:00"},
        {"id": "b", "updated_at": "2026-06-02T00:00:00+00:00"},
    ])
    seq = SequenceConnector([{"id": "x", "seq": 10}, {"id": "y", "seq": 11}])

    results = change_runner.run_ingestors(
        [ts, seq], "org-1",
        read_checkpoint=store.read, save_checkpoint=store.save,
    )

    assert all(r.checkpoint_advanced for r in results)
    # Two entirely different opaque value shapes persisted by the same code path.
    assert store.read("org-1", "ts_source").value == "2026-06-02T00:00:00+00:00"
    assert store.read("org-1", "seq_source").value == "11"


def test_ac5_runner_never_parses_value_even_when_unparseable():
    """A value the runner would choke on IF it interpreted it must round-trip fine.

    The sequence connector owns int-parsing of its value; the runner must not.
    We seed a checkpoint whose value is a stringified int and confirm the runner
    persists/echoes a brand-new opaque value with no inspection of either.
    """
    store = Store()
    # Opaque value containing characters that would break any numeric/date parse.
    weird = "::opaque::{not-a-date}{not-an-int}::"
    conn = PagedConnector(pages=[([{"id": 1}], weird, True)])
    res = _drive(conn, "org-1", store)
    assert res.checkpoint_advanced is True
    assert store.read("org-1", "paged_source").value == weird   # verbatim, uninterpreted


# ─────────────────────────────────────────────────────────────────────────────
# run_ingestors isolation: one connector failing must not stop the others.
# ─────────────────────────────────────────────────────────────────────────────
def test_run_ingestors_isolates_failures():
    store = Store()
    store.save(Checkpoint.create("paged_source", "org-1", "cp1"))

    good = TimestampConnector([{"id": "a", "updated_at": "2026-06-10T00:00:00+00:00"}])
    bad = PagedConnector(pages=[([{"id": 1}], "x", False), ([{"id": 2}], "y", True)], fail_at=1)

    results = change_runner.run_ingestors(
        [bad, good], "org-1",
        read_checkpoint=store.read, save_checkpoint=store.save,
    )

    by_id = {r.connector_id: r for r in results}
    # The bad connector failed and did NOT advance.
    assert by_id["paged_source"].ok is False
    assert store.read("org-1", "paged_source").value == "cp1"
    # The good connector still ran and advanced.
    assert by_id["ts_source"].ok is True
    assert store.read("org-1", "ts_source").value == "2026-06-10T00:00:00+00:00"


# ─────────────────────────────────────────────────────────────────────────────
# Integration with the real AT-378 repository (fake DB connection).
# ─────────────────────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, store):
        self._store = store
        self._result = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("INSERT INTO ingestion_checkpoints"):
            org_id, connector_id, value, captured_at = params
            self._store[(org_id, connector_id)] = (value, captured_at)
        elif s.startswith("SELECT value, captured_at FROM ingestion_checkpoints"):
            org_id, connector_id = params
            self._result = self._store.get((org_id, connector_id))

    def fetchone(self):
        return self._result

    def close(self):
        pass


class _FakeConn:
    def __init__(self, store):
        self._store = store

    def cursor(self):
        return _FakeCursor(self._store)

    def commit(self):
        pass

    def close(self):
        pass


def test_runner_integrates_with_real_repository(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(repo, "_connect", lambda: _FakeConn(data))

    conn = SequenceConnector([{"id": "x", "seq": 1}, {"id": "y", "seq": 2}])
    # Use repository defaults (no injected read/save) → exercises AT-378 code.
    res = change_runner.ingest_with_checkpoint(conn, "org-1")

    assert res.checkpoint_advanced is True
    assert repo.read_checkpoint("org-1", "seq_source").value == "2"
