"""R16-A2 / AT-421 (T6) — Slack ingest is driven through the change runner.

The discovery runner's Slack entry point (`_ingest_slack_corroboration`) must route
Slack through the shared change runner (R16-A1) rather than calling
`ingest_changes()` directly. Driving it through the runner is what:

  * advances the per-(org, 'slack') checkpoint (incremental reads, not a full
    re-read every run), and
  * emits one `ingestion.artifact_changed` event per changed Slack artifact (AC7).

These tests assert the WIRING (the helper drives `change_runner.ingest_with_checkpoint`
and feeds corroboration from the runner-processed batches) without needing a DB —
the runner call is faked. The REAL emission through the runner with the real
SlackIngestor is covered by `test_slack_artifact_changed_events.py`.
"""
from __future__ import annotations

import discovery.ingest.change_runner as change_runner
from discovery.ingest.base import DeltaBatch
from discovery.ingest.change_runner import IngestionResult
from discovery.runner import _ingest_slack_corroboration

_ESC_RECORD = {
    "channel_id": "C1",
    "channel_name": "ops-incidents",
    "ts": "1718000000.000100",
    "artifact_id": "C1:1718000000.000100",
    "change_kind": "created",
    "user": "u1",
    "reply_count": 6,
    "reply_users_count": 4,  # escalation
    "reactions": [],
    "text": "war room",
}


def test_ingest_slack_corroboration_drives_change_runner(monkeypatch):
    """The helper calls change_runner.ingest_with_checkpoint with a SlackIngestor,
    and builds the corroboration block from the batches the runner processes."""
    seen = {}

    def fake_run(ingestor, org_id, *, process_batch=None, **kwargs):
        seen["connector_id"] = ingestor.connector_id
        seen["org_id"] = org_id
        # Simulate the runner handing a fully-processed batch to process_batch
        # (this is also where the runner emits artifact_changed for each record).
        if process_batch is not None:
            process_batch(
                DeltaBatch(records=[_ESC_RECORD], next_checkpoint="cp1", is_complete=True)
            )
        return IngestionResult(
            connector_id="slack", org_id=org_id, batches=1, records=1,
            complete=True, first_run=True, checkpoint_advanced=True,
        )

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", fake_run)

    payload = _ingest_slack_corroboration("org1", "run1")

    # Routed through the change runner with the real Slack ingestor.
    assert seen == {"connector_id": "slack", "org_id": "org1"}
    # Corroboration block built from the runner-processed records.
    assert payload["slack"]["escalation_pattern"]["fired"] is True
    assert "C1" in payload["slack"]["activity"]


def test_ingest_slack_corroboration_degrades_on_runner_error(monkeypatch):
    """A runner-reported ingestion error is non-blocking: the helper still returns
    a (possibly empty) corroboration block rather than raising."""
    def fake_run(ingestor, org_id, *, process_batch=None, **kwargs):
        # No batch processed; runner captured an error and left the checkpoint alone.
        return IngestionResult(
            connector_id="slack", org_id=org_id,
            error=RuntimeError("slack api boom"),
        )

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", fake_run)

    payload = _ingest_slack_corroboration("org1", "run1")
    assert payload["slack"]["escalation_pattern"]["fired"] is False


def test_ingest_slack_corroboration_swallows_unexpected_exception(monkeypatch):
    """A hard failure in the runner call degrades to an empty block ({}), never
    aborting the discovery run."""
    def fake_run(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", fake_run)

    assert _ingest_slack_corroboration("org1", "run1") == {}
