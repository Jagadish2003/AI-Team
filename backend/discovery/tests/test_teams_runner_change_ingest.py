"""R17-A1 / AT-435 (T6) + AT-433 (T4) — Teams ingest is driven through the change runner.

The discovery runner's Teams entry point (`_ingest_teams_corroboration`) must route
Teams through the shared change runner (R16-A1) so that changed Teams artifacts emit
`ingestion.artifact_changed` events (T6) and the per-(org, 'teams') Graph delta
checkpoint advances — and aggregate the changed records into the corroboration block
(T4). These tests assert the WIRING (the helper drives
`change_runner.ingest_with_checkpoint` with a TeamsIngestor and builds the block from
the runner-processed batches, non-blocking) without needing a DB — the runner call is
faked. The REAL emission with the real TeamsIngestor is covered by
`test_teams_artifact_changed_events.py`; the ceiling by `test_teams_corroboration_ceiling.py`.
"""
from __future__ import annotations

import discovery.ingest.change_runner as change_runner
from discovery.ingest.base import DeltaBatch
from discovery.ingest.change_runner import IngestionResult
from discovery.runner import _ingest_teams_corroboration

# A record that build_teams_signal will treat as an escalated thread (>= 3 repliers).
_ESC_RECORD = {
    "team_id": "T-eng",
    "channel_id": "19:ops",
    "channel_name": "ops-incidents",
    "reply_count": 6,
    "reply_users_count": 4,
    "reactions": [],
    "text": "war room — customers blocked",
}


def test_ingest_teams_corroboration_drives_change_runner(monkeypatch):
    """The helper calls change_runner.ingest_with_checkpoint with a TeamsIngestor,
    and builds the corroboration block from the batches the runner processes."""
    seen = {}

    def fake_run(ingestor, org_id, *, process_batch=None, **kwargs):
        seen["connector_id"] = ingestor.connector_id
        seen["org_id"] = org_id
        # Simulate the runner handing a fully-processed batch to process_batch
        # (this is also where it emits artifact_changed for each record).
        if process_batch is not None:
            process_batch(
                DeltaBatch(records=[_ESC_RECORD], next_checkpoint="cp1", is_complete=True)
            )
        return IngestionResult(
            connector_id="teams", org_id=org_id, batches=1, records=1,
            complete=True, first_run=True, checkpoint_advanced=True,
        )

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", fake_run)

    payload = _ingest_teams_corroboration("org1", "run1")

    # Routed through the change runner with the real Teams ingestor.
    assert seen == {"connector_id": "teams", "org_id": "org1"}
    # Corroboration block built from the runner-processed records, keyed 'teams'.
    assert payload["teams"]["escalation_pattern"]["fired"] is True
    assert "19:ops" in payload["teams"]["activity"]
    # The adapter must never smuggle a confidence/elevation — the engine owns that.
    assert "confidence" not in payload["teams"]


def test_ingest_teams_corroboration_degrades_on_runner_error(monkeypatch):
    """A runner-reported ingestion error is non-blocking: the helper still returns
    a (possibly empty) corroboration block rather than raising."""
    def fake_run(ingestor, org_id, *, process_batch=None, **kwargs):
        return IngestionResult(
            connector_id="teams", org_id=org_id, error=RuntimeError("graph delta boom"),
        )

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", fake_run)

    payload = _ingest_teams_corroboration("org1", "run1")
    assert payload["teams"]["escalation_pattern"]["fired"] is False


def test_ingest_teams_corroboration_swallows_unexpected_exception(monkeypatch):
    """A hard failure in the runner call degrades to an empty block ({}), never
    aborting the discovery run."""
    def fake_run(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", fake_run)

    assert _ingest_teams_corroboration("org1", "run1") == {}
