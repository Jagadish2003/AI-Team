"""GET /api/runs/{runId}/normalization must reflect a run that fired no detector.

Regression cover for the Source Intelligence "0 signals" defect.

Before the fix the endpoint could only describe a source once a detector had
FIRED, because both of its tiers keyed off run evidence and an evidence item
only exists behind an opportunity. A discovery run that ingested real data and
evaluated every detector below threshold therefore returned ``rows: []``, and
Source Intelligence rendered it identically to a run that ingested nothing —
"Connected sources: 4 / 0 signals mapped", every source "NO SIGNALS".

The run's per-detector evaluations are persisted to ``signal_snapshots`` either
way (``fired`` False on each), so the endpoint now derives rows from them for
any source the higher-priority tiers did not cover.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.temporal import _insert_signal_snapshots
from database.models.signal_snapshots import SignalSnapshot


ORG_ID = "default"


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {os.getenv('DEV_JWT', 'dev-token-change-me')}"}


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _snapshot(run_id: str, source: str, detector_id: str, metric: str, fired: bool):
    return SignalSnapshot(
        org_id=ORG_ID,
        run_id=run_id,
        pack_id="cloud_ops",
        detector_id=detector_id,
        signal_key=f"cloud_ops::{detector_id}::{metric}",
        metric_name=metric,
        metric_value=0.0,
        threshold=3.0,
        fired=fired,
        signal_source=source,
        captured_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def unfired_run() -> str:
    """A completed run with evaluations from two sources and NO evidence."""
    run_id = f"run_sigtest_{uuid.uuid4().hex[:8]}"
    db.run_set(run_id, {
        "id": run_id,
        "orgId": ORG_ID,
        "org_id": ORG_ID,
        "status": "complete",
        "packId": "cloud_ops",
        "packIds": ["cloud_ops"],
    })
    # Exactly what a below-threshold run leaves behind.
    db.run_kv_set("evidence", run_id, [])
    db.run_kv_set("opps", run_id, [])

    _insert_signal_snapshots([
        _snapshot(run_id, "servicenow", "QUEUE_AGEING", "open_count", False),
        _snapshot(run_id, "servicenow", "QUEUE_AGEING", "metric_value", False),
        _snapshot(run_id, "servicenow", "ALERT_TRIAGE_TOIL", "incident_volume", False),
        # A second source proves the derivation is not ServiceNow-specific.
        _snapshot(run_id, "azure_events", "ALERT_TRIAGE_TOIL", "event_count", False),
    ])
    return run_id


def test_unfired_run_still_reports_its_signals(client, unfired_run):
    """The core regression: no evidence, no fired detector — but rows are served."""
    r = client.get(f"/api/runs/{unfired_run}/normalization", headers=_auth())
    assert r.status_code == 200

    data = r.json()
    assert data["rows"], (
        "a run that evaluated detectors below threshold must still describe what "
        "it read from each source — this is the Source Intelligence '0 signals' bug"
    )
    assert data["counts"]["MAPPED"] == len(data["rows"])


def test_rows_are_attributed_to_the_canonical_source_key(client, unfired_run):
    """Rows must carry the display key the frontend joins connectors on."""
    rows = client.get(
        f"/api/runs/{unfired_run}/normalization", headers=_auth()
    ).json()["rows"]

    by_source: Dict[str, int] = {}
    for row in rows:
        by_source[row["sourceSystem"]] = by_source.get(row["sourceSystem"], 0) + 1

    # 'servicenow' → 'ServiceNow', 'azure_events' → 'Azure Events' (source_keys.py),
    # matching sourceKeyForConnector on the frontend.
    assert by_source.get("ServiceNow") == 3
    assert by_source.get("Azure Events") == 1


def test_sample_values_are_real_not_invented(client, unfired_run):
    """No fabricated numbers — sample values come from the persisted metric."""
    rows = client.get(
        f"/api/runs/{unfired_run}/normalization", headers=_auth()
    ).json()["rows"]

    for row in rows:
        assert row["sampleValues"] == ["0.0"]
        assert row["sourceField"].split(".", 1)[1] in {
            "open_count", "metric_value", "incident_volume", "event_count",
        }


def test_snapshots_only_fill_gaps_and_never_restate_a_covered_source(client):
    """Tier 3 is a gap-filler: a source tiers 1-2 already describe is left alone.

    Snapshots sit at one row per (source, detector, metric) while evidence sits at
    one row per (source, detector), so letting snapshots take over a covered source
    replaces a few summary rows with every metric of every detector that source
    ran. This page reports the summary, so a covered source keeps its evidence rows
    and only an UNCOVERED source is filled in from snapshots.
    """
    run_id = f"run_sigtest_{uuid.uuid4().hex[:8]}"
    db.run_set(run_id, {
        "id": run_id,
        "orgId": ORG_ID,
        "org_id": ORG_ID,
        "status": "complete",
        "packId": "cloud_ops",
    })
    db.run_kv_set("evidence", run_id, [
        # ServiceNow is COVERED by evidence → its snapshot rows must not be added,
        # even though the snapshots below describe the very same detector.
        {"id": "ev_1", "source": "ServiceNow", "detectorId": "QUEUE_AGEING",
         "title": "Queue ageing", "confidence": "HIGH"},
        # No detector declares Jira as a signal_source → must survive.
        {"id": "ev_2", "source": "Jira", "detectorId": "CROSS_SYSTEM_ECHO",
         "title": "Cross-system echo", "confidence": "MEDIUM"},
    ])
    _insert_signal_snapshots([
        _snapshot(run_id, "servicenow", "QUEUE_AGEING", "open_count", True),
        _snapshot(run_id, "servicenow", "QUEUE_AGEING", "metric_value", True),
        _snapshot(run_id, "azure_events", "ALERT_TRIAGE_TOIL", "event_count", False),
    ])

    rows = client.get(
        f"/api/runs/{run_id}/normalization", headers=_auth()
    ).json()["rows"]

    servicenow_rows = [r for r in rows if r["sourceSystem"] == "ServiceNow"]
    jira_rows = [r for r in rows if r["sourceSystem"] == "Jira"]
    azure_rows = [r for r in rows if r["sourceSystem"] == "Azure Events"]

    # ServiceNow is covered by evidence: it keeps its 1 summary row and gains
    # NONE of its 2 snapshot rows (no restatement, and no double count either).
    assert [r["id"] for r in servicenow_rows] == ["norm_ev_1"]
    # Jira contributed evidence but no detector signals — never dropped.
    assert [r["id"] for r in jira_rows] == ["norm_ev_2"]
    # Azure had no evidence at all — the gap IS filled from its snapshot.
    assert len(azure_rows) == 1
    assert azure_rows[0]["id"].startswith("norm_sig_")


def test_internal_merge_marker_is_not_serialized(client, unfired_run):
    """The detector key is an implementation detail, never part of the contract."""
    rows = client.get(
        f"/api/runs/{unfired_run}/normalization", headers=_auth()
    ).json()["rows"]

    for row in rows:
        assert "_detectorKey" not in row


def test_run_with_no_signals_at_all_still_returns_empty(client):
    """Honesty check: nothing ingested must still read as nothing. No invention."""
    run_id = f"run_sigtest_{uuid.uuid4().hex[:8]}"
    db.run_set(run_id, {
        "id": run_id, "orgId": ORG_ID, "org_id": ORG_ID, "status": "complete",
    })
    db.run_kv_set("evidence", run_id, [])

    data = client.get(f"/api/runs/{run_id}/normalization", headers=_auth()).json()
    assert data["rows"] == []
    assert data["counts"]["MAPPED"] == 0


def test_covered_sources_keep_their_summarized_counts(client):
    """Regression guard for the Source Intelligence summary counts.

    Reproduces the shape of a real Service Cloud run: 3 Salesforce evidence rows,
    1 ServiceNow, 1 Jira — against snapshots holding many metrics per detector for
    Salesforce and ServiceNow. The page must still report 3 / 1 / 1. Before the
    gap-fill rule this returned 29 / 36 / 1, because snapshot rows superseded the
    covered sources and were then listed at per-metric grain.
    """
    run_id = f"run_sigtest_{uuid.uuid4().hex[:8]}"
    db.run_set(run_id, {
        "id": run_id, "orgId": ORG_ID, "org_id": ORG_ID,
        "status": "complete", "packId": "service_cloud",
    })
    db.run_kv_set("evidence", run_id, [
        {"id": "ev_sf1", "source": "Salesforce", "detectorId": "APPROVAL_BOTTLENECK",
         "title": "Approvals", "confidence": "HIGH"},
        {"id": "ev_sf2", "source": "Salesforce", "detectorId": "PERMISSION_BOTTLENECK",
         "title": "Permissions", "confidence": "HIGH"},
        {"id": "ev_sf3", "source": "Salesforce", "detectorId": "REPETITIVE_AUTOMATION",
         "title": "Automation", "confidence": "HIGH"},
        {"id": "ev_sn1", "source": "ServiceNow", "detectorId": "CROSS_SYSTEM_ECHO",
         "title": "Echo", "confidence": "HIGH"},
        {"id": "ev_jr1", "source": "Jira", "detectorId": "CROSS_SYSTEM_ECHO",
         "title": "Echo", "confidence": "MEDIUM"},
    ])
    # Many metrics per detector — the expansion that caused the regression.
    _insert_signal_snapshots([
        _snapshot(run_id, "salesforce", det, metric, False)
        for det in ("APPROVAL_BOTTLENECK", "PERMISSION_BOTTLENECK",
                    "REPETITIVE_AUTOMATION", "HANDOFF_FRICTION")
        for metric in ("m1", "m2", "m3", "m4", "m5")
    ] + [
        _snapshot(run_id, "servicenow", "CROSS_SYSTEM_ECHO", f"m{i}", False)
        for i in range(9)
    ])

    rows = client.get(
        f"/api/runs/{run_id}/normalization", headers=_auth()
    ).json()["rows"]
    by_source: dict = {}
    for row in rows:
        by_source[row["sourceSystem"]] = by_source.get(row["sourceSystem"], 0) + 1

    assert by_source == {"Salesforce": 3, "ServiceNow": 1, "Jira": 1}
    # Every surviving row is the evidence summary, not a per-metric restatement.
    assert all(r["id"].startswith("norm_ev_") for r in rows)


def test_gap_fill_is_per_source_not_per_detector(client):
    """A covered source is covered WHOLE — including detectors evidence never named.

    Salesforce evidence names one detector; snapshots name a second. The second
    must NOT leak in: mixing one evidence summary row with another detector's
    per-metric rows would report one source at two different grains at once.
    """
    run_id = f"run_sigtest_{uuid.uuid4().hex[:8]}"
    db.run_set(run_id, {
        "id": run_id, "orgId": ORG_ID, "org_id": ORG_ID,
        "status": "complete", "packId": "service_cloud",
    })
    db.run_kv_set("evidence", run_id, [
        {"id": "ev_sf1", "source": "Salesforce", "detectorId": "APPROVAL_BOTTLENECK",
         "title": "Approvals", "confidence": "HIGH"},
    ])
    _insert_signal_snapshots([
        _snapshot(run_id, "salesforce", "APPROVAL_BOTTLENECK", "m1", False),
        _snapshot(run_id, "salesforce", "KNOWLEDGE_GAP", "m1", False),   # unnamed by evidence
    ])

    rows = client.get(
        f"/api/runs/{run_id}/normalization", headers=_auth()
    ).json()["rows"]
    # Only the evidence row. KNOWLEDGE_GAP's snapshot is suppressed too, because
    # Salesforce as a whole is already covered.
    assert [r["id"] for r in rows] == ["norm_ev_sf1"]
