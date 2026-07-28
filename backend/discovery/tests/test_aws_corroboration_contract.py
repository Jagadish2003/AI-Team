"""AWS cloud events are CORROBORATION, never a standalone finding source.

Pins the contract documented in docs/msp_operational_event_schema.md §16. The
question this answers — "AWS is connected and events arrive, why are there no
findings?" — has a design answer, not a bug answer, and these tests keep that
answer honest if the gate ever moves.

The positive (events + incidents -> detector fires) direction is pinned by
test_cloud_ops_runtime.py::test_runtime_wires_b4_b5_and_b8_into_existing_cloud_detectors.
"""
from __future__ import annotations

import importlib

from discovery.cloud_ops_runtime import build_cloud_ops_runtime
from discovery.ingest.aws_event_connector import build_ingestor
from discovery.packs.pack_config import get_pack

ORG = "aws-corroboration-contract"


def _aws_only_block():
    """The cloud_ops block built from AWS events with NO ITSM data at all."""
    records = [
        rec
        for batch in build_ingestor(ORG).ingest_changes(ORG, None)
        for rec in batch.records
    ]
    runtime = build_cloud_ops_runtime(ORG, {"org_id": ORG}, bridge_records=records)
    return records, runtime


def test_aws_events_reach_the_cloud_ops_block():
    """The pipeline is wired: AWS events DO arrive as detector-visible signatures.

    Guards the first half of the contract — absence of findings must never be
    caused by events failing to reach the detectors.
    """
    records, runtime = _aws_only_block()

    assert records, "AWS connector produced no records from the offline fixture"
    assert all(r.get("event") is not None for r in records), (
        "every record must carry its OperationalEvent"
    )
    assert runtime.health["status"] == "ok"
    signatures = runtime.block["event_signatures"]
    assert signatures, "AWS events did not reach cloud_ops.event_signatures"
    assert {s["provider"] for s in signatures} == {"aws"}


def test_aws_only_signatures_are_not_detector_eligible():
    """Without an ITSM join, every signature is structurally ineligible.

    window_overlap is `bool(joined_incidents)`, and the ITSM-derived companion
    fields are empty — so a cloud event cannot satisfy any detector's gate on
    its own, however many times it fired.
    """
    _, runtime = _aws_only_block()

    for sig in runtime.block["event_signatures"]:
        assert sig["window_overlap"] is False, sig["signature"]
        assert sig["incident_count"] == 0
        assert sig["median_ttr_minutes"] == 0.0
        assert sig["distinct_close_codes"] == 0
        assert sig["assignment_group"] == ""
        # Recurrence itself IS observed — the events are real and folded (B7 T1);
        # they are simply not, alone, evidence of repeated human resolution work.
        assert sig["recurring"] is True
        assert sig["event_count"] >= 1


def test_aws_alone_produces_no_cloud_ops_findings():
    """The headline contract: AWS connected + no ITSM => zero findings, no error.

    A quiet run here is CORRECT behaviour, not a degraded one.
    """
    _, runtime = _aws_only_block()
    sn_data = {"org_id": ORG, "cloud_ops": runtime.block}

    detectors = [
        importlib.import_module(d) if isinstance(d, str) else d
        for d in (get_pack("cloud_ops").get("detectors") or [])
    ]
    assert detectors, "cloud_ops pack registered no detectors"

    for det in detectors:
        fired = det.detect(None, sn_data, None)
        assert not fired, (
            f"{getattr(det, 'DETECTOR_ID', det.__name__)} fired on AWS events alone; "
            "cloud events must corroborate ITSM-anchored findings, never assert one"
        )
