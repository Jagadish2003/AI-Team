from app.executive_report_engine import build_executive_report
from app.materialize_t2 import _selected_system_ids_for_report
from app.opportunity_display import with_exec_report_display_titles
from discovery.ingest.strs_sn_corroboration import fetch_strs_sn_incidents


def test_executive_report_counts_selected_system_ids():
    report = build_executive_report(
        run_id="run_001",
        opps=[],
        roadmap={},
        selected_system_ids=["salesforce", "jira", "servicenow"],
    )

    assert report["sourcesAnalyzed"]["totalConnected"] == 3


def test_executive_report_confidence_uses_contract_title_case():
    report = build_executive_report(
        run_id="run_001",
        opps=[],
        roadmap={},
    )

    assert report["confidence"] == "Low"


def test_persisted_executive_report_confidence_is_normalized():
    report = with_exec_report_display_titles({"confidence": "MODERATE"})

    assert report["confidence"] == "Moderate"


def test_report_system_ids_prefer_stack_builder_setup_context(monkeypatch):
    def fake_run_kv_get(key, run_id, default=None):
        assert key == "setup_context"
        assert run_id == "run_001"
        return {"selected_system_ids": ["salesforce", "jira", "servicenow"]}

    monkeypatch.setattr("app.materialize_t2.db.run_kv_get", fake_run_kv_get)

    selected = _selected_system_ids_for_report(
        "run_001",
        run={},
        run_inputs={"connectedSources": []},
        systems=["salesforce"],
    )

    assert selected == ["salesforce", "jira", "servicenow"]


def test_fetch_strs_sn_incidents_uses_servicenow_table_query():
    class FakeServiceNowClient:
        def __init__(self):
            self.calls = []

        def table_query(self, table, params):
            self.calls.append((table, params))
            return [
                {
                    "number": "INC001",
                    "short_description": "Retirement application stalled",
                    "description": "Member benefit processing backlog",
                    "state": "2",
                    "priority": "2",
                }
            ]

    client = FakeServiceNowClient()
    incidents = fetch_strs_sn_incidents(client)

    assert incidents[0]["number"] == "INC001"
    assert client.calls[0][0] == "incident"
    assert "sysparm_query" in client.calls[0][1]
