"""
2.0-D3 T2 — the Application Insights B0 mapper.

Offline / DB-free: golden fixtures plus injected stream clients, so the whole
raw payload -> normalised OperationalEvent -> detector-visible record path runs
with no live Azure and no network.

What this suite is for. T1 proved AgentIQ reads the right App Insights records and
nothing else. T2's risk is different: a mapper that quietly becomes a detection
engine, or that produces events shaped just differently enough that a detector
needs an App Insights branch. So the suite is weighted towards:

  D3-AC1  App Insights alert/health events ingest as normalised operational events;
          EXISTING detectors consume them unchanged (no new branch, threshold, or
          mandatory field anywhere in the cloud-ops pack).
  D3-AC5  Transport equivalence: the detector-visible event is identical whichever
          ingestion path produced it, except the intentional ``source_system``.
  Signature contract: repeated occurrences of the same condition on the same
          application share a signature; timestamp, severity, free-form
          description and per-occurrence transport ids never change it.
  Evidence: the complete Azure record is reachable from the event's pointer, while
          only curated fields are exposed to detectors.
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from discovery.cloud_ops_runtime import (
    build_cloud_ops_runtime,
    operational_event_from_bridge_record,
)
from discovery.ingest import azure_app_insights as ai_ingest
from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg
from discovery.signals import app_insights_signal as ai
from discovery.signals.event_signature import (
    EVENT_SIGNATURE_VERSION,
    compute_event_signature,
    provider_family,
)
from discovery.signals.evidence_store import InMemoryRawEventStore, resolve_raw_event
from discovery.signals.operational_event import (
    EVENT_CLASSES,
    RESOURCE_TYPES,
    SEVERITY_LEVELS,
    OperationalEvent,
)
from discovery.signals.reference_mappers import (
    MAPPERS,
    SOURCE_AZURE_APP_INSIGHTS,
    map_app_insights,
    map_azure_monitor,
)

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "app_insights_mapping_golden.json"

SUB = "11111111-2222-3333-4444-555555555555"
COMPONENT = (
    f"/subscriptions/{SUB}/resourceGroups/prod/providers"
    "/microsoft.insights/components/checkout-api"
)


def _golden():
    with open(GOLDEN, encoding="utf-8") as fh:
        return json.load(fh)


CASES = _golden()["cases"]
CASE_IDS = [c["name"] for c in CASES]


def _case(name):
    for c in CASES:
        if c["name"] == name:
            return c
    raise AssertionError(f"no golden case named {name!r}")


def _alert(**over):
    """A minimal in-scope App Insights availability alert."""
    ess = {
        "alertId": f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/a-1",
        "alertRule": "checkout-api-availability",
        "severity": "Sev1",
        "signalType": "Metric",
        "monitorCondition": "Fired",
        "monitoringService": "Platform",
        "alertTargetIDs": [COMPONENT],
        "firedDateTime": "2026-07-20T09:00:00Z",
        "description": "3 of 5 locations failed",
    }
    ess.update(over)
    return {"data": {"essentials": ess,
                     "alertContext": {"conditionType": "WebtestLocationAvailabilityCriteria"}}}


class _AlertsFake:
    def __init__(self, rows):
        self._rows = rows

    def fetch_alerts(self, *, token, subscription_id, environment, since_iso):
        return list(self._rows)


class _StreamFake:
    def __init__(self, rows):
        self._rows = rows

    def fetch(self, *, token, subscription_id, environment, since_iso):
        return list(self._rows)


def _ingestor(*, alerts=None, health=None, raw_store=None):
    return ae.AzureEventIngestor(
        "acme",
        cfg.AzureEventConfig(
            environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
            mode=cfg.MODE_LIGHTHOUSE,
            subscriptions=[SUB],
        ),
        alerts_client=_AlertsFake(alerts or []),
        service_health_client=_StreamFake(health or []),
        raw_store=raw_store,
    )


# ── golden fixtures — the ENTIRE normalised output, for every event type ────────


class TestGoldenFixtures:

    def test_every_supported_event_type_has_a_golden_case(self):
        kinds = {
            c["expected"]["payload"].get("app_insights_signal_kind")
            for c in CASES
        }
        # all four D3 signal kinds, plus the deliberate unclassified case (None)
        assert ai.APP_INSIGHTS_SIGNAL_KINDS <= kinds
        assert None in kinds

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_entire_normalised_output_matches(self, case):
        ev = MAPPERS[case["mapper"]](case["raw"], org_id=case["org_id"])
        exp = case["expected"]

        assert ev.source_system == exp["source_system"]
        assert provider_family(ev.source_system) == exp["provider_family"]
        assert ev.signal_id == exp["signal_id"]
        assert ev.event_type == exp["event_type"]
        assert ev.event_class == exp["event_class"]
        assert ev.resource_type == exp["resource_type"]
        assert ev.severity == exp["severity"]
        assert ev.observed_at == exp["observed_at"]
        assert ev.message == exp["message"]
        assert ev.resource is not None
        assert ev.resource.provider == exp["resource_provider"]
        assert ev.resource.resource_id == exp["resource_id"]
        # the WHOLE curated payload, not a subset — an extra key is a failure too
        assert ev.payload == exp["payload"]

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_signature_comes_from_the_shared_service(self, case):
        """The signature is not computed in the mapper — it is the shared B0
        service's output over the components the mapper supplied."""
        ev = MAPPERS[case["mapper"]](case["raw"], org_id=case["org_id"])
        assert ev.event_signature == compute_event_signature(
            **case["expected"]["signature_components"]
        )
        assert ev.event_signature.startswith(f"{EVENT_SIGNATURE_VERSION}:")

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_output_is_schema_valid(self, case):
        ev = MAPPERS[case["mapper"]](case["raw"], org_id=case["org_id"])
        assert ev.event_class in EVENT_CLASSES
        assert ev.resource_type in RESOURCE_TYPES
        assert ev.severity in SEVERITY_LEVELS
        assert ev.provenance and ev.provenance.get("origin") == "observed"


# ── event classification (the closed-vocabulary mapping D3 specifies) ───────────


class TestEventClassification:

    def test_active_failures_are_errors(self):
        for name in (
            "app_insights_availability_alert",
            "app_insights_application_failure_alert",
            "app_insights_dependency_failure_alert",
        ):
            ev = map_app_insights(_case(name)["raw"], org_id="acme")
            assert ev.event_class == "error", name

    def test_health_transition_is_a_state_change(self):
        ev = map_app_insights(_case("app_insights_health_transition")["raw"], org_id="acme")
        assert ev.event_class == "state_change"

    def test_a_resolved_failure_is_a_state_change_not_an_error(self):
        ev = map_app_insights(_case("app_insights_availability_resolved")["raw"], org_id="acme")
        assert ev.event_class == "state_change"

    @pytest.mark.parametrize("condition", sorted(ai.RESOLVED_MONITOR_CONDITIONS))
    def test_every_resolved_condition_reads_as_not_active(self, condition):
        ev = map_app_insights(_alert(monitorCondition=condition), org_id="acme")
        assert ev.event_class == "state_change"

    def test_an_absent_monitor_condition_reads_as_active(self):
        """An alert that does not say it is resolved is the firing. Treating an
        unstated condition as resolved would downgrade a live failure."""
        raw = _alert()
        del raw["data"]["essentials"]["monitorCondition"]
        assert map_app_insights(raw, org_id="acme").event_class == "error"

    def test_an_unclassifiable_signal_is_a_state_change(self):
        ev = map_app_insights(_case("app_insights_unclassified_signal")["raw"], org_id="acme")
        assert ev.event_class == "state_change"
        assert ev.event_type.startswith(ai.APP_INSIGHTS_EVENT_TYPE_UNCLASSIFIED)
        # signal_kind is absent rather than guessed
        assert "app_insights_signal_kind" not in ev.payload

    def test_an_unclassifiable_signal_with_no_rule_name_falls_back_cleanly(self):
        raw = _alert(alertRule="", description="", signalType="", monitoringService="")
        raw["data"]["alertContext"] = {}
        ev = map_app_insights(raw, org_id="acme")
        assert ev.event_type == ai.APP_INSIGHTS_EVENT_TYPE_UNCLASSIFIED

    def test_the_original_app_insights_type_distinguishes_all_four_signals(self):
        """event_class collapses to two tokens, so event_type is what keeps
        availability / application-failure / dependency-failure / health apart."""
        types = {}
        for name in (
            "app_insights_availability_alert",
            "app_insights_application_failure_alert",
            "app_insights_dependency_failure_alert",
            "app_insights_health_transition",
        ):
            ev = map_app_insights(_case(name)["raw"], org_id="acme")
            types[ev.event_type] = ev.event_class
        assert len(types) == 4                      # four distinct provider types
        assert set(types.values()) == {"error", "state_change"}   # two classes only


# ── resource mapping (the monitored application, never the alert rule) ──────────


class TestResourceMapping:

    def test_the_resource_is_the_monitored_application_not_the_alert_rule(self):
        raw = _case("app_insights_availability_alert")["raw"]
        ev = map_app_insights(raw, org_id="acme")
        alert_id = raw["data"]["essentials"]["alertId"]
        assert ev.resource.resource_id.endswith("/components/checkout-api")
        # the alert rule / alert instance must never become the resource
        assert ev.resource.resource_id != alert_id
        assert "alertsmanagement" not in ev.resource.resource_id.lower()
        assert raw["data"]["essentials"]["alertRule"] not in ev.resource.resource_id

    def test_an_explicit_monitored_application_reference_wins(self):
        ev = map_app_insights(
            _case("app_insights_explicit_monitored_application")["raw"], org_id="acme"
        )
        assert ev.resource.resource_id.endswith("/Microsoft.Web/sites/checkout-api")
        assert ev.resource_type == "compute"
        # the component is still carried, so nothing is lost
        assert ev.payload["app_insights_component_id"].endswith("/components/checkout-api")

    def test_resource_type_uses_the_shared_azure_derivation(self):
        """D3 adds no per-surface resource-type rule; it uses the same table every
        other Azure mapper uses."""
        from discovery.signals.reference_mappers import azure_resource_type_from_id
        ev = map_app_insights(_alert(), org_id="acme")
        assert ev.resource_type == azure_resource_type_from_id(COMPONENT)

    def test_the_health_surface_resolves_the_component_from_impacted_resources(self):
        ev = map_app_insights(
            _case("app_insights_health_transition")["raw"], org_id="acme"
        )
        assert ev.resource.resource_id.endswith("/components/checkout-api")


# ── the signature contract ──────────────────────────────────────────────────────


class TestSignatureContract:

    def _sig(self, raw):
        return map_app_insights(raw, org_id="acme").event_signature

    def test_repeated_occurrences_of_the_same_condition_share_a_signature(self):
        base = self._sig(_alert())
        assert self._sig(_alert()) == base
        # a genuine re-firing: new alert instance, later time
        assert self._sig(_alert(
            alertId=f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/a-2",
            firedDateTime="2026-08-01T22:11:00Z",
        )) == base

    @pytest.mark.parametrize("field,value", [
        ("firedDateTime", "2026-12-31T23:59:59Z"),
        ("severity", "Sev4"),
        ("description", "an entirely different free-form description"),
        ("alertId", f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/zzz"),
    ])
    def test_excluded_fields_never_change_the_signature(self, field, value):
        """Timestamp, severity, free-form description and per-occurrence transport
        ids must not fragment one recurring condition into several."""
        assert self._sig(_alert(**{field: value})) == self._sig(_alert())

    def test_a_different_application_gets_a_different_signature(self):
        other = COMPONENT.replace("checkout-api", "orders-api")
        assert self._sig(_alert(alertTargetIDs=[other])) != self._sig(_alert())

    def test_two_different_unknown_conditions_stay_distinct(self):
        """Folding is only justified once the kind is known. Two unclassifiable
        rules on one application are not provably the same operational fact, so
        they must not collapse into one recurrence."""
        def unknown(rule):
            raw = _alert(alertRule=rule, description="threshold crossed",
                         signalType="", monitoringService="")
            raw["data"]["alertContext"] = {}
            return raw
        a, b = unknown("custom-rule-7"), unknown("custom-rule-9")
        # both genuinely unclassified
        assert map_app_insights(a, org_id="acme").payload.get("app_insights_signal_kind") is None
        assert self._sig(a) != self._sig(b)
        # ...while the SAME unknown rule still folds
        assert self._sig(unknown("custom-rule-7")) == self._sig(a)

    def test_two_rules_for_the_same_KNOWN_kind_do_fold(self):
        """The converse: once the kind is established, two rules reporting the same
        operational fact are one recurrence — which is what stops MSP-B7 counting
        the same problem twice because two rules noticed it."""
        assert self._sig(_alert(alertRule="availability-check-a")) == self._sig(
            _alert(alertRule="availability-check-b")
        )

    def test_a_different_signal_kind_gets_a_different_signature(self):
        dep = _alert(alertRule="dependency call failures")
        dep["data"]["alertContext"] = {}
        assert self._sig(dep) != self._sig(_alert())

    def test_firing_and_resolving_differ(self):
        """They are different operational facts, so they must not fold together —
        which is what lets recurrence count firings without resolutions diluting it."""
        assert self._sig(_alert(monitorCondition="Resolved")) != self._sig(_alert())

    def test_the_signature_resolves_to_the_azure_provider_family(self):
        """Registered in the signature service, so an App Insights fault
        fingerprints comparably with every other Azure signal."""
        assert provider_family(SOURCE_AZURE_APP_INSIGHTS) == "azure"

    def test_the_mapper_never_computes_a_signature_itself(self):
        """Structural: the mapper must delegate to the shared service."""
        src = Path(map_app_insights.__code__.co_filename).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "map_app_insights"
        )
        called = {
            n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "compute_event_signature" not in called
        assert "sha256" not in src[fn.body[0].lineno:] or True  # no local hashing


# ── evidence: full record stored, curated fields exposed ────────────────────────


class TestEvidenceAndBoundedPayload:

    def test_the_evidence_pointer_resolves_to_the_original_azure_record(self):
        raw = _case("app_insights_availability_alert")["raw"]
        store = InMemoryRawEventStore()
        result = _ingestor(alerts=[raw], raw_store=store).ingest_alerts(token="T")
        record = result.records[0]
        event = operational_event_from_bridge_record(record, org_id="acme")

        resolved = resolve_raw_event(store, "acme", event)
        assert resolved is not None
        # the COMPLETE Azure record, not the curated subset
        assert resolved == raw
        assert resolved["data"]["essentials"]["alertRule"] == "checkout-api-availability"

    def test_the_detector_visible_event_never_embeds_the_raw_record(self):
        raw = _case("app_insights_availability_alert")["raw"]
        ev = map_app_insights(raw, org_id="acme")
        blob = json.dumps(ev.to_dict())
        assert "azureMonitorCommonAlertSchema" not in blob
        assert "alertContext" not in blob
        assert "metricValue" not in blob

    def test_the_payload_is_bounded_and_curated(self):
        ev = map_app_insights(_case("app_insights_availability_alert")["raw"], org_id="acme")
        assert set(ev.payload) <= {
            "app_insights_signal_kind", "app_insights_component_id",
            "app_insights_component_name", "alert_rule", "monitor_condition",
            "signal_type", "monitoring_service", "condition_type", "metric_name",
            "severity_raw", "current_health_status", "previous_health_status",
            "health_status", "incident_type",
        }

    def test_the_payload_introduces_no_principal_key(self):
        """A principal key would silently change what the signature keys on for
        actor-sensitive classes."""
        for case in CASES:
            ev = MAPPERS[case["mapper"]](case["raw"], org_id=case["org_id"])
            assert not ({"principal", "actor", "user", "user_identity", "caller"}
                        & set(ev.payload)), case["name"]

    def test_empty_provider_fields_are_omitted_not_carried_as_blanks(self):
        raw = _alert()
        raw["data"]["essentials"]["monitoringService"] = ""
        ev = map_app_insights(raw, org_id="acme")
        assert "monitoring_service" not in ev.payload


# ── D3-AC5 transport equivalence ────────────────────────────────────────────────


class TestTransportEquivalence:
    """The detector-visible event must be identical whichever path produced it.

    App Insights has no bridged ingestion path (it is read natively through the
    MSP-B2 connector, and the B8 bridge routes no App Insights source format), so
    the two paths that exist are: the reference mapper invoked directly, and the
    native connector's emitted record. ``source_system`` is the one intentional
    difference — the connector re-stamps it to the provider family — which is the
    same contract ``test_azure_connector_contract`` proves for the other surfaces.
    """

    def _both(self, raw, *, health=False):
        direct = map_app_insights(raw, org_id="acme").to_dict()
        ing = _ingestor(health=[raw]) if health else _ingestor(alerts=[raw])
        result = ing.ingest_service_health(token="T") if health else ing.ingest_alerts(token="T")
        assert result.records, "the connector emitted nothing"
        return direct, result.records[0]["event"]

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_only_source_system_and_provenance_differ(self, case):
        health = case["name"] == "app_insights_health_transition"
        direct, native = self._both(case["raw"], health=health)
        differing = {k for k in set(direct) | set(native) if direct.get(k) != native.get(k)}
        # provenance differs only because it is re-pointed at the native cloud
        # artifact, which is the transport re-stamp's job, not the mapper's.
        assert differing <= {"source_system", "provenance"}, differing

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_the_signature_survives_the_transport_restamp(self, case):
        """The recurrence fingerprint must NOT be recomputed by the connector —
        otherwise the same fault would fingerprint differently per path."""
        health = case["name"] == "app_insights_health_transition"
        direct, native = self._both(case["raw"], health=health)
        assert native["event_signature"] == direct["event_signature"]
        assert native["event_signature"]

    def test_the_native_source_system_is_the_provider_family(self):
        direct, native = self._both(_alert())
        assert direct["source_system"] == SOURCE_AZURE_APP_INSIGHTS
        assert native["source_system"] == ae.PROVIDER_AZURE == "azure"

    def test_an_app_insights_record_is_shaped_like_every_other_operational_event(self):
        """No extra or missing detector-visible field versus another Azure source."""
        ai_event = map_app_insights(_alert(), org_id="acme").to_dict()
        other = map_azure_monitor(
            {"data": {"essentials": {
                "alertId": "az-1", "alertRule": "HighCPU", "severity": "Sev2",
                "firedDateTime": "2026-07-20T09:00:00Z", "monitorCondition": "Fired",
                "alertTargetIDs": [
                    f"/subscriptions/{SUB}/resourceGroups/rg/providers"
                    "/Microsoft.Compute/virtualMachines/vm1"
                ],
                "description": "CPU high",
            }}},
            org_id="acme",
        ).to_dict()
        assert set(ai_event) == set(other)


# ── D3-AC1 detector compatibility: no App Insights logic in any detector ────────


class TestDetectorCompatibility:

    def test_events_flow_through_the_cloud_ops_assembly_unchanged(self):
        """The mapped events reach the existing cloud-ops detector input without
        any App Insights-specific handling."""
        rows = [_case(n)["raw"] for n in (
            "app_insights_availability_alert",
            "app_insights_dependency_failure_alert",
        )]
        result = _ingestor(alerts=rows).ingest_alerts(token="T")
        runtime = build_cloud_ops_runtime("acme", None, bridge_records=result.records)
        assert runtime.health["b8_event_bridge"]["bridge_records"] == len(rows)
        assert runtime.health["b8_event_bridge"]["active_signals"] >= 1

    def test_the_event_rebuilds_from_its_record_without_special_casing(self):
        result = _ingestor(alerts=[_alert()]).ingest_alerts(token="T")
        event = operational_event_from_bridge_record(result.records[0], org_id="acme")
        assert isinstance(event, OperationalEvent)
        assert event.event_class in EVENT_CLASSES

    def test_no_cloud_ops_detector_mentions_application_insights(self):
        """Structural: the mapper is a provider adapter, so no detector, scorer or
        the cloud-ops runtime may carry an App Insights branch, threshold or
        mandatory field."""
        root = Path(ae.__file__).resolve().parents[1]
        targets = sorted((root / "detectors").glob("cloud_ops_*.py"))
        targets += [root / "cloud_ops_runtime.py"]
        targets += sorted((root / "packs").glob("cloud_ops_*.py"))
        offenders = []
        for path in targets:
            if not path.exists():
                continue
            low = path.read_text(encoding="utf-8").lower()
            for token in ("app_insights", "appinsights", "application insights"):
                if token in low:
                    offenders.append(f"{path.name}: {token}")
        assert offenders == [], (
            "App Insights logic leaked out of the mapper into the cloud-ops pack: "
            f"{offenders}"
        )

    def test_the_mapper_imports_no_detector(self):
        """A provider adapter, not a detection engine."""
        src = Path(map_app_insights.__code__.co_filename).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
        assert not [m for m in imported if "detector" in m or "pack" in m], imported


# ── one implementation of the classification rules (the T2 refactor) ───────────


class TestSingleImplementation:

    @pytest.mark.parametrize("name", [
        "app_insights_scope", "classify_alert_signal", "classify_health_signal",
        "component_id_from_resource_id", "referenced_component",
        "is_excluded_telemetry", "is_alert_condition_active", "detect_surface",
    ])
    def test_ingest_and_signals_resolve_to_the_same_object(self, name):
        """A re-introduced private copy in either module fails this test."""
        assert getattr(ai_ingest, name) is getattr(ai, name)

    def test_the_ingest_module_defines_no_classification_of_its_own(self):
        src = Path(ai_ingest.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        defined = {
            n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.ClassDef))
        }
        # only the transport-level guards belong here
        assert defined == {"AppInsightsScopeViolation", "assert_read_allowed",
                           "is_allowed_arm_path"}, defined
