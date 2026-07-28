"""End-to-end tests for the B4/B5/B8 -> Cloud Operations production seam."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.provenance import EvidencePointer
from app.runbook_match_decisions import InMemoryRunbookMatchDecisionStore
from discovery.cloud_ops_runtime import build_cloud_ops_runtime
from discovery.detectors import (
    cloud_ops_reassignment_ping_pong,
    cloud_ops_recurring_resolution_loop,
    cloud_ops_runbook_documentation_gap,
)
from discovery.detectors.runbook_documentation_gap import DocumentationGapConfig
from discovery.detectors.runbook_match import (
    InMemoryRunbookLibrary,
    RunbookPage,
)
from discovery.detectors.runbook_pipeline import evaluate_runbook_recurrences
from discovery.packs.cloud_ops_scorer import is_cloud_ops_detector
from discovery.packs.pack_config import get_detector_modules
from discovery.packs.cloud_ops_finding import enforce_pack_findings
from discovery.signals.operational_event import OperationalEvent, ResourceRef
from discovery.signals.resolution_signature import (
    compute_incident_identity_signature,
    compute_resolution_signature,
)

ORG = "org-runtime"
BASE = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _incident(
    index: int,
    *,
    event_signature: str | None = None,
    runbook_ref: str | None = None,
) -> dict:
    opened = BASE + timedelta(days=index - 1)
    resolved = opened + timedelta(hours=2)
    sys_id = f"incident-{index}"
    evidence = EvidencePointer.observed(
        source_system="servicenow",
        source_artifact=sys_id,
        source_timestamp=_iso(resolved),
        source_artifact_type="record_id",
    ).to_dict()
    category = "software"
    close_code = "Solved (Permanently)"
    group = "Platform Operations"
    ci_class = "cmdb_ci_server"
    incident = {
        "sys_id": sys_id,
        "number": f"INC{index:07d}",
        "org_id": ORG,
        "opened_at": _iso(opened),
        "resolved_at": _iso(resolved),
        "category": category,
        "ci_class": ci_class,
        "short_description": "Message worker is unavailable",
        "assignment_group": group,
        "business_service": "message-service",
        "close_code": close_code,
        "resolution": {
            "is_resolved": True,
            "incident_sys_id": sys_id,
            "resolution_category": category,
            "close_code": close_code,
            "resolved_by_group": group,
            "resolved_at": _iso(resolved),
            "time_to_resolve_seconds": 7200,
            "incident_identity_signature": compute_incident_identity_signature(
                category=category,
                short_description="Message worker is unavailable",
                ci_class=ci_class,
            ),
            "resolution_signature": compute_resolution_signature(
                category=category,
                close_code=close_code,
                resolved_by_group=group,
                ci_class=ci_class,
            ),
            "runbook_references": [runbook_ref] if runbook_ref else [],
            "evidence": evidence,
        },
    }
    if event_signature:
        incident["event_signatures"] = [event_signature]
    return incident


def _routing_incident() -> dict:
    groups = ("Platform Operations", "Network Operations") * 2
    return {
        "sys_id": "incident-routing",
        "number": "INC9000001",
        "org_id": ORG,
        "business_service": "message-service",
        "assignment_history": [
            {
                "assignment_group": group,
                "changed_at": _iso(BASE + timedelta(minutes=index)),
                "history_sys_id": f"audit-{index}",
            }
            for index, group in enumerate(groups)
        ],
    }


def _payload(incidents: list[dict]) -> dict:
    return {
        "org_id": ORG,
        "incident_metrics": {"org_id": ORG, "incidents": incidents},
    }


def _event(signal_id: str, observed_at: datetime) -> OperationalEvent:
    return OperationalEvent.build(
        org_id=ORG,
        source_system="bridge:aws",
        signal_id=signal_id,
        event_type="WorkerFailure",
        event_class="error",
        severity="warning",
        observed_at=_iso(observed_at),
        resource=ResourceRef(
            provider="aws",
            resource_type="compute",
            resource_id="worker-1",
        ),
        provenance=EvidencePointer.observed(
            source_system="bridge:aws",
            source_artifact=signal_id,
            source_timestamp=_iso(observed_at),
            source_artifact_type="staged_event",
        ).to_dict(),
    )


def _record(event: OperationalEvent, row_id: int) -> dict:
    return {
        "artifact_id": f"bridge:aws:{event.signal_id}",
        "change_kind": "created",
        "source_system": "bridge:aws",
        "provider_event_id": event.signal_id,
        "batch_id": "batch-1",
        "staging_row_id": row_id,
        "event": event.to_dict(),
        "evidence_pointer": dict(event.provenance),
    }


def _observed_runbook_batch(org_id: str, payload: dict):
    page = RunbookPage(
        org_id=ORG,
        source_system="document",
        source_artifact="runbooks/message-worker",
        identifiers=("RB1000",),
        title="Restore the message worker",
        source_timestamp="2026-06-01T00:00:00+00:00",
    )
    return evaluate_runbook_recurrences(
        org_id,
        payload,
        citation_library=InMemoryRunbookLibrary([page]),
        decision_store=InMemoryRunbookMatchDecisionStore(),
        note_ingest_fn=lambda _org, _artifacts: None,
        record_event_fn=lambda _name, _payload: None,
    )


def test_runtime_wires_b4_b5_and_b8_into_existing_cloud_detectors():
    first_event = _event("event-1", BASE + timedelta(minutes=30))
    second_event = _event("event-2", BASE + timedelta(minutes=40))
    signature = first_event.event_signature
    assert second_event.event_signature == signature

    incidents = [
        _incident(
            index,
            event_signature=signature,
            runbook_ref="RB1000",
        )
        for index in range(1, 4)
    ]
    incidents.append(_routing_incident())
    runtime = build_cloud_ops_runtime(
        ORG,
        _payload(incidents),
        bridge_records=[_record(first_event, 1), _record(second_event, 2)],
        runbook_batch_fn=_observed_runbook_batch,
    )

    assert runtime.health["status"] == "ok"
    assert len(runtime.block["recurrence_records"]) == 1
    recurrence = runtime.block["recurrence_records"][0]
    assert recurrence["count"] == 3
    assert recurrence["event_signature"] == signature
    assert recurrence["runbook_state"] == "observed"
    assert recurrence["runbook_match"]["runbook"]["source_artifact"] == (
        "runbooks/message-worker"
    )

    assert len(runtime.block["oscillation_records"]) == 1
    assert runtime.block["oscillation_records"][0]["hop_count"] == 3

    [event_row] = runtime.block["event_signatures"]
    assert event_row["signature"] == signature
    assert event_row["event_count"] == 2
    assert event_row["recurring"] is True
    assert event_row["window_overlap"] is True
    assert event_row["incident_ids"] == ["incident-1"]
    assert event_row["batch_ids"] == ["batch-1"]

    sn_data = {"cloud_ops": runtime.block}
    [recurrence_finding] = cloud_ops_recurring_resolution_loop.detect(
        None, sn_data, None
    )
    contract = recurrence_finding.raw_evidence["finding_contract"]
    assert contract["confidence"]["level"] == "HIGH"
    assert contract["corroboration"]["window_gated"] is True
    assert contract["evidence"]["composite"]["runbook_state"] == "observed"
    assert contract["evidence"]["composite"]["runbook_id"] == (
        "runbooks/message-worker"
    )
    assert cloud_ops_reassignment_ping_pong.detect(None, sn_data, None)


def test_runtime_surfaces_b5_documentation_gap_as_pack_finding():
    incidents = [_incident(index) for index in range(1, 6)]

    def no_match_batch(org_id: str, payload: dict):
        return evaluate_runbook_recurrences(
            org_id,
            payload,
            citation_library=InMemoryRunbookLibrary(),
            retrieve_fn=lambda *_args, **_kwargs: [],
            embedding_available_fn=lambda _query: True,
            decision_store=InMemoryRunbookMatchDecisionStore(),
            gap_config=DocumentationGapConfig(
                recurrence_floor=5,
                confidence_cap=0.60,
            ),
            note_ingest_fn=lambda _org, _artifacts: None,
            record_event_fn=lambda _name, _payload: None,
        )

    runtime = build_cloud_ops_runtime(
        ORG,
        _payload(incidents),
        runbook_batch_fn=no_match_batch,
    )

    [recurrence] = runtime.block["recurrence_records"]
    assert recurrence["runbook_state"] == "absent"
    assert recurrence["runbook_matching_available"] is True
    assert len(runtime.block["documentation_gaps"]) == 1

    sn_data = {"cloud_ops": runtime.block}
    [gap] = cloud_ops_runbook_documentation_gap.detect(None, sn_data, None)
    assert gap.provenance_type == "inferred"
    assert gap.metric_value == 5
    assert gap.threshold == 5
    assert gap.raw_evidence["finding_contract"]["confidence"]["level"] == "MEDIUM"
    assert enforce_pack_findings([gap]) == 1

    [loop] = cloud_ops_recurring_resolution_loop.detect(None, sn_data, None)
    leg = loop.raw_evidence["finding_contract"]["evidence"]["composite"]
    assert leg["runbook_state"] == "absent"
    assert leg["degraded"] is False
    assert leg["label"] == "no runbook match"


def test_runtime_refuses_cross_org_bridge_event():
    event = _event("event-cross-org", BASE)
    record = _record(event, 1)
    record["event"]["org_id"] = "another-org"

    runtime = build_cloud_ops_runtime(
        ORG,
        _payload([]),
        bridge_records=[record],
    )

    assert runtime.health["b8_event_bridge"]["status"] == "unavailable"
    assert runtime.block["event_signatures"] == []


def test_missing_b5_evaluation_is_reported_as_unavailable_not_no_match():
    runtime = build_cloud_ops_runtime(
        ORG,
        _payload([_incident(index) for index in range(1, 4)]),
        runbook_batch_fn=lambda _org, _payload: SimpleNamespace(
            recurrences=(),
            note_handoff={"status": "ok"},
        ),
    )

    [recurrence] = runtime.block["recurrence_records"]
    assert recurrence["runbook_state"] == "unavailable"
    assert recurrence["runbook_matching_available"] is False
    assert runtime.block["runbook_matching"]["available"] is False
    assert runtime.health["b5_runbook_matching"]["status"] == "unavailable"
    assert runtime.health["b5_runbook_matching"]["missing_evaluations"] == 1


def test_documentation_gap_is_registered_and_scored_as_cloud_ops():
    modules = get_detector_modules("cloud_ops")
    assert (
        "discovery.detectors.cloud_ops_runbook_documentation_gap" in modules
    )
    assert is_cloud_ops_detector("OPS_RUNBOOK_DOCUMENTATION_GAP") is True
