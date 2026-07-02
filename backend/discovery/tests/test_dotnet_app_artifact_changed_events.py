"""
R17-A4 / T6 + T7 — ingestion.artifact_changed emission for .NET-app artifacts (AC7).

This subtask makes .NET ingestion participate in the existing change-based
telemetry model, exactly as Java does. It REUSES the R16-A1 mechanism: the shared
change runner (``change_runner.ingest_with_checkpoint``) emits one
``ingestion.artifact_changed`` event per changed record in every fully-processed
batch. There is deliberately NO .NET-specific event path.

These tests bind AC7 to the REAL ``DotNetAppIngestor`` driven through that shared
runner, proving every changed log artifact / fresh diagnostics sample emits an
event carrying:

    org_id, connector_id='dotnet_app', artifact_id, change_kind, observed_at (UTC ISO)

and — the load-bearing invariant — that events fire ONLY for fully-processed
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
from discovery.ingest.dotnet_app import DotNetAppIngestor, _encode_checkpoint

EVENT = "ingestion.artifact_changed"

# Deterministic fixture artifact identities (dotnet_app_sample.json). The offset-4
# orders log carries a native event_id, so its artifact reference is event-based.
_ORDERS = [
    "orders-api:metrics:2026-06-10T08:00:00+00:00",
    "orders-api:metrics:2026-06-10T08:05:00+00:00",
    "orders-api:metrics:2026-06-10T08:10:00+00:00",
    "orders-api:log:1",
    "orders-api:log:2",
    "orders-api:log:3",
    "orders-api:log:event:evt-orders-0042",
    "orders-api:log:5",
]
_INVENTORY = [
    "inventory-svc:metrics:2026-06-10T08:00:00+00:00",
    "inventory-svc:metrics:2026-06-10T08:05:00+00:00",
    "inventory-svc:log:1",
    "inventory-svc:log:2",
]
_ALL = _ORDERS + _INVENTORY


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    """Pin offline so the real DotNetAppIngestor reads the deterministic fixture."""
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

    def read(self, o, c):
        return self.data.get((o, c))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _drive(ingestor, org_id, store=None, **kw):
    store = store or Store()
    res = change_runner.ingest_with_checkpoint(
        ingestor, org_id, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )
    return res, store


def _dotnet_events(captured):
    return [p for (e, p) in captured if e == EVENT]


# ─────────────────────────────────────────────────────────────────────────────
# Registration precondition (record_event raises for unregistered types)
# ─────────────────────────────────────────────────────────────────────────────
def test_artifact_changed_event_type_is_registered():
    assert EVENT in REGISTERED_EVENT_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — every changed .NET artifact emits an event with all required fields
# ─────────────────────────────────────────────────────────────────────────────
def test_first_run_emits_one_event_per_changed_artifact(captured):
    res, _ = _drive(DotNetAppIngestor(), "org-1")
    events = _dotnet_events(captured)
    assert {e["artifact_id"] for e in events} == set(_ALL)
    assert len(events) == len(_ALL) == res.records


def test_every_event_carries_all_required_fields(captured):
    _drive(DotNetAppIngestor(), "org-9")
    events = _dotnet_events(captured)
    assert events
    required = {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    for e in events:
        assert required <= set(e.keys())
        assert e["org_id"] == "org-9"
        assert e["connector_id"] == "dotnet_app"        # the .NET application source
        assert e["artifact_id"]                          # log line / diagnostics sample id
        assert e["change_kind"] in ("created", "updated", "deleted")
        assert isinstance(e["observed_at"], str) and e["observed_at"]
        datetime.fromisoformat(e["observed_at"])         # valid UTC ISO timestamp


def test_events_cover_both_operational_surfaces(captured):
    _drive(DotNetAppIngestor(), "org-1")
    ids = {e["artifact_id"] for e in _dotnet_events(captured)}
    assert any(":log:" in a for a in ids)      # application logs
    assert any(":metrics:" in a for a in ids)  # health/diagnostics samples


def test_change_kind_is_created_for_new_artifacts(captured):
    _drive(DotNetAppIngestor(), "org-1")
    assert {e["change_kind"] for e in _dotnet_events(captured)} == {"created"}


# ─────────────────────────────────────────────────────────────────────────────
# Only CHANGED artifacts emit — idle / incremental
# ─────────────────────────────────────────────────────────────────────────────
def test_idle_deployment_emits_no_events(captured):
    _, store = _drive(DotNetAppIngestor(), "org-1")          # first run emits all
    captured.clear()
    _drive(DotNetAppIngestor(), "org-1", store=store)        # nothing new
    assert _dotnet_events(captured) == []


def test_full_cursor_emits_no_events(captured):
    # A hand-set checkpoint at the end of both apps' streams → nothing to emit.
    store = Store()
    store.save(
        Checkpoint.create(
            "dotnet_app", "org-1",
            _encode_checkpoint(
                {"orders-api": {"log_offset": 5, "metrics_ts": "2026-06-10T08:10:00+00:00"},
                 "inventory-svc": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00"}}
            ),
        )
    )
    _drive(DotNetAppIngestor(), "org-1", store=store)
    assert _dotnet_events(captured) == []


def test_incremental_emits_only_newly_changed_artifacts(captured):
    # Checkpoint mid orders-api (after log 1 / first sample); inventory absent.
    store = Store()
    store.save(
        Checkpoint.create(
            "dotnet_app", "org-1",
            _encode_checkpoint(
                {"orders-api": {"log_offset": 1, "metrics_ts": "2026-06-10T08:00:00+00:00"}}
            ),
        )
    )
    _drive(DotNetAppIngestor(), "org-1", store=store)
    emitted = {e["artifact_id"] for e in _dotnet_events(captured)}
    assert emitted == {
        "orders-api:metrics:2026-06-10T08:05:00+00:00",
        "orders-api:metrics:2026-06-10T08:10:00+00:00",
        "orders-api:log:2",
        "orders-api:log:3",
        "orders-api:log:event:evt-orders-0042",
        "orders-api:log:5",
        *_INVENTORY,
    }
    assert "orders-api:log:1" not in emitted                                  # older offset
    assert "orders-api:metrics:2026-06-10T08:00:00+00:00" not in emitted      # equal ts


# ─────────────────────────────────────────────────────────────────────────────
# AC7 core — events fire ONLY for fully-processed changes
# ─────────────────────────────────────────────────────────────────────────────
def test_no_event_for_a_batch_that_fails_before_it_is_processed(captured):
    """If processing fails before a batch is fully handled, its artifacts must NOT
    be reported as changed (the runner emits only AFTER process_batch succeeds)."""
    processed: list = []

    def boom_on_third(batch):
        if len(processed) == 2:          # the 3rd batch fails before processing
            raise RuntimeError("downstream processing failed")
        processed.append(batch)

    res, _ = _drive(DotNetAppIngestor(batch_size=1), "org-1", process_batch=boom_on_third)

    events = _dotnet_events(captured)
    assert len(events) == 2                          # only the 2 processed batches emitted
    assert res.error is not None                     # the run recorded the failure
    emitted_ids = {e["artifact_id"] for e in events}
    assert len(emitted_ids) == 2
    assert emitted_ids < set(_ALL)                   # the unprocessed artifacts were NOT reported


def test_emission_failure_never_breaks_ingestion(monkeypatch):
    """Telemetry is fire-and-forget — a record_event failure must not break ingestion."""
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)
    res, _ = _drive(DotNetAppIngestor(), "org-1")
    assert res.ok
    assert res.checkpoint_advanced
    assert res.records == len(_ALL)


# ─────────────────────────────────────────────────────────────────────────────
# Reuse, not a .NET-specific event path (R17-A4 / T6 design requirement)
# ─────────────────────────────────────────────────────────────────────────────
def test_connector_does_not_mint_its_own_artifact_changed_events():
    """The .NET connector reuses the shared runner's emission — it must not call
    the telemetry emission API itself (no .NET-specific event path)."""
    import discovery.ingest.dotnet_app as dotnet_app_mod

    src = inspect.getsource(dotnet_app_mod)
    assert "record_event" not in src           # never calls the emission API
    assert "app.telemetry" not in src          # never imports the telemetry path


# ─────────────────────────────────────────────────────────────────────────────
# Runner integration — the discovery runner drives .NET through the shared runner
# ─────────────────────────────────────────────────────────────────────────────
def test_runner_helper_drives_dotnet_ingestor_through_shared_runner(monkeypatch):
    """``runner._ingest_dotnet_app_corroboration`` — the single path that ingests
    .NET for a discovery run — must drive the real DotNetAppIngestor through the
    shared change runner (the only emission path), not a bespoke one."""
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

    payload = runner._ingest_dotnet_app_corroboration("org-1", "run-1")

    assert isinstance(seen.get("ingestor"), DotNetAppIngestor)
    assert seen["org_id"] == "org-1"
    # The helper returns the corroboration block built from the collected records
    # (empty here — the spy processes no batches), keyed for the engine's COR-10.
    assert isinstance(payload, dict) and "dotnet_app" in payload
