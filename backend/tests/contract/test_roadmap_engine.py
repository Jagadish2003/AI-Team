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
