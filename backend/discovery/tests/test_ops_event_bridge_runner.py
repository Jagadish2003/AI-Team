"""Runner-boundary coverage for MSP-B8's checkpointed production path."""
from __future__ import annotations

import discovery.ingest.change_runner as change_runner
from app.provenance import EvidencePointer
from discovery.ingest.base import DeltaBatch
from discovery.ingest.change_runner import IngestionResult
from discovery.runner import _ingest_ops_event_bridge
from discovery.signals.operational_event import OperationalEvent, ResourceRef


def _record(org_id: str = "org-bridge") -> dict:
    event = OperationalEvent.build(
        org_id=org_id,
        source_system="bridge:aws",
        signal_id="evt-1",
        event_type="WorkerFailure",
        event_class="error",
        severity="warning",
        observed_at="2026-07-01T10:00:00+00:00",
        resource=ResourceRef(
            provider="aws",
            resource_type="compute",
            resource_id="worker-1",
        ),
        provenance=EvidencePointer.observed(
            source_system="bridge:aws",
            source_artifact="evt-1",
            source_timestamp="2026-07-01T10:00:00+00:00",
            source_artifact_type="staged_event",
        ).to_dict(),
    )
    return {
        "artifact_id": "bridge:aws:evt-1",
        "change_kind": "created",
        "event": event.to_dict(),
        "batch_id": "batch-1",
        "staging_row_id": 1,
    }


def test_runner_drives_bridge_through_change_runner_and_collects_validated_batch(
    monkeypatch,
):
    seen = {}

    def fake_run(ingestor, org_id, *, process_batch=None, **_kwargs):
        seen["connector_id"] = ingestor.connector_id
        seen["org_id"] = org_id
        process_batch(
            DeltaBatch(
                records=[_record(org_id)],
                next_checkpoint="1",
                is_complete=True,
            )
        )
        return IngestionResult(
            connector_id=ingestor.connector_id,
            org_id=org_id,
            batches=1,
            records=1,
            complete=True,
            first_run=True,
            checkpoint_advanced=True,
        )

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", fake_run)

    result = _ingest_ops_event_bridge("org-bridge", "run-1")

    assert seen == {
        "connector_id": "ops_event_bridge",
        "org_id": "org-bridge",
    }
    assert len(result["records"]) == 1
    assert result["health"]["status"] == "ok"
    assert result["health"]["checkpoint_advanced"] is True


def test_runner_rejects_cross_org_event_before_reporting_checkpoint_success(
    monkeypatch,
):
    def fake_run(ingestor, org_id, *, process_batch=None, **_kwargs):
        try:
            process_batch(
                DeltaBatch(
                    records=[_record("another-org")],
                    next_checkpoint="1",
                    is_complete=True,
                )
            )
        except Exception as exc:
            return IngestionResult(
                connector_id=ingestor.connector_id,
                org_id=org_id,
                error=exc,
            )
        raise AssertionError("cross-org record unexpectedly passed validation")

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", fake_run)

    result = _ingest_ops_event_bridge("org-bridge", "run-1")

    assert result["records"] == []
    assert result["health"]["status"] == "degraded"
    assert result["health"]["checkpoint_advanced"] is False
    assert result["health"]["reason"] == "ValueError"
