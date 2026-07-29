"""2.0-C1 T1 (AT-826) — the app-layer pack activation gate.

Parent-story criterion: **AC1** — a pack declaring an unmet platform range cannot
be activated; the refusal names the unmet requirement.

``discovery/tests/test_pack_compatibility.py`` pins the RULE (the declaration and
the verdict). This suite pins the app-layer ENFORCEMENT that both API activation
edges share:

  * ``app/pack_activation.py``  — the single gate + refusal telemetry both edges call;
  * ``routes_sprint4_t1._gate_pack_activation`` — the compute edge's resolution of
    the effective selection (launch record + request) it gates.

Deliberately DB-free (no contract database, no TestClient): the gate is pure
logic over the pack registry, so it is testable — and must stay testable — without
infrastructure. The full HTTP path (409 status, response detail, run-record
persistence) is covered by
``tests/contract/test_pack_compatibility_activation.py``.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app import pack_activation  # noqa: E402
from app.pack_activation import (  # noqa: E402
    compatibility_snapshot,
    gate_pack_activation,
    record_activation_refused,
)
from discovery.packs.pack_compatibility import (  # noqa: E402
    PackIncompatibleError,
    check_pack_compatibility,
)
from discovery.packs.pack_config import PACK_REGISTRY  # noqa: E402
from discovery.packs.platform_capabilities import (  # noqa: E402
    PLATFORM_VERSION,
    get_platform_version,
)

_INCOMPATIBLE_PACK_ID = "test_gate_future_pack"
_MISSING_CONCEPT_PACK_ID = "test_gate_missing_concept_pack"


@pytest.fixture
def incompatible_packs(monkeypatch):
    """Register packs whose declarations this platform cannot satisfy."""
    packs = {
        _INCOMPATIBLE_PACK_ID: {
            "packId": _INCOMPATIBLE_PACK_ID,
            "packVersion": "3.0.0",
            "packName": "Future Platform Pack",
            "domain": "service_cloud",
            "pack_domain": "service_cloud",
            "detectors": [],
            "ui_labels_path": None,
            "llm_context": "test",
            "compatibility": {
                "minPlatformVersion": "99.0.0",
                "maxPlatformVersion": None,
                "requiredConcepts": ["case_workflow"],
            },
        },
        _MISSING_CONCEPT_PACK_ID: {
            "packId": _MISSING_CONCEPT_PACK_ID,
            "packVersion": "1.0.0",
            "packName": "Missing Concept Pack",
            "domain": "service_cloud",
            "pack_domain": "service_cloud",
            "detectors": [],
            "ui_labels_path": None,
            "llm_context": "test",
            "compatibility": {
                "minPlatformVersion": "1.0.0",
                "maxPlatformVersion": None,
                "requiredConcepts": ["mind_reading_workflow"],
            },
        },
    }
    for pack_id, pack in packs.items():
        monkeypatch.setitem(PACK_REGISTRY, pack_id, pack)
    return packs


@pytest.fixture
def captured_events(monkeypatch):
    """Capture telemetry instead of writing it (no DB)."""
    events: list = []

    def _record(event_type, payload):
        events.append((event_type, payload))

    import app.telemetry as telemetry

    monkeypatch.setattr(telemetry, "record_event", _record)
    return events


# ── gate_pack_activation ──────────────────────────────────────────────────────


class TestGateAllowsCompatibleSelections:
    def test_shipped_single_pack_passes(self, captured_events):
        reports = gate_pack_activation(org_id="default", pack_ids=["service_cloud"])
        assert [report.pack_id for report in reports] == ["service_cloud"]
        assert captured_events == []

    def test_shipped_multi_pack_selection_passes(self, captured_events):
        reports = gate_pack_activation(
            org_id="default", pack_ids=["cloud_ops", "security_ops"]
        )
        assert [report.pack_id for report in reports] == [
            "cloud_ops",
            "security_ops",
        ]
        assert all(report.compatible for report in reports)
        assert captured_events == []

    def test_empty_selection_passes_on_the_default_pack(self, captured_events):
        # No selection is the historical default-pack path — it must not 409.
        reports = gate_pack_activation(org_id="default", pack_ids=[])
        assert len(reports) == 1
        assert reports[0].compatible is True
        assert captured_events == []

    def test_unknown_pack_id_passes(self, captured_events):
        # Regression bar: get_pack() falls back to the default pack with a warning.
        # An unknown id must keep that behaviour, not become an activation refusal.
        reports = gate_pack_activation(org_id="default", pack_ids=["no_such_pack"])
        assert reports[0].compatible is True
        assert captured_events == []


class TestGateRefusesIncompatibleSelections:
    def test_unmet_platform_range_raises(self, incompatible_packs, captured_events):
        with pytest.raises(PackIncompatibleError) as excinfo:
            gate_pack_activation(
                org_id="default", pack_ids=[_INCOMPATIBLE_PACK_ID]
            )
        assert excinfo.value.pack_ids == [_INCOMPATIBLE_PACK_ID]

    def test_refusal_names_the_unmet_requirement(
        self, incompatible_packs, captured_events
    ):
        # AC1: the reason must name the pack, the unmet bound, and this platform.
        with pytest.raises(PackIncompatibleError) as excinfo:
            gate_pack_activation(
                org_id="default", pack_ids=[_INCOMPATIBLE_PACK_ID]
            )
        message = str(excinfo.value)
        assert _INCOMPATIBLE_PACK_ID in message
        assert "99.0.0" in message
        assert PLATFORM_VERSION in message

    def test_refusal_names_an_unmet_normalised_concept(
        self, incompatible_packs, captured_events
    ):
        with pytest.raises(PackIncompatibleError) as excinfo:
            gate_pack_activation(
                org_id="default", pack_ids=[_MISSING_CONCEPT_PACK_ID]
            )
        assert "mind_reading_workflow" in str(excinfo.value)

    def test_only_the_incompatible_pack_in_a_mixed_selection_is_refused(
        self, incompatible_packs, captured_events
    ):
        with pytest.raises(PackIncompatibleError) as excinfo:
            gate_pack_activation(
                org_id="default",
                pack_ids=["service_cloud", _INCOMPATIBLE_PACK_ID],
            )
        assert excinfo.value.pack_ids == [_INCOMPATIBLE_PACK_ID]

    def test_refusal_is_recorded_as_telemetry(
        self, incompatible_packs, captured_events
    ):
        with pytest.raises(PackIncompatibleError):
            gate_pack_activation(
                org_id="acme",
                pack_ids=[_INCOMPATIBLE_PACK_ID],
                run_id="run-123",
            )
        assert len(captured_events) == 1
        event_type, payload = captured_events[0]
        assert event_type == "pack.activation_refused"
        assert payload["org_id"] == "acme"
        assert payload["run_id"] == "run-123"
        assert payload["pack_ids"] == [_INCOMPATIBLE_PACK_ID]
        assert payload["platform_version"] == get_platform_version()
        assert payload["unmet"][0]["requirement"] == "99.0.0"
        assert "99.0.0" in payload["reason"]

    def test_telemetry_failure_never_masks_the_refusal(
        self, incompatible_packs, monkeypatch
    ):
        import app.telemetry as telemetry

        def _boom(event_type, payload):
            raise RuntimeError("telemetry down")

        monkeypatch.setattr(telemetry, "record_event", _boom)
        # The refusal is the point; observability failing must not swallow it.
        with pytest.raises(PackIncompatibleError):
            gate_pack_activation(
                org_id="default", pack_ids=[_INCOMPATIBLE_PACK_ID]
            )

    def test_record_activation_refused_swallows_telemetry_errors(
        self, incompatible_packs, monkeypatch
    ):
        import app.telemetry as telemetry

        monkeypatch.setattr(
            telemetry,
            "record_event",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("down")),
        )
        error = PackIncompatibleError(
            [check_pack_compatibility(_INCOMPATIBLE_PACK_ID)]
        )
        # Must return normally — it is called from an except block.
        record_activation_refused(org_id="default", error=error)


class TestCompatibilitySnapshot:
    def test_snapshot_is_keyed_by_pack_id_and_json_shaped(self):
        reports = gate_pack_activation(
            org_id="default", pack_ids=["service_cloud", "cloud_ops"]
        )
        snapshot = compatibility_snapshot(reports)
        assert sorted(snapshot) == ["cloud_ops", "service_cloud"]
        assert snapshot["cloud_ops"]["minPlatformVersion"] == "1.9.0"
        assert snapshot["cloud_ops"]["platformVersion"] == PLATFORM_VERSION
        assert snapshot["cloud_ops"]["compatible"] is True
        assert "resolution_signature" in snapshot["cloud_ops"]["requiredConcepts"]

    def test_snapshot_of_an_empty_report_list_is_empty(self):
        assert compatibility_snapshot([]) == {}


# ── The compute edge resolves the same selection it gates ─────────────────────


class TestComputeEdgeSelectionResolution:
    """``_gate_pack_activation`` must gate the SAME effective selection
    ``_run_trackb_and_persist`` will execute — the launch record's packIds plus the
    request's — or a pack could be refused that never runs (or worse, run without
    being gated."""

    def _body(self, pack=None, pack_ids=None):
        from app.routes_sprint4_t1 import ComputeRequest

        return ComputeRequest(mode="offline", systems=[], pack=pack, pack_ids=pack_ids)

    def test_gates_a_pack_carried_only_by_the_launch_record(
        self, incompatible_packs, captured_events
    ):
        from fastapi import HTTPException

        from app.routes_sprint4_t1 import _gate_pack_activation

        run = {"id": "run-1", "packIds": [_INCOMPATIBLE_PACK_ID]}
        with pytest.raises(HTTPException) as excinfo:
            _gate_pack_activation("run-1", run, self._body())
        assert excinfo.value.status_code == 409
        assert "99.0.0" in excinfo.value.detail

    def test_gates_a_pack_supplied_only_by_the_request(
        self, incompatible_packs, captured_events
    ):
        from fastapi import HTTPException

        from app.routes_sprint4_t1 import _gate_pack_activation

        run = {"id": "run-2", "packIds": ["service_cloud"]}
        with pytest.raises(HTTPException) as excinfo:
            _gate_pack_activation(
                "run-2", run, self._body(pack_ids=[_INCOMPATIBLE_PACK_ID])
            )
        assert excinfo.value.status_code == 409

    def test_gates_the_singular_pack_alias(
        self, incompatible_packs, captured_events
    ):
        from fastapi import HTTPException

        from app.routes_sprint4_t1 import _gate_pack_activation

        run = {"id": "run-3"}
        with pytest.raises(HTTPException) as excinfo:
            _gate_pack_activation(
                "run-3", run, self._body(pack=_INCOMPATIBLE_PACK_ID)
            )
        assert excinfo.value.status_code == 409

    def test_compatible_selection_is_not_refused(self, captured_events):
        from app.routes_sprint4_t1 import _gate_pack_activation

        run = {"id": "run-4", "packIds": ["service_cloud", "cloud_ops"]}
        # Returns None (no raise) — compute proceeds.
        assert _gate_pack_activation("run-4", run, self._body()) is None
        assert captured_events == []

    def test_run_with_no_pack_configured_is_not_refused(self, captured_events):
        from app.routes_sprint4_t1 import _gate_pack_activation

        assert _gate_pack_activation("run-5", {"id": "run-5"}, self._body()) is None
        assert captured_events == []

    def test_refusal_records_the_run_id(self, incompatible_packs, captured_events):
        from fastapi import HTTPException

        from app.routes_sprint4_t1 import _gate_pack_activation

        run = {"id": "run-6", "orgId": "acme", "packIds": [_INCOMPATIBLE_PACK_ID]}
        with pytest.raises(HTTPException):
            _gate_pack_activation("run-6", run, self._body())
        _event_type, payload = captured_events[0]
        assert payload["run_id"] == "run-6"
        assert payload["org_id"] == "acme"


def test_gate_module_delegates_to_the_discovery_rule():
    """The app layer must not re-implement the compatibility rule — a second
    implementation is exactly the drift this module exists to prevent."""
    import inspect

    source = inspect.getsource(pack_activation)
    assert "assert_selection_activatable" in source
    # No local version arithmetic / concept vocabulary in the app layer.
    assert "minPlatformVersion" not in source
    assert "NORMALISED_CONCEPTS" not in source
