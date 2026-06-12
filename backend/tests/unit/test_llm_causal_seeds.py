from app import llm_enrichment


def test_causal_seed_prefers_detector_process_in_current_relationships(monkeypatch):
    """Detector entity comes first, but ALL relationship endpoints are included so
    depth-3 BFS builds a full connected neighbourhood rather than a single-entity
    stub that fails the minimum-3 gate."""
    monkeypatch.setattr(
        llm_enrichment,
        "_run_relationship_entity_ids_from_db",
        lambda org_id, run_id: ["entity-detector", "entity-upstream"],
    )
    monkeypatch.setattr(
        llm_enrichment,
        "_detector_process_entity_id",
        lambda org_id, run_id, detector_id: "entity-detector",
    )

    result = llm_enrichment._causal_seed_entity_ids(
        "org-1",
        "run-1",
        {"detector_id": "CHECKLIST_BOTTLENECK"},
        [],
    )

    # Detector entity leads; all relationship endpoints follow for a full BFS.
    assert result[0] == "entity-detector"
    assert "entity-upstream" in result
    assert len(result) == 2


def test_causal_seed_uses_current_relationship_component_when_detector_absent(monkeypatch):
    monkeypatch.setattr(
        llm_enrichment,
        "_run_relationship_entity_ids_from_db",
        lambda org_id, run_id: ["entity-a", "entity-b"],
    )
    monkeypatch.setattr(
        llm_enrichment,
        "_detector_process_entity_id",
        lambda org_id, run_id, detector_id: None,
    )

    result = llm_enrichment._causal_seed_entity_ids(
        "org-1",
        "run-1",
        {"detector_id": "UNKNOWN_DETECTOR"},
        [],
    )

    assert result == ["entity-a", "entity-b"]
