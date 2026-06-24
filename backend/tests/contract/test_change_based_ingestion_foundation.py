"""R16-A1 / AT-384 (T8) — full contract test suite for the Change-Based
Ingestion Foundation (Section 7, AC1–AC8).

This is the *consolidating* suite: it drives the REAL runner
(``discovery/ingest/change_runner``) end-to-end through realistic connectors —
exercising the checkpoint lifecycle, change-event emission, deletes, opacity,
reset-driven re-read, and the efficiency guarantee together — rather than in
isolation. The per-subtask unit suites remain the fine-grained coverage:

    AC1  test_change_based_ingestion_base.py            (+ here)
    AC2  test_change_runner.py / test_ingestion_checkpoints.py   (+ here)
    AC3  test_first_run_resumable.py                    (+ here)
    AC4  test_artifact_changed_events.py                (+ here)
    AC5  test_change_runner.py / base                   (+ here)
    AC6  test_delete_tombstone.py                       (+ here)
    AC7  test_ingestion_checkpoint_reset.py             (+ behavioural reset here)
    AC8  THIS FILE (the criterion assigned to AT-384)

Everything here is hermetic: the checkpoint store is an in-memory dict wired
through the runner's injectable read/save seam, and ``record_event`` is captured
via monkeypatch — no provisioned DB is required.
"""
from __future__ import annotations

import time

import pytest

from discovery.ingest.base import (
    CHANGE_KINDS,
    ChangeBasedIngestor,
    ChangeKind,
    Checkpoint,
    DeltaBatch,
    tombstone,
)
from discovery.ingest import change_runner

EVENT = "ingestion.artifact_changed"


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles: an in-memory source + a realistic connector built against the
# contract. The source uses a logical version clock; the connector encodes the
# max version it has seen as its OPAQUE checkpoint value (a stringified int).
# ─────────────────────────────────────────────────────────────────────────────
class InMemorySource:
    """A tiny change-tracking source: every mutation bumps a logical clock.

    Each artifact records the version at which it was first created and the
    version of its latest change, so ``changes_since`` can label a change
    'created' vs 'updated' relative to the caller's position. Deletions are kept
    as tombstones with their own version.
    """

    def __init__(self):
        self.clock = 0
        self.created_at: dict = {}  # artifact_id -> version of first create
        self.live: dict = {}        # artifact_id -> version of latest change
        self.tombstones: dict = {}  # artifact_id -> version of deletion

    def upsert(self, artifact_id: str) -> None:
        self.clock += 1
        self.created_at.setdefault(artifact_id, self.clock)
        self.live[artifact_id] = self.clock
        self.tombstones.pop(artifact_id, None)

    def delete(self, artifact_id: str) -> None:
        self.clock += 1
        self.tombstones[artifact_id] = self.clock
        self.live.pop(artifact_id, None)

    def changes_since(self, since_version: int):
        """Records with version > since_version, as (artifact_id, version, kind)."""
        out = []
        for aid, v in self.live.items():
            if v > since_version:
                kind = ChangeKind.CREATED if self.created_at[aid] > since_version else ChangeKind.UPDATED
                out.append((aid, v, kind))
        for aid, v in self.tombstones.items():
            if v > since_version:
                out.append((aid, v, ChangeKind.DELETED))
        return sorted(out, key=lambda r: r[1])


class VersionConnector(ChangeBasedIngestor):
    """Realistic connector: incremental by logical version, supports deletes.

    Opaque checkpoint value = the highest source version included so far, as a
    string. Streams in pages of ``page_size`` so a first-run full load arrives as
    multiple checkpointed batches (resumable). Instrumented so tests can measure
    the work each run actually performs (AC8).
    """

    connector_id = "versioned_source"
    reports_deletes = True  # this source natively reports deletions (tombstones)

    def __init__(self, source: InMemorySource, page_size: int = 5):
        self.source = source
        self.page_size = page_size
        # Instrumentation:
        self.runs = 0
        self.records_yielded = 0
        self.started_from_version = None

    def ingest_changes(self, org_id, since):
        self.runs += 1
        start_v = 0 if since is None else int(since.value)
        self.started_from_version = start_v
        changes = self.source.changes_since(start_v)

        if not changes:
            # Unchanged source → empty delta that echoes the incoming position
            # (cheap, no records). This is the steady-state fast path (AC1/AC8).
            echo = since.value if since is not None else "0"
            yield DeltaBatch(records=[], next_checkpoint=echo, is_complete=True)
            return

        for i in range(0, len(changes), self.page_size):
            page = changes[i : i + self.page_size]
            records = [
                {"artifact_id": aid, "change_kind": kind, "version": v}
                for (aid, v, kind) in page
            ]
            self.records_yielded += len(records)
            last_version = page[-1][1]
            is_last = (i + self.page_size) >= len(changes)
            yield DeltaBatch(
                records=records,
                next_checkpoint=str(last_version),
                is_complete=is_last,
            )


class ISOTimestampConnector(ChangeBasedIngestor):
    """A second connector whose opaque checkpoint shape is an ISO timestamp.

    Used only to prove the runner is shape-agnostic (AC5) alongside the integer
    VersionConnector.
    """

    connector_id = "iso_source"

    def __init__(self, records):
        self._records = records

    def ingest_changes(self, org_id, since):
        if since is None:
            changed = list(self._records)
        else:
            changed = [r for r in self._records if r["updated_at"] > since.value]
        nxt = max((r["updated_at"] for r in self._records), default=since.value if since else "")
        yield DeltaBatch(records=changed, next_checkpoint=nxt, is_complete=True)


# ─────────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.data: dict = {}
        self.saves = 0

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.saves += 1
        self.data[(cp.org_id, cp.connector_id)] = cp

    def reset(self, org_id, connector_id):
        """Model the admin checkpoint reset (AT-383): clear the row so the next
        read is a first run. Equivalent to repo.reset_checkpoint's observable
        effect (a subsequent read returns None)."""
        return self.data.pop((org_id, connector_id), None) is not None


@pytest.fixture
def captured(monkeypatch):
    events: list = []
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda etype, payload=None: events.append((etype, payload or {})),
    )
    return events


def _drive(connector, store, org="org-1", **kw):
    return change_runner.ingest_with_checkpoint(
        connector, org, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )


def _artifacts(captured):
    return [p for (e, p) in captured if e == EVENT]


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — only-changed-since-checkpoint; unchanged source returns empty delta.
# ─────────────────────────────────────────────────────────────────────────────
def test_ac1_returns_only_changed_and_empty_on_unchanged():
    src = InMemorySource()
    for i in range(3):
        src.upsert(f"a{i}")
    store = Store()
    conn = VersionConnector(src)

    # First run: all three.
    r1 = _drive(conn, store)
    assert r1.records == 3

    # No changes → empty delta.
    r2 = _drive(conn, store)
    assert r2.records == 0
    assert r2.ok and r2.complete

    # One new change → only that record.
    src.upsert("a3")
    r3 = _drive(conn, store)
    assert r3.records == 1


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — checkpoint written only on full success; mid-batch failure no-advance.
# ─────────────────────────────────────────────────────────────────────────────
def test_ac2_incremental_failure_leaves_prior_checkpoint():
    src = InMemorySource()
    src.upsert("seed")
    store = Store()
    conn = VersionConnector(src)
    _drive(conn, store)                       # establish a prior checkpoint
    prior = store.read("org-1", "versioned_source").value

    # Now add changes and fail during processing of the (incremental) delta.
    for i in range(3):
        src.upsert(f"x{i}")

    def boom(_batch):
        raise RuntimeError("downstream failed mid-batch")

    res = _drive(conn, store, process_batch=boom)
    assert res.ok is False
    assert res.checkpoint_advanced is False
    assert store.read("org-1", "versioned_source").value == prior   # unchanged


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — first-run streamed as checkpointed batches; failure resumes from N-1.
# ─────────────────────────────────────────────────────────────────────────────
def test_ac3_first_run_streams_and_is_resumable():
    src = InMemorySource()
    for i in range(12):
        src.upsert(f"a{i}")                   # 12 records, page_size 5 → 3 batches
    store = Store()

    r1 = _drive(VersionConnector(src, page_size=5), store)
    assert r1.first_run is True
    assert r1.batches >= 3                     # streamed, not one monolithic read
    assert r1.batches_checkpointed == r1.batches   # each batch checkpointed (AC3)
    assert r1.complete is True

    # Resume semantics are exercised in depth in test_first_run_resumable.py;
    # here we assert the streamed full load completed and advanced.
    assert store.read("org-1", "versioned_source") is not None


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — ingestion.artifact_changed emitted with required fields, all kinds.
# ─────────────────────────────────────────────────────────────────────────────
def test_ac4_events_emitted_with_required_fields(captured):
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert EVENT in REGISTERED_EVENT_TYPES     # event type registered

    src = InMemorySource()
    src.upsert("c1")
    store = Store()
    _drive(VersionConnector(src), store)

    arts = _artifacts(captured)
    assert len(arts) == 1
    a = arts[0]
    assert set(a) >= {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    assert a["org_id"] == "org-1"
    assert a["connector_id"] == "versioned_source"
    assert a["artifact_id"] == "c1"
    assert a["change_kind"] in CHANGE_KINDS


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — runner treats the checkpoint value as opaque across different shapes.
# ─────────────────────────────────────────────────────────────────────────────
def test_ac5_two_checkpoint_shapes_share_one_runner():
    store = Store()
    src = InMemorySource()
    src.upsert("v1")
    int_conn = VersionConnector(src)                       # value = "1" (int-ish)
    iso_conn = ISOTimestampConnector([
        {"artifact_id": "t1", "updated_at": "2026-06-01T00:00:00+00:00"},
    ])                                                     # value = ISO timestamp

    results = change_runner.run_ingestors(
        [int_conn, iso_conn], "org-1",
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    assert all(r.checkpoint_advanced for r in results)
    assert store.read("org-1", "versioned_source").value == "1"
    assert store.read("org-1", "iso_source").value == "2026-06-01T00:00:00+00:00"


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — deletes propagate as change_kind='deleted'; gap declared on unsupported.
# ─────────────────────────────────────────────────────────────────────────────
def test_ac6_deletion_propagates_as_deleted_event(captured):
    src = InMemorySource()
    src.upsert("keep")
    src.upsert("remove-me")
    store = Store()
    conn = VersionConnector(src)
    _drive(conn, store)                        # initial load (created events)
    captured.clear()

    src.delete("remove-me")                    # source reports a deletion
    _drive(conn, store)

    kinds = {a["artifact_id"]: a["change_kind"] for a in _artifacts(captured)}
    assert kinds.get("remove-me") == ChangeKind.DELETED
    assert conn.reports_deletes is True


def test_ac6_unsupported_source_declares_limitation():
    # A connector that cannot detect deletes leaves the flag False (the default)
    # and documents it — the foundation never silently pretends.
    assert ChangeBasedIngestor.reports_deletes is False
    assert tombstone("p1")["change_kind"] == ChangeKind.DELETED  # vocabulary present


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — a checkpoint reset forces a full re-read on the next run.
# (The route's audit+telemetry recording is covered by
#  test_ingestion_checkpoint_reset.py; here we prove the behavioural contract.)
# ─────────────────────────────────────────────────────────────────────────────
def test_ac7_reset_forces_full_reread():
    src = InMemorySource()
    for i in range(4):
        src.upsert(f"a{i}")
    store = Store()
    conn = VersionConnector(src)

    _drive(conn, store)                        # first run loads 4
    r_inc = _drive(conn, store)                # unchanged → 0
    assert r_inc.records == 0
    assert r_inc.first_run is False

    # Admin reset clears the checkpoint.
    assert store.reset("org-1", "versioned_source") is True

    # Next run is a first run again → full re-read of all 4 records.
    r_after = _drive(conn, store)
    assert r_after.first_run is True
    assert r_after.records == 4                 # full re-read forced (AC7)


# ─────────────────────────────────────────────────────────────────────────────
# AC8 — unchanged re-run is substantially faster than the initial load AND
#       produces no duplicate ingestion.  (The criterion assigned to AT-384.)
# ─────────────────────────────────────────────────────────────────────────────
def test_ac8_unchanged_rerun_does_far_less_work():
    src = InMemorySource()
    N = 50
    for i in range(N):
        src.upsert(f"a{i}")
    store = Store()
    conn = VersionConnector(src, page_size=10)

    initial = _drive(conn, store)
    assert initial.records == N                 # initial load processes everything
    assert conn.records_yielded == N

    yielded_before = conn.records_yielded
    rerun = _drive(conn, store)
    assert rerun.records == 0                    # empty delta
    # The connector did NO record work on the unchanged re-run — the cost is
    # O(changes), not O(corpus): the essence of "substantially faster" (AC8).
    assert conn.records_yielded == yielded_before


def test_ac8_unchanged_rerun_is_measurably_faster_wallclock():
    """Wall-clock proof: with a per-record processing cost, the unchanged re-run
    is dramatically faster than the initial full load."""
    src = InMemorySource()
    N = 40
    for i in range(N):
        src.upsert(f"a{i}")
    store = Store()
    conn = VersionConnector(src, page_size=10)

    def costly(_batch):
        # Simulated downstream per-record cost (extraction/embedding stand-in).
        time.sleep(0.001 * len(_batch.records))

    t0 = time.perf_counter()
    _drive(conn, store, process_batch=costly)
    initial_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    _drive(conn, store, process_batch=costly)
    rerun_s = time.perf_counter() - t1

    # Initial load paid the per-record cost N times; the unchanged re-run pays it
    # zero times. Generous bound to stay CI-stable while still proving the point.
    assert rerun_s < initial_s * 0.5


def test_ac8_no_duplicate_ingestion_across_repeat_runs(captured):
    src = InMemorySource()
    for i in range(5):
        src.upsert(f"a{i}")
    store = Store()
    conn = VersionConnector(src)

    processed: list = []

    def collect(batch):
        processed.extend(r["artifact_id"] for r in batch.records)

    # Initial load + three unchanged re-runs.
    _drive(conn, store, process_batch=collect)
    for _ in range(3):
        _drive(conn, store, process_batch=collect)

    # Each artifact ingested exactly once across all runs — no duplicates.
    assert sorted(processed) == [f"a{i}" for i in range(5)]
    assert len(processed) == len(set(processed))
    # And exactly one artifact_changed event per artifact (no re-emission).
    assert len(_artifacts(captured)) == 5


def test_ac8_only_changed_record_reingested_after_a_single_change(captured):
    src = InMemorySource()
    for i in range(10):
        src.upsert(f"a{i}")
    store = Store()
    conn = VersionConnector(src)
    _drive(conn, store)                          # initial load of 10
    captured.clear()

    src.upsert("a3")                             # exactly one record changes

    processed: list = []
    _drive(conn, store, process_batch=lambda b: processed.extend(r["artifact_id"] for r in b.records))

    assert processed == ["a3"]                   # only the changed record, not all 10
    assert [a["artifact_id"] for a in _artifacts(captured)] == ["a3"]


# ─────────────────────────────────────────────────────────────────────────────
# Coverage manifest — AC1–AC7 each have automated coverage (AT-384 AC8 clause).
# A light guard that the sibling suites exist and remain the per-AC homes.
# ─────────────────────────────────────────────────────────────────────────────
def test_all_acceptance_criteria_have_automated_coverage():
    import pathlib

    here = pathlib.Path(__file__).parent
    discovery_tests = here.parent.parent / "discovery" / "tests"
    expected = {
        "AC1/AC5": discovery_tests / "test_change_based_ingestion_base.py",
        "AC2": here / "test_change_runner.py",
        "AC2-repo": here / "test_ingestion_checkpoints.py",
        "AC3": here / "test_first_run_resumable.py",
        "AC4": here / "test_artifact_changed_events.py",
        "AC6": here / "test_delete_tombstone.py",
        "AC7": here / "test_ingestion_checkpoint_reset.py",
    }
    missing = {ac: str(p) for ac, p in expected.items() if not p.exists()}
    assert not missing, f"missing per-AC coverage files: {missing}"
