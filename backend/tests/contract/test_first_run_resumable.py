"""Contract tests for R16-A1 / AT-380 — first-run streamed full load, resumable.

Verifies the acceptance criterion assigned to this subtask:

  AC3 — First run (no checkpoint) performs an initial load STREAMED as
        checkpointed batches. A failure at batch N resumes from batch N-1 on the
        next run, not from the start.

The driver under test is ``discovery/ingest/change_runner``. First-run mode is
selected purely by the ABSENCE of a prior checkpoint (``since is None``) — never
by inspecting the checkpoint value (AC5 stays intact). The complementary
incremental write-only-on-full-success behaviour (AC2) is guarded here too, to
prove the two modes do not bleed into each other.
"""
from __future__ import annotations

from discovery.ingest.base import Checkpoint, DeltaBatch, ChangeBasedIngestor
from discovery.ingest import change_runner


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


class ResumableFullLoadConnector(ChangeBasedIngestor):
    """Models a large source whose full load streams as ``total`` batches.

    The opaque checkpoint value is the count of fully-emitted batches ("offset").
    On resume the connector reads that offset back and continues from it — the
    connector owns interpreting its own value; the runner never does.
    """

    connector_id = "bigsource"

    def __init__(self, total_batches, fail_at=None):
        self.total = total_batches
        self.fail_at = fail_at          # 1-based batch number to fail at, or None
        self.start_offset_seen = None   # records where the run actually started

    def ingest_changes(self, org_id, since):
        start = 0 if since is None else int(since.value)
        self.start_offset_seen = start
        for i in range(start, self.total):
            batch_no = i + 1
            if self.fail_at is not None and batch_no == self.fail_at:
                raise RuntimeError(f"source dropped at batch {batch_no}")
            yield DeltaBatch(
                records=[{"id": f"r{batch_no}"}],
                next_checkpoint=str(batch_no),       # opaque offset
                is_complete=(batch_no == self.total),
            )


def _drive(ingestor, org_id, store, **kw):
    return change_runner.ingest_with_checkpoint(
        ingestor, org_id,
        read_checkpoint=store.read, save_checkpoint=store.save, **kw,
    )


# ─────────────────────────────────────────────────────────────────────────────
# First run streams as checkpointed batches (not one monolithic write).
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_checkpoints_each_batch():
    store = Store()
    conn = ResumableFullLoadConnector(total_batches=4)
    res = _drive(conn, "org-1", store)

    assert res.first_run is True
    assert conn.start_offset_seen == 0            # started from scratch
    assert res.batches == 4 and res.records == 4
    assert res.batches_checkpointed == 4          # one checkpoint per batch
    assert res.complete is True
    assert res.checkpoint_advanced is True
    assert store.read("org-1", "bigsource").value == "4"   # terminal position


def test_first_run_writes_intermediate_checkpoints_progressively():
    """Each batch's checkpoint is persisted as it completes — not just at the end."""
    store = Store()
    seen_after_each = []

    conn = ResumableFullLoadConnector(total_batches=3)

    def record_progress(_batch):
        # Snapshot the persisted checkpoint right before this batch's own write.
        cp = store.read("org-1", "bigsource")
        seen_after_each.append(cp.value if cp else None)

    _drive(conn, "org-1", store, process_batch=record_progress)
    # Before batch1 write: None; before batch2: "1"; before batch3: "2".
    assert seen_after_each == [None, "1", "2"]
    assert store.read("org-1", "bigsource").value == "3"


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — failure at batch N resumes from batch N-1, not from the start.
# ─────────────────────────────────────────────────────────────────────────────
def test_ac3_failure_at_batch_n_resumes_from_n_minus_1():
    store = Store()

    # First run: 5 batches, fails at batch 3.
    run1 = ResumableFullLoadConnector(total_batches=5, fail_at=3)
    res1 = _drive(run1, "org-1", store)

    assert run1.start_offset_seen == 0            # first run started at the beginning
    assert res1.ok is False                       # it failed…
    assert res1.complete is False
    assert res1.checkpoint_advanced is True       # …but batches 1 & 2 were checkpointed
    assert res1.batches_checkpointed == 2
    assert store.read("org-1", "bigsource").value == "2"   # resume point = batch N-1

    # Next run: source recovered. Must RESUME from batch 2, not restart at 0.
    run2 = ResumableFullLoadConnector(total_batches=5)
    res2 = _drive(run2, "org-1", store)

    assert run2.start_offset_seen == 2            # resumed from batch 2 (AC3)
    assert res2.records == 3                       # only batches 3,4,5 — no re-ingest of 1,2
    assert res2.complete is True
    assert store.read("org-1", "bigsource").value == "5"


def test_repeated_interruptions_never_regress_and_eventually_complete():
    """Repeated interruptions are SAFE: the resume point never regresses, no data
    is skipped, and the load eventually completes.

    Note the boundary (documented in change_runner): once the first-run load has
    written any checkpoint, the next run sees a checkpoint and is treated as
    incremental (all-or-nothing, AC2). So a second failure mid-resume does not
    persist that run's partial progress — but the connector still resumes from the
    last good checkpoint, so correctness (no skip, eventual completion) holds.
    """
    store = Store()

    # First run (no checkpoint) fails at batch 3 → per-batch writes saved 1 & 2.
    _drive(ResumableFullLoadConnector(total_batches=6, fail_at=3), "org-1", store)
    assert store.read("org-1", "bigsource").value == "2"

    # Second run resumes from 2 (incremental) and fails at 5: all-or-nothing, so
    # the checkpoint does NOT regress and does not skip anything — stays at 2.
    r2 = ResumableFullLoadConnector(total_batches=6, fail_at=5)
    _drive(r2, "org-1", store)
    assert r2.start_offset_seen == 2                       # resumed, not restarted
    assert store.read("org-1", "bigsource").value == "2"  # no regression, no skip

    # Third run resumes from 2 and completes — the load finishes, nothing skipped.
    r3 = ResumableFullLoadConnector(total_batches=6)
    res3 = _drive(r3, "org-1", store)
    assert r3.start_offset_seen == 2
    assert res3.complete is True
    assert store.read("org-1", "bigsource").value == "6"


# ─────────────────────────────────────────────────────────────────────────────
# Mode isolation — incremental runs stay all-or-nothing (AC2 not weakened).
# ─────────────────────────────────────────────────────────────────────────────
def test_incremental_run_is_all_or_nothing_not_per_batch():
    store = Store()
    # A prior checkpoint makes this an INCREMENTAL run, not a first run.
    store.save(Checkpoint.create("bigsource", "org-1", "2"))

    # Resumes at offset 2, emits batch 3 (incomplete), then fails at batch 4.
    conn = ResumableFullLoadConnector(total_batches=5, fail_at=4)
    res = _drive(conn, "org-1", store)

    assert res.first_run is False
    assert res.ok is False
    # Incremental mode does NOT write per batch — batch 3 was not checkpointed,
    # so the prior position is untouched (AC2).
    assert res.checkpoint_advanced is False
    assert res.batches_checkpointed == 0
    assert store.read("org-1", "bigsource").value == "2"


def test_incremental_full_success_writes_once_at_terminal():
    store = Store()
    store.save(Checkpoint.create("bigsource", "org-1", "2"))
    conn = ResumableFullLoadConnector(total_batches=4)   # resumes at 2, completes
    res = _drive(conn, "org-1", store)
    assert res.first_run is False
    assert res.batches_checkpointed == 0                 # not per-batch
    assert res.complete is True
    assert res.checkpoint_advanced is True
    assert store.read("org-1", "bigsource").value == "4"


# ─────────────────────────────────────────────────────────────────────────────
# AC5 preserved in first-run mode — values stay opaque even mid-stream.
# ─────────────────────────────────────────────────────────────────────────────
def test_ac5_first_run_persists_opaque_values_verbatim():
    store = Store()

    class OpaqueResumable(ChangeBasedIngestor):
        connector_id = "opaque_src"

        def ingest_changes(self, org_id, since):
            self.seen_since = since
            yield DeltaBatch(records=[{"id": 1}], next_checkpoint="<<sha:9f2c>>", is_complete=False)
            yield DeltaBatch(records=[{"id": 2}], next_checkpoint="<<sha:1ab7>>", is_complete=True)

    conn = OpaqueResumable()
    res = _drive(conn, "org-1", store)

    assert conn.seen_since is None                       # first run
    assert res.batches_checkpointed == 2
    assert res.complete is True
    # Non-numeric, non-date values the runner could never parse — stored verbatim.
    assert store.read("org-1", "opaque_src").value == "<<sha:1ab7>>"
