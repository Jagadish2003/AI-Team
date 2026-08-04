"""The run's record of its assembled cloud-event signature rows.

``runner._persist_cloud_ops_event_signatures`` is the only place a run's
``cloud_ops.event_signatures`` values survive it. These tests pin the two
properties that make the write safe to add to a working pipeline:

* it is **read-only over the detector input** — the block it is handed comes back
  unchanged and the stored rows are the same objects' contents verbatim, so no
  detector, corroboration, or scoring input can shift because of it; and
* it is **non-blocking** — a KV failure is swallowed, exactly like the connector
  health surfacing beside it, so recording can never fail a run.

Offline only. No database and no credentials required.
"""
from __future__ import annotations

import copy
import os

import pytest

os.environ["INGEST_MODE"] = "offline"

_SIG_A = "1:c819b460fa81d2108ae91f64908a688c"
_SIG_B = "1:d60a9095881341c45b03dfa0d005b068"


def _block():
    return {
        "event_signatures": [
            {
                "signature": _SIG_A,
                "event_count": 6,
                "recurring": True,
                "resource_id": "/subscriptions/s/resourceGroups/rg/x",
                "window_overlap": False,
            },
            {"signature": _SIG_B, "event_count": 1, "recurring": False},
        ],
        "recurrence_records": [],
    }


@pytest.fixture
def captured(monkeypatch):
    """Capture ``run_kv_set`` calls without touching a database."""
    import app.db as app_db

    calls = []
    monkeypatch.setattr(app_db, "run_kv_set", lambda k, r, v: calls.append((k, r, v)))
    return calls


class TestRecordsWhatTheRunSaw:
    def test_writes_the_rows_verbatim_under_the_shared_kv_key(self, captured):
        from discovery.runner import (
            KV_CLOUD_OPS_EVENT_SIGNATURES,
            _persist_cloud_ops_event_signatures,
        )

        block = _block()
        _persist_cloud_ops_event_signatures("run-1", block)

        assert len(captured) == 1
        key, run_id, payload = captured[0]
        assert key == KV_CLOUD_OPS_EVENT_SIGNATURES
        assert run_id == "run-1"
        assert payload["rows"] == block["event_signatures"]
        assert payload["count"] == 2
        assert payload["capturedAt"]

    def test_the_kv_key_matches_the_route_module(self):
        """One key, two modules — a drift here silently empties the endpoint."""
        from app.routes_cloud_ops_signatures import (
            KV_CLOUD_OPS_EVENT_SIGNATURES as route_key,
        )
        from discovery.runner import KV_CLOUD_OPS_EVENT_SIGNATURES as runner_key

        assert route_key == runner_key

    def test_count_is_derived_never_asserted(self, captured):
        """The stored count must always equal the stored rows — no carried number."""
        from discovery.runner import _persist_cloud_ops_event_signatures

        block = _block()
        block["event_signatures"].append({"signature": "1:" + "a" * 32})
        _persist_cloud_ops_event_signatures("run-1", block)

        _, _, payload = captured[0]
        assert payload["count"] == len(payload["rows"]) == 3


class TestDoesNotDisturbTheRun:
    def test_the_detector_input_block_is_not_mutated(self, captured):
        from discovery.runner import _persist_cloud_ops_event_signatures

        block = _block()
        before = copy.deepcopy(block)
        _persist_cloud_ops_event_signatures("run-1", block)
        assert block == before

    def test_a_kv_failure_is_swallowed(self, monkeypatch):
        import app.db as app_db
        from discovery.runner import _persist_cloud_ops_event_signatures

        def _boom(*_args, **_kwargs):
            raise RuntimeError("kv down")

        monkeypatch.setattr(app_db, "run_kv_set", _boom)
        # Must not raise — recording is never allowed to fail a run.
        _persist_cloud_ops_event_signatures("run-1", _block())

    @pytest.mark.parametrize("block", [{}, {"event_signatures": None}])
    def test_no_write_when_there_is_nothing_to_record(self, captured, block):
        """A run with no cloud_ops assembly records nothing — never an empty stub
        that would read as 'assembled, and there were zero'."""
        from discovery.runner import _persist_cloud_ops_event_signatures

        _persist_cloud_ops_event_signatures("run-1", block)
        assert captured == []

    def test_an_empty_but_present_row_list_is_recorded_as_empty(self, captured):
        """`[]` is a real outcome (assembly ran, nothing survived the noise floor)
        and is distinguishable from 'never assembled'."""
        from discovery.runner import _persist_cloud_ops_event_signatures

        _persist_cloud_ops_event_signatures("run-1", {"event_signatures": []})
        assert len(captured) == 1
        assert captured[0][2]["count"] == 0
