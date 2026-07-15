"""Offline tests for the month-scale volume harness — MSP-B8 / T5.

DB-free: runs the harness at a small scale against an ``InMemoryStagingSink``
(used as both sink and reader) so the measurement accounting, skip/dupe injection,
resume behaviour, evidence resolution, and envelope evaluation are all verified
without a database. The real month-scale on-DB run is
``tests/contract/test_ops_event_volume.py``.
"""
from __future__ import annotations

from discovery.ingest.ops_event_staging_store import InMemoryStagingSink
from discovery.ingest.ops_event_volume_harness import (
    DEFAULT_ENVELOPE,
    gen_azure_activity,
    gen_azure_monitor,
    gen_cloudtrail,
    gen_eventbridge,
    render_markdown,
    run_volume_validation,
)
from discovery.signals.evidence_store import InMemoryRawEventStore


def _run(per_format=200, batch_size=64, **kw):
    sink = InMemoryStagingSink()
    return run_volume_validation(
        "vol_org",
        sink=sink,
        reader=sink,
        raw_store=InMemoryRawEventStore(),
        per_format=per_format,
        batch_size=batch_size,
        **kw,
    )


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def test_generators_are_deterministic_and_unique():
    a = gen_cloudtrail(100)
    b = gen_cloudtrail(100)
    assert a == b  # deterministic
    assert len({r["eventID"] for r in a}) == 100  # unique ids
    assert len({r["id"] for r in gen_eventbridge(50)}) == 50
    assert len({r["eventDataId"] for r in gen_azure_activity(50)}) == 50
    assert len({r["data"]["essentials"]["alertId"] for r in gen_azure_monitor(50)}) == 50


# ---------------------------------------------------------------------------
# End-to-end measurement accounting
# ---------------------------------------------------------------------------


def test_load_and_ingest_counts_reconcile():
    r = _run(per_format=200)
    # Four surfaces generated.
    assert set(r.per_format_counts) == {
        "eventbridge_archive", "cloudtrail", "azure_monitor", "azure_activity_log"
    }
    # Everything valid+unique that loaded is emitted by the bridge.
    assert r.events_emitted == r.rows_loaded > 0
    # Malformed were skipped (three top-level-id surfaces inject them).
    assert r.rows_skipped == r.expected_malformed > 0
    # Duplicates were collapsed.
    assert r.duplicate_count == r.expected_dupes > 0
    # staging rows == rows loaded.
    assert r.staging_rows == r.rows_loaded


def test_every_event_has_signature_and_evidence_resolves():
    r = _run(per_format=150)
    assert r.events_with_signature == r.events_emitted
    assert r.evidence_samples_checked > 0
    assert r.evidence_samples_resolved == r.evidence_samples_checked


def test_bridge_batching_is_bounded():
    r = _run(per_format=300, batch_size=64)
    assert r.max_batch_size <= 64
    assert r.batches >= (r.events_emitted // 64)


# ---------------------------------------------------------------------------
# Checkpoint resume at volume
# ---------------------------------------------------------------------------


def test_resume_processes_only_new_rows():
    r = _run(per_format=200)
    assert r.resume_new_rows == 200
    assert r.resume_events == 200  # exactly the new rows, not the whole corpus


# ---------------------------------------------------------------------------
# Envelope evaluation
# ---------------------------------------------------------------------------


def test_envelope_passes_on_a_healthy_run():
    r = _run(per_format=200)
    assert r.envelope_pass, r.envelope_failures
    assert r.envelope_failures == []


def test_envelope_fails_when_threshold_impossible():
    # An impossibly high throughput floor forces a recorded failure.
    r = _run(per_format=100, envelope={"min_ingest_events_per_sec": 10**12})
    assert not r.envelope_pass
    assert any("ingest throughput" in f for f in r.envelope_failures)


# ---------------------------------------------------------------------------
# Report rendering (the MSP-B7 hand-off)
# ---------------------------------------------------------------------------


def test_markdown_report_contains_key_measurements():
    r = _run(per_format=100)
    md = render_markdown(r, environment="unit-test")
    assert "Month-Scale Volume Validation" in md
    assert "Load throughput" in md
    assert "Ingest throughput" in md
    assert "Peak memory" in md
    assert "MSP-B7" in md


def test_report_is_json_serialisable():
    import json

    r = _run(per_format=80)
    json.dumps(r.to_dict())  # must not raise
