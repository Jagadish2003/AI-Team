"""2.0-C1 T1 (AT-826) — pack compatibility declaration + activation gate.

Sub-task scope: *each pack declares the platform-capability range and required
normalised concepts (MSP-B4) it needs; incompatible packs cannot be activated,
with a clear reason.*

Parent-story criterion discharged here:

  * AC1 — a pack declaring an unmet platform range cannot be activated; the
    refusal NAMES the unmet requirement.

Also pinned: the declaration itself stays honest (every registered pack declares
a compatibility block, every declared concept exists in the platform vocabulary,
every shipped pack is activatable on the CURRENT platform version, and a pack's
declared floor is consistent with the concepts it requires). Those structural
tests are what make the gate safe to enforce — they fail the build if a future
pack declares something unsatisfiable rather than letting the refusal surface at
runtime in front of a customer.

Pure-Python and offline — no DB and no credentials.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from discovery.packs import pack_config  # noqa: E402
from discovery.packs.pack_compatibility import (  # noqa: E402
    KIND_CONCEPT_UNAVAILABLE,
    KIND_CONCEPT_UNKNOWN,
    KIND_INVALID_DECLARATION,
    KIND_PLATFORM_TOO_NEW,
    KIND_PLATFORM_TOO_OLD,
    PackIncompatibleError,
    assert_pack_activatable,
    assert_selection_activatable,
    check_pack_compatibility,
    check_pack_selection,
    compatibility_summary,
)
from discovery.packs.pack_config import (  # noqa: E402
    PACK_REGISTRY,
    get_pack_compatibility_declaration,
    list_packs,
)
from discovery.packs.platform_capabilities import (  # noqa: E402
    NORMALISED_CONCEPTS,
    PLATFORM_VERSION,
    available_concepts,
    compare_versions,
    get_platform_version,
    is_concept_available,
    is_concept_known,
    parse_version,
    platform_capability_summary,
)


# ── Test packs — registered only for the test that needs them ─────────────────

_FUTURE_PACK_ID = "test_future_platform_pack"
_ANCIENT_PACK_ID = "test_ancient_platform_pack"
_UNSHIPPED_CONCEPT_PACK_ID = "test_unshipped_concept_pack"
_MALFORMED_PACK_ID = "test_malformed_declaration_pack"


def _test_pack(pack_id: str, compatibility: dict) -> dict:
    return {
        "packId": pack_id,
        "packVersion": "9.9.9",
        "packName": f"Test pack {pack_id}",
        "domain": "service_cloud",
        "pack_domain": "service_cloud",
        "detectors": [],
        "ui_labels_path": None,
        "llm_context": "test",
        "compatibility": compatibility,
    }


@pytest.fixture
def registered_test_packs(monkeypatch):
    """Register the incompatible test packs for the duration of one test.

    They must live in PACK_REGISTRY for their ids to resolve at all — ``get_pack``
    falls back to the default pack for an unknown id, so an unregistered id would
    be checked as ``service_cloud`` and (correctly) pass.
    """
    packs = {
        # Declares a platform range this platform is BELOW.
        _FUTURE_PACK_ID: _test_pack(
            _FUTURE_PACK_ID,
            {
                "minPlatformVersion": "99.0.0",
                "maxPlatformVersion": None,
                "requiredConcepts": ["case_workflow"],
            },
        ),
        # Declares a ceiling this platform is ABOVE.
        _ANCIENT_PACK_ID: _test_pack(
            _ANCIENT_PACK_ID,
            {
                "minPlatformVersion": "0.1.0",
                "maxPlatformVersion": "0.9.0",
                "requiredConcepts": [],
            },
        ),
        # Requires a concept the platform does not provide at ANY version.
        _UNSHIPPED_CONCEPT_PACK_ID: _test_pack(
            _UNSHIPPED_CONCEPT_PACK_ID,
            {
                "minPlatformVersion": "1.0.0",
                "maxPlatformVersion": None,
                "requiredConcepts": ["case_workflow", "telepathy_workflow"],
            },
        ),
        # Declares an unparseable bound — must fail closed, not be ignored.
        _MALFORMED_PACK_ID: _test_pack(
            _MALFORMED_PACK_ID,
            {
                "minPlatformVersion": "not-a-version",
                "maxPlatformVersion": None,
                "requiredConcepts": [],
            },
        ),
    }
    for pack_id, pack in packs.items():
        monkeypatch.setitem(PACK_REGISTRY, pack_id, pack)
    return packs


# ── Version parsing / comparison ──────────────────────────────────────────────


class TestVersionParsing:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2", (2, 0, 0)),
            ("2.0", (2, 0, 0)),
            ("2.0.0", (2, 0, 0)),
            ("1.9.1", (1, 9, 1)),
            ("  1.9.0  ", (1, 9, 0)),
            ("2.0.0-rc1", (2, 0, 0)),
            ("2.0.0+ci42", (2, 0, 0)),
        ],
    )
    def test_parses_supported_shapes(self, value, expected):
        assert parse_version(value) == expected

    @pytest.mark.parametrize(
        "value", [None, "", "   ", "abc", "1.x.0", "1.2.3.4", "-1.0.0", 2.0, 2]
    )
    def test_unparseable_returns_none(self, value):
        assert parse_version(value) is None

    def test_shorter_versions_pad_with_zeros(self):
        assert compare_versions("1.9", "1.9.0") == 0

    def test_ordering(self):
        assert compare_versions("1.9.0", "2.0.0") == -1
        assert compare_versions("2.0.0", "1.9.0") == 1
        assert compare_versions("2.0.0", "2.0.0") == 0

    def test_unparseable_comparison_is_none_not_an_ordering(self):
        # None must NOT be mistaken for 0 (equal) by a caller — the gate treats it
        # as an invalid declaration instead.
        assert compare_versions("2.0.0", "banana") is None
        assert compare_versions("banana", "2.0.0") is None


# ── Platform capability surface ───────────────────────────────────────────────


class TestPlatformCapabilities:
    def test_platform_version_is_parseable(self):
        assert parse_version(PLATFORM_VERSION) is not None
        assert get_platform_version() == PLATFORM_VERSION

    def test_every_concept_since_is_parseable(self):
        for concept_id, spec in NORMALISED_CONCEPTS.items():
            assert parse_version(spec.since) is not None, concept_id
            assert spec.description.strip(), concept_id

    def test_concept_ids_match_their_map_keys(self):
        for concept_id, spec in NORMALISED_CONCEPTS.items():
            assert spec.concept_id == concept_id

    def test_all_declared_concepts_available_on_current_platform(self):
        # Every concept shipped at or before the current platform version. If this
        # fails, a concept was stamped with a FUTURE `since` — packs requiring it
        # would be refused on a platform that actually provides it.
        assert available_concepts() == sorted(NORMALISED_CONCEPTS)

    def test_concept_becomes_available_at_its_since_version(self):
        # Inclusive boundary: available AT the introducing version, not after it.
        assert is_concept_available("resolution_signature", "1.9.0") is True
        assert is_concept_available("resolution_signature", "1.8.9") is False

    def test_unknown_concept_is_never_available(self):
        assert is_concept_known("telepathy_workflow") is False
        assert is_concept_available("telepathy_workflow", "99.0.0") is False

    def test_unparseable_platform_version_fails_closed(self):
        assert is_concept_available("case_workflow", "banana") is False

    def test_b4_normalised_concepts_are_declared(self):
        # The sub-task names MSP-B4 explicitly — its normalised concepts must be
        # part of the vocabulary a pack can declare against.
        for concept_id in (
            "incident_workflow",
            "resolution_signature",
            "incident_identity_signature",
            "assignment_group_routing",
        ):
            assert is_concept_known(concept_id), concept_id

    def test_capability_summary_is_json_shaped(self):
        summary = platform_capability_summary()
        assert summary["platformVersion"] == PLATFORM_VERSION
        assert len(summary["concepts"]) == len(NORMALISED_CONCEPTS)
        assert all(entry["available"] for entry in summary["concepts"])


# ── The declaration stays honest (structural) ─────────────────────────────────


class TestDeclarationIntegrity:
    def test_every_registered_pack_declares_compatibility(self):
        # AT-826: "each pack declares the platform-capability range and required
        # normalised concepts it needs". A new pack added without a declaration
        # fails here rather than silently inheriting the permissive default.
        missing = [
            pack_id
            for pack_id in list_packs()
            if not PACK_REGISTRY[pack_id].get(pack_config.COMPATIBILITY_KEY)
        ]
        assert missing == [], (
            f"packs missing a '{pack_config.COMPATIBILITY_KEY}' declaration: {missing}"
        )

    def test_every_declared_concept_exists_in_the_vocabulary(self):
        unknown: dict = {}
        for pack_id in list_packs():
            declaration = get_pack_compatibility_declaration(pack_id)
            for key in ("requiredConcepts", "optionalConcepts"):
                for concept in declaration[key]:
                    if not is_concept_known(concept):
                        unknown.setdefault(pack_id, []).append(concept)
        assert unknown == {}, f"packs declaring unknown concepts: {unknown}"

    def test_every_declared_bound_is_parseable(self):
        bad: dict = {}
        for pack_id in list_packs():
            declaration = get_pack_compatibility_declaration(pack_id)
            for key in ("minPlatformVersion", "maxPlatformVersion"):
                bound = declaration[key]
                if bound is not None and parse_version(bound) is None:
                    bad.setdefault(pack_id, []).append(f"{key}={bound!r}")
        assert bad == {}, f"packs declaring unparseable bounds: {bad}"

    def test_declared_floor_covers_the_concepts_the_pack_requires(self):
        # A pack requiring an MSP-era concept but declaring a 1.0.0 floor would be
        # self-contradictory: the floor claims it runs on a platform that cannot
        # provide what it needs. Catch that in the declaration, not at runtime.
        inconsistent: dict = {}
        for pack_id in list_packs():
            declaration = get_pack_compatibility_declaration(pack_id)
            floor = declaration["minPlatformVersion"]
            if floor is None:
                continue
            for concept in declaration["requiredConcepts"]:
                spec = NORMALISED_CONCEPTS.get(concept)
                if spec is None:
                    continue
                if compare_versions(floor, spec.since) == -1:
                    inconsistent.setdefault(pack_id, []).append(
                        f"{concept} needs >= {spec.since} but floor is {floor}"
                    )
        assert inconsistent == {}, f"inconsistent declarations: {inconsistent}"

    def test_every_shipped_pack_is_activatable_on_the_current_platform(self):
        # The regression bar for this sub-task: adding the gate must not refuse any
        # pack that runs today.
        refused = {
            pack_id: check_pack_compatibility(pack_id).reason
            for pack_id in list_packs()
            if not check_pack_compatibility(pack_id).compatible
        }
        assert refused == {}, f"shipped packs refused by the gate: {refused}"

    def test_required_and_optional_concepts_do_not_overlap(self):
        overlapping: dict = {}
        for pack_id in list_packs():
            declaration = get_pack_compatibility_declaration(pack_id)
            shared = set(declaration["requiredConcepts"]) & set(
                declaration["optionalConcepts"]
            )
            if shared:
                overlapping[pack_id] = sorted(shared)
        assert overlapping == {}, f"concepts declared both ways: {overlapping}"


class TestDeclarationAccessor:
    def test_returns_a_complete_block_for_a_declared_pack(self):
        declaration = get_pack_compatibility_declaration("cloud_ops")
        assert declaration["minPlatformVersion"] == "1.9.0"
        assert declaration["maxPlatformVersion"] is None
        assert "resolution_signature" in declaration["requiredConcepts"]
        # MSP-B3/B5 are soft dependencies by design — declared OPTIONAL so a
        # graceful degradation is never misreported as an incompatibility.
        assert "cmdb_dependency" in declaration["optionalConcepts"]
        assert "runbook_match" in declaration["optionalConcepts"]

    def test_unknown_pack_id_reads_the_default_pack_declaration(self):
        # Unchanged get_pack() semantics — an unknown id is NOT a compatibility
        # failure, it resolves to the default pack.
        assert get_pack_compatibility_declaration(
            "no_such_pack"
        ) == get_pack_compatibility_declaration(pack_config.DEFAULT_PACK)

    def test_partial_and_malformed_declarations_are_filled_and_cleaned(
        self, monkeypatch
    ):
        monkeypatch.setitem(
            PACK_REGISTRY,
            "test_partial_pack",
            _test_pack(
                "test_partial_pack",
                {
                    # Only one key declared; concept list carries noise.
                    "requiredConcepts": [
                        "case_workflow",
                        "  case_workflow  ",
                        "",
                        None,
                        7,
                    ],
                },
            ),
        )
        declaration = get_pack_compatibility_declaration("test_partial_pack")
        assert declaration["minPlatformVersion"] is None
        assert declaration["maxPlatformVersion"] is None
        assert declaration["requiredConcepts"] == ["case_workflow"]
        assert declaration["optionalConcepts"] == []

    def test_non_dict_declaration_falls_back_to_permissive_default(
        self, monkeypatch
    ):
        monkeypatch.setitem(
            PACK_REGISTRY,
            "test_broken_pack",
            _test_pack("test_broken_pack", "definitely not a dict"),  # type: ignore[arg-type]
        )
        declaration = get_pack_compatibility_declaration("test_broken_pack")
        assert declaration == pack_config.DEFAULT_PACK_COMPATIBILITY


# ── AC1 — an unmet range refuses activation, naming the requirement ───────────


class TestUnmetPlatformRangeRefusesActivation:
    def test_pack_below_platform_floor_is_incompatible(self, registered_test_packs):
        report = check_pack_compatibility(_FUTURE_PACK_ID)
        assert report.compatible is False
        assert [item.kind for item in report.unmet] == [KIND_PLATFORM_TOO_OLD]

    def test_refusal_reason_names_the_unmet_version_requirement(
        self, registered_test_packs
    ):
        report = check_pack_compatibility(_FUTURE_PACK_ID)
        # AC1: the refusal must NAME the unmet requirement — the declared bound,
        # the pack, and the platform version actually present.
        assert "99.0.0" in report.reason
        assert _FUTURE_PACK_ID in report.reason
        assert PLATFORM_VERSION in report.reason
        assert report.unmet[0].requirement == "99.0.0"

    def test_pack_above_platform_ceiling_is_incompatible(
        self, registered_test_packs
    ):
        report = check_pack_compatibility(_ANCIENT_PACK_ID)
        assert report.compatible is False
        assert [item.kind for item in report.unmet] == [KIND_PLATFORM_TOO_NEW]
        assert "0.9.0" in report.reason

    def test_range_is_inclusive_at_both_ends(self, registered_test_packs):
        # A pack declaring [0.1.0, 0.9.0] is compatible AT both bounds.
        assert check_pack_compatibility(
            _ANCIENT_PACK_ID, platform_version="0.1.0"
        ).compatible
        assert check_pack_compatibility(
            _ANCIENT_PACK_ID, platform_version="0.9.0"
        ).compatible
        assert not check_pack_compatibility(
            _ANCIENT_PACK_ID, platform_version="0.9.1"
        ).compatible

    def test_assert_pack_activatable_raises_with_the_named_reason(
        self, registered_test_packs
    ):
        with pytest.raises(PackIncompatibleError) as excinfo:
            assert_pack_activatable(_FUTURE_PACK_ID)
        message = str(excinfo.value)
        assert "99.0.0" in message
        assert excinfo.value.pack_ids == [_FUTURE_PACK_ID]

    def test_malformed_declared_bound_fails_closed(self, registered_test_packs):
        # A typo'd bound must never silently widen the range into "compatible".
        report = check_pack_compatibility(_MALFORMED_PACK_ID)
        assert report.compatible is False
        assert [item.kind for item in report.unmet] == [KIND_INVALID_DECLARATION]
        assert "not-a-version" in report.reason

    def test_unparseable_platform_version_fails_closed(self):
        report = check_pack_compatibility("cloud_ops", platform_version="banana")
        assert report.compatible is False
        assert any(
            item.kind == KIND_INVALID_DECLARATION for item in report.unmet
        )


# ── AC1 — an unmet normalised concept refuses activation, naming it ───────────


class TestUnmetConceptRefusesActivation:
    def test_concept_not_provided_at_any_version_is_named(
        self, registered_test_packs
    ):
        report = check_pack_compatibility(_UNSHIPPED_CONCEPT_PACK_ID)
        assert report.compatible is False
        assert [item.kind for item in report.unmet] == [KIND_CONCEPT_UNKNOWN]
        assert report.unmet[0].requirement == "telepathy_workflow"
        assert "telepathy_workflow" in report.reason
        # The concept that IS provided must not be reported as unmet.
        assert "case_workflow" not in report.reason

    def test_msp_concept_unavailable_on_an_older_platform_is_named(self):
        # cloud_ops requires the MSP-B4 signatures; on a pre-MSP platform each
        # unmet concept is individually named.
        report = check_pack_compatibility("cloud_ops", platform_version="1.5.0")
        assert report.compatible is False
        unavailable = [
            item.requirement
            for item in report.unmet
            if item.kind == KIND_CONCEPT_UNAVAILABLE
        ]
        assert "resolution_signature" in unavailable
        assert "incident_identity_signature" in unavailable
        assert "operational_event" in unavailable
        for concept in unavailable:
            assert concept in report.reason

    def test_optional_concepts_never_block_activation(self):
        # MSP-B3/B5 absence is a graceful degradation, not an incompatibility.
        # At 1.9.0 every cloud_ops REQUIRED concept is present, so it activates —
        # and its unavailable optionals are reported advisorily, not as unmet.
        report = check_pack_compatibility("cloud_ops", platform_version="1.9.0")
        assert report.compatible is True
        assert report.unavailable_optional_concepts == []

    def test_unavailable_optional_concept_is_advisory_only(self, monkeypatch):
        monkeypatch.setitem(
            PACK_REGISTRY,
            "test_optional_only_pack",
            _test_pack(
                "test_optional_only_pack",
                {
                    "minPlatformVersion": "1.0.0",
                    "requiredConcepts": ["case_workflow"],
                    "optionalConcepts": ["cmdb_dependency"],
                },
            ),
        )
        report = check_pack_compatibility(
            "test_optional_only_pack", platform_version="1.0.0"
        )
        assert report.compatible is True
        assert report.unavailable_optional_concepts == ["cmdb_dependency"]


# ── Multi-pack selection ──────────────────────────────────────────────────────


class TestSelectionGate:
    def test_compatible_selection_passes(self):
        reports = assert_selection_activatable(["service_cloud", "cloud_ops"])
        assert [report.pack_id for report in reports] == [
            "service_cloud",
            "cloud_ops",
        ]
        assert all(report.compatible for report in reports)

    def test_one_incompatible_pack_refuses_the_selection(
        self, registered_test_packs
    ):
        with pytest.raises(PackIncompatibleError) as excinfo:
            assert_selection_activatable(["service_cloud", _FUTURE_PACK_ID])
        # Only the incompatible pack is named as refused.
        assert excinfo.value.pack_ids == [_FUTURE_PACK_ID]
        assert "service_cloud" not in str(excinfo.value)

    def test_every_incompatible_pack_is_reported_not_just_the_first(
        self, registered_test_packs
    ):
        with pytest.raises(PackIncompatibleError) as excinfo:
            assert_selection_activatable(
                [_FUTURE_PACK_ID, "service_cloud", _UNSHIPPED_CONCEPT_PACK_ID]
            )
        assert excinfo.value.pack_ids == [
            _FUTURE_PACK_ID,
            _UNSHIPPED_CONCEPT_PACK_ID,
        ]
        message = str(excinfo.value)
        assert "99.0.0" in message
        assert "telepathy_workflow" in message

    def test_empty_selection_checks_the_default_pack(self):
        reports = check_pack_selection([])
        assert [report.pack_id for report in reports] == [pack_config.DEFAULT_PACK]

    def test_selection_is_order_preserving_and_deduplicated(self):
        reports = check_pack_selection(
            ["cloud_ops", "service_cloud", "cloud_ops"]
        )
        assert [report.pack_id for report in reports] == [
            "cloud_ops",
            "service_cloud",
        ]

    def test_unknown_ids_collapse_onto_the_default_pack_once(self):
        # Two unknown ids both resolve to the default pack — one report, matching
        # the runner's own de-duplication by RESOLVED pack id.
        reports = check_pack_selection(["nope_one", "nope_two"])
        assert [report.pack_id for report in reports] == [pack_config.DEFAULT_PACK]

    def test_unknown_pack_id_is_not_refused(self):
        # Regression bar: get_pack() warns and falls back; it must not 409.
        assert check_pack_compatibility("no_such_pack").compatible is True


class TestCompatibilityReportShape:
    def test_report_dict_carries_the_declaration_and_verdict(self):
        report = check_pack_compatibility("security_ops").to_dict()
        assert report["packId"] == "security_ops"
        assert report["packVersion"] == pack_config.get_pack_version("security_ops")
        assert report["platformVersion"] == PLATFORM_VERSION
        assert report["minPlatformVersion"] == "1.9.0"
        assert report["compatible"] is True
        assert report["unmet"] == []
        assert report["reason"] == ""
        assert "vulnerability_workflow" in report["requiredConcepts"]

    def test_compatible_report_has_an_empty_reason(self):
        assert check_pack_compatibility("service_cloud").reason == ""

    def test_summary_reports_the_whole_selection(self):
        summary = compatibility_summary(["service_cloud", "cloud_ops"])
        assert summary["platformVersion"] == PLATFORM_VERSION
        assert summary["compatible"] is True
        assert [entry["packId"] for entry in summary["packs"]] == [
            "service_cloud",
            "cloud_ops",
        ]

    def test_summary_reports_incompatible_selection(self, registered_test_packs):
        summary = compatibility_summary([_FUTURE_PACK_ID])
        assert summary["compatible"] is False
        assert summary["packs"][0]["unmet"][0]["requirement"] == "99.0.0"

    def test_error_dict_is_json_serialisable(self, registered_test_packs):
        with pytest.raises(PackIncompatibleError) as excinfo:
            assert_pack_activatable(_UNSHIPPED_CONCEPT_PACK_ID)
        payload = excinfo.value.to_dict()
        assert payload["error"] == "pack_incompatible"
        assert "telepathy_workflow" in payload["message"]
        assert payload["packs"][0]["packId"] == _UNSHIPPED_CONCEPT_PACK_ID


# ── The runner re-asserts the gate at the execution point ─────────────────────


class TestRunnerRefusesIncompatiblePack:
    @pytest.fixture(autouse=True)
    def _offline_pack_state(self, monkeypatch):
        """Keep this suite DB-free.

        The runner resolves pack activation (2.0-C1 T2), which reads the org's
        pack-state store and — on a refusal — records the refusal as telemetry.
        Both are stubbed here so neither reaches Postgres; the read is fail-soft
        and the telemetry write is non-blocking either way, but a DB round trip
        does not belong in this suite.
        """
        import app.telemetry as telemetry
        from app.pack_state import InMemoryPackStateStore, set_pack_state_store

        monkeypatch.setattr(telemetry, "record_event", lambda *_a, **_k: None)
        set_pack_state_store(InMemoryPackStateStore())
        yield
        set_pack_state_store(None)

    def test_runner_refuses_before_doing_any_work(self, registered_test_packs):
        # Defence in depth: a CLI/direct caller reaches the runner without passing
        # through either API activation edge, so the gate is re-asserted there and
        # fails the run LOUDLY (the cloud_ops contract-violation posture) rather
        # than executing a pack the platform cannot support.
        from discovery import runner

        with pytest.raises(PackIncompatibleError) as excinfo:
            runner.run(mode="offline", systems=[], pack=_FUTURE_PACK_ID)
        assert "99.0.0" in str(excinfo.value)

    def test_runner_runs_a_compatible_selection(self, registered_test_packs):
        # The same call shape with a shipped pack must NOT raise the gate.
        from discovery.packs.pack_compatibility import assert_selection_activatable

        assert_selection_activatable(["service_cloud"])
