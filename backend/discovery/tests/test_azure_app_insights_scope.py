"""
2.0-D3 T1 — the BOUNDED Application Insights read scope.

Offline / DB-free: the stream clients and the ARM token are injected, so the whole
poll -> scope-defence -> classify -> map -> emit -> checkpoint flow runs with no
live Azure and no network.

What this suite is actually for. D3's risk is not "does it read alerts" (MSP-B2
already does) — it is scope creep: the moment AgentIQ reads Application Insights
telemetry it becomes an observability product, and the moment it infers which
application an alert belongs to it starts inventing associations. So the tests
below are weighted towards the two NEGATIVE guarantees:

  D3-AC1  App Insights alert/health events ingest as normalised operational events
          via the EXISTING Azure rails; detectors consume them unchanged.
  D3-AC2  Raw telemetry, traces and analytics queries are NOT ingested — proved by
          seeding them, plus a structural check that no excluded endpoint exists in
          the Azure ingest layer at all.
  (AC3 foundation) Association is by EXPLICIT reference only; ambiguous stays
          unresolved. Full component/CI association is D3 T3.
  D3-AC4  Events pass through MSP-B7 admission (dedup with counts, budgets).
  D3-AC5  Transport equivalence: a D3 record is shaped exactly like any other
          operational record; the App Insights scope rides the WRAPPER only.

Plus the task's incremental-behaviour requirements: pinned subscriptions only,
per-subscription checkpoints preserved and resumable, and a clear degraded status
when a subscription fails or a volume budget stops processing.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from discovery.ingest import azure_app_insights as ai
from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg


# ── fixtures / builders ──────────────────────────────────────────────────────────

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "ingest" / "fixtures" / "azure_app_insights_sample.json"
)

SUB_A = "11111111-2222-3333-4444-555555555555"
SUB_B = "bbbbbbbb-0000-0000-0000-000000000002"

COMPONENT = (
    f"/subscriptions/{SUB_A}/resourceGroups/prod/providers"
    "/microsoft.insights/components/prod-checkout-api"
)


@pytest.fixture(scope="module")
def fx():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _by_kind(records, kind):
    """The fixture record whose declared expected kind is ``kind``."""
    for r in records:
        if r.get("_expected_signal_kind") == kind:
            return r
    raise AssertionError(f"no fixture case for kind {kind!r}")


def _in_scope_cases(records):
    return [r for r in records if r.get("_expected_in_scope") is not False]


def _out_of_scope_cases(records):
    return [r for r in records if r.get("_expected_in_scope") is False]


class FakeAlertsClient:
    def __init__(self, by_sub=None):
        self.by_sub = dict(by_sub or {})
        self.fail_subs = set()
        self.calls = []

    def fetch_alerts(self, *, token, subscription_id, environment, since_iso):
        self.calls.append({"sub": subscription_id, "since": since_iso})
        if subscription_id in self.fail_subs:
            raise RuntimeError("throttled (429)")
        return list(self.by_sub.get(subscription_id, []))


class FakeStreamClient:
    def __init__(self, by_sub=None):
        self.by_sub = dict(by_sub or {})
        self.fail_subs = set()
        self.calls = []

    def fetch(self, *, token, subscription_id, environment, since_iso):
        self.calls.append({"sub": subscription_id, "since": since_iso})
        if subscription_id in self.fail_subs:
            raise RuntimeError("throttled (429)")
        return list(self.by_sub.get(subscription_id, []))


def _config(subs):
    return cfg.AzureEventConfig(
        environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
        mode=cfg.MODE_LIGHTHOUSE,
        subscriptions=list(subs),
    )


def _ingestor(subs, *, alerts=None, health=None, **kw):
    return ae.AzureEventIngestor(
        "org1", _config(subs),
        alerts_client=alerts, service_health_client=health, **kw,
    )


# ── D3-AC1 — the four signal kinds ingest via the existing rails ────────────────


class TestAC1SignalsIngestViaExistingRails:

    @pytest.mark.parametrize(
        "kind",
        [ai.SIGNAL_AVAILABILITY, ai.SIGNAL_APPLICATION_FAILURE, ai.SIGNAL_DEPENDENCY_FAILURE],
    )
    def test_each_alert_kind_is_classified(self, fx, kind):
        raw = _by_kind(fx["positive_alerts"], kind)
        scope = ai.app_insights_scope(raw, surface=ai.SURFACE_AZURE_MONITOR)
        assert scope is not None
        assert scope.signal_kind == kind
        assert scope.component_id == COMPONENT
        assert scope.component_name == "prod-checkout-api"

    def test_health_transition_is_classified(self, fx):
        raw = _by_kind(fx["positive_health_events"], ai.SIGNAL_HEALTH_TRANSITION)
        scope = ai.app_insights_scope(raw, surface=ai.SURFACE_AZURE_SERVICE_HEALTH)
        assert scope is not None
        assert scope.signal_kind == ai.SIGNAL_HEALTH_TRANSITION
        assert scope.component_id == COMPONENT

    def test_every_kind_is_in_the_closed_vocabulary(self, fx):
        for raw in _in_scope_cases(fx["positive_alerts"]):
            scope = ai.app_insights_scope(raw, surface=ai.SURFACE_AZURE_MONITOR)
            assert scope is not None
            assert scope.signal_kind is None or scope.signal_kind in ai.APP_INSIGHTS_SIGNAL_KINDS

    def test_alerts_ingest_end_to_end_through_the_b2_connector(self, fx):
        alerts = _in_scope_cases(fx["positive_alerts"])
        client = FakeAlertsClient({SUB_A: alerts})
        result = _ingestor([SUB_A], alerts=client).ingest_alerts(token="T")

        assert result.emitted_count == len(alerts)
        assert result.subscription_status[SUB_A]["status"] == "ok"
        # every emitted record carries a normalised MSP-B0 event
        for rec in result.records:
            assert rec["event"]["org_id"] == "org1"
            assert rec["event"]["source_system"] == ae.PROVIDER_AZURE
            assert rec["event"]["event_signature"]

    def test_health_events_ingest_end_to_end(self, fx):
        events = _in_scope_cases(fx["positive_health_events"])
        client = FakeStreamClient({SUB_A: events})
        result = _ingestor([SUB_A], health=client).ingest_service_health(token="T")
        assert result.emitted_count == len(events)
        annotated = [r for r in result.records if "app_insights" in r]
        assert len(annotated) == 1
        assert annotated[0]["app_insights"]["signal_kind"] == ai.SIGNAL_HEALTH_TRANSITION

    def test_scope_count_is_reported_in_run_health(self, fx):
        alerts = _in_scope_cases(fx["positive_alerts"])
        client = FakeAlertsClient({SUB_A: alerts})
        result = _ingestor([SUB_A], alerts=client).ingest_alerts(token="T")
        assert result.subscription_status[SUB_A]["app_insights"] == len(alerts)

    def test_no_new_read_surface_is_introduced(self, fx):
        """D3 rides surfaces B2 already reaches — the alerts client is the only reader."""
        client = FakeAlertsClient({SUB_A: _in_scope_cases(fx["positive_alerts"])})
        _ingestor([SUB_A], alerts=client).ingest_alerts(token="T")
        assert [c["sub"] for c in client.calls] == [SUB_A]


# ── D3-AC2 — raw telemetry and analytics are NOT ingested ───────────────────────


class TestAC2TelemetryAndAnalyticsAreNeverIngested:

    def test_every_negative_fixture_is_recognised_as_excluded(self, fx):
        for raw in fx["negative_excluded_telemetry"]:
            assert ai.is_excluded_telemetry(raw) is True, raw.get("_exclusion")

    def test_excluded_records_never_acquire_an_app_insights_identity(self, fx):
        for raw in fx["negative_excluded_telemetry"]:
            assert ai.app_insights_scope(raw, surface=ai.SURFACE_AZURE_MONITOR) is None
            assert ai.app_insights_scope(raw, surface=ai.SURFACE_AZURE_SERVICE_HEALTH) is None

    def test_seeded_telemetry_emits_nothing_through_the_alerts_stream(self, fx):
        """The scope-defence test the AC asks for: seed telemetry, ingest nothing."""
        client = FakeAlertsClient({SUB_A: list(fx["negative_excluded_telemetry"])})
        result = _ingestor([SUB_A], alerts=client).ingest_alerts(token="T")
        assert result.emitted_count == 0
        assert result.records == []
        st = result.subscription_status[SUB_A]
        assert st["telemetry_excluded"] == len(fx["negative_excluded_telemetry"])
        assert "app_insights" not in st

    def test_seeded_telemetry_emits_nothing_through_the_health_stream(self, fx):
        client = FakeStreamClient({SUB_A: list(fx["negative_excluded_telemetry"])})
        result = _ingestor([SUB_A], health=client).ingest_service_health(token="T")
        assert result.emitted_count == 0
        assert result.subscription_status[SUB_A]["telemetry_excluded"] == len(
            fx["negative_excluded_telemetry"]
        )

    def test_excluded_records_are_dropped_loudly(self, fx, caplog):
        client = FakeAlertsClient({SUB_A: [fx["negative_excluded_telemetry"][0]]})
        with caplog.at_level("WARNING"):
            _ingestor([SUB_A], alerts=client).ingest_alerts(token="T")
        assert any("out-of-scope" in r.message or "out-of-scope" in r.getMessage()
                   for r in caplog.records)

    def test_excluded_records_do_not_advance_a_checkpoint(self, fx):
        """An excluded record is not data this connector processed, so it must not
        move the position — otherwise seeding telemetry could skip real alerts."""
        client = FakeAlertsClient({SUB_A: list(fx["negative_excluded_telemetry"])})
        result = _ingestor([SUB_A], alerts=client).ingest_alerts(
            token="T", checkpoint=ae.encode_checkpoints({SUB_A: "2026-06-01T00:00:00Z"})
        )
        assert ae.decode_checkpoints(result.next_checkpoint)[SUB_A] == "2026-06-01T00:00:00Z"

    def test_mixed_page_keeps_the_alert_and_drops_the_telemetry(self, fx):
        """Telemetry alongside real alerts must not take the alerts down with it."""
        page = [
            fx["negative_excluded_telemetry"][0],
            _by_kind(fx["positive_alerts"], ai.SIGNAL_AVAILABILITY),
            fx["negative_excluded_telemetry"][1],
        ]
        client = FakeAlertsClient({SUB_A: page})
        result = _ingestor([SUB_A], alerts=client).ingest_alerts(token="T")
        assert result.emitted_count == 1
        assert result.subscription_status[SUB_A]["telemetry_excluded"] == 2
        assert result.records[0]["app_insights"]["signal_kind"] == ai.SIGNAL_AVAILABILITY

    @pytest.mark.parametrize("url", [
        "https://api.applicationinsights.io/v1/apps/abc/query?query=requests",
        "https://api.applicationinsights.io/v1/apps/abc/metrics/requests%2Fcount",
        "https://management.azure.com/subscriptions/s/resourceGroups/rg/providers/"
        "microsoft.insights/components/app/providers/microsoft.insights/metrics",
        "https://management.azure.com/subscriptions/s/providers/"
        "Microsoft.OperationalInsights/workspaces/w/query",
        "https://api.loganalytics.io/v1/workspaces/w/query",
    ])
    def test_excluded_endpoints_are_refused_at_call_time(self, url):
        with pytest.raises(ai.AppInsightsScopeViolation):
            ai.assert_read_allowed(url)

    @pytest.mark.parametrize("path", sorted(ai.ALLOWED_ARM_PATHS))
    def test_the_permitted_arm_paths_are_allowed(self, path):
        url = f"https://management.azure.com/subscriptions/s/{path}"
        ai.assert_read_allowed(url)  # must not raise
        assert ai.is_allowed_arm_path(url)

    def test_azure_ingest_layer_reads_only_permitted_arm_paths(self):
        """Structural: every ARM path CONSTANT in the Azure ingest layer is permitted.

        A docstring promising "no telemetry endpoint" is not a control. This walks
        the AST of the Azure ingest modules, collects every string literal that
        looks like an ARM provider path, and asserts each is in ALLOWED_ARM_PATHS.
        Adding a metrics / Log Analytics / App Insights REST read therefore fails
        the build without anyone remembering to update a test.
        """
        ingest_dir = Path(ae.__file__).resolve().parent
        offenders = []
        for module in sorted(ingest_dir.glob("azure_*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                text = node.value
                low = text.lower()
                if "providers/microsoft." not in low:
                    continue
                # Resource ids (they name a resource, not a read path) and the
                # scope module's own exclusion markers are not read paths.
                if "/subscriptions/" in low or module.name == "azure_app_insights.py":
                    continue
                if not ai.is_allowed_arm_path(low):
                    offenders.append(f"{module.name}: {text}")
        assert offenders == [], (
            "Azure ingest layer references an ARM path outside the 2.0-D3 "
            f"permitted set: {offenders}"
        )

    def test_azure_ingest_layer_names_no_excluded_endpoint(self):
        """Structural: no excluded telemetry/analytics host or path is referenced."""
        ingest_dir = Path(ae.__file__).resolve().parent
        offenders = []
        for module in sorted(ingest_dir.glob("azure_*.py")):
            if module.name == "azure_app_insights.py":
                continue  # it DEFINES the exclusion list
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    low = node.value.lower()
                    for marker in ("api.applicationinsights.io", "api.loganalytics.io",
                                   "operationalinsights", "kusto", "/v1/apps/"):
                        if marker in low:
                            offenders.append(f"{module.name}: {node.value}")
        assert offenders == []

    def test_the_gate_goes_red_when_an_excluded_path_is_introduced(self):
        """The guard is proved to FAIL, not merely asserted to pass.

        A gate never observed rejecting anything is not known to be a gate. This
        runs the same predicate the structural tests use against a path that a
        telemetry-reading change would introduce, and requires it to be refused.
        """
        telemetry_path = "providers/microsoft.insights/metrics"
        assert not ai.is_allowed_arm_path(telemetry_path)
        with pytest.raises(ai.AppInsightsScopeViolation):
            ai.assert_read_allowed(f"https://management.azure.com/subscriptions/s/{telemetry_path}")


# ── explicit reference only (the foundation D3 T3's AC3 builds on) ───────────────


class TestExplicitReferenceOnly:

    def test_a_component_reference_resolves(self):
        assert ai.component_id_from_resource_id(COMPONENT) == COMPONENT
        assert ai.component_name_from_resource_id(COMPONENT) == "prod-checkout-api"

    def test_a_child_resource_resolves_to_its_parent_component(self):
        child = COMPONENT + "/providers/microsoft.insights/webtests/ping-1"
        assert ai.component_id_from_resource_id(child) == COMPONENT

    @pytest.mark.parametrize("rid", [
        "",
        None,
        "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/sites/prod-api",
        # a different microsoft.insights type — NOT a monitored application
        "/subscriptions/s/resourceGroups/rg/providers/microsoft.insights/metricalerts/r",
        "/subscriptions/s/resourceGroups/rg/providers/microsoft.insights/actiongroups/ag",
        "/subscriptions/s/resourceGroups/rg/providers/microsoft.insights/webtests/w",
        # the segment present but no component name
        "/subscriptions/s/resourceGroups/rg/providers/microsoft.insights/components/",
        # a name that merely mentions the words
        "prod-checkout-api microsoft.insights components",
    ])
    def test_non_components_ids_never_resolve(self, rid):
        assert ai.component_id_from_resource_id(rid) is None

    def test_matching_is_case_insensitive_on_the_provider_path(self):
        upper = COMPONENT.replace(
            "microsoft.insights/components", "Microsoft.Insights/Components"
        )
        assert ai.component_id_from_resource_id(upper) is not None

    def test_an_alert_naming_no_component_is_out_of_scope(self, fx):
        for raw in _out_of_scope_cases(fx["positive_alerts"]):
            assert ai.app_insights_scope(raw, surface=ai.SURFACE_AZURE_MONITOR) is None

    def test_a_health_event_naming_no_component_is_out_of_scope(self, fx):
        for raw in _out_of_scope_cases(fx["positive_health_events"]):
            assert ai.app_insights_scope(raw, surface=ai.SURFACE_AZURE_SERVICE_HEALTH) is None

    def test_an_unclassifiable_in_scope_alert_keeps_a_null_kind(self, fx):
        """Ambiguous stays unresolved — never forced into a bucket."""
        raw = _by_kind(fx["positive_alerts"], None)
        scope = ai.app_insights_scope(raw, surface=ai.SURFACE_AZURE_MONITOR)
        assert scope is not None            # it IS in scope
        assert scope.signal_kind is None    # but the kind is not invented

    def test_classification_precedence_is_most_specific_first(self):
        """A dependency-failure alert also says "failure"; the specific kind wins."""
        raw = {"data": {"essentials": {
            "alertRule": "dependency call failure rate",
            "alertTargetIDs": [COMPONENT],
        }}}
        assert ai.classify_alert_signal(raw) == ai.SIGNAL_DEPENDENCY_FAILURE
        raw2 = {"data": {"essentials": {
            "alertRule": "availability test failure",
            "alertTargetIDs": [COMPONENT],
        }}}
        assert ai.classify_alert_signal(raw2) == ai.SIGNAL_AVAILABILITY

    def test_an_unknown_surface_is_never_in_scope(self, fx):
        raw = _by_kind(fx["positive_alerts"], ai.SIGNAL_AVAILABILITY)
        assert ai.app_insights_scope(raw, surface="azure_activity") is None
        assert ai.app_insights_scope(raw, surface="") is None


# ── D3-AC5 — transport equivalence, and no B2 regression ────────────────────────


class TestAC5TransportEquivalenceAndNoRegression:

    def test_the_scope_rides_the_wrapper_never_the_event(self, fx):
        client = FakeAlertsClient({SUB_A: [
            _by_kind(fx["positive_alerts"], ai.SIGNAL_AVAILABILITY)
        ]})
        rec = _ingestor([SUB_A], alerts=client).ingest_alerts(token="T").records[0]
        assert "app_insights" in rec                      # wrapper
        assert "app_insights" not in rec["event"]          # never the event
        assert "component_id" not in rec["event"]
        assert "signal_kind" not in rec["event"]
        assert "app_insights" not in (rec["event"].get("payload") or {})

    def test_an_out_of_scope_record_is_shaped_exactly_as_before_d3(self, fx):
        """D3 must not change a record MSP-B2 already emitted."""
        raw = _out_of_scope_cases(fx["positive_alerts"])[0]
        client = FakeAlertsClient({SUB_A: [raw]})
        rec = _ingestor([SUB_A], alerts=client).ingest_alerts(token="T").records[0]
        assert "app_insights" not in rec
        assert set(rec) == {
            "artifact_id", "change_kind", "source_system", "provider", "stream",
            "surface", "account_scope", "provider_event_id", "event_signature",
            "event", "evidence_pointer", "admission",
        }

    def test_an_in_scope_record_differs_from_an_out_of_scope_one_by_one_key(self, fx):
        in_scope = _by_kind(fx["positive_alerts"], ai.SIGNAL_AVAILABILITY)
        out_scope = _out_of_scope_cases(fx["positive_alerts"])[0]
        client = FakeAlertsClient({SUB_A: [in_scope, out_scope]})
        recs = _ingestor([SUB_A], alerts=client).ingest_alerts(token="T").records
        a, b = recs[0], recs[1]
        assert set(a) - set(b) == {"app_insights"}
        assert set(b) - set(a) == set()

    def test_existing_b2_fixtures_are_unaffected_by_the_scope_gate(self):
        """Regression: the shipped MSP-B2 fixtures must still pass straight through."""
        fixtures_dir = Path(ae.__file__).resolve().parent / "fixtures"
        for name in ("azure_monitor_alerts_sample.json",
                     "azure_activity_log_sample.json",
                     "azure_service_health_sample.json"):
            with open(fixtures_dir / name, encoding="utf-8") as fh:
                data = json.load(fh)
            for raw in data.get("value", []):
                assert ai.is_excluded_telemetry(raw) is False, f"{name}: {raw}"

    def test_an_unrecognised_record_is_not_excluded(self):
        """The gate keeps known telemetry OUT; it is not an allow-list that would
        silently narrow B2 whenever Azure adds a field."""
        assert ai.is_excluded_telemetry({"somethingNew": 1, "id": "x"}) is False
        assert ai.is_excluded_telemetry({}) is False
        assert ai.is_excluded_telemetry(None) is False


# ── D3-AC4 — MSP-B7 admission (dedup with counts, budgets) ──────────────────────


class TestAC4B7Admission:

    def test_a_redelivered_app_insights_alert_is_deduped_with_a_count(self, fx):
        raw = _by_kind(fx["positive_alerts"], ai.SIGNAL_AVAILABILITY)
        client = FakeAlertsClient({SUB_A: [raw, dict(raw)]})
        result = _ingestor([SUB_A], alerts=client).ingest_alerts(token="T")
        # The exact redelivery folds rather than emitting twice.
        assert result.emitted_count == 1
        assert result.subscription_status[SUB_A]["deduped"] == 1

    def test_repeated_firings_fold_into_one_active_signal_with_a_count(self, fx):
        base = _by_kind(fx["positive_alerts"], ai.SIGNAL_DEPENDENCY_FAILURE)
        firings = []
        for i in range(4):
            r = json.loads(json.dumps(base))
            ess = r["data"]["essentials"]
            ess["alertId"] = f"{ess['alertId']}-{i}"
            ess["firedDateTime"] = f"2026-06-03T11:3{i}:00.000Z"
            firings.append(r)
        client = FakeAlertsClient({SUB_A: firings})
        ing = _ingestor([SUB_A], alerts=client)
        ing.ingest_alerts(token="T")
        signals = list(ing.active_signals("org1"))
        assert len(signals) == 1
        assert signals[0].occurrence_count == 4

    def test_a_budget_defers_and_leaves_the_checkpoint_resumable(self, fx):
        alerts = _in_scope_cases(fx["positive_alerts"])
        assert len(alerts) >= 2
        client = FakeAlertsClient({SUB_A: alerts})
        # The connector builds its own OpsEventStream from `budget`, exactly as the
        # runner does via build_ingestor — so this exercises the shipped path.
        ing = _ingestor([SUB_A], alerts=client, budget=1)
        result = ing.ingest_alerts(token="T")
        st = result.subscription_status[SUB_A]
        assert st["status"] == "deferred"
        assert st["reason"] == "run_event_budget_exhausted"
        assert st["checkpoint_advanced"] is False
        assert ae.decode_checkpoints(result.next_checkpoint).get(SUB_A) in (None, "")
        assert result.budget["breached"] is True


# ── incremental behaviour (the task's own requirements) ─────────────────────────


class TestIncrementalBehaviour:

    def test_only_pinned_subscriptions_are_read(self, fx):
        client = FakeAlertsClient({
            SUB_A: _in_scope_cases(fx["positive_alerts"]),
            SUB_B: _in_scope_cases(fx["positive_alerts"]),
        })
        _ingestor([SUB_A], alerts=client).ingest_alerts(token="T")
        assert {c["sub"] for c in client.calls} == {SUB_A}

    def test_a_second_run_re_reads_nothing(self, fx):
        alerts = _in_scope_cases(fx["positive_alerts"])
        client = FakeAlertsClient({SUB_A: alerts})
        ing = _ingestor([SUB_A], alerts=client)
        first = ing.ingest_alerts(token="T")
        assert first.emitted_count == len(alerts)
        second = ing.ingest_alerts(token="T", checkpoint=first.next_checkpoint)
        assert second.emitted_count == 0
        assert second.subscription_status[SUB_A]["status"] == "ok"

    def test_the_checkpoint_advances_to_the_newest_record_seen(self, fx):
        alerts = _in_scope_cases(fx["positive_alerts"])
        client = FakeAlertsClient({SUB_A: alerts})
        result = _ingestor([SUB_A], alerts=client).ingest_alerts(token="T")
        newest = max(
            a["data"]["essentials"]["firedDateTime"] for a in alerts
        )
        assert ae.decode_checkpoints(result.next_checkpoint)[SUB_A] == newest

    def test_a_failing_subscription_degrades_clearly_and_stays_resumable(self, fx):
        alerts = _in_scope_cases(fx["positive_alerts"])
        client = FakeAlertsClient({SUB_A: alerts, SUB_B: alerts})
        client.fail_subs.add(SUB_B)
        prior = ae.encode_checkpoints({SUB_B: "2026-06-01T00:00:00Z"})
        result = _ingestor([SUB_A, SUB_B], alerts=client).ingest_alerts(
            token="T", checkpoint=prior
        )
        # A degraded status carrying a NAMED failure category (the exact category
        # depends on how the provider surfaced the failure; what matters is that one
        # is reported rather than a bare "error"), and the OTHER subscription ran.
        failed = result.subscription_status[SUB_B]
        assert failed["status"] == "error"
        assert failed["category"] in ae._RETRYABLE_CATEGORIES | {
            ae.CATEGORY_AUTHENTICATION, ae.CATEGORY_AUTHORIZATION,
            ae.CATEGORY_NOT_FOUND, ae.CATEGORY_MALFORMED,
            ae.CATEGORY_CLIENT_ERROR, ae.CATEGORY_UNEXPECTED,
        }
        assert "error" in failed and failed["error"]
        assert result.subscription_status[SUB_A]["status"] == "ok"
        assert result.all_ok is False
        assert result.failed_subscriptions == [SUB_B]
        # Resumable: the failed subscription's position is preserved, not advanced.
        assert ae.decode_checkpoints(result.next_checkpoint)[SUB_B] == "2026-06-01T00:00:00Z"

    def test_each_subscription_keeps_its_own_position(self, fx):
        raw = _by_kind(fx["positive_alerts"], ai.SIGNAL_AVAILABILITY)
        other = json.loads(json.dumps(raw))
        other["data"]["essentials"]["alertId"] = "/subscriptions/x/alerts/b1"
        other["data"]["essentials"]["firedDateTime"] = "2026-06-05T00:00:00.000Z"
        client = FakeAlertsClient({SUB_A: [raw], SUB_B: [other]})
        result = _ingestor([SUB_A, SUB_B], alerts=client).ingest_alerts(token="T")
        cps = ae.decode_checkpoints(result.next_checkpoint)
        assert cps[SUB_A] != cps[SUB_B]

    def test_ingest_all_keeps_the_app_insights_scope_across_streams(self, fx):
        alerts = FakeAlertsClient({SUB_A: [
            _by_kind(fx["positive_alerts"], ai.SIGNAL_AVAILABILITY)
        ]})
        health = FakeStreamClient({SUB_A: [
            _by_kind(fx["positive_health_events"], ai.SIGNAL_HEALTH_TRANSITION)
        ]})
        result = _ingestor([SUB_A], alerts=alerts, health=health).ingest_all(token="T")
        kinds = {
            r["app_insights"]["signal_kind"]
            for r in result.records if "app_insights" in r
        }
        assert kinds == {ai.SIGNAL_AVAILABILITY, ai.SIGNAL_HEALTH_TRANSITION}
        # Namespaced per-stream checkpoints are preserved.
        ns = ae.decode_stream_checkpoints(result.next_checkpoint)
        assert ns[ae.STREAM_ALERTS][SUB_A]
        assert ns[ae.STREAM_SERVICE_HEALTH][SUB_A]
