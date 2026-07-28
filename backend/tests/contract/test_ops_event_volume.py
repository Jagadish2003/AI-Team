"""Real-DB month-scale volume validation — MSP-B8 / T5 (AC7).

Runs the volume harness end to end against the actual staging table: loaders
(T2/T3) write via :class:`DbStagingSink`, the bridge (T4) reads via the read-only
:class:`DbStagingReader`, at a volume large enough to exercise real IDENTITY
row-id paging, bridge batching, staging size, and memory. Proves a month-scale
export loads and ingests within the (proposed) operational envelope, with the
measured numbers recorded (AC7).

Volume is ``MSP_B8_VOLUME_PER_FORMAT`` per surface (default 2000 → 8k events) so
CI stays fast; the recorded month-scale evidence in
``docs/MSP-B8_VOLUME_VALIDATION.md`` was produced at a larger volume via the same
harness.
"""
import os

import pytest

from discovery.ingest.ops_event_staging_store import DbStagingReader, DbStagingSink
from discovery.ingest.ops_event_volume_harness import run_volume_validation
from discovery.signals.evidence_store import InMemoryRawEventStore

_PER_FORMAT = int(os.environ.get("MSP_B8_VOLUME_PER_FORMAT", "2000"))


@pytest.fixture(scope="session")
def report():
    # Session-scoped: the harness runs ONCE against a fresh org and all tests
    # assert on that single measured report. Re-running per test against the same
    # persistent DB org would make later loads see the first run's rows (skewing
    # the insert/duplicate/resume accounting).
    return run_volume_validation(
        "org_volume_ac7",
        sink=DbStagingSink(),
        reader=DbStagingReader(),
        raw_store=InMemoryRawEventStore(),
        per_format=_PER_FORMAT,
        batch_size=500,
    )


def test_month_scale_loads_and_ingests_within_envelope(report):
    # Substantial volume actually moved through the full path.
    assert report.rows_loaded > 4 * (_PER_FORMAT - _PER_FORMAT // 40)  # minus injected skips
    assert report.events_emitted == report.rows_loaded
    # Envelope holds (throughput floors, memory ceiling, resume, evidence).
    assert report.envelope_pass, report.envelope_failures


def test_measurements_are_populated_not_assumed(report):
    # Every measured field carries a real, positive number — nothing left at 0.
    assert report.load_seconds > 0 and report.load_rows_per_sec > 0
    assert report.ingest_seconds > 0 and report.ingest_events_per_sec > 0
    assert report.peak_memory_mb > 0
    assert report.batches > 1  # multiple row-id-paged bridge batches
    assert report.max_batch_size <= 500
    assert int(report.final_checkpoint) > 0  # real IDENTITY row_id checkpoint


def test_skip_and_dedupe_accounting_reconciles(report):
    assert report.rows_skipped == report.expected_malformed > 0
    assert report.duplicate_count == report.expected_dupes > 0


def test_resume_processes_only_new_rows_on_real_db(report):
    assert report.resume_new_rows > 0
    assert report.resume_events == report.resume_new_rows


def test_evidence_traces_resolve_at_volume(report):
    assert report.evidence_samples_checked > 0
    assert report.evidence_samples_resolved == report.evidence_samples_checked
    assert report.events_with_signature == report.events_emitted


def test_write_report_artifact_when_requested(report):
    """Write the measured report to disk when MSP_B8_VOLUME_REPORT_OUT is set.

    Used to (re)generate ``docs/MSP-B8_VOLUME_VALIDATION.md`` — the recorded MSP-B7
    calibration hand-off — from a real month-scale run:

        MSP_B8_VOLUME_PER_FORMAT=7500 \\
        MSP_B8_VOLUME_REPORT_OUT=docs/MSP-B8_VOLUME_VALIDATION.md \\
        python -m pytest tests/contract/test_ops_event_volume.py

    A normal run (no env var) skips this test.
    """
    from discovery.ingest.ops_event_volume_harness import render_markdown

    out = os.environ.get("MSP_B8_VOLUME_REPORT_OUT")
    if not out:
        pytest.skip("set MSP_B8_VOLUME_REPORT_OUT to write the report artifact")
    env_label = os.environ.get("MSP_B8_VOLUME_ENV", "unspecified")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report, environment=env_label))
