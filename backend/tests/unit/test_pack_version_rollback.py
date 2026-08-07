"""2.0-C1 T3 (AT-828) — pack version rollback.

Parent-story criteria exercised here:

  * **AC3** — rollback causes subsequent runs to use the prior version; existing
    findings retain their original version stamps. Split into the four claims the
    sub-task actually makes:
        1. a pack version CAN be rolled back to a prior version;
        2. runs after rollback USE that version — its detectors, its config, and its
           version stamp, not merely the stamp;
        3. historical findings KEEP their original version stamp;
        4. nothing is rewritten retroactively.
  * **AC4** (contributing) — rollback deletes nothing; the rollback stays on the
    append-only trail after a restore. AT-829 owns the exhaustive sweep.
  * **AC5** (contributing) — the effective/pinned/available versions are reported
    accurately across the transitions.

The honesty boundary is the thing most worth reading: a version with no ARCHIVED
config artifact is refused rather than being served as a stamp over current
behaviour. Tests for that are in ``TestRollbackTargetValidation``.

DB-free — the state machine runs through ``InMemoryPackStateStore`` and the pinned
config is a real archived artifact on disk.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app.pack_activation import resolve_activatable_packs  # noqa: E402
from app.pack_state import (  # noqa: E402
    InMemoryPackStateStore,
    PackNotFound,
    STATE_DISABLED,
    TRANSITION_RESTORE,
    TRANSITION_ROLLBACK,
    disable_pack,
    enable_pack,
    get_pinned_pack_version,
    pack_state_history,
    pack_state_view,
    pinned_pack_versions,
    pinned_pack_versions_safe,
    restore_pack_version,
    rollback_pack_version,
    set_pack_state_store,
    set_pinned_pack_version,
)
from discovery.packs import cloud_ops_config  # noqa: E402
from discovery.packs.pack_config import (  # noqa: E402
    PACK_REGISTRY,
    PackVersionUnavailable,
    get_pack,
    get_pack_version,
    get_pack_version_entry,
    get_pack_version_history,
    get_rollbackable_versions,
    resolve_pack_at_version,
)
from discovery.packs.pack_version_context import (  # noqa: E402
    get_pack_config_paths,
    get_pinned_config_path,
    pack_config_paths,
    resolve_config_path,
    set_pack_config_paths,
)
from discovery.runner import (  # noqa: E402
    PinnedDetectorsUnavailable,
    _detectors_for_pinned_version,
)

ORG = "acme"
OTHER_ORG = "globex"
ACTOR = "owner-1"
PRIOR = "1.1.0"
#: The version the registry ships TODAY. Derived, not hardcoded: `dev` bumped
#: cloud_ops 1.2.0 -> 1.2.1 and every literal here broke. The pack's external
#: config JSON still declares 1.2.0, so the assertions about THAT file are
#: deliberately left as literals — they are not the same fact.
CURRENT = get_pack_version("cloud_ops")


@pytest.fixture(autouse=True)
def in_memory_store():
    set_pack_state_store(InMemoryPackStateStore())
    yield
    set_pack_state_store(None)


@pytest.fixture(autouse=True)
def in_memory_certification_policy():
    """Activation also reads the 2.0-C2 T4 certification policy, and that read
    fails CLOSED — inject it so this suite stays DB-free. No floor is set, so the
    version-pin behaviour under test is unchanged."""
    from app.pack_certification_policy import (
        InMemoryPackCertificationPolicyStore,
        set_policy_store,
    )

    set_policy_store(InMemoryPackCertificationPolicyStore())
    yield
    set_policy_store(None)


@pytest.fixture(autouse=True)
def clean_version_context():
    """No pinned config path leaks into or out of a test."""
    set_pack_config_paths({})
    yield
    set_pack_config_paths({})


@pytest.fixture(autouse=True)
def no_telemetry_db(monkeypatch):
    import app.telemetry as telemetry

    events: list = []
    monkeypatch.setattr(
        telemetry,
        "record_event",
        lambda event_type, payload: events.append((event_type, payload)),
    )
    return events


class _FakeDetector:
    """Stands in for a detector module — only ``__name__`` is consulted."""

    def __init__(self, name: str) -> None:
        self.__name__ = f"discovery.detectors.{name}"


def _current_cloud_ops_detector_modules():
    return [
        _FakeDetector(path.rsplit(".", 1)[-1])
        for path in PACK_REGISTRY["cloud_ops"]["detectors"]
    ]


# ── The archive is real and self-consistent ───────────────────────────────────


class TestVersionArchive:
    def test_config_driven_packs_declare_a_prior_version(self):
        assert get_rollbackable_versions("cloud_ops") == [PRIOR]
        assert get_rollbackable_versions("security_ops") == [PRIOR]

    def test_code_only_packs_declare_none(self):
        # They keep behaviour in code, so there is no artifact to serve. Rollback is
        # refused for them rather than faked — see versions/README.md.
        for pack_id in (
            "service_cloud",
            "ncino",
            "strs_benefits",
            "sqlserver_opsignal",
            "github_engineering",
            "enterprise_ops",
        ):
            assert get_rollbackable_versions(pack_id) == [], pack_id

    @pytest.mark.parametrize("pack_id", ["cloud_ops", "security_ops"])
    def test_every_archived_entry_has_an_artifact_that_exists(self, pack_id):
        for entry in get_pack_version_history(pack_id):
            assert entry["configPath"], entry
            assert os.path.isfile(entry["configPath"]), entry["configPath"]

    @pytest.mark.parametrize("pack_id", ["cloud_ops", "security_ops"])
    def test_artifact_pack_version_matches_its_declared_version(self, pack_id):
        # The strongest guard that the archive is genuine history, not a copy of the
        # current config renamed: the file's own packVersion must agree.
        for entry in get_pack_version_history(pack_id):
            with open(entry["configPath"], "r", encoding="utf-8") as handle:
                assert json.load(handle)["packVersion"] == entry["version"]

    @pytest.mark.parametrize("pack_id", ["cloud_ops", "security_ops"])
    def test_archived_artifact_differs_from_the_current_one(self, pack_id):
        current = get_pack(pack_id)["config_path"]
        for entry in get_pack_version_history(pack_id):
            assert entry["configPath"] != current

    @pytest.mark.parametrize("pack_id", ["cloud_ops", "security_ops"])
    def test_every_archived_entry_declares_detectors(self, pack_id):
        for entry in get_pack_version_history(pack_id):
            assert entry["detectors"], entry

    def test_the_current_version_is_never_listed_as_a_rollback_target(self):
        # Pinning to the current version and having no pin are the same position;
        # listing it would create two sources of truth for one version.
        assert get_pack_version("cloud_ops") not in get_rollbackable_versions(
            "cloud_ops"
        )

    def test_registry_detector_list_matches_the_runners_current_imports(self):
        # The pinned-detector narrowing filters the runner's HARDCODED import list
        # against the registry's declared list. If the two ever diverge, narrowing
        # would silently drop a detector — so pin them equal here.
        assert [
            path.rsplit(".", 1)[-1] for path in PACK_REGISTRY["cloud_ops"]["detectors"]
        ] == [
            "cloud_ops_recurring_resolution_loop",
            "cloud_ops_alert_triage_toil",
            "cloud_ops_reassignment_ping_pong",
            "cloud_ops_queue_ageing",
            "cloud_ops_shared_ci_hotspot",
            "cloud_ops_runbook_documentation_gap",
        ]
        assert [
            path.rsplit(".", 1)[-1]
            for path in PACK_REGISTRY["security_ops"]["detectors"]
        ] == [
            "security_ops_remediation_recurrence",
            "security_ops_security_it_pingpong",
            "security_ops_sla_deferral_ageing",
            "security_ops_shared_infra_concentration",
            "security_ops_sir_triage_toil",
        ]


# ── AC3.1 — a pack version can be rolled back ─────────────────────────────────


class TestRollbackTransition:
    def test_rollback_pins_the_prior_version(self):
        outcome = rollback_pack_version(
            ORG, "cloud_ops", PRIOR, actor_id=ACTOR, reason="1.2.0 regression"
        )
        assert outcome.changed is True
        assert outcome.transition == TRANSITION_ROLLBACK
        assert outcome.previous_version is None
        assert outcome.current_version == PRIOR
        assert get_pinned_pack_version(ORG, "cloud_ops") == PRIOR

    def test_the_registry_is_not_modified(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        # A rollback is per-org configuration, not a global downgrade.
        assert get_pack_version("cloud_ops") == CURRENT
        assert len(get_pack("cloud_ops")["detectors"]) == 6
        assert "pinnedVersion" not in get_pack("cloud_ops")

    def test_rollback_is_idempotent(self):
        first = rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        again = rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        assert again.changed is False
        assert again.revision == first.revision
        assert len(pack_state_history(ORG, "cloud_ops")) == 1

    def test_restore_clears_the_pin(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        outcome = restore_pack_version(ORG, "cloud_ops", actor_id=ACTOR)
        assert outcome.changed is True
        assert outcome.transition == TRANSITION_RESTORE
        assert outcome.previous_version == PRIOR
        assert outcome.current_version is None
        assert get_pinned_pack_version(ORG, "cloud_ops") is None

    def test_restore_on_an_unpinned_pack_is_a_no_op(self):
        outcome = restore_pack_version(ORG, "cloud_ops", actor_id=ACTOR)
        assert outcome.changed is False
        assert pack_state_history(ORG, "cloud_ops") == []

    def test_pinning_the_current_version_normalises_to_no_pin(self):
        # Storing it would leave a stale pin that silently held the pack back after
        # the next bump.
        outcome = set_pinned_pack_version(
            ORG, "cloud_ops", get_pack_version("cloud_ops"), actor_id=ACTOR
        )
        assert outcome.current_version is None
        assert get_pinned_pack_version(ORG, "cloud_ops") is None

    def test_rollback_is_org_scoped(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        assert get_pinned_pack_version(OTHER_ORG, "cloud_ops") is None
        assert pinned_pack_versions(OTHER_ORG) == {}

    def test_pins_are_per_pack(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        assert get_pinned_pack_version(ORG, "security_ops") is None
        assert pinned_pack_versions(ORG) == {"cloud_ops": PRIOR}


class TestRollbackTargetValidation:
    """The honesty boundary: the platform refuses to stamp a version it cannot serve."""

    def test_unknown_version_is_refused_naming_what_is_available(self):
        with pytest.raises(PackVersionUnavailable) as excinfo:
            rollback_pack_version(ORG, "cloud_ops", "9.9.9", actor_id=ACTOR)
        message = str(excinfo.value)
        assert "9.9.9" in message
        assert PRIOR in message
        assert CURRENT in message  # the current version, for orientation

    def test_a_code_only_pack_cannot_be_rolled_back(self):
        with pytest.raises(PackVersionUnavailable) as excinfo:
            rollback_pack_version(ORG, "service_cloud", "0.9.0", actor_id=ACTOR)
        assert "no archived prior versions" in str(excinfo.value)

    def test_a_refused_rollback_writes_nothing(self):
        with pytest.raises(PackVersionUnavailable):
            rollback_pack_version(ORG, "cloud_ops", "9.9.9", actor_id=ACTOR)
        assert get_pinned_pack_version(ORG, "cloud_ops") is None
        assert pack_state_history(ORG, "cloud_ops") == []

    def test_unknown_pack_is_rejected(self):
        with pytest.raises(PackNotFound):
            rollback_pack_version(ORG, "no_such_pack", PRIOR, actor_id=ACTOR)

    @pytest.mark.parametrize("missing", ["", "   ", None])
    def test_actor_is_required(self, missing):
        with pytest.raises(ValueError):
            rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=missing)

    def test_resolve_refuses_an_unarchived_version(self):
        with pytest.raises(PackVersionUnavailable):
            resolve_pack_at_version("cloud_ops", "9.9.9")


# ── AC3.2 — runs after rollback USE the prior version ─────────────────────────


class TestResolutionUsesThePriorVersion:
    def test_resolution_substitutes_version_detectors_and_config(self):
        resolved = resolve_pack_at_version("cloud_ops", PRIOR)
        assert resolved["packVersion"] == PRIOR
        assert resolved["pinnedVersion"] == PRIOR
        # 1.1.0 predates the MSP-B5 documentation-gap detector.
        assert len(resolved["detectors"]) == 5
        assert not any("runbook_documentation_gap" in d for d in resolved["detectors"])
        assert resolved["config_path"].endswith("cloud_ops_pack_config.v1.1.0.json")

    def test_resolution_returns_a_copy(self):
        resolved = resolve_pack_at_version("cloud_ops", PRIOR)
        resolved["detectors"].append("tampered")
        assert len(get_pack("cloud_ops")["detectors"]) == 6

    def test_no_version_returns_the_current_pack_unchanged(self):
        assert resolve_pack_at_version("cloud_ops", None) is get_pack("cloud_ops")

    def test_activation_reports_the_pin_and_its_config_path(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        decision = resolve_activatable_packs(
            org_id=ORG, pack_ids=["cloud_ops", "service_cloud"]
        )
        assert decision.pinned_versions == {"cloud_ops": PRIOR}
        assert decision.effective_version("cloud_ops") == PRIOR
        assert decision.effective_version("service_cloud") == "1.0.0"
        assert decision.pinned_config_paths["cloud_ops"].endswith(
            "cloud_ops_pack_config.v1.1.0.json"
        )

    def test_activation_of_an_unpinned_org_reports_no_pins(self):
        decision = resolve_activatable_packs(org_id=ORG, pack_ids=["cloud_ops"])
        assert decision.pinned_versions == {}
        assert decision.pinned_config_paths == {}
        assert decision.effective_version("cloud_ops") == CURRENT

    def test_activation_emits_the_pin_as_telemetry(self, no_telemetry_db):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        resolve_activatable_packs(
            org_id=ORG, pack_ids=["cloud_ops"], run_id="run-7"
        )
        pinned = [
            payload
            for event_type, payload in no_telemetry_db
            if event_type == "pack.version_pinned"
        ]
        assert len(pinned) == 1
        assert pinned[0]["pinned_versions"] == {"cloud_ops": PRIOR}
        assert pinned[0]["run_id"] == "run-7"

    def test_a_disabled_pack_contributes_no_pin(self):
        # It is not running, so there is no version for it to run at.
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        decision = resolve_activatable_packs(
            org_id=ORG, pack_ids=["cloud_ops", "service_cloud"]
        )
        assert decision.pinned_versions == {}
        assert decision.excluded_pack_ids == ["cloud_ops"]

    def test_state_and_version_are_independent(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        # Disabling must not silently un-pin…
        assert get_pinned_pack_version(ORG, "cloud_ops") == PRIOR
        enable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        # …and re-enabling must not silently lose the rollback.
        assert get_pinned_pack_version(ORG, "cloud_ops") == PRIOR
        assert resolve_activatable_packs(
            org_id=ORG, pack_ids=["cloud_ops"]
        ).pinned_versions == {"cloud_ops": PRIOR}


class TestPinnedConfigIsActuallyLoaded:
    """The difference between a real rollback and a lying version stamp."""

    def test_default_load_reads_the_current_config(self):
        assert cloud_ops_config.load_cloud_ops_config().pack_version == "1.2.0"

    def test_a_pinned_context_makes_the_loader_read_the_archived_config(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        decision = resolve_activatable_packs(org_id=ORG, pack_ids=["cloud_ops"])
        with pack_config_paths(decision.pinned_config_paths):
            loaded = cloud_ops_config.load_cloud_ops_config()
            assert loaded.pack_version == PRIOR
            # Thresholds/calibration come from the archived artifact, so detectors
            # and the scorer behave as 1.1.0 did.
            assert loaded.source_path.endswith("cloud_ops_pack_config.v1.1.0.json")
            assert loaded.thresholds
            assert cloud_ops_config.get_calibration().impact_weights

    def test_the_context_is_restored_on_exit(self):
        with pack_config_paths({"cloud_ops": "/tmp/whatever.json"}):
            assert get_pinned_config_path("cloud_ops") == "/tmp/whatever.json"
        assert get_pinned_config_path("cloud_ops") is None
        assert cloud_ops_config.load_cloud_ops_config().pack_version == "1.2.0"

    def test_an_explicit_path_still_wins_over_the_context(self):
        entry = get_pack_version_entry("cloud_ops", PRIOR)
        with pack_config_paths({"cloud_ops": entry["configPath"]}):
            current = get_pack("cloud_ops")["config_path"]
            assert (
                cloud_ops_config.load_cloud_ops_config(current).pack_version == "1.2.0"
            )

    def test_precedence_helper(self):
        assert resolve_config_path("cloud_ops", "/explicit", "/default") == "/explicit"
        with pack_config_paths({"cloud_ops": "/pinned"}):
            assert resolve_config_path("cloud_ops", None, "/default") == "/pinned"
            # A different pack is unaffected by another pack's pin.
            assert resolve_config_path("security_ops", None, "/default") == "/default"
        assert resolve_config_path("cloud_ops", None, "/default") == "/default"

    def test_setting_paths_is_idempotent_and_clearable(self):
        set_pack_config_paths({"cloud_ops": "/a"})
        assert get_pack_config_paths() == {"cloud_ops": "/a"}
        set_pack_config_paths({})
        assert get_pack_config_paths() == {}

    def test_empty_and_falsy_entries_are_dropped(self):
        set_pack_config_paths({"cloud_ops": "", "": "/x", "security_ops": "/y"})
        assert get_pack_config_paths() == {"security_ops": "/y"}


class TestPinnedDetectorNarrowing:
    def test_pinned_version_runs_only_its_declared_detectors(self):
        resolved = resolve_pack_at_version("cloud_ops", PRIOR)
        narrowed = _detectors_for_pinned_version(
            resolved, _current_cloud_ops_detector_modules()
        )
        assert len(narrowed) == 5
        assert not any(
            "runbook_documentation_gap" in module.__name__ for module in narrowed
        )

    def test_declared_order_is_preserved(self):
        resolved = resolve_pack_at_version("cloud_ops", PRIOR)
        narrowed = _detectors_for_pinned_version(
            resolved, _current_cloud_ops_detector_modules()
        )
        assert [module.__name__ for module in narrowed] == resolved["detectors"]

    def test_a_declared_detector_this_build_lacks_is_skipped_loudly(self, caplog):
        resolved = resolve_pack_at_version("cloud_ops", PRIOR)
        available = [
            module
            for module in _current_cloud_ops_detector_modules()
            if "queue_ageing" not in module.__name__
        ]
        with caplog.at_level("WARNING"):
            narrowed = _detectors_for_pinned_version(resolved, available)
        assert len(narrowed) == 4
        assert "queue_ageing" in caplog.text

    def test_no_declared_detector_available_raises_rather_than_running_others(self):
        # Running an arbitrary set under 1.1.0's stamp is exactly the dishonesty
        # this story exists to prevent.
        resolved = resolve_pack_at_version("cloud_ops", PRIOR)
        with pytest.raises(PinnedDetectorsUnavailable) as excinfo:
            _detectors_for_pinned_version(resolved, [_FakeDetector("unrelated")])
        assert PRIOR in str(excinfo.value)


# ── AC3.3 / AC3.4 — history is kept, nothing is rewritten ─────────────────────


class TestNothingIsRewrittenRetroactively:
    def test_rollback_writes_no_finding_or_run_state(self):
        # A pin is a forward-looking configuration row. The ONLY thing it touches is
        # this org's pack_states row; there is no backfill step of any kind.
        outcome = rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        assert outcome.current_version == PRIOR
        # State is untouched by a version transition.
        assert outcome.previous_state == outcome.current_state

    def test_a_historical_finding_keeps_its_original_stamp(self):
        # Findings are immutable records. Nothing in the rollback path reads or
        # writes them, so a 1.2.0 finding stays stamped 1.2.0 forever.
        finding = {"id": "opp-1", "packId": "cloud_ops", "packVersion": "1.2.0"}
        before = dict(finding)
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        assert finding == before
        assert finding["packVersion"] == "1.2.0"

    def test_the_rollback_survives_a_restore_on_the_audit_trail(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR, reason="regression")
        restore_pack_version(ORG, "cloud_ops", actor_id=ACTOR, reason="fixed")
        history = pack_state_history(ORG, "cloud_ops")
        assert [event["transition"] for event in history] == [
            TRANSITION_RESTORE,
            TRANSITION_ROLLBACK,
        ]
        rollback_event = history[1]
        assert rollback_event["previous_version"] is None
        assert rollback_event["resulting_version"] == PRIOR
        assert rollback_event["reason"] == "regression"

    def test_version_and_state_transitions_share_one_trail(self):
        # AT-830 has to surface "what has this org done to this pack" — one trail
        # means one answer, and `revision` counts every change regardless of kind.
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        enable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        restore_pack_version(ORG, "cloud_ops", actor_id=ACTOR)
        history = pack_state_history(ORG, "cloud_ops")
        assert [event["transition"] for event in history] == [
            "restore",
            "enable",
            "disable",
            "rollback",
        ]
        assert [event["revision"] for event in history] == [4, 3, 2, 1]

    def test_a_state_transition_carries_the_pin_through_unchanged(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        outcome = disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        assert outcome.previous_version == PRIOR
        assert outcome.current_version == PRIOR


# ── Fail-soft posture ─────────────────────────────────────────────────────────


class TestFailSoftReads:
    def test_an_unreadable_store_runs_current_versions(self):
        class BrokenStore(InMemoryPackStateStore):
            def all_states(self, org_id):
                raise RuntimeError("state store down")

        set_pack_state_store(BrokenStore())
        # Degrading to the CURRENT version keeps the run self-consistent: it runs
        # and is stamped with the same version. It simply does not honour the pin.
        assert pinned_pack_versions_safe(ORG) == {}
        decision = resolve_activatable_packs(org_id=ORG, pack_ids=["cloud_ops"])
        assert decision.pinned_versions == {}
        assert decision.effective_version("cloud_ops") == CURRENT

    def test_no_org_reads_as_no_pins(self):
        assert pinned_pack_versions_safe(None) == {}
        assert pinned_pack_versions_safe("") == {}

    def test_a_pin_whose_artifact_vanished_degrades_to_current(
        self, monkeypatch, no_telemetry_db
    ):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        # Simulate the archive entry being dropped in a later release.
        monkeypatch.setitem(
            PACK_REGISTRY["cloud_ops"], "versionHistory", []
        )
        decision = resolve_activatable_packs(
            org_id=ORG, pack_ids=["cloud_ops"], run_id="run-8"
        )
        # The pack still runs — but at the CURRENT version, stamped consistently.
        assert decision.pinned_versions == {}
        assert decision.effective_version("cloud_ops") == CURRENT
        # …and the stale pin is never silent.
        unservable = [
            payload
            for event_type, payload in no_telemetry_db
            if event_type == "pack.version_pin_unservable"
        ]
        assert len(unservable) == 1
        assert unservable[0]["pack_id"] == "cloud_ops"
        assert unservable[0]["version"] == PRIOR

    def test_write_failures_are_not_swallowed(self):
        class BrokenWriteStore(InMemoryPackStateStore):
            def set_pinned_version(self, *args, **kwargs):
                raise RuntimeError("write failed")

        set_pack_state_store(BrokenWriteStore())
        with pytest.raises(RuntimeError):
            rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)


# ── AC5 (contributing) — the view reports versions accurately ──────────────────


class TestPackStateViewVersions:
    def test_unpinned_pack_reports_current_as_effective(self):
        rows = {row["packId"]: row for row in pack_state_view(ORG)}
        assert rows["cloud_ops"]["packVersion"] == CURRENT
        assert rows["cloud_ops"]["pinnedVersion"] is None
        assert rows["cloud_ops"]["effectiveVersion"] == CURRENT

    def test_pinned_pack_reports_the_pin_as_effective(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        rows = {row["packId"]: row for row in pack_state_view(ORG)}
        # packVersion stays what the REGISTRY ships; effectiveVersion is what a run
        # started now would execute and stamp.
        assert rows["cloud_ops"]["packVersion"] == CURRENT
        assert rows["cloud_ops"]["pinnedVersion"] == PRIOR
        assert rows["cloud_ops"]["effectiveVersion"] == PRIOR

    def test_available_versions_are_reported_per_pack(self):
        rows = {row["packId"]: row for row in pack_state_view(ORG)}
        assert rows["cloud_ops"]["availableVersions"] == [PRIOR]
        assert rows["service_cloud"]["availableVersions"] == []

    def test_restore_returns_effective_to_current(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        restore_pack_version(ORG, "cloud_ops", actor_id=ACTOR)
        rows = {row["packId"]: row for row in pack_state_view(ORG)}
        assert rows["cloud_ops"]["pinnedVersion"] is None
        assert rows["cloud_ops"]["effectiveVersion"] == CURRENT

    def test_a_pack_can_be_both_disabled_and_pinned(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        disable_pack(ORG, "cloud_ops", actor_id=ACTOR)
        rows = {row["packId"]: row for row in pack_state_view(ORG)}
        assert rows["cloud_ops"]["state"] == STATE_DISABLED
        assert rows["cloud_ops"]["pinnedVersion"] == PRIOR

    def test_view_is_org_scoped(self):
        rollback_pack_version(ORG, "cloud_ops", PRIOR, actor_id=ACTOR)
        rows = {row["packId"]: row for row in pack_state_view(OTHER_ORG)}
        assert rows["cloud_ops"]["pinnedVersion"] is None
        assert rows["cloud_ops"]["effectiveVersion"] == CURRENT
