"""NC-2 detector unit coverage used by the Sprint 5 exit suite."""
from __future__ import annotations


def test_routing_detector_fires_on_owner_changes() -> None:
    from discovery.detectors.loan_origination_routing_friction import detect

    result = detect(
        {
            "ncino": {
                "origination_metrics": {
                    "total_loans": 1,
                    "avg_stage_transitions": 3,
                    "max_stage_transitions": 3,
                    "avg_owner_changes": 2,
                    "max_owner_changes": 2,
                    "high_friction_loans": [{"loan_id": "loan_001"}],
                    "owner_change_source": "LOAN_HISTORY_CREATEDBY",
                }
            }
        }
    )

    assert len(result) == 1
    assert result[0].detector_id == "LOAN_ORIGINATION_ROUTING_FRICTION"
    assert result[0].metric_value == 2


def test_routing_detector_fires_on_stage_transitions_without_owner_changes() -> None:
    from discovery.detectors.loan_origination_routing_friction import detect

    result = detect(
        {
            "origination_metrics": {
                "total_loans": 1,
                "avg_stage_transitions": 4,
                "max_stage_transitions": 4,
                "avg_owner_changes": 0,
                "max_owner_changes": 0,
                "high_friction_loans": [{"loan_id": "loan_001"}],
                "owner_change_source": "UNKNOWN",
            }
        }
    )

    assert len(result) == 1
    assert result[0].threshold == 4


def test_routing_detector_does_not_fire_below_thresholds() -> None:
    from discovery.detectors.loan_origination_routing_friction import detect

    result = detect(
        {
            "ncino": {
                "origination_metrics": {
                    "total_loans": 1,
                    "avg_stage_transitions": 1,
                    "max_stage_transitions": 1,
                    "avg_owner_changes": 1,
                    "max_owner_changes": 1,
                    "high_friction_loans": [],
                    "owner_change_source": "LOAN_HISTORY_CREATEDBY",
                }
            }
        }
    )

    assert result == []


def test_covenant_detector_fires_on_breach() -> None:
    from discovery.detectors.covenant_tracking_gap import detect

    result = detect(
        {
            "ncino": {
                "covenant_metrics": {
                    "total_covenants": 2,
                    "overdue_count": 1,
                    "breached_count": 1,
                    "compliance_override": True,
                    "max_days_past_evaluation": 12,
                }
            }
        }
    )

    assert len(result) == 1
    assert result[0].detector_id == "COVENANT_TRACKING_GAP"
    assert result[0].raw_evidence["compliance_override"] is True


def test_checklist_detector_fires_on_duration_overrun() -> None:
    from discovery.detectors.checklist_bottleneck import detect

    result = detect(
        {
            "ncino": {
                "checklist_metrics": {
                    "total_checklists": 3,
                    "overrun_count": 1,
                    "stalled_count": 0,
                    "max_overrun_days": 7,
                    "avg_overrun_days": 7,
                }
            }
        }
    )

    assert len(result) == 1
    assert result[0].detector_id == "CHECKLIST_BOTTLENECK"


def test_spreading_detector_fires_on_unlocked_periods() -> None:
    from discovery.detectors.spreading_bottleneck import detect

    result = detect(
        {
            "ncino": {
                "spreading_metrics": {
                    "total_periods": 2,
                    "unlocked_count": 1,
                    "max_days_unlocked": 18,
                    "avg_days_unlocked": 18,
                    "analyst_bottlenecks": [{"analyst_id": "u1", "unlocked_count": 1}],
                }
            }
        }
    )

    assert len(result) == 1
    assert result[0].detector_id == "SPREADING_BOTTLENECK"
    assert result[0].raw_evidence["signal_field"] == "LLC_BI__Is_Locked__c"


def test_approval_detector_fires_on_pending_process_instance() -> None:
    from discovery.detectors.approval_bottleneck import detect

    result = detect(
        {
            "ncino": {
                "approval_metrics": {
                    "total_instances": 1,
                    "pending_count": 1,
                    "avg_cycle_days": 3,
                    "max_cycle_days": 3,
                }
            }
        }
    )

    assert len(result) == 1
    assert result[0].detector_id == "APPROVAL_BOTTLENECK"


def test_all_lending_detectors_return_empty_without_metrics() -> None:
    from discovery.detectors.approval_bottleneck import detect as approval
    from discovery.detectors.checklist_bottleneck import detect as checklist
    from discovery.detectors.covenant_tracking_gap import detect as covenant
    from discovery.detectors.loan_origination_routing_friction import detect as routing
    from discovery.detectors.spreading_bottleneck import detect as spreading

    sf_data = {"ncino": {}}

    assert routing(sf_data) == []
    assert covenant(sf_data) == []
    assert checklist(sf_data) == []
    assert spreading(sf_data) == []
    assert approval(sf_data) == []
