"""R16-A2 / AT-418 (T3) — EvidencePointer on every Slack signal.

AC5: Every Slack signal carries a valid EvidencePointer (R16-B1) with
``source_system='slack'``, a message/thread id, a timestamp, and
``origin='observed'``. No Slack signal may enter the system without a fully
populated, valid EvidencePointer.

These tests exercise both the per-message builder
(:func:`discovery.ingest.slack_signals.build_evidence_pointer`) and the end-to-end
ingestor output (:class:`discovery.ingest.slack.SlackIngestor`), and assert the
pointers validate against the canonical R16-B1 model
(:class:`app.provenance.EvidencePointer`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest.slack import SlackIngestor
from discovery.ingest.slack_signals import build_evidence_pointer

# The mandatory R16-B1 spine fields every pointer must populate.
_SPINE = ("source_system", "source_artifact", "source_timestamp", "origin")


def _all_records():
    return [r for b in SlackIngestor().ingest_changes("org1", None) for r in b.records]


# ─────────────────────────────────────────────────────────────────────────────
# build_evidence_pointer — unit
# ─────────────────────────────────────────────────────────────────────────────
def test_build_evidence_pointer_has_observed_spine():
    ptr = build_evidence_pointer("C001", "1718000600.000200")

    assert ptr["source_system"] == "slack"
    assert ptr["source_artifact"] == "C001:1718000600.000200"
    assert ptr["origin"] == OBSERVED
    # source_timestamp is the message's own UTC ISO-8601 moment.
    expected = datetime.fromtimestamp(1718000600.000200, tz=timezone.utc).isoformat()
    assert ptr["source_timestamp"] == expected
    # Observed → no extraction job required, and the artifact is a stable id.
    assert ptr["extraction_job_id"] is None
    assert ptr["source_artifact_type"] == "record_id"


def test_build_evidence_pointer_is_valid_observed_pointer():
    ptr = build_evidence_pointer("C001", "1718000600.000200")
    assert EvidencePointer.from_dict(ptr).is_valid() is True


def test_build_evidence_pointer_carries_extensible_fields_null():
    """R16-B1: extensible detail fields ship present-but-null in 1.6 (AC8 of B1)."""
    ptr = build_evidence_pointer("C001", "1718000600.000200")
    for field in ("chunk_id", "retrieval_result_id", "detector_evidence_id", "confidence"):
        assert field in ptr and ptr[field] is None


def test_build_evidence_pointer_unparseable_ts_still_valid():
    """A missing/garbled ts must never yield an invalid pointer — the spine still
    populates (falls back to 'now') so the signal is never dropped for provenance."""
    ptr = build_evidence_pointer("C001", "")
    assert ptr["source_system"] == "slack"
    assert ptr["origin"] == OBSERVED
    assert ptr["source_timestamp"]  # non-empty fallback
    assert EvidencePointer.from_dict(ptr).is_valid() is True


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end — every emitted record carries a valid observed pointer (AC5)
# ─────────────────────────────────────────────────────────────────────────────
def test_every_record_carries_evidence_pointer():
    records = _all_records()
    assert records  # the fixture yields signals

    for r in records:
        ptr = r.get("evidence_pointer")
        assert ptr is not None, f"record {r.get('artifact_id')} has no evidence_pointer"

        # Mandatory spine populated.
        for field in _SPINE:
            assert ptr.get(field), f"{field} missing/empty on {r['artifact_id']}"

        # AC5 exact values.
        assert ptr["source_system"] == "slack"
        assert ptr["origin"] == OBSERVED
        # message id == the record's own artifact identity (channel:ts).
        assert ptr["source_artifact"] == r["artifact_id"]
        # timestamp is the message's own ts as UTC ISO-8601.
        expected = datetime.fromtimestamp(float(r["ts"]), tz=timezone.utc).isoformat()
        assert ptr["source_timestamp"] == expected

        # Observed signal → never carries an inference job.
        assert ptr["extraction_job_id"] is None


def test_every_record_pointer_validates_against_canonical_model():
    """No Slack signal enters the system with an invalid EvidencePointer."""
    for r in _all_records():
        assert EvidencePointer.from_dict(r["evidence_pointer"]).is_valid() is True


def test_pointer_is_observed_never_inferred():
    """Slack reach-phase signal is read directly — never inferred (provenance side
    of 'LLM proposes, never authors truth')."""
    for r in _all_records():
        assert r["evidence_pointer"]["origin"] == OBSERVED


def test_evidence_pointer_does_not_pollute_signals_block():
    """The provenance pointer is a top-level record field — it must not leak into
    the reach-phase ``signals`` block (which stays exactly cross_references +
    escalation)."""
    for r in _all_records():
        assert set(r["signals"].keys()) == {"cross_references", "escalation"}
        assert "evidence_pointer" not in r["signals"]
