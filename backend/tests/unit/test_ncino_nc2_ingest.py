"""NC-2 nCino ingest unit coverage used by the Sprint 5 exit suite."""
from __future__ import annotations

from datetime import date


def test_stage_history_query_uses_parentid_and_createdbyid() -> None:
    from discovery.ingest.ncino import _fetch_stage_history

    class Client:
        soql = ""

        def query(self, soql):
            self.soql = soql
            return []

    client = Client()
    _fetch_stage_history(client)

    assert "ParentId" in client.soql
    assert "CreatedById" in client.soql
    assert "CreatedByIdId" not in client.soql


def test_loan_query_uses_confirmed_loan_type_code() -> None:
    from discovery.ingest.ncino import _fetch_loans

    class Client:
        soql = ""

        def query(self, soql):
            self.soql = soql
            return []

    client = Client()
    _fetch_loans(client)

    assert "LLC_BI__Loan_Type_Code__c" in client.soql
    assert "OwnerId" in client.soql


def test_origination_metrics_count_stage_transitions_and_owner_changes() -> None:
    from discovery.ingest.ncino import _build_origination_metrics

    loans = [{"Id": "loan_001", "LLC_BI__Loan_Type_Code__c": "TERM"}]
    history = [
        {"ParentId": "loan_001", "CreatedDate": "2026-01-01", "CreatedById": "u1"},
        {"ParentId": "loan_001", "CreatedDate": "2026-01-02", "CreatedById": "u2"},
        {"ParentId": "loan_001", "CreatedDate": "2026-01-03", "CreatedById": "u3"},
        {"ParentId": "loan_001", "CreatedDate": "2026-01-04", "CreatedById": "u4"},
    ]

    metrics = _build_origination_metrics(loans, history)

    assert metrics["total_loans"] == 1
    assert metrics["max_stage_transitions"] == 4
    assert metrics["max_owner_changes"] == 3
    assert metrics["high_friction_loans"][0]["loan_type"] == "TERM"


def test_covenant_metrics_set_compliance_override_on_breach() -> None:
    from discovery.ingest.ncino import _build_covenant_metrics

    metrics = _build_covenant_metrics(
        [
            {
                "Id": "cov_001",
                "LLC_BI__Overdue__c": True,
                "LLC_BI__Breached__c": True,
                "LLC_BI__Days_Past_Next_Evaluation__c": 9,
            }
        ]
    )

    assert metrics["overdue_count"] == 1
    assert metrics["breached_count"] == 1
    assert metrics["compliance_override"] is True


def test_checklist_metrics_detect_overrun_and_stalled_status() -> None:
    from discovery.ingest.ncino import _build_checklist_metrics

    metrics = _build_checklist_metrics(
        [
            {
                "Id": "chk_001",
                "LLC_BI__Status__c": "To Do",
                "LLC_BI__Actual_Duration_Days__c": 20,
                "LLC_BI__Expected_Duration_Days__c": 5,
                "CreatedDate": "2026-01-01",
            }
        ]
    )

    assert metrics["overrun_count"] == 1
    assert metrics["stalled_count"] >= 1
    assert metrics["max_overrun_days"] == 15


def test_spreading_metrics_resolve_loan_via_spread_header() -> None:
    from discovery.ingest.ncino import _build_spreading_metrics

    metrics = _build_spreading_metrics(
        [
            {
                "Id": "period_001",
                "LLC_BI__Spread__c": "spread_001",
                "LLC_BI__Analyst__c": "analyst_001",
                "IsLocked__c": False,
                "CreatedDate": "2026-01-01",
            }
        ],
        [{"Id": "spread_001", "LLC_BI__Loan__c": "loan_001"}],
    )

    assert metrics["unlocked_count"] == 1
    assert metrics["bottleneck_records"][0]["loan_id"] == "loan_001"
    assert metrics["bottleneck_records"][0]["analyst_id"] == "analyst_001"


def test_approval_metrics_filter_to_loan_ids() -> None:
    from discovery.ingest.ncino import _build_approval_metrics

    metrics = _build_approval_metrics(
        [
            {
                "Id": "pi_001",
                "TargetObjectId": "loan_001",
                "Status": "Pending",
                "CreatedDate": "2026-01-01",
                "CompletedDate": None,
            },
            {
                "Id": "pi_002",
                "TargetObjectId": "case_001",
                "Status": "Pending",
                "CreatedDate": "2026-01-01",
                "CompletedDate": None,
            },
        ],
        {"loan_001"},
    )

    assert metrics["total_instances"] == 1
    assert metrics["pending_count"] == 1


class _RecordingClient:
    """Fake nCino REST client that records every SOQL query it receives."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, soql: str):
        self.queries.append(soql)
        return []


def _live_ingest(monkeypatch, **kwargs):
    """Run ncino.ingest() in live mode against a recording client."""
    from discovery.ingest import ncino

    client = _RecordingClient()
    monkeypatch.setattr(ncino, "is_live", lambda: True)
    monkeypatch.setattr(ncino, "_get_client", lambda: client)
    result = ncino.ingest(**kwargs)
    return result, client


def _process_instance_query_count(client: "_RecordingClient") -> int:
    return sum(1 for q in client.queries if "FROM ProcessInstance" in q)


def test_ingest_skips_processinstance_query_when_preloaded(monkeypatch) -> None:
    """AT-309 AC1: preloaded data => zero ProcessInstance API calls."""
    preloaded = [
        {
            "Id": "pi_001",
            "TargetObjectId": "loan_001",
            "Status": "Pending",
            "CreatedDate": "2026-01-01",
            "CompletedDate": None,
        }
    ]

    result, client = _live_ingest(
        monkeypatch, preloaded_process_instances=preloaded
    )

    assert _process_instance_query_count(client) == 0
    assert result["process_instances"] == preloaded


def test_ingest_fetches_processinstance_when_not_preloaded(monkeypatch) -> None:
    """AT-309: fallback path still fetches ProcessInstance via the live query."""
    result, client = _live_ingest(monkeypatch)

    assert _process_instance_query_count(client) == 1
    assert result["process_instances"] == []


def test_ingest_empty_preloaded_list_still_skips_fetch(monkeypatch) -> None:
    """AT-310: an empty list (no approval_processes) must NOT trigger a fetch."""
    result, client = _live_ingest(monkeypatch, preloaded_process_instances=[])

    assert _process_instance_query_count(client) == 0
    assert result["process_instances"] == []


def test_ingest_preloaded_reduces_salesforce_call_count_by_one(monkeypatch) -> None:
    """AT-309 AC2: total Salesforce queries drop by exactly 1 when preloaded."""
    _, fallback_client = _live_ingest(monkeypatch)
    _, preloaded_client = _live_ingest(
        monkeypatch, preloaded_process_instances=[]
    )

    assert (
        len(fallback_client.queries) - len(preloaded_client.queries) == 1
    )


def test_ingest_signature_defaults_preloaded_to_none(monkeypatch) -> None:
    """preloaded_process_instances defaults to None and offline mode is unaffected."""
    import inspect

    from discovery.ingest import ncino

    sig = inspect.signature(ncino.ingest)
    assert sig.parameters["preloaded_process_instances"].default is None

    monkeypatch.setattr(ncino, "is_live", lambda: False)
    result = ncino.ingest()
    assert "process_instances" in result


def test_stage_duration_helpers_are_deterministic_for_supplied_dates() -> None:
    from discovery.ingest.ncino import (
        derive_approval_cycle_days,
        derive_stage_duration_days,
        derive_stage_duration_overrun,
    )

    assert derive_stage_duration_days(date(2026, 1, 1), date(2026, 1, 6)) == 5
    assert derive_stage_duration_overrun("Approval", 8) == 3
    assert derive_approval_cycle_days(date(2026, 1, 1), date(2026, 1, 4)) == 3
