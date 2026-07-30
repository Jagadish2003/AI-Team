"""2.0-C1 T2 (AT-827) — the safe disable state machine.

Parent-story criteria exercised here:

  * **AC2** — disabling a pack stops future execution while all historical findings
    remain retrievable and correctly labelled; re-enable is supported.
  * **AC4** (contributing) — no path in disable/re-enable deletes findings,
    evidence, or run records. AT-829 owns the exhaustive data-layer sweep; this
    suite pins that the state module itself has no delete path and that a
    re-enable does not erase the disable from history.
  * **AC5** (contributing) — the state a transition leaves behind is reported
    accurately (state, revision, transition, actor).

Deliberately DB-free: the state machine is exercised through
``InMemoryPackStateStore``, the injection seam the production Postgres store
shares a contract with. The HTTP path, RBAC, and run-health reporting are covered
by ``tests/contract/test_pack_disable_lifecycle.py``.
"""
from __future__ import annotations

import inspect
import os

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app import pack_state as pack_state_module  # noqa: E402
from app.opportunity_display import (  # noqa: E402
    with_display_title,
    with_pack_state,
)
from app.pack_activation import (  # noqa: E402
    AllPacksDisabledError,
    ExcludedPack,
    resolve_activatable_packs,
)
from app.pack_state import (  # noqa: E402
    DISABLED_PACK_LABEL,
    InMemoryPackStateStore,
    PackNotFound,
    PackStateError,
    STATE_ACTIVE,
    STATE_DISABLED,
    disable_pack,
    disabled_pack_ids,
    disabled_pack_ids_safe,
    enable_pack,
    get_pack_state,
    is_pack_disabled,
    pack_state_history,
    pack_state_rows,
    pack_state_view,
    set_pack_state,
    set_pack_state_store,
)
from discovery.packs.pack_compatibility import PackIncompatibleError  # noqa: E402
from discovery.packs.pack_config import PACK_REGISTRY  # noqa: E402

ORG = "acme"
OTHER_ORG = "globex"
ACTOR = "owner-1"


@pytest.fixture(autouse=True)
def in_memory_store():
    """Fresh in-memory pack state per test; production store restored after."""
    set_pack_state_store(InMemoryPackStateStore())
    yield
    set_pack_state_store(None)


@pytest.fixture(autouse=True)
def no_telemetry_db(monkeypatch):
    """Capture telemetry instead of writing it (these tests must not touch a DB)."""
    import app.telemetry as telemetry

    events: list = []
    monkeypatch.setattr(
        telemetry, "record_event", lambda event_type, payload: events.append(
            (event_type, payload)
        )
    )
    return events


# ── The state machine ─────────────────────────────────────────────────────────


class TestDefaultState:
    def test_a_pack_with_no_row_is_active(self):
        # Absence of a row means active — provisioning the tables changes nothing
        # until a customer actually disables something.
        assert get_pack_state(ORG, "cloud_ops") == STATE_ACTIVE

    def test_no_rows_are_written_by_reading(self):
        get_pack_state(ORG, "cloud_ops")
        assert pack_state_rows(ORG) == {}

    def test_nothing_is_disabled_by_default(self):
        assert disabled_pack_ids(ORG) == set()
        assert is_pack_disabled(ORG, "cloud_ops") is False


class TestDisableTransition:
    def test_disable_moves_active_to_disabled(self):
        outcome = disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        assert outcome.previous_state == STATE_ACTIVE
        assert outcome.current_state == STATE_DISABLED
        assert outcome.transition == "disable"
        assert outcome.changed is True
        assert outcome.revision == 1
        assert get_pack_state(ORG, "cloud_ops") == STATE_DISABLED

    def test_disable_records_actor_and_reason(self):
        outcome = disable_pack(
            ORG, "cloud_ops", actor_id=ACTOR, reason="superseded by security_ops"
        )
        assert outcome.actor_id == ACTOR
        assert outcome.reason == "superseded by security_ops"
        assert pack_state_rows(ORG)["cloud_ops"]["reason"] == (
            "superseded by security_ops"
        )

    def test_disable_is_idempotent_and_writes_no_extra_history(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        repeat = disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        assert repeat.changed is False
        assert repeat.revision == 1
        assert len(pack_state_history(ORG, "cloud_ops")) == 1

    def test_disabling_one_pack_does_not_affect_another(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        assert get_pack_state(ORG, "service_cloud") == STATE_ACTIVE
        assert disabled_pack_ids(ORG) == {"cloud_ops"}


class TestEnableTransition:
    def test_re_enable_is_supported(self):
        # The sub-task states re-enable explicitly.
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        outcome = enable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        assert outcome.previous_state == STATE_DISABLED
        assert outcome.current_state == STATE_ACTIVE
        assert outcome.transition == "enable"
        assert outcome.changed is True
        assert get_pack_state(ORG, "cloud_ops") == STATE_ACTIVE

    def test_revision_increments_across_both_directions(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        enable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        third = disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        assert third.revision == 3

    def test_enable_on_an_active_pack_is_a_no_op(self):
        outcome = enable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        assert outcome.changed is False
        assert pack_state_history(ORG, "cloud_ops") == []

    def test_a_re_enabled_pack_runs_again(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        enable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        decision = resolve_activatable_packs(org_id=ORG, pack_ids=["cloud_ops"])
        assert decision.activated_pack_ids == ["cloud_ops"]
        assert decision.excluded == []


class TestTransitionValidation:
    def test_unknown_pack_id_is_rejected(self):
        # Deliberately stricter than get_pack(), which falls back to the default
        # pack: silently disabling service_cloud on a typo would be a foot-gun.
        with pytest.raises(PackNotFound):
            disable_pack(ORG, "no_such_pack", actor_id=ACTOR)

    def test_unknown_pack_id_writes_nothing(self):
        with pytest.raises(PackNotFound):
            disable_pack(ORG, "no_such_pack", actor_id=ACTOR)
        assert pack_state_rows(ORG) == {}

    def test_illegal_state_is_rejected(self):
        with pytest.raises(PackStateError):
            set_pack_state(ORG, "cloud_ops", "paused", actor_id=ACTOR)

    @pytest.mark.parametrize("missing", ["", "   ", None])
    def test_actor_is_required(self, missing):
        with pytest.raises(ValueError):
            disable_pack(ORG, "cloud_ops", actor_id=missing)

    @pytest.mark.parametrize("missing", ["", "   ", None])
    def test_org_is_required(self, missing):
        with pytest.raises(ValueError):
            disable_pack(missing, "cloud_ops", actor_id=ACTOR)


class TestOrgIsolation:
    def test_one_orgs_disable_does_not_affect_another(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        assert get_pack_state(OTHER_ORG, "cloud_ops") == STATE_ACTIVE
        assert disabled_pack_ids(OTHER_ORG) == set()

    def test_another_orgs_run_still_executes_the_pack(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        assert resolve_activatable_packs(
            org_id=OTHER_ORG, pack_ids=["cloud_ops"]
        ).activated_pack_ids == ["cloud_ops"]

    def test_history_does_not_leak_across_orgs(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        assert pack_state_history(OTHER_ORG, "cloud_ops") == []


# ── AC4 (contributing) — nothing is ever deleted ──────────────────────────────


class TestNothingIsDeleted:
    def test_history_is_append_only_across_a_disable_enable_cycle(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR, reason="turning off")
        enable_pack(ORG, "cloud_ops", actor_id=ACTOR, reason="turning back on")
        history = pack_state_history(ORG, "cloud_ops")
        # Newest first (repo audit convention), and the disable SURVIVES the
        # re-enable — that is what makes this an audit trail.
        assert [event["transition"] for event in history] == ["enable", "disable"]
        assert [event["revision"] for event in history] == [2, 1]
        assert history[1]["reason"] == "turning off"

    def test_history_preserves_every_transition_of_a_long_cycle(self):
        for _ in range(3):
            disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
            enable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        history = pack_state_history(ORG, "cloud_ops")
        assert len(history) == 6
        assert [event["revision"] for event in history] == [6, 5, 4, 3, 2, 1]

    def test_state_module_has_no_delete_path(self):
        # The state store must have no way to remove state, history, findings,
        # evidence, or runs. AT-829 sweeps the whole data layer; this pins the
        # module AT-827 introduces.
        source = inspect.getsource(pack_state_module).upper()
        for forbidden in ("DELETE FROM", "DROP TABLE", "TRUNCATE"):
            assert forbidden not in source, forbidden

    def test_store_contract_exposes_no_delete_method(self):
        store_methods = {
            name
            for name, _ in inspect.getmembers(
                pack_state_module.PackStateStore, inspect.isfunction
            )
        }
        assert not {
            name for name in store_methods if "delete" in name or "remove" in name
        }


# ── AC2 — disabling stops FUTURE execution ────────────────────────────────────


class TestDisabledPacksDoNotExecute:
    def test_disabled_pack_is_excluded_from_a_mixed_selection(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        decision = resolve_activatable_packs(
            org_id=ORG, pack_ids=["service_cloud", "cloud_ops"]
        )
        assert decision.activated_pack_ids == ["service_cloud"]
        assert decision.excluded_pack_ids == ["cloud_ops"]

    def test_exclusion_records_the_reason_and_state(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        decision = resolve_activatable_packs(
            org_id=ORG, pack_ids=["service_cloud", "cloud_ops"]
        )
        excluded = decision.excluded[0]
        assert isinstance(excluded, ExcludedPack)
        assert excluded.pack_id == "cloud_ops"
        assert excluded.reason == "pack_disabled"
        assert excluded.state == STATE_DISABLED

    def test_selection_order_of_remaining_packs_is_preserved(self):
        disable_pack(ORG, "service_cloud", actor_id=ACTOR)
        decision = resolve_activatable_packs(
            org_id=ORG, pack_ids=["cloud_ops", "service_cloud", "security_ops"]
        )
        assert decision.activated_pack_ids == ["cloud_ops", "security_ops"]

    def test_exclusion_is_recorded_as_telemetry_not_silent(self, no_telemetry_db):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        resolve_activatable_packs(
            org_id=ORG, pack_ids=["service_cloud", "cloud_ops"], run_id="run-9"
        )
        skipped = [
            payload
            for event_type, payload in no_telemetry_db
            if event_type == "pack.execution_skipped"
        ]
        assert len(skipped) == 1
        assert skipped[0]["pack_ids"] == ["cloud_ops"]
        assert skipped[0]["run_id"] == "run-9"
        assert skipped[0]["reason"] == "pack_disabled"

    def test_all_packs_disabled_is_an_error_naming_them(self):
        # A run with zero packs would report success having produced nothing.
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        disable_pack(ORG, "service_cloud", actor_id=ACTOR)
        with pytest.raises(AllPacksDisabledError) as excinfo:
            resolve_activatable_packs(
                org_id=ORG, pack_ids=["cloud_ops", "service_cloud"]
            )
        message = str(excinfo.value)
        assert "cloud_ops" in message and "service_cloud" in message
        assert set(excinfo.value.pack_ids) == {"cloud_ops", "service_cloud"}

    def test_all_disabled_never_falls_back_to_the_default_pack(self):
        # Silently running service_cloud because the requested pack is off would
        # produce findings nobody asked for.
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        with pytest.raises(AllPacksDisabledError):
            resolve_activatable_packs(org_id=ORG, pack_ids=["cloud_ops"])

    def test_no_selection_still_resolves_the_default_pack(self):
        # An empty selection is the historical default-pack path, not an
        # all-disabled selection.
        decision = resolve_activatable_packs(org_id=ORG, pack_ids=[])
        assert decision.activated_pack_ids == ["service_cloud"]
        assert decision.excluded == []

    def test_an_empty_selection_still_checks_the_default_pack_state(self):
        # The runner resolves an empty selection to the default pack BEFORE it
        # reaches activation, so the API edges must apply the disabled check to that
        # default too — otherwise an edge would pass a run the runner then fails.
        disable_pack(ORG, "service_cloud", actor_id=ACTOR)
        with pytest.raises(AllPacksDisabledError) as excinfo:
            resolve_activatable_packs(org_id=ORG, pack_ids=[])
        assert excinfo.value.pack_ids == ["service_cloud"]

    def test_an_empty_selection_is_unaffected_when_another_pack_is_disabled(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        decision = resolve_activatable_packs(org_id=ORG, pack_ids=[])
        assert decision.activated_pack_ids == ["service_cloud"]
        assert decision.excluded == []


class TestDisabledIsEvaluatedBeforeCompatibility:
    """A pack the customer already turned off must not be able to fail a run on
    compatibility grounds — it is not going to execute either way."""

    def test_a_disabled_incompatible_pack_does_not_refuse_the_run(
        self, monkeypatch
    ):
        monkeypatch.setitem(
            PACK_REGISTRY,
            "test_disabled_incompatible",
            {
                "packId": "test_disabled_incompatible",
                "packVersion": "1.0.0",
                "packName": "Disabled + incompatible",
                "domain": "service_cloud",
                "pack_domain": "service_cloud",
                "detectors": [],
                "ui_labels_path": None,
                "llm_context": "test",
                "compatibility": {"minPlatformVersion": "99.0.0"},
            },
        )
        disable_pack(ORG, "test_disabled_incompatible", actor_id=ACTOR)
        decision = resolve_activatable_packs(
            org_id=ORG, pack_ids=["service_cloud", "test_disabled_incompatible"]
        )
        assert decision.activated_pack_ids == ["service_cloud"]
        assert decision.excluded_pack_ids == ["test_disabled_incompatible"]

    def test_an_enabled_incompatible_pack_still_refuses_the_run(self, monkeypatch):
        # AT-826's gate must be untouched for packs that WOULD have run.
        monkeypatch.setitem(
            PACK_REGISTRY,
            "test_enabled_incompatible",
            {
                "packId": "test_enabled_incompatible",
                "packVersion": "1.0.0",
                "packName": "Incompatible",
                "domain": "service_cloud",
                "pack_domain": "service_cloud",
                "detectors": [],
                "ui_labels_path": None,
                "llm_context": "test",
                "compatibility": {"minPlatformVersion": "99.0.0"},
            },
        )
        with pytest.raises(PackIncompatibleError):
            resolve_activatable_packs(
                org_id=ORG, pack_ids=["service_cloud", "test_enabled_incompatible"]
            )


# ── AC2 — historical findings remain retrievable AND labelled ─────────────────


class TestHistoricalFindingsAreLabelledNotRemoved:
    def _finding(self, **overrides):
        finding = {
            "id": "opp-1",
            "title": "Recurring resolution loop",
            "packId": "cloud_ops",
            "packVersion": "1.2.0",
            "impact": 8.0,
            "effort": 3.0,
            "evidenceIds": ["ev-1", "ev-2"],
        }
        finding.update(overrides)
        return finding

    def test_a_disabled_packs_finding_is_still_returned(self):
        labelled = with_pack_state(
            self._finding(), disabled_pack_ids={"cloud_ops"}
        )
        # Retrievable — the finding is returned, not filtered out or blanked.
        assert labelled["id"] == "opp-1"
        assert labelled["title"] == "Recurring resolution loop"
        assert labelled["evidenceIds"] == ["ev-1", "ev-2"]

    def test_it_is_clearly_marked_as_produced_by_a_now_disabled_pack(self):
        labelled = with_pack_state(
            self._finding(), disabled_pack_ids={"cloud_ops"}
        )
        assert labelled["packState"] == STATE_DISABLED
        assert labelled["packStateLabel"] == DISABLED_PACK_LABEL

    def test_the_original_pack_version_stamp_is_preserved(self):
        # R16-B1 §4's provenance stamp must survive — the finding still records the
        # pack VERSION that produced it (this is also 2.0-C1 AC3's guarantee).
        labelled = with_pack_state(
            self._finding(), disabled_pack_ids={"cloud_ops"}
        )
        assert labelled["packId"] == "cloud_ops"
        assert labelled["packVersion"] == "1.2.0"

    def test_scores_are_not_altered_by_the_label(self):
        labelled = with_pack_state(
            self._finding(), disabled_pack_ids={"cloud_ops"}
        )
        assert labelled["impact"] == 8.0
        assert labelled["effort"] == 3.0

    def test_an_active_packs_finding_reports_active_with_no_label(self):
        labelled = with_pack_state(self._finding(), disabled_pack_ids=set())
        assert labelled["packState"] == STATE_ACTIVE
        assert "packStateLabel" not in labelled

    def test_only_the_disabled_packs_findings_are_labelled(self):
        disabled = {"cloud_ops"}
        cloud = with_pack_state(self._finding(), disabled_pack_ids=disabled)
        service = with_pack_state(
            self._finding(packId="service_cloud"), disabled_pack_ids=disabled
        )
        assert cloud["packState"] == STATE_DISABLED
        assert service["packState"] == STATE_ACTIVE

    def test_a_finding_with_no_pack_id_is_returned_unchanged(self):
        # Pre-R16-B1 findings carry no packId; never guess at one.
        legacy = {"id": "old", "title": "Legacy"}
        assert with_pack_state(legacy, disabled_pack_ids={"cloud_ops"}) == legacy

    def test_the_label_flows_through_the_shared_display_funnel(self):
        # with_display_title is the funnel every opportunity serve site uses, so
        # the label reaches list, decision, roadmap, report, and blueprint alike.
        shaped = with_display_title(
            self._finding(), disabled_pack_ids={"cloud_ops"}
        )
        assert shaped["packState"] == STATE_DISABLED
        assert shaped["packStateLabel"] == DISABLED_PACK_LABEL

    def test_input_finding_is_never_mutated(self):
        finding = self._finding()
        with_pack_state(finding, disabled_pack_ids={"cloud_ops"})
        assert "packState" not in finding


# ── Read posture: fail-soft so a finding is never hidden ──────────────────────


class TestFailSoftReads:
    def test_an_unreadable_store_reports_every_pack_active(self, monkeypatch):
        class BrokenStore(InMemoryPackStateStore):
            def all_states(self, org_id):
                raise RuntimeError("state store down")

        set_pack_state_store(BrokenStore())
        # "Historical findings remain retrievable and viewable" outranks the label,
        # so the label degrades rather than the finding disappearing.
        assert disabled_pack_ids_safe(ORG) == set()
        assert is_pack_disabled(ORG, "cloud_ops") is False

    def test_a_broken_store_does_not_stop_a_run(self, monkeypatch):
        class BrokenStore(InMemoryPackStateStore):
            def all_states(self, org_id):
                raise RuntimeError("state store down")

        set_pack_state_store(BrokenStore())
        decision = resolve_activatable_packs(
            org_id=ORG, pack_ids=["service_cloud", "cloud_ops"]
        )
        assert decision.activated_pack_ids == ["service_cloud", "cloud_ops"]

    def test_no_org_reads_as_nothing_disabled(self):
        assert disabled_pack_ids_safe(None) == set()
        assert disabled_pack_ids_safe("") == set()

    def test_write_failures_are_not_swallowed(self):
        # A disable that did not persist must never look like it succeeded.
        class BrokenWriteStore(InMemoryPackStateStore):
            def set_state(self, *args, **kwargs):
                raise RuntimeError("write failed")

        set_pack_state_store(BrokenWriteStore())
        with pytest.raises(RuntimeError):
            disable_pack(ORG, "cloud_ops", actor_id=ACTOR)


# ── AC5 (contributing) — the state view reports accurately ────────────────────


class TestPackStateView:
    def test_view_covers_every_registered_pack(self):
        view = pack_state_view(ORG)
        assert {row["packId"] for row in view} == set(PACK_REGISTRY)

    def test_packs_with_no_row_report_active_at_revision_zero(self):
        rows = {row["packId"]: row for row in pack_state_view(ORG)}
        assert rows["cloud_ops"]["state"] == STATE_ACTIVE
        assert rows["cloud_ops"]["revision"] == 0

    def test_view_reflects_a_disable_with_its_metadata(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR, reason="customer opted out")
        rows = {row["packId"]: row for row in pack_state_view(ORG)}
        assert rows["cloud_ops"]["state"] == STATE_DISABLED
        assert rows["cloud_ops"]["revision"] == 1
        assert rows["cloud_ops"]["reason"] == "customer opted out"
        assert rows["cloud_ops"]["updatedBy"] == ACTOR

    def test_view_reports_the_current_pack_version(self):
        from discovery.packs.pack_config import get_pack_version

        rows = {row["packId"]: row for row in pack_state_view(ORG)}
        assert rows["cloud_ops"]["packVersion"] == get_pack_version("cloud_ops")

    def test_view_is_org_scoped(self):
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        rows = {row["packId"]: row for row in pack_state_view(OTHER_ORG)}
        assert rows["cloud_ops"]["state"] == STATE_ACTIVE
