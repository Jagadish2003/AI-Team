"""
R18-C0 P5 — the Slack ingestor honours the per-org channel selection.

The customer chooses which public channels AgentIQ reads
(PATCH /api/connectors/slack/channels, stored on the Slack connector record).
The ingestor must read ONLY the selected channels — a channel the customer did
not select is never ingested, even though the token can see it (AC5).

Runs offline against the deterministic ``slack_sample.json`` fixture. The
selection store (``app.db.org_connector_get``) is monkeypatched so no database is
required. Fixture public+member channels: C001 (ops-incidents), C002 (deploys).
"""
from __future__ import annotations

import app.db as app_db
from discovery.ingest.slack import SlackIngestor, list_selectable_channels


def _channel_ids(records):
    return {r["channel_id"] for r in records}


def _all_records(org_id="org_a"):
    ing = SlackIngestor()
    records = []
    for batch in ing.ingest_changes(org_id, None):
        records.extend(batch.records)
    return records


def _set_selection(monkeypatch, value):
    """Patch the connector record the ingestor reads its selection from."""
    def fake_get(org_id, connector_id):
        if connector_id != "slack":
            return None
        record = {"id": "slack", "status": "connected"}
        if value is not None:
            record["channels"] = value
        return record

    monkeypatch.setattr(app_db, "org_connector_get", fake_get)


def test_no_selection_reads_all_accessible_channels(monkeypatch):
    # No 'channels' key saved → backwards-compatible default: read every
    # accessible public channel.
    _set_selection(monkeypatch, None)
    ids = _channel_ids(_all_records())
    assert ids == {"C001", "C002"}


def test_selection_limits_ingestion_to_selected_channels(monkeypatch):
    # Only C001 selected → C002 must not be ingested even though it is accessible.
    _set_selection(monkeypatch, ["C001"])
    ids = _channel_ids(_all_records())
    assert ids == {"C001"}
    assert "C002" not in ids


def test_empty_selection_reads_nothing(monkeypatch):
    # An explicit empty selection means read no channels.
    _set_selection(monkeypatch, [])
    assert _all_records() == []


def test_selection_never_admits_private_or_unauthorised_channels(monkeypatch):
    # Selecting private / not-member / archived ids does not bypass the AC4
    # access guarantee — they are still excluded.
    _set_selection(monkeypatch, ["C900", "C901", "C902", "C001"])
    ids = _channel_ids(_all_records())
    assert ids == {"C001"}


def test_list_selectable_channels_returns_only_accessible(monkeypatch):
    # The options a customer picks from are the accessible public channels,
    # unaffected by any saved selection.
    _set_selection(monkeypatch, ["C001"])
    options = list_selectable_channels("org_a")
    ids = {o["id"] for o in options}
    assert ids == {"C001", "C002"}
    assert all(o["name"] for o in options)
