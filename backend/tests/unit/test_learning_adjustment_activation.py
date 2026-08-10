"""Regression coverage for automatic A3 recomputation at signal boundaries."""

from types import SimpleNamespace


def test_non_blocking_refresh_delegates_with_actor(monkeypatch):
    from app import learning_adjustment_state as state

    calls = []

    def fake_recompute(org_id, *, actor_id):
        calls.append((org_id, actor_id))
        return {"orgId": org_id, "groupsWritten": 2}

    monkeypatch.setattr(state, "recompute_adjustments", fake_recompute)

    result = state.recompute_after_signal_change(
        "org-a",
        actor_id="analyst-a",
        trigger="analyst_decision",
    )

    assert result == {"orgId": "org-a", "groupsWritten": 2}
    assert calls == [("org-a", "analyst-a")]


def test_non_blocking_refresh_never_breaks_the_source_mutation(monkeypatch):
    from app import learning_adjustment_state as state

    def fail(*_args, **_kwargs):
        raise RuntimeError("adjustment store unavailable")

    monkeypatch.setattr(state, "recompute_adjustments", fail)

    assert (
        state.recompute_after_signal_change(
            "org-a",
            actor_id="analyst-a",
            trigger="analyst_decision",
        )
        is None
    )


def test_review_decision_recomputes_after_feedback_is_committed(monkeypatch):
    from app import learning_adjustment_state as state
    from app import learning_feedback
    from app import main

    events = []
    monkeypatch.setattr(main, "get_current_org_id", lambda: "org-a")
    monkeypatch.setattr(
        learning_feedback,
        "record_feedback",
        lambda *args, **kwargs: events.append(("feedback", args, kwargs)),
    )
    monkeypatch.setattr(
        state,
        "recompute_after_signal_change",
        lambda *args, **kwargs: events.append(("recompute", args, kwargs)),
    )

    main._mirror_decision_to_learning(
        {
            "opportunity_identity": "opp-stable",
            "packId": "service_cloud",
            "_debug": {"detector_id": "approval_delay"},
        },
        "APPROVED",
        "run-current",
    )

    assert [event[0] for event in events] == ["feedback", "recompute"]
    assert events[1][1] == ("org-a",)
    assert events[1][2]["trigger"] == "analyst_decision"


def test_movement_batch_recomputes_once_after_all_measurements(monkeypatch):
    from app import learning_adjustment_state as state
    from app import opportunity_instances
    from app import opportunity_movement as movement

    measured = []
    refreshes = []
    monkeypatch.setattr(movement, "ensure_opportunity_movement_table", lambda: None)
    monkeypatch.setattr(
        opportunity_instances,
        "get_instances_for_run",
        lambda *_args, **_kwargs: [
            SimpleNamespace(opportunity_identity="opp-b"),
            SimpleNamespace(opportunity_identity="opp-a"),
        ],
    )
    monkeypatch.setattr(
        movement,
        "measure_movement",
        lambda org_id, identity, run_id: measured.append((org_id, identity, run_id)),
    )
    monkeypatch.setattr(
        state,
        "recompute_after_signal_change",
        lambda *args, **kwargs: refreshes.append((args, kwargs)),
    )

    result = movement.measure_movements_for_run("org-a", "run-current")

    assert result["measured"] == 2
    assert measured == [
        ("org-a", "opp-a", "run-current"),
        ("org-a", "opp-b", "run-current"),
    ]
    assert refreshes == [
        (
            ("org-a",),
            {
                "actor_id": "outcome_measurement",
                "trigger": "measured_outcome_batch",
            },
        )
    ]


def test_empty_movement_batch_does_not_recompute(monkeypatch):
    from app import learning_adjustment_state as state
    from app import opportunity_instances
    from app import opportunity_movement as movement

    refreshes = []
    monkeypatch.setattr(movement, "ensure_opportunity_movement_table", lambda: None)
    monkeypatch.setattr(
        opportunity_instances,
        "get_instances_for_run",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        state,
        "recompute_after_signal_change",
        lambda *args, **kwargs: refreshes.append((args, kwargs)),
    )

    result = movement.measure_movements_for_run("org-a", "run-current")

    assert result["measured"] == 0
    assert refreshes == []
