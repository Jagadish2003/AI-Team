"""Offline tests for the bridge <-> native equivalence harness — MSP-B8 / T6.

DB-free: golden fixtures are staged into an ``InMemoryStagingSink`` (used as both
sink and reader) and drained through the bridge, then compared field by field
against the direct mapper invocation. The real-DB proof is
``tests/contract/test_ops_event_equivalence_contract.py``.

Proves the core MSP-B8 promise (AC2): AWS and Azure golden fixtures pass through
BOTH paths with equivalent normalised output (only ``source_system`` differs) and
stable evidence resolution.
"""
from __future__ import annotations

import pytest

from discovery.ingest.ops_event_equivalence import (
    all_passed,
    format_report,
    load_golden_cases,
    run_equivalence,
)

_CASES = load_golden_cases()
_AWS = [c for c in _CASES if c["mapper"].startswith("map_") and "azure" not in c["mapper"]]
_AZURE = [c for c in _CASES if "azure" in c["mapper"]]


def _results():
    return run_equivalence(_CASES)


# ---------------------------------------------------------------------------
# Corpus sanity
# ---------------------------------------------------------------------------


def test_golden_corpus_has_both_providers():
    assert len(_AWS) >= 3 and len(_AZURE) >= 2  # 4 AWS + 3 Azure in the B0 corpus
    assert len(_CASES) == 7


# ---------------------------------------------------------------------------
# The core promise: every golden case is equivalent across both paths
# ---------------------------------------------------------------------------


def test_all_golden_cases_are_equivalent():
    results = _results()
    assert len(results) == len(_CASES)
    assert all_passed(results), format_report(results)


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_each_case_only_source_system_differs(case):
    [r] = [x for x in _results() if x.case_name == case["name"]]
    assert r.bridge_record_found
    assert r.unexpected_diffs == [], format_report([r])
    assert r.source_system_differs_as_expected


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_each_case_evidence_resolves_stably(case):
    [r] = [x for x in _results() if x.case_name == case["name"]]
    assert r.evidence_resolves_native
    assert r.evidence_resolves_bridge
    assert r.evidence_stable  # both paths resolve to the identical raw payload


def test_bridge_source_system_prefix_matches_provider():
    for r in _results():
        assert r.source_system_bridge == f"bridge:{r.provider}"
        assert r.source_system_native != r.source_system_bridge


# ---------------------------------------------------------------------------
# The harness produces useful diffs when equivalence FAILS (drift diagnosis)
# ---------------------------------------------------------------------------


def test_injected_mapper_drift_is_caught_with_a_useful_diff(monkeypatch):
    # Simulate mapper-contract drift: the bridge path routes to a mapper whose
    # output has changed. Patch the mapper the bridge uses so its severity drifts,
    # then confirm the harness FAILS the case and reports the exact field diff.
    import discovery.signals.reference_mappers as rm
    from discovery.ingest import ops_event_bridge as bridge_mod

    real = rm.map_cloudtrail

    def drifted(payload, *, org_id):
        ev = real(payload, org_id=org_id)
        ev.severity = "critical" if ev.severity != "critical" else "low"  # force drift
        return ev

    # The bridge resolves the mapper via its registry, which holds a direct
    # reference; patch that entry so only the BRIDGE path drifts.
    monkeypatch.setitem(bridge_mod._MAPPER_REGISTRY, ("aws", "cloudtrail"), drifted)

    results = run_equivalence(_CASES)
    ct = [r for r in results if r.mapper == "map_cloudtrail"]
    assert ct and all(not r.passed for r in ct)
    # A precise, actionable diff is surfaced.
    offending = ct[0]
    assert any(d.field == "severity" for d in offending.unexpected_diffs)
    report = format_report(results)
    assert "DIFF severity" in report
    assert "FAIL" in report


def test_missing_bridge_record_is_reported_not_crashed():
    # If the bridge emits nothing for a case, the harness records it as a failure
    # with a clear message rather than raising.
    from discovery.ingest.ops_event_staging_store import InMemoryStagingSink

    class _EmptyReader:
        def fetch_after(self, org_id, *, after_row_id, limit):
            return []

    results = run_equivalence(_CASES, sink=InMemoryStagingSink(), reader=_EmptyReader())
    assert results  # did not raise
    assert all(not r.bridge_record_found for r in results)
    assert all(not r.passed for r in results)
    assert all("no event" in r.message for r in results)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def test_report_lists_every_case_and_totals():
    report = format_report(_results())
    for c in _CASES:
        assert c["name"] in report
    assert "7/7 cases equivalent" in report


# ---------------------------------------------------------------------------
# Contract-vs-reality gap that T6 exists to surface (cheap, early)
# ---------------------------------------------------------------------------


def test_cloudwatch_export_format_reconciliation_is_a_known_gap():
    """T6 surfaces a mapper-contract gap for reconciliation before B1 inherits it.

    The B0 ``map_cloudwatch`` reference mapper (and the golden fixture above) use
    the CloudWatch *Alarm State Change* EVENT shape (EventBridge-delivered:
    ``detail.state.value``, ``resources``, ``detail-type``). The T2 export loader
    ingests the *DescribeAlarmHistory* shape (``AlarmName`` / ``HistoryItemType`` /
    ``HistoryData``). They are different shapes under the same
    ``cloudwatch_alarm_history`` token, so an alarm-history export does NOT
    normalise through ``map_cloudwatch`` cleanly — it degrades.

    This is exactly the cheap, early surfacing T6 exists for ("the contract's
    first proof"): it must be reconciled before the native B1 connector inherits
    the assumption — either B1/the exporter emits the state-change event, or a
    dedicated alarm-history mapper is registered. The four other surfaces
    (EventBridge, CloudTrail, Azure Monitor, Azure Activity Log) align cleanly and
    pass full equivalence above.
    """
    from discovery.signals.reference_mappers import map_cloudwatch

    alarm_history_item = {  # the T2 export (DescribeAlarmHistory) shape
        "AlarmName": "prod-api-5xx",
        "Timestamp": "2026-06-01T12:34:56.789+00:00",
        "HistoryItemType": "StateUpdate",
        "HistorySummary": "Alarm updated from OK to ALARM",
        "HistoryData": "{\"newState\":{\"stateValue\":\"ALARM\"}}",
    }
    degraded = map_cloudwatch(alarm_history_item, org_id="x")
    # map_cloudwatch reads detail.state / resources, which alarm-history lacks, so
    # the normalised event is degraded — the tracked reconciliation signal.
    assert degraded.resource is None
    assert not degraded.payload.get("state")
