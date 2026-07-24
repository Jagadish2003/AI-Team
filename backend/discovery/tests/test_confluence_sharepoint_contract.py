"""
R17-A2 / AT-467 (T8) — consolidated contract suite for the Confluence & SharePoint
connectors (Section 5).

The per-subtask suites already cover each connector in depth:
  * OAuth flow + token-in-vault (AC1)   — tests/contract/test_{confluence,sharepoint}_connector_oauth.py
  * incremental / resumable / access    — discovery/tests/test_{…}_ingestor.py
  * reach-phase signal, no body (AC7)   — discovery/tests/test_{…}_signals.py
  * EvidencePointer on every signal      — discovery/tests/test_{…}_evidence_pointer.py
  * ingestion.artifact_changed events    — discovery/tests/test_{…}_artifact_changed_events.py

This file is the *contract* guard the story's Section 5 asks for: ONE suite that
runs the SAME assertions across BOTH connectors, so the two "paired by nature"
reach-phase connectors are held to an identical contract and neither can drift
from it as the platform evolves. Each test is parametrized over both connectors
and maps to an acceptance criterion.

Everything here is offline (the deterministic fixtures) and needs no DB: the
change-runner checkpoint lifecycle is exercised through its in-memory seam, and
telemetry is captured via monkeypatch. The token-in-vault half of AC1 (which
needs the credential vault) is validated by the DB-backed OAuth contract tests
above; here AC1 is asserted at the OAuth-config contract level.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List

import pytest

from app.provenance import OBSERVED, EvidencePointer
from discovery.ingest import change_runner
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint
from discovery.ingest.confluence import ConfluenceIngestor
from discovery.ingest.sharepoint import SharePointIngestor

EVENT = "ingestion.artifact_changed"

# Scope fragments that would grant write / management access. Both connectors are
# strictly read-only (reach phase reads activity/metadata, never mutates), so none
# of these may appear in either connector's OAuth scopes (permission boundary).
_WRITE_SCOPE_FRAGMENTS = ("write", "manage", "fullcontrol", ".send")


@dataclass
class ConnectorCase:
    name: str
    connector_id: str
    make: Callable[[int], ChangeBasedIngestor]  # batch_size -> ingestor instance
    # An artifact id that lives in an UNGRANTED / excluded scope — it must never
    # appear in any emitted record (AC4 permission boundary).
    excluded_artifact_id: str


CASES: List[ConnectorCase] = [
    ConnectorCase(
        name="confluence",
        connector_id="confluence",
        make=lambda bs: ConfluenceIngestor(batch_size=bs),
        excluded_artifact_id="HR:900",  # ungranted Confluence space
    ),
    ConnectorCase(
        name="sharepoint",
        connector_id="sharepoint",
        make=lambda bs: SharePointIngestor(batch_size=bs),
        excluded_artifact_id="S-secret/x-docs:x100",  # ungranted SharePoint site
    ),
]
_IDS = [c.name for c in CASES]


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Pin offline so the real ingestors read their deterministic fixtures rather
    than attempting a live Atlassian / Microsoft Graph call."""
    monkeypatch.setenv("INGEST_MODE", "offline")


@pytest.fixture
def captured(monkeypatch):
    """Capture telemetry events (the change runner lazily imports record_event)."""
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


def _drive(ingestor, org_id, store, **kw):
    return change_runner.ingest_with_checkpoint(
        ingestor, org_id, read_checkpoint=store.read, save_checkpoint=store.save, **kw
    )


def _all_records(case: ConnectorCase):
    return [r for b in case.make(100).ingest_changes("org1", None) for r in b.records]


# ─────────────────────────────────────────────────────────────────────────────
# Shared shape
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_implements_change_based_ingestor(case: ConnectorCase):
    ing = case.make(100)
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == case.connector_id
    # R18-A5 (T4): Confluence now detects deletions via a full-inventory id diff
    # (AT-603, reports_deletes=True); SharePoint content deletion/archival is
    # deferred to R18-B2, so its content ingestor still declares reports_deletes=False.
    expected_reports_deletes = case.connector_id == "confluence"
    assert ing.reports_deletes is expected_reports_deletes


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — OAuth flow contract (token-in-vault is covered by the DB-backed OAuth tests)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_ac1_oauth_config_is_authorization_code_and_least_privilege(case: ConnectorCase):
    from app.auth.configs import CONNECTOR_AUTH_CONFIGS

    cfg = CONNECTOR_AUTH_CONFIGS[case.connector_id]
    assert cfg.flow == "authorization_code"
    # Real provider authorize + token endpoints (not a dead end).
    assert cfg.authorization_url.startswith("https://")
    assert cfg.token_url.startswith("https://")
    # Secret referenced by env-var name only (stored in the vault, never in code).
    assert cfg.secret_key.endswith("_CLIENT_SECRET")
    # offline_access → a refresh token so the access token auto-refreshes.
    assert "offline_access" in cfg.scopes
    # Least-privilege / read-only: no write, manage, or send scope (AC4 at the
    # scope level — the connectors only ever read activity/metadata signal).
    for scope in cfg.scopes:
        low = scope.lower()
        for frag in _WRITE_SCOPE_FRAGMENTS:
            assert frag not in low, f"{case.name} scope {scope!r} grants write access"


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — incremental returns only changed; unchanged source returns an empty delta
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_ac2_first_run_loads_all_then_unchanged_is_empty_delta(case: ConnectorCase):
    all_ids = {r["artifact_id"] for r in _all_records(case)}
    assert all_ids  # the fixture yields changed artifacts

    store = Store()
    seen: list = []
    res = _drive(
        case.make(100),
        "org1",
        store,
        process_batch=lambda b: seen.extend(r["artifact_id"] for r in b.records),
    )
    assert res.ok and res.checkpoint_advanced
    assert set(seen) == all_ids
    head = store.read("org1", case.connector_id).value

    # Second run with nothing new → empty delta; the position never regresses.
    res2 = _drive(case.make(100), "org1", store)
    assert res2.ok
    assert res2.records == 0
    assert store.read("org1", case.connector_id).value == head


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — resumable, checkpointed first load (a mid-load failure resumes without
#        loss or duplication; the resume also proves incremental returns ONLY the
#        not-yet-seen artifacts — AC2)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_ac3_first_load_is_resumable_without_loss_or_duplication(case: ConnectorCase):
    all_ids = {r["artifact_id"] for r in _all_records(case)}
    total = len(all_ids)
    assert total >= 3  # enough to fail mid-load

    store = Store()
    processed: list = []

    def fail_on_third(batch):
        processed.extend(r["artifact_id"] for r in batch.records)
        if len(processed) == 3:
            raise RuntimeError("network dropped mid initial load")

    res1 = _drive(case.make(1), "org1", store, process_batch=fail_on_third)
    assert res1.ok is False
    # Batches 1 & 2 fully processed AND checkpointed; batch 3 raised before its
    # checkpoint was written (streamed resumable first load).
    assert res1.batches_checkpointed == 2

    # Resume: a checkpoint now exists → incremental mode. It returns EXACTLY the
    # not-yet-seen artifacts (AC2), so no work is lost or duplicated (AC3).
    resumed: list = []
    res2 = _drive(
        case.make(1),
        "org1",
        store,
        process_batch=lambda b: resumed.extend(r["artifact_id"] for r in b.records),
    )
    assert res2.ok and res2.checkpoint_advanced
    already = processed[:2]
    assert not set(already) & set(resumed)          # the 2 saved items are not re-read
    combined = already + resumed
    assert sorted(combined) == sorted(all_ids)       # every artifact processed
    assert len(combined) == len(set(combined)) == total  # exactly once (no dup/loss)


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — only granted spaces/sites are read; the source's permissions are respected
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_ac4_ungranted_scope_never_emitted(case: ConnectorCase):
    ids = {r["artifact_id"] for r in _all_records(case)}
    assert case.excluded_artifact_id not in ids


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — every signal carries a valid observed EvidencePointer
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_ac5_every_record_has_valid_observed_evidence_pointer(case: ConnectorCase):
    records = _all_records(case)
    assert records
    for r in records:
        ptr = r.get("evidence_pointer")
        assert ptr is not None, f"{case.name} record {r.get('artifact_id')} has no evidence_pointer"
        assert ptr["source_system"] == case.connector_id
        assert ptr["source_artifact"] == r["artifact_id"]  # the page/document id
        assert ptr["source_timestamp"]                      # a timestamp
        assert ptr["origin"] == OBSERVED
        assert ptr["extraction_job_id"] is None             # observed → no inference job
        assert EvidencePointer.from_dict(ptr).is_valid() is True


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — every changed artifact emits an ingestion.artifact_changed event
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_ac6_changed_artifacts_emit_events(case: ConnectorCase, captured):
    from app.telemetry import REGISTERED_EVENT_TYPES

    assert EVENT in REGISTERED_EVENT_TYPES  # precondition: registered, or emit raises

    all_ids = {r["artifact_id"] for r in _all_records(case)}
    _drive(case.make(100), "org-evt", Store())

    events = [p for (e, p) in captured if e == EVENT]
    assert {e["artifact_id"] for e in events} == all_ids
    required = {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    for e in events:
        assert required <= set(e.keys())
        assert e["org_id"] == "org-evt"
        assert e["connector_id"] == case.connector_id
        assert e["change_kind"] in ("created", "updated", "deleted")
        datetime.fromisoformat(e["observed_at"])  # valid UTC ISO timestamp


@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_ac6_unchanged_source_emits_no_events(case: ConnectorCase, captured):
    store = Store()
    _drive(case.make(100), "org-evt", store)  # first run emits for all
    captured.clear()
    _drive(case.make(100), "org-evt", store)  # nothing new → no events
    assert [p for (e, p) in captured if e == EVENT] == []


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — activity/metadata signal only: no document/page BODY is ever read
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_ac7_records_are_metadata_signal_only_no_body(case: ConnectorCase):
    records = _all_records(case)
    assert records
    for r in records:
        # No document/page body under any of the common body/content keys.
        assert "body" not in r
        assert "content" not in r
        # Reach-phase signal travels with the delta, and is metadata-only: exactly
        # cross-reference markers (from the title / item name) + activity counts.
        assert "signals" in r
        assert set(r["signals"].keys()) == {"cross_references", "activity"}
