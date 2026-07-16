"""R18-A6 / AT-609 (T4) — runtime-to-structure entity resolution.

Covers AC4: a runtime (phase-one operational) entity resolves to its structural
counterpart when the evidence supports it, and ambiguous cases stay separate —
conservative resolution consistent with the standing discipline in
``app.entity_resolution``.

The behaviours pinned here mirror that engine's three-branch rule:
  * 0 structural candidates       → unresolved (left separate);
  * exactly 1 candidate           → resolved (merge), 1.0 on stable app_id / 0.8 on name;
  * 2+ candidates                 → ambiguous (left separate, never force-merged, N+1).
Plus a platform gate (a Java runtime never resolves to a .NET app) and the same
canonical-name normalisation.

Pure/offline: the resolver touches no DB and no ``app`` package, so this runs with
the deterministic discovery suite.
"""
from __future__ import annotations

import pytest

from discovery.enterprise_apps.app_repo_map import AppRepoMapping
from discovery.enterprise_apps.runtime_structure_resolution import (
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_NAME,
    CONFIDENCE_STABLE_ID,
    MATCH_APP_ID,
    MATCH_SERVICE,
    STATUS_AMBIGUOUS,
    STATUS_RESOLVED,
    STATUS_UNRESOLVED,
    ResolutionOutcome,
    RuntimeEntity,
    StructuralEntity,
    resolve_runtime_entity,
    resolve_runtime_to_structure,
    runtime_entity_from_operational,
    structural_entities_from_mappings,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
def _runtime(app_id, service=None, platform="java", source_system="java_app"):
    return RuntimeEntity(
        app_id=app_id,
        service=service if service is not None else app_id,
        platform=platform,
        source_system=source_system,
    )


def _struct(app_id, name=None, platform="java", service=None, kind="application"):
    return StructuralEntity(
        app_id=app_id,
        name=name if name is not None else app_id,
        platform=platform,
        service=service if service is not None else app_id,
        kind=kind,
        qualified_name=app_id,
    )


_APPS = [
    _struct("covenant-service", "Covenant Service", "java", "covenant-service"),
    _struct("billing", "Billing", "java", "billing"),
    _struct("billing-dotnet", "Billing (.NET)", "dotnet", "billing"),
]


# ═════════════════════════════════════════════════════════════════════════════
# AC4 — confident match merges
# ═════════════════════════════════════════════════════════════════════════════
def test_exact_app_id_match_resolves_with_full_confidence():
    o = resolve_runtime_entity(_runtime("covenant-service"), _APPS)
    assert o.status == STATUS_RESOLVED
    assert o.matched.app_id == "covenant-service"
    assert o.match_kind == MATCH_APP_ID
    assert o.confidence == CONFIDENCE_STABLE_ID
    assert o.is_resolved


def test_service_name_match_resolves_with_name_confidence():
    # app_id differs, but the service/display name matches exactly one app.
    o = resolve_runtime_entity(_runtime("cov-svc-01", service="Covenant Service"), _APPS)
    assert o.status == STATUS_RESOLVED
    assert o.matched.app_id == "covenant-service"
    assert o.match_kind == MATCH_SERVICE
    assert o.confidence == CONFIDENCE_NAME


def test_app_id_match_takes_precedence_over_name_match():
    # Runtime app_id matches app-a's id; its service name matches app-b's name.
    apps = [
        _struct("app-a", name="A", service="a"),
        _struct("app-b", name="app-a", service="b"),  # name collides with runtime app_id
    ]
    o = resolve_runtime_entity(_runtime("app-a", service="app-a"), apps)
    assert o.status == STATUS_RESOLVED
    assert o.matched.app_id == "app-a"  # stable-id wins
    assert o.match_kind == MATCH_APP_ID


def test_normalization_matches_case_and_whitespace_insensitively():
    o = resolve_runtime_entity(
        _runtime("x", service="  Covenant   SERVICE "), _APPS
    )
    assert o.status == STATUS_RESOLVED
    assert o.matched.app_id == "covenant-service"


# ═════════════════════════════════════════════════════════════════════════════
# AC4 — ambiguous / no-match stays separate
# ═════════════════════════════════════════════════════════════════════════════
def test_no_candidate_is_unresolved_and_separate():
    o = resolve_runtime_entity(_runtime("ghost-service"), _APPS)
    assert o.status == STATUS_UNRESOLVED
    assert o.matched is None
    assert o.candidates == ()
    assert not o.is_resolved


def test_multiple_candidates_are_ambiguous_and_never_merged():
    # Two Java apps share the service name "shared" → ambiguous, left separate.
    apps = [
        _struct("app-a", name="A", service="shared"),
        _struct("app-b", name="B", service="shared"),
    ]
    o = resolve_runtime_entity(_runtime("runtime-x", service="shared"), apps)
    assert o.status == STATUS_AMBIGUOUS
    assert o.matched is None  # NEVER force-merged (N+1 discipline)
    assert o.confidence == CONFIDENCE_AMBIGUOUS
    assert {c.app_id for c in o.candidates} == {"app-a", "app-b"}


def test_platform_gate_excludes_cross_platform_matches():
    # A .NET runtime service must NOT resolve to the identically-named Java app.
    o = resolve_runtime_entity(
        _runtime("covenant-service", platform="dotnet", source_system="dotnet_app"),
        _APPS,
    )
    assert o.status == STATUS_UNRESOLVED
    assert o.matched is None


def test_platform_gate_turns_cross_platform_collision_into_single_match():
    # "billing" exists as BOTH a Java and a .NET app; a Java runtime resolves only
    # to the Java one (the .NET one is gated out) → a clean single-candidate merge.
    o = resolve_runtime_entity(_runtime("billing", platform="java"), _APPS)
    assert o.status == STATUS_RESOLVED
    assert o.matched.app_id == "billing"
    assert o.matched.platform == "java"


def test_unknown_platform_does_not_gate():
    # When a platform is unknown we cannot rule a match out on platform grounds.
    apps = [_struct("svc", name="Svc", platform="")]
    o = resolve_runtime_entity(_runtime("svc", platform=""), apps)
    assert o.status == STATUS_RESOLVED


# ═════════════════════════════════════════════════════════════════════════════
# Batch + determinism
# ═════════════════════════════════════════════════════════════════════════════
def test_batch_resolves_each_entity_independently_and_in_order():
    runtimes = [
        _runtime("covenant-service"),        # resolved (app_id)
        _runtime("ghost"),                   # unresolved
        _runtime("cov-svc-01", service="Covenant Service"),  # resolved (name)
    ]
    outcomes = resolve_runtime_to_structure(runtimes, _APPS)
    assert [o.status for o in outcomes] == [
        STATUS_RESOLVED,
        STATUS_UNRESOLVED,
        STATUS_RESOLVED,
    ]


def test_resolution_is_deterministic():
    runtimes = [_runtime("covenant-service"), _runtime("billing")]
    first = [o.to_dict() for o in resolve_runtime_to_structure(runtimes, _APPS)]
    second = [o.to_dict() for o in resolve_runtime_to_structure(runtimes, list(reversed(_APPS)))]
    assert first == second  # candidate order must not change the decision


def test_outcome_to_dict_is_serialisable():
    import json

    o = resolve_runtime_entity(_runtime("covenant-service"), _APPS)
    blob = json.dumps(o.to_dict())
    assert "covenant-service" in blob
    assert json.loads(blob)["status"] == STATUS_RESOLVED


# ═════════════════════════════════════════════════════════════════════════════
# Builders
# ═════════════════════════════════════════════════════════════════════════════
def test_runtime_entity_from_operational_record():
    re = runtime_entity_from_operational(
        {"app_id": "payments-api", "service": "payments", "source_system": "java_app"}
    )
    assert re.app_id == "payments-api"
    assert re.service == "payments"
    assert re.platform == "java"  # derived from source_system


def test_runtime_entity_service_falls_back_to_app_id():
    re = runtime_entity_from_operational({"app_id": "orders", "source_system": "dotnet_app"})
    assert re.service == "orders"
    assert re.platform == "dotnet"


def test_runtime_entity_from_empty_record_is_none():
    assert runtime_entity_from_operational({}) is None
    assert runtime_entity_from_operational({"source_system": "java_app"}) is None
    assert runtime_entity_from_operational("not a dict") is None


def test_structural_entities_from_mappings():
    mappings = [
        AppRepoMapping("covenant-service", "Covenant Service", "java", ("web", "core"),
                       {"service": "covenant-service"}),
    ]
    ents = structural_entities_from_mappings(mappings)
    assert len(ents) == 1
    assert ents[0].app_id == "covenant-service"
    assert ents[0].platform == "java"
    assert ents[0].kind == "application"
    assert ents[0].service == "covenant-service"


def test_end_to_end_operational_record_resolves_to_configured_app():
    # The AC4 join in one line: an operational record resolves to its configured app.
    apps = structural_entities_from_mappings(
        [AppRepoMapping("covenant-service", "Covenant Service", "java", ("web",),
                        {"service": "covenant-service"})]
    )
    record = {
        "app_id": "covenant-service",
        "service": "covenant-service",
        "source_system": "java_app",
        "artifact_kind": "metrics",
        "error_rate": 0.11,  # "error rate rising" — the runtime signal
    }
    runtime = runtime_entity_from_operational(record)
    outcome = resolve_runtime_entity(runtime, apps)
    assert outcome.is_resolved
    assert outcome.matched.app_id == "covenant-service"
    assert outcome.confidence == CONFIDENCE_STABLE_ID
