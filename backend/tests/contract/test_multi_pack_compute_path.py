"""R191-P1 fix — the live Stack Builder /compute path threads pack_ids to the runner.

Regression guard for the bug where a multi-pack run (Salesforce declaring e.g.
Service Cloud + nCino) only ran the PRIMARY pack: `routes_sprint4_t1`'s
`_run_trackb_and_persist` passed only the singular `pack` to `discovery.runner.run`,
never the full `pack_ids` from the run record — so nCino was never ingested.

This test seeds a run whose record carries `packIds=[service_cloud, ncino]` and
asserts the runner is invoked with that full list.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app import db
from app.routes_sprint4_t1 import _run_trackb_and_persist


def test_compute_path_forwards_all_pack_ids_to_runner(monkeypatch):
    run_id = f"run_{uuid4().hex[:10]}"
    db.upsert_run(
        run_id,
        {
            "id": run_id,
            "orgId": "default",
            "org_id": "default",
            "status": "running",
            "packId": "service_cloud",
            "packIds": ["service_cloud", "ncino"],
            "selectedSystemIds": ["salesforce", "jira"],
        },
    )

    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        # Short-circuit the rest of materialization — we only need the call args.
        raise RuntimeError("stop-after-capture")

    # The runner is imported inside _run_trackb_and_persist as
    # `from discovery.runner import run as trackb_run`, so patch it at its source.
    import discovery.runner as runner_mod
    monkeypatch.setattr(runner_mod, "run", fake_run)

    # Non-fatal: the fake runner raises, which _run_trackb_and_persist catches and
    # records as a failed run. We only assert on what it passed to the runner.
    _run_trackb_and_persist(run_id, "offline", ["salesforce", "jira"], "service_cloud")

    assert captured, "runner was never invoked"
    assert captured.get("pack_ids") == ["service_cloud", "ncino"], (
        f"compute path did not forward the full pack list; got {captured.get('pack_ids')!r}"
    )
    # The singular primary pack is still passed for backward compatibility.
    assert captured.get("pack") == "service_cloud"


def test_compute_path_falls_back_to_request_pack_ids(monkeypatch):
    # A run record without packIds (older/single-pack run) uses the compute
    # request's pack_ids, so nothing regresses when the record predates the field.
    run_id = f"run_{uuid4().hex[:10]}"
    db.upsert_run(
        run_id,
        {"id": run_id, "orgId": "default", "org_id": "default", "status": "running",
         "packId": "service_cloud"},
    )

    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop-after-capture")

    import discovery.runner as runner_mod
    monkeypatch.setattr(runner_mod, "run", fake_run)

    _run_trackb_and_persist(
        run_id, "offline", ["salesforce"], "service_cloud",
        ["service_cloud", "ncino"],  # pack_ids from the compute request
    )

    assert captured.get("pack_ids") == ["service_cloud", "ncino"]
