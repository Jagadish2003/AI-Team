from app.roadmap_engine import build_roadmap


def test_stage90_not_empty_when_unreviewed_complex_exists():
    opps = [
        {"id": "opp_a", "tier": "Quick Win", "decision": "UNREVIEWED", "requiredPermissions": []},
        {"id": "opp_b", "tier": "Strategic", "decision": "UNREVIEWED", "requiredPermissions": []},
        {"id": "opp_c", "tier": "Complex", "decision": "UNREVIEWED", "requiredPermissions": []},
    ]
    rm = build_roadmap(opps)
    s90 = next(s for s in rm["stages"] if s["id"] == "NEXT_90")
    assert len(s90["opportunities"]) >= 1


def test_permissions_merge_rules_required_or_satisfied_and():
    opps = [
        {"id": "opp1", "tier": "Quick Win", "decision": "UNREVIEWED", "requiredPermissions": [
            {"label": "Microsoft 365: read Teams metadata", "required": True, "satisfied": False}
        ]},
        {"id": "opp2", "tier": "Quick Win", "decision": "UNREVIEWED", "requiredPermissions": [
            {"label": "Microsoft 365: read Teams metadata", "required": False, "satisfied": True}
        ]},
    ]
    rm = build_roadmap(opps)
    s30 = next(s for s in rm["stages"] if s["id"] == "NEXT_30")
    p = next(p for p in s30["requiredPermissions"] if p["label"].startswith("Microsoft 365"))
    assert p["required"] is True
    assert p["satisfied"] is False
    assert p["readiness"] == "MISSING"


def test_phase1_string_permissions_keep_one_missing_item():
    opps = [
        {"id": "opp1", "tier": "Quick Win", "decision": "UNREVIEWED", "requiredPermissions": [
            "Salesforce: read FlowVersionView (Tooling API)",
            "Salesforce: read Flow Metadata (Tooling API)",
        ]},
        {"id": "opp2", "tier": "Quick Win", "decision": "UNREVIEWED", "requiredPermissions": [
            "Salesforce: read Case",
            "Salesforce: read CaseArticle",
        ]},
        {"id": "opp3", "tier": "Quick Win", "decision": "UNREVIEWED", "requiredPermissions": [
            "Salesforce: read Case",
            "ServiceNow: read incident (if applicable)",
            "Jira: read issues (if applicable)",
        ]},
    ]

    rm = build_roadmap(opps)
    s30 = next(s for s in rm["stages"] if s["id"] == "NEXT_30")
    readiness_counts = {"READY": 0, "PENDING": 0, "MISSING": 0}

    for permission in s30["requiredPermissions"]:
        readiness_counts[permission["readiness"]] += 1

    assert readiness_counts == {"READY": 2, "PENDING": 3, "MISSING": 1}


def test_phase2_string_permissions_now_roll_up_to_ready():
    opps = [
        {"id": "opp1", "tier": "Strategic", "decision": "UNREVIEWED", "requiredPermissions": [
            "Salesforce: read ProcessInstance",
            "Salesforce: read ProcessInstanceWorkitem",
            "Salesforce: read ProcessDefinition",
        ]},
    ]

    rm = build_roadmap(opps)
    s60 = next(s for s in rm["stages"] if s["id"] == "NEXT_60")
    readiness_counts = {"READY": 0, "PENDING": 0, "MISSING": 0}

    for permission in s60["requiredPermissions"]:
        readiness_counts[permission["readiness"]] += 1

    assert readiness_counts == {"READY": 3, "PENDING": 0, "MISSING": 0}
