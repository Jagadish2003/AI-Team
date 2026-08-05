"""The discovery steps added for sequential progress are actually PERSISTED.

``db.update_run_step()`` silently SKIPS an id that ``discovery/steps.py`` does not
declare (it logs a WARNING so a typo cannot clobber a valid step). These tests
assert the new ids survive that guard and land in the run payload, which is what
``GET /api/runs/{run_id}/status`` serves as ``current_step`` — the single signal
the Discovery Progress checklist and its percentage are both derived from. The
``sf_fsc`` case is a regression test: the runner emitted it while the vocabulary
had no such id, so every FSC run's pack row stayed pending for the whole run.

The DB-free half of this story (the structural runner-emission cross-check, step
ordering, and the health-status → step-outcome mapping) lives in
``tests/unit/test_discovery_step_vocabulary.py``.
"""
from __future__ import annotations

import time

import pytest

from app import db


@pytest.fixture()
def run_id() -> str:
    rid = f"run_step_emit_{int(time.time() * 1000)}"
    db.upsert_run(
        rid,
        {
            "id": rid,
            "status": "running",
            "startedAt": db.now_iso(),
            "updatedAt": db.now_iso(),
            "inputs": {},
        },
    )
    return rid


@pytest.mark.parametrize("step_id", ["azure_events", "aws_events", "sf_fsc"])
def test_new_step_ids_are_written_not_skipped(run_id, step_id):
    db.update_run_step(run_id, step_id)
    run = db.get_run(run_id)
    assert run is not None
    assert run.get("current_step") == step_id, (
        f"{step_id} was not persisted — db.update_run_step() skipped it as an "
        "unknown id, so its Discovery Progress row would never advance"
    )


def test_a_failed_cloud_step_is_recorded_in_failed_steps(run_id):
    db.update_run_step(run_id, "aws_events", ok=False)
    run = db.get_run(run_id)
    assert run is not None
    assert "aws_events" in (run.get("failed_steps") or [])
    # current_step still advances so the run keeps progressing.
    assert run.get("current_step") == "aws_events"
    # A later success clears it, so a retry is not reported as a permanent failure.
    db.update_run_step(run_id, "aws_events", ok=True)
    run = db.get_run(run_id)
    assert run is not None
    assert "aws_events" not in (run.get("failed_steps") or [])


def test_an_unknown_step_id_is_still_refused(run_id):
    """The guard that hid sf_fsc must stay — it stops a typo clobbering a step."""
    db.update_run_step(run_id, "sf_crm")
    db.update_run_step(run_id, "sf_typo_not_a_step")
    run = db.get_run(run_id)
    assert run is not None
    assert run.get("current_step") == "sf_crm"
