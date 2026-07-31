"""Run Health "Last ingestion" — the connector-agnostic completion channel.

The connectors panel previously read only ``db.ingestor_completed`` (native DB
ingestors) with a fallback to the ``lastSynced`` display string that
``app/connector_metrics.py`` writes for three hardcoded ids. Every change-based
connector ingested successfully and reported "Not available".

``connectors_view`` now also reads ``ingestion.completed`` — emitted by the shared
change-based ingestion runner for EVERY ``ChangeBasedIngestor`` — and takes the
newest timestamp across both channels. These tests pin that merge and prove the
pre-existing behaviour (DB ingestors, the lastSynced fallback, ``last_error``) is
unchanged.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app import health_aggregation as health

DB_EVENT = "db.ingestor_completed"
COMPLETION_EVENT = "ingestion.completed"


def _event(connector_id: str, iso: str, **payload):
    """A telemetry row as the read API returns it (timestamp + promoted columns)."""
    return SimpleNamespace(
        connector_id=connector_id,
        timestamp=datetime.fromisoformat(iso.replace("Z", "+00:00")),
        payload=dict(payload),
    )


@pytest.fixture
def panel(monkeypatch):
    """Drive connectors_view with injected connector records and telemetry."""

    def _run(records, events_by_type):
        monkeypatch.setattr(health.db, "org_connectors_list", lambda _org: list(records))
        monkeypatch.setattr(health, "_read_checkpoint", lambda _o, _c: None)
        monkeypatch.setattr(health, "_auth_mode", lambda _o, _c: "oauth")
        monkeypatch.setattr(
            health, "_safe_range", lambda _org, etype: list(events_by_type.get(etype, []))
        )
        return {entry["connector_id"]: entry for entry in health.connectors_view("org-1")}

    return _run


def _connected(connector_id, **over):
    record = {"id": connector_id, "name": connector_id, "status": "connected", "configured": True}
    record.update(over)
    return record


# ── the fix ──────────────────────────────────────────────────────────────────


def test_change_based_connector_reports_its_ingestion_time(panel):
    # The reported bug: connected, no db.ingestor_completed, lastSynced still the
    # seed em-dash. Previously "Not available"; now the completion event answers.
    view = panel(
        [_connected("azure_events", lastSynced="—")],
        {COMPLETION_EVENT: [_event("azure_events", "2026-07-30T10:15:00Z", count=127)]},
    )
    assert view["azure_events"]["last_successful_ingestion"] == "2026-07-30T10:15:00+00:00"


def test_no_connector_is_named_an_unknown_id_works_identically(panel):
    view = panel(
        [_connected("brand_new_thing")],
        {COMPLETION_EVENT: [_event("brand_new_thing", "2026-07-30T11:00:00Z")]},
    )
    assert view["brand_new_thing"]["last_successful_ingestion"] is not None


def test_zero_record_pass_still_reports_a_time(panel):
    view = panel(
        [_connected("documents")],
        {COMPLETION_EVENT: [_event("documents", "2026-07-30T09:00:00Z", count=0)]},
    )
    assert view["documents"]["last_successful_ingestion"] == "2026-07-30T09:00:00+00:00"


def test_newest_event_wins_and_other_connectors_are_ignored(panel):
    view = panel(
        [_connected("slack"), _connected("teams")],
        {
            COMPLETION_EVENT: [
                _event("slack", "2026-07-28T08:00:00Z"),
                _event("slack", "2026-07-30T08:00:00Z"),
            ]
        },
    )
    assert view["slack"]["last_successful_ingestion"] == "2026-07-30T08:00:00+00:00"
    assert view["teams"]["last_successful_ingestion"] is None


# ── both channels ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "db_iso, completion_iso, expected",
    [
        ("2026-07-30T07:00:00Z", "2026-07-30T12:00:00Z", "2026-07-30T12:00:00+00:00"),
        ("2026-07-30T12:00:00Z", "2026-07-30T07:00:00Z", "2026-07-30T12:00:00+00:00"),
    ],
)
def test_newest_of_the_two_channels_wins(panel, db_iso, completion_iso, expected):
    view = panel(
        [_connected("sqlserver")],
        {
            DB_EVENT: [_event("sqlserver", db_iso)],
            COMPLETION_EVENT: [_event("sqlserver", completion_iso)],
        },
    )
    assert view["sqlserver"]["last_successful_ingestion"] == expected


# ── existing behaviour preserved ─────────────────────────────────────────────


def test_db_ingestor_alone_is_unchanged(panel):
    view = panel(
        [_connected("oracle")],
        {DB_EVENT: [_event("oracle", "2026-07-30T06:00:00Z")]},
    )
    assert view["oracle"]["last_successful_ingestion"] == "2026-07-30T06:00:00+00:00"


def test_last_synced_fallback_is_unchanged(panel):
    # Salesforce/ServiceNow/Jira are not on the change-based path; they still rely
    # on the connector_metrics overlay.
    view = panel([_connected("salesforce", lastSynced="Just now")], {})
    assert view["salesforce"]["last_successful_ingestion"] == "Just now"


def test_em_dash_last_synced_still_reads_as_absent(panel):
    view = panel([_connected("salesforce", lastSynced="—")], {})
    assert view["salesforce"]["last_successful_ingestion"] is None


def test_completion_event_takes_precedence_over_the_last_synced_string(panel):
    # A real timestamp beats the "Just now" display string.
    view = panel(
        [_connected("git_content", lastSynced="Just now")],
        {COMPLETION_EVENT: [_event("git_content", "2026-07-30T10:00:00Z")]},
    )
    assert view["git_content"]["last_successful_ingestion"] == "2026-07-30T10:00:00+00:00"


def test_last_error_still_comes_from_the_db_channel_only(panel):
    # last_error is derived from db.ingestor_completed's degraded_count. The new
    # channel must not participate in it.
    view = panel(
        [_connected("sqlserver")],
        {DB_EVENT: [_event("sqlserver", "2026-07-30T06:00:00Z", degraded_count=3)]},
    )
    assert view["sqlserver"]["last_error"] == "3 record(s) degraded during last ingestion"


def test_completion_event_never_invents_a_last_error(panel):
    view = panel(
        [_connected("azure_events")],
        {COMPLETION_EVENT: [_event("azure_events", "2026-07-30T10:00:00Z", count=127)]},
    )
    assert view["azure_events"]["last_error"] is None


def test_newest_timestamp_ignores_none_and_undated_events():
    dated = _event("x", "2026-07-30T10:00:00Z")
    undated = SimpleNamespace(connector_id="x", timestamp=None, payload={})
    assert health._newest_timestamp(None, undated, dated) == "2026-07-30T10:00:00+00:00"
    assert health._newest_timestamp(None, undated) is None
    assert health._newest_timestamp() is None
