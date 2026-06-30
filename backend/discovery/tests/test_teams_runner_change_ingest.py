"""R17-A1 / AT-435 (T6) — Teams ingest is driven through the change runner.

The discovery runner's Teams entry point (`_ingest_teams_changes`) must route
Teams through the shared change runner (R16-A1) so that changed Teams artifacts
emit `ingestion.artifact_changed` events and the per-(org, 'teams') Graph delta
checkpoint advances. These tests assert the WIRING (the helper drives
`change_runner.ingest_with_checkpoint` with a TeamsIngestor, non-blocking) without
needing a DB — the runner call is faked. The REAL emission through the runner with
the real TeamsIngestor is covered by `test_teams_artifact_changed_events.py`.
"""
from __future__ import annotations

import discovery.ingest.change_runner as change_runner
from discovery.ingest.change_runner import IngestionResult
from discovery.runner import _ingest_teams_changes


def test_ingest_teams_changes_drives_change_runner(monkeypatch):
    """The helper calls change_runner.ingest_with_checkpoint with a TeamsIngestor."""
    seen = {}

    def fake_run(ingestor, org_id, **kwargs):
        seen["connector_id"] = ingestor.connector_id
        seen["org_id"] = org_id
        return IngestionResult(
            connector_id="teams", org_id=org_id, batches=1, records=2,
            complete=True, first_run=True, checkpoint_advanced=True,
        )

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", fake_run)

    # Returns None; the side effect (driving the runner → events + checkpoint) is
    # the deliverable. Must not raise.
    assert _ingest_teams_changes("org1", "run1") is None
    assert seen == {"connector_id": "teams", "org_id": "org1"}


def test_ingest_teams_changes_non_blocking_on_runner_error(monkeypatch):
    """A runner-reported ingestion error is surfaced as a warning, not raised."""
    def fake_run(ingestor, org_id, **kwargs):
        return IngestionResult(
            connector_id="teams", org_id=org_id,
            error=RuntimeError("graph delta boom"),
        )

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", fake_run)

    assert _ingest_teams_changes("org1", "run1") is None  # no raise


def test_ingest_teams_changes_swallows_unexpected_exception(monkeypatch):
    """A hard failure in the runner call degrades to a no-op, never aborting the run."""
    def fake_run(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", fake_run)

    assert _ingest_teams_changes("org1", "run1") is None  # no raise
