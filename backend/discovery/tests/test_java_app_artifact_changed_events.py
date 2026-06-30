"""
R17-A3 / T6 — ingestion.artifact_changed emission for Java-app artifacts (AC6).

This subtask makes Java ingestion participate in the existing change-ingestion
telemetry model. It REUSES the R16-A1 mechanism: the shared change runner
(``change_runner.ingest_with_checkpoint``) emits one ``ingestion.artifact_changed``
event per changed record in every fully-processed batch. There is deliberately
NO Java-specific event path.

These tests bind AC6 to the REAL ``JavaAppIngestor`` (R17-A3 / T1) driven through
that shared runner, proving every changed log artifact / fresh Actuator sample
emits an event carrying:

    org_id, connector_id='java_app', artifact_id, change_kind, observed_at (UTC ISO)

and — the load-bearing T6 invariant — that events fire ONLY for fully-processed
changes: if a batch fails before it is processed, its artifacts are not reported
as handled.

They run offline against the deterministic fixture and capture ``record_event``
via monkeypatch, with the checkpoint store wired through the runner's in-memory
seam — so no DB is needed.
"""
from __future__ import annotations

import inspect
from datetime import datetime

import pytest

from app.telemetry import REGISTERED_EVENT_TYPES
from discovery.ingest import change_runner
from discovery.ingest.base import Checkpoint
from discovery.ingest.java_app import JavaAppIngestor, _encode_checkpoint

EVENT = "ingestion.artifact_changed"

# Deterministic fixture artifact identities (channel of T1's java_app_sample.json).
_PAY_LOGS = [f"payments-api:log:{i}" for i in (1, 2, 3, 4)]
_PAY_ACT = [
    "payments-api:actuator:2026-06-28T09:00:00+00:00",
    "payments-api:actuator:2026-06-28T10:00:00+00:00",
]
_INV = ["inventory-api:log:1", "inventory-api:actuator:2026-06-28T09:30:00+00:00"]
_ALL = _PAY_LOGS + _PAY_ACT + _INV


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    """Pin offline so the real JavaAppIngestor reads the deterministic fixture."""
    monkeypatch.setenv("INGEST_MODE", "offline")


@pytest.fixture
def captured(monkeypatch):
    """Capture telemetry events (the runner lazily imports record_event at emit)."""
    events: list = []
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda etype, payload=None: events.append((etype, payload or {})),
    )
    return events


class Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _drive(ingestor, org_id, store=None, **kw):
    store = store or Store()
    res = change_runner.ingest_with_checkpoint(
        ingestor, org_id, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )
    return res, store


def _java_events(captured):
    return [p for (e, p) in captured if e == EVENT]


# ─────────────────────────────────────────────────────────────────────────────
# Registration precondition (record_event raises for unregistered types)
# ─────────────────────────────────────────────────────────────────────────────
def test_artifact_changed_event_type_is_registered():
    assert EVENT in REGISTERED_EVENT_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — every changed Java artifact emits an event with all required fields
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_emits_one_event_per_changed_artifact(captured):
    res, _ = _drive(JavaAppIngestor(), "org-1")
    events = _java_events(captured)
    assert {e["artifact_id"] for e in events} == set(_ALL)
    assert len(events) == len(_ALL) == res.records


def test_every_event_carries_all_required_fields(captured):
    _drive(JavaAppIngestor(), "org-9")
    events = _java_events(captured)
    assert events
    required = {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    for e in events:
        assert required <= set(e.keys())
        assert e["org_id"] == "org-9"
        assert e["connector_id"] == "java_app"          # the Java application source
        assert e["artifact_id"]                          # log line / Actuator sample id
        assert e["change_kind"] in ("created", "updated", "deleted")
        assert isinstance(e["observed_at"], str) and e["observed_at"]
        datetime.fromisoformat(e["observed_at"])         # valid UTC ISO timestamp


def test_events_cover_both_operational_surfaces(captured):
    _drive(JavaAppIngestor(), "org-1")
    ids = {e["artifact_id"] for e in _java_events(captured)}
    assert any(":log:" in a for a in ids)        # application logs
    assert any(":actuator:" in a for a in ids)   # framework health/diagnostics


def test_change_kind_is_created_for_new_artifacts(captured):
    _drive(JavaAppIngestor(), "org-1")
    assert {e["change_kind"] for e in _java_events(captured)} == {"created"}


# ─────────────────────────────────────────────────────────────────────────────
# Only CHANGED artifacts emit — idle / incremental
# ─────────────────────────────────────────────────────────────────────────────
def test_idle_deployment_emits_no_events(captured):
    _, store = _drive(JavaAppIngestor(), "org-1")          # first run emits all
    captured.clear()
    _drive(JavaAppIngestor(), "org-1", store=store)        # nothing new
    assert _java_events(captured) == []


def test_incremental_emits_only_newly_changed_artifacts(captured):
    # Checkpoint mid payments-api (after log 2 / first sample); inventory absent.
    store = Store()
    store.save(
        Checkpoint.create(
            "java_app", "org-1",
            _encode_checkpoint(
                {"payments-api": {"log_offset": 2, "sample_ts": "2026-06-28T09:00:00+00:00"}}
            ),
        )
    )
    _drive(JavaAppIngestor(), "org-1", store=store)
    emitted = {e["artifact_id"] for e in _java_events(captured)}
    # Only payments-api changes newer than the cursor + all of inventory-api.
    assert emitted == {
        "payments-api:log:3",
        "payments-api:log:4",
        "payments-api:actuator:2026-06-28T10:00:00+00:00",
        *_INV,
    }
    assert "payments-api:log:1" not in emitted                       # older offset
    assert "payments-api:actuator:2026-06-28T09:00:00+00:00" not in emitted  # equal ts


# ─────────────────────────────────────────────────────────────────────────────
# AC6 core — events fire ONLY for fully-processed changes
# ─────────────────────────────────────────────────────────────────────────────
def test_no_event_for_a_batch_that_fails_before_it_is_processed(captured):
    """If processing fails before a batch is fully handled, its artifacts must NOT
    be reported as changed (the runner emits only AFTER process_batch succeeds)."""
    # One record per batch so the failure boundary is exact.
    processed: list = []

    def boom_on_third(batch):
        if len(processed) == 2:          # the 3rd batch fails before processing
            raise RuntimeError("downstream processing failed")
        processed.append(batch)

    res, store = _drive(JavaAppIngestor(batch_size=1), "org-1", process_batch=boom_on_third)

    events = _java_events(captured)
    # Only the 2 batches that fully processed emitted; the failing batch (and the
    # batches after it, which never ran) did not.
    assert len(events) == 2
    assert res.error is not None                      # the run recorded the failure
    # The unprocessed artifacts were never reported as handled.
    emitted_ids = {e["artifact_id"] for e in events}
    assert len(emitted_ids) == 2
    assert emitted_ids < set(_ALL)


def test_emission_failure_never_breaks_ingestion(monkeypatch):
    """Telemetry is fire-and-forget — a record_event failure must not break ingestion."""
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)
    res, _ = _drive(JavaAppIngestor(), "org-1")
    assert res.ok
    assert res.checkpoint_advanced
    assert res.records == len(_ALL)


# ─────────────────────────────────────────────────────────────────────────────
# Reuse, not a Java-specific event path (R17-A3 / T6 design requirement)
# ─────────────────────────────────────────────────────────────────────────────
def test_connector_does_not_mint_its_own_artifact_changed_events():
    """The Java connector reuses the shared runner's emission — it must not call
    the telemetry emission API itself (no Java-specific event path). The runner
    owns ``record_event``; the connector only shapes records with artifact_id +
    change_kind. (Docstrings may *mention* the event — what matters is the code
    never emits.)"""
    import discovery.ingest.java_app as java_app_mod

    src = inspect.getsource(java_app_mod)
    assert "record_event" not in src           # never calls the emission API
    assert "app.telemetry" not in src          # never imports the telemetry path


# ─────────────────────────────────────────────────────────────────────────────
# Runner integration — the discovery runner drives Java through the shared runner
# ─────────────────────────────────────────────────────────────────────────────
def test_runner_helper_drives_java_ingestor_through_shared_runner(monkeypatch):
    """``runner._ingest_java_app_changes`` must drive the real JavaAppIngestor
    through the shared change runner (the only emission path), not a bespoke one."""
    from discovery import runner

    seen = {}

    def spy_ingest_with_checkpoint(ingestor, org_id, **kw):
        seen["ingestor"] = ingestor
        seen["org_id"] = org_id

        class _Res:
            error = None
            first_run = True
            batches = 1
            records = len(_ALL)
            checkpoint_advanced = True

        return _Res()

    monkeypatch.setattr(change_runner, "ingest_with_checkpoint", spy_ingest_with_checkpoint)

    result = runner._ingest_java_app_changes("org-1", "run-1")

    assert isinstance(seen.get("ingestor"), JavaAppIngestor)
    assert seen["org_id"] == "org-1"
    assert result is not None and result.records == len(_ALL)
