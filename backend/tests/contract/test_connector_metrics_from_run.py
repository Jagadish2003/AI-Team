from __future__ import annotations

import logging
from typing import Any, Dict

from app import connector_metrics

_SEP = "::"


def _connector_records() -> Dict[str, Dict[str, Any]]:
    return {
        "salesforce": {
            "id": "salesforce",
            "metrics": [{"label": "Loans", "value": "60"}],
            "lastSynced": "—",
            "signalStrength": 94,
        },
        "servicenow": {
            "id": "servicenow",
            "metrics": [{"label": "Incidents (90d)", "value": "—"}],
            "lastSynced": "—",
            "signalStrength": 88,
        },
        "jira_confluence": {
            "id": "jira_confluence",
            "metrics": [{"label": "Issues (90d)", "value": "—"}],
            "lastSynced": "—",
            "signalStrength": 85,
        },
    }


def _install_fake_store(monkeypatch, records: Dict[str, Dict[str, Any]]) -> list[str]:
    """Patch db.get_one / db.upsert so the org-scoped connector helpers
    (org_connector_get / org_connector_set) operate over an in-memory dict.

    Returns the list that records every upserted key, in order.
    """
    upserted: list[str] = []

    def fake_get_one(table: str, id_: str) -> Dict[str, Any] | None:
        assert table == "connectors"
        row = records.get(id_)
        # The real db.get_one deserializes a fresh dict from JSON each call, so a
        # caller mutating the returned row can never reach back into the stored
        # row. Mirror that here (return a copy) — otherwise the in-memory fake
        # would let org_connector_get's catalog passthrough alias the seed row.
        return dict(row) if row is not None else None

    def fake_upsert(table: str, id_: str, payload: Dict[str, Any]) -> None:
        assert table == "connectors"
        upserted.append(id_)
        records[id_] = dict(payload)

    monkeypatch.setattr(connector_metrics.db, "get_one", fake_get_one)
    monkeypatch.setattr(connector_metrics.db, "upsert", fake_upsert)
    return upserted


def test_update_connector_metrics_from_completed_run(monkeypatch) -> None:
    records = _connector_records()
    upserted = _install_fake_store(monkeypatch, records)
    org_id = "org_metrics"

    payload = {
        "completedAt": "2026-08-07T08:15:00+00:00",
        "inputs": {
            "sn_total_incidents_90d": 130,
            "sn_lending_signal_count": 5,
            "jira_total_issues_90d": 3,
            "jira_lending_signal_count": 5,
        },
        "opportunities": [
            {
                "raw_evidence": {"total_loans": 60},
                "evidence": [
                    {"id": "ev_sf_1", "source": "Salesforce"},
                    {"id": "ev_sn_1", "source": "ServiceNow"},
                    {"id": "ev_jira_1", "source": "Jira"},
                ],
            },
            {
                "raw_evidence": {"total_covenants": 65},
                "evidence": [
                    {"id": "ev_sn_2", "source": "ServiceNow"},
                    {"id": "ev_jira_2", "source": "Jira"},
                ],
            },
            {
                "raw_evidence": {"total_loans": 20, "total_covenants": 5},
                "evidence": [{"id": "ev_jira_2", "source": "Jira"}],
            },
        ]
    }

    connector_metrics.update_connector_metrics_from_run(
        payload, ["salesforce", "servicenow", "jira"], org_id
    )

    # R17-D3 / AT-448: metrics are written to the org-namespaced overlay rows,
    # never the shared catalog rows.
    assert upserted == [
        f"{org_id}{_SEP}salesforce",
        f"{org_id}{_SEP}servicenow",
        f"{org_id}{_SEP}jira_confluence",
    ]
    assert records[f"{org_id}{_SEP}salesforce"]["metrics"] == [
        {"label": "Loans", "value": "60"},
        {"label": "Covenants", "value": "65"},
    ]
    assert records[f"{org_id}{_SEP}servicenow"]["metrics"] == [
        {"label": "Incidents (90d)", "value": "130"},
        {"label": "Lending signals", "value": "2"},
    ]
    assert records[f"{org_id}{_SEP}jira_confluence"]["metrics"] == [
        {"label": "Issues (90d)", "value": "3"},
        {"label": "Lending signals", "value": "2"},
    ]
    assert records[f"{org_id}{_SEP}salesforce"]["lastSynced"] == "Just now"
    assert records[f"{org_id}{_SEP}servicenow"]["lastSynced"] == "Just now"
    assert records[f"{org_id}{_SEP}jira_confluence"]["lastSynced"] == "Just now"
    assert (
        records[f"{org_id}{_SEP}salesforce"]["lastSuccessfulIngestionAt"]
        == "2026-08-07T08:15:00+00:00"
    )
    assert (
        records[f"{org_id}{_SEP}servicenow"]["lastSuccessfulIngestionAt"]
        == "2026-08-07T08:15:00+00:00"
    )
    assert (
        records[f"{org_id}{_SEP}jira_confluence"]["lastSuccessfulIngestionAt"]
        == "2026-08-07T08:15:00+00:00"
    )

    # The shared catalog rows are never mutated — another org reading the catalog
    # default must not see this org's run metrics or its "Just now" sync time.
    assert records["salesforce"]["metrics"] == [{"label": "Loans", "value": "60"}]
    assert records["salesforce"]["lastSynced"] == "—"
    assert records["servicenow"]["lastSynced"] == "—"
    assert records["jira_confluence"]["lastSynced"] == "—"


def test_metrics_are_isolated_between_orgs(monkeypatch) -> None:
    """A run in org A must not change the connector metrics org B sees."""
    records = _connector_records()
    _install_fake_store(monkeypatch, records)
    monkeypatch.setattr(
        connector_metrics.db, "now_iso", lambda: "2026-08-07T09:00:00+00:00"
    )

    connector_metrics.update_connector_metrics_from_run(
        {"opportunities": [{"evidence": [{"id": "ev_1", "source": "Jira"}]}]},
        ["jira_confluence"],
        "org_A",
    )

    # org_A has its own overlay row with refreshed metrics.
    assert records[f"org_A{_SEP}jira_confluence"]["lastSynced"] == "Just now"
    assert (
        records[f"org_A{_SEP}jira_confluence"]["lastSuccessfulIngestionAt"]
        == "2026-08-07T09:00:00+00:00"
    )
    # org_B never got an overlay row — it still resolves to the catalog default.
    assert f"org_B{_SEP}jira_confluence" not in records
    assert records["jira_confluence"]["lastSynced"] == "—"


def test_connector_metrics_update_accepts_jira_connector_alias(monkeypatch) -> None:
    records = _connector_records()
    _install_fake_store(monkeypatch, records)

    connector_metrics.update_connector_metrics_from_run(
        {"opportunities": [{"evidence": [{"id": "ev_1", "source": "Jira"}]}]},
        ["jira_confluence"],
        "default",
    )

    assert records[f"default{_SEP}jira_confluence"]["metrics"][0]["value"] == "1"
    # The salesforce overlay was never created by a jira-only run.
    assert f"default{_SEP}salesforce" not in records


def test_connector_metrics_update_accepts_salesforce_product_alias(monkeypatch) -> None:
    records = _connector_records()
    _install_fake_store(monkeypatch, records)
    monkeypatch.setattr(
        connector_metrics.db, "now_iso", lambda: "2026-08-07T09:30:00+00:00"
    )

    connector_metrics.update_connector_metrics_from_run(
        {"opportunities": [{"raw_evidence": {"total_loans": 4}}]},
        ["salesforce_ncino"],
        "default",
    )

    assert records[f"default{_SEP}salesforce"]["metrics"][0]["value"] == "4"
    assert (
        records[f"default{_SEP}salesforce"]["lastSuccessfulIngestionAt"]
        == "2026-08-07T09:30:00+00:00"
    )


def test_connector_metrics_update_is_non_blocking(monkeypatch, caplog) -> None:
    def fail_get_one(table: str, id_: str) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(connector_metrics.db, "get_one", fail_get_one)

    with caplog.at_level(logging.WARNING):
        connector_metrics.update_connector_metrics_from_run(
            {"opportunities": [{"evidence": [{"source": "Jira"}]}]},
            ["jira"],
            "default",
        )

    assert "Connector metrics update failed (non-blocking)" in caplog.text
