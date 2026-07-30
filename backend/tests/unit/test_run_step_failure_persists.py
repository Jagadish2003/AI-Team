"""Materialization must not clobber the progress the pipeline wrote.

Bug: a run whose Salesforce ingest failed showed a red cross in Discovery Progress
while running, then flipped EVERY stage to a green tick the moment it reached
"Completed 100%" — the failure silently disappeared from the UI.

Root cause: ``materialize_t2.run_trackb_and_persist`` reads the run record ONCE at the
top, before the pipeline runs. During the run, ``db.update_run_step()`` writes
``current_step`` and ``failed_steps`` STRAIGHT TO THE STORED PAYLOAD — they are never
reflected back onto that in-memory dict. ``_finalise`` then wrote the whole stale dict
back, reverting them.

``current_step`` had already been patched (a single line forcing it to "complete"), but
``failed_steps`` had not, so it was reset to its pre-run value — usually absent — and
``GET /api/runs/{id}/status`` reported ``failed_steps: []``. The UI's own rendering was
correct all along: it gives a failed step precedence over the run-complete state, so an
empty list was the whole bug.

These tests pin the fix at the data layer, so a future write that forgets to carry
pipeline-owned fields forward fails here rather than in the UI. DB-free — ``db`` is
faked with an in-memory run store.
"""
from __future__ import annotations

import os
from typing import Any, Dict

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app import materialize_t2  # noqa: E402


class _FakeDb:
    """In-memory stand-in for the run-record layer.

    Models the property that matters: ``update_run_step`` mutates the STORED payload
    while a caller may be holding an older copy of it.
    """

    def __init__(self, run: Dict[str, Any]) -> None:
        self.runs: Dict[str, Dict[str, Any]] = {"run-1": dict(run)}
        self.kv: Dict[str, Any] = {}

    def run_get(self, run_id: str):
        stored = self.runs.get(run_id)
        # Return a COPY, exactly as a real read does — so a caller holding the result
        # cannot accidentally observe later writes.
        return dict(stored) if stored is not None else None

    def run_set(self, run_id: str, run: Dict[str, Any]) -> None:
        self.runs[run_id] = dict(run)

    def update_run_step(self, run_id: str, step_id: str, ok: bool = True) -> None:
        """The real signature's behaviour: write straight to the stored payload."""
        stored = self.runs[run_id]
        stored["current_step"] = step_id
        failed = list(stored.get("failed_steps") or [])
        if ok:
            failed = [s for s in failed if s != step_id]
        elif step_id not in failed:
            failed.append(step_id)
        stored["failed_steps"] = failed

    def now_iso(self) -> str:
        return "2026-07-30T10:00:00Z"

    def run_kv_get(self, key: str, run_id: str, default: Any = None) -> Any:
        return self.kv.get(f"{key}:{run_id}", default)

    def run_kv_set(self, key: str, run_id: str, value: Any) -> None:
        self.kv[f"{key}:{run_id}"] = value


@pytest.fixture
def fake_db(monkeypatch):
    fake = _FakeDb({"id": "run-1", "status": "running", "orgId": "acme"})
    monkeypatch.setattr(materialize_t2, "db", fake)
    # _finalise also writes a status KV entry and an audit list.
    monkeypatch.setattr(materialize_t2, "set_status", lambda *_a, **_k: None)
    monkeypatch.setattr(materialize_t2, "_audit_prepend", lambda *_a, **_k: None)
    return fake


def _stale_copy(fake: _FakeDb) -> Dict[str, Any]:
    """The run dict as materialization holds it: read BEFORE the pipeline ran."""
    return fake.run_get("run-1")


class TestFailedStepsSurviveMaterialization:
    def test_a_failed_step_is_not_cleared_when_the_run_completes(self, fake_db):
        stale = _stale_copy(fake_db)          # read first, as production does
        fake_db.update_run_step("run-1", "sf_crm", ok=False)   # pipeline records failure
        assert fake_db.runs["run-1"]["failed_steps"] == ["sf_crm"]

        materialize_t2._finalise(
            "run-1", stale, "complete", "live", ["salesforce"], {}, {}, {},
            "DISCOVERY_MATERIALIZED",
        )

        # THE bug: this used to be [] because the stale dict was written back.
        assert fake_db.runs["run-1"]["failed_steps"] == ["sf_crm"]

    def test_a_failed_step_survives_a_partial_run(self, fake_db):
        stale = _stale_copy(fake_db)
        fake_db.update_run_step("run-1", "sf_crm", ok=False)
        materialize_t2._finalise(
            "run-1", stale, "partial", "live", ["salesforce"], {}, {}, {},
            "DISCOVERY_PARTIAL",
        )
        assert fake_db.runs["run-1"]["failed_steps"] == ["sf_crm"]

    def test_multiple_failed_steps_all_survive(self, fake_db):
        stale = _stale_copy(fake_db)
        fake_db.update_run_step("run-1", "sf_crm", ok=False)
        fake_db.update_run_step("run-1", "servicenow", ok=False)
        materialize_t2._finalise(
            "run-1", stale, "complete", "live", ["salesforce"], {}, {}, {},
            "DISCOVERY_MATERIALIZED",
        )
        assert fake_db.runs["run-1"]["failed_steps"] == ["sf_crm", "servicenow"]

    def test_a_recovered_step_is_not_resurrected_as_failed(self, fake_db):
        # ok=True clears a step from failed_steps; carrying the field forward must
        # respect that, not restore an earlier failure.
        stale = _stale_copy(fake_db)
        fake_db.update_run_step("run-1", "sf_crm", ok=False)
        fake_db.update_run_step("run-1", "sf_crm", ok=True)
        materialize_t2._finalise(
            "run-1", stale, "complete", "live", ["salesforce"], {}, {}, {},
            "DISCOVERY_MATERIALIZED",
        )
        assert fake_db.runs["run-1"]["failed_steps"] == []

    def test_a_clean_run_records_no_failures(self, fake_db):
        stale = _stale_copy(fake_db)
        fake_db.update_run_step("run-1", "sf_crm", ok=True)
        materialize_t2._finalise(
            "run-1", stale, "complete", "live", ["salesforce"], {}, {}, {},
            "DISCOVERY_MATERIALIZED",
        )
        assert fake_db.runs["run-1"]["failed_steps"] == []


class TestCurrentStepHandling:
    def test_a_completed_run_stamps_current_step_complete(self, fake_db):
        stale = _stale_copy(fake_db)
        fake_db.update_run_step("run-1", "sf_crm", ok=True)
        materialize_t2._finalise(
            "run-1", stale, "complete", "live", ["salesforce"], {}, {}, {},
            "DISCOVERY_MATERIALIZED",
        )
        # Otherwise the progress list shows an early step still spinning.
        assert fake_db.runs["run-1"]["current_step"] == "complete"

    def test_a_failed_run_keeps_the_step_it_failed_at(self, fake_db):
        # Previously this reverted to the stale pre-run value too, because the
        # current_step patch only applied to complete/partial.
        stale = _stale_copy(fake_db)
        fake_db.update_run_step("run-1", "servicenow", ok=False)
        materialize_t2._finalise(
            "run-1", stale, "failed", "live", ["servicenow"], {}, {}, {},
            "DISCOVERY_FAILED",
        )
        assert fake_db.runs["run-1"]["current_step"] == "servicenow"
        assert fake_db.runs["run-1"]["failed_steps"] == ["servicenow"]


class TestMaterializationFieldsAreStillWritten:
    def test_status_and_pack_metadata_from_the_in_memory_dict_are_preserved(
        self, fake_db
    ):
        # The fix must not swing the other way: materialization legitimately adds
        # pack execution metadata to its copy, and that must still be written.
        stale = _stale_copy(fake_db)
        stale["packId"] = "cloud_ops"
        stale["packVersion"] = "1.2.0"
        stale["packs"] = [{"packId": "cloud_ops"}]
        fake_db.update_run_step("run-1", "sf_crm", ok=False)

        materialize_t2._finalise(
            "run-1", stale, "complete", "live", ["salesforce"], {}, {}, {},
            "DISCOVERY_MATERIALIZED",
        )

        stored = fake_db.runs["run-1"]
        assert stored["status"] == "complete"
        assert stored["packId"] == "cloud_ops"
        assert stored["packVersion"] == "1.2.0"
        assert stored["packs"] == [{"packId": "cloud_ops"}]
        assert stored["failed_steps"] == ["sf_crm"]

    def test_a_vanished_run_record_does_not_crash_finalise(self, fake_db):
        stale = _stale_copy(fake_db)
        fake_db.runs.clear()
        # run_get returns None — the carry-forward must degrade, not raise.
        materialize_t2._finalise(
            "run-1", stale, "complete", "live", ["salesforce"], {}, {}, {},
            "DISCOVERY_MATERIALIZED",
        )
        assert fake_db.runs["run-1"]["status"] == "complete"


def test_pipeline_owned_fields_are_declared():
    """The carry-forward list must name both fields update_run_step writes.

    If a new field is added to update_run_step without being listed here it becomes
    the same bug again, so this is the reminder.
    """
    assert set(materialize_t2._PIPELINE_OWNED_RUN_FIELDS) == {
        "current_step",
        "failed_steps",
    }
