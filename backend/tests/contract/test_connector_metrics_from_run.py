from __future__ import annotations

import logging
from typing import Any, Dict

from app import connector_metrics


def _connector_records() -> Dict[str, Dict[str, Any]]:
    return {
        "salesforce": {
            "id": "salesforce",
            "metrics": [{"label": "Loans", "value": "60"}],
            "lastSynced": "\u2014",
            "signalStrength": 94,
        },
        "servicenow": {
            "id": "servicenow",
            "metrics": [{"label": "Incidents (90d)", "value": "—"}],
            "lastSynced": "\u2014",
            "signalStrength": 88,
        },
        "jira_confluence": {
            "id": "jira_confluence",
            "metrics": [{"label": "Issues (90d)", "value": "—"}],
            "lastSynced": "\u2014",
            "signalStrength": 85,
        },
    }


def test_update_connector_metrics_from_completed_run(monkeypatch) -> None:
    records = _connector_records()
    upserted: list[str] = []

    def fake_get_one(table: str, id_: str) -> Dict[str, Any] | None:
        assert table == "connectors"
        return records.get(id_)

    def fake_upsert(table: str, id_: str, payload: Dict[str, Any]) -> None:
        assert table == "connectors"
        upserted.append(id_)
        records[id_] = dict(payload)

    monkeypatch.setattr(connector_metrics.db, "get_one", fake_get_one)
    monkeypatch.setattr(connector_metrics.db, "upsert", fake_upsert)

    payload = {
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
        payload, ["salesforce", "servicenow", "jira"]
    )

    assert upserted == ["salesforce", "servicenow", "jira_confluence"]
    assert records["salesforce"]["metrics"] == [
        {"label": "Loans", "value": "60"},
        {"label": "Covenants", "value": "65"},
    ]
    assert records["servicenow"]["metrics"] == [
        {"label": "Incidents (90d)", "value": "130"},
        {"label": "Lending signals", "value": "2"},
    ]
    assert records["jira_confluence"]["metrics"] == [
        {"label": "Issues (90d)", "value": "3"},
        {"label": "Lending signals", "value": "2"},
    ]
    assert records["salesforce"]["lastSynced"] == "Just now"
    assert records["servicenow"]["lastSynced"] == "Just now"
    assert records["jira_confluence"]["lastSynced"] == "Just now"


def test_connector_metrics_update_accepts_jira_connector_alias(monkeypatch) -> None:
    records = _connector_records()

    monkeypatch.setattr(connector_metrics.db, "get_one", lambda table, id_: records.get(id_))
    monkeypatch.setattr(
        connector_metrics.db,
        "upsert",
        lambda table, id_, payload: records.__setitem__(id_, dict(payload)),
    )

    connector_metrics.update_connector_metrics_from_run(
        {"opportunities": [{"evidence": [{"id": "ev_1", "source": "Jira"}]}]},
        ["jira_confluence"],
    )

    assert records["jira_confluence"]["metrics"][0]["value"] == "1"
    assert records["salesforce"]["lastSynced"] == "\u2014"


def test_connector_metrics_update_is_non_blocking(monkeypatch, caplog) -> None:
    def fail_get_one(table: str, id_: str) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(connector_metrics.db, "get_one", fail_get_one)

    with caplog.at_level(logging.WARNING):
        connector_metrics.update_connector_metrics_from_run(
            {"opportunities": [{"evidence": [{"source": "Jira"}]}]},
            ["jira"],
        )

    assert "Connector metrics update failed (non-blocking)" in caplog.text
