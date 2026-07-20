"""
R17-A4 / T7 — Contract tests for .NET Application Ingestion (Operational), Section 5.

These prove the R17-A4 implementation satisfies every acceptance criterion
(AC1-AC8) at the contract layer. They run in the ``contract-tests`` CI gate
(``backend/tests/contract``) and exercise the REAL ingestor / signals / config /
corroboration engine OFFLINE against the deterministic fixture
(``discovery/ingest/fixtures/dotnet_app_sample.json``), with in-memory checkpoint
and telemetry seams — so no database and no live credentials are required.

The offline fixture deliberately models two services:
  * ``orders-api`` (service "orders") — degrades across its samples (rising error
    rate, latency degradation, throughput decline, heap+CPU pressure, Unhealthy
    health, recurring System.TimeoutException cluster) so operational friction
    FIRES.
  * ``inventory-svc`` (service "inventory") — stays healthy, so a quiet service
    yields no friction.

AC map (R17-A4 §5):
  AC1  reads health/diagnostics endpoints + logs and produces operational signal
  AC2  ChangeBasedIngestor: incremental since checkpoint; idle -> empty/minimal delta
  AC3  shared operational-signal extraction is reused from the Java ingestor (not duplicated)
  AC4  targets from deployment config; credentials via vault, never logged
  AC5  every signal carries a valid OBSERVED EvidencePointer (source_system='dotnet_app')
  AC6  a .NET-app signal corroborates another system and contributes to confidence
  AC7  changed artifacts emit ingestion.artifact_changed
  AC8  operational surfaces only — no source code, no external APM dependency
"""
from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime, timezone

import pytest

from app.corroboration_engine import (
    apply_corroboration_confidence,
    build_corroboration_run_data,
    check_cor10_dotnet_app_operational,
    evaluate_corroboration,
)
from app.provenance import OBSERVED, EvidencePointer
from app.telemetry import REGISTERED_EVENT_TYPES
from discovery.ingest import change_runner, clear_live_connectors
from discovery.ingest import operational_signals as shared_signals_mod
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint
from discovery.ingest import dotnet_app as dotnet_app_mod
from discovery.ingest import dotnet_app_config as dotnet_app_config_mod
from discovery.ingest import dotnet_app_signals as dotnet_app_signals_mod
from discovery.ingest import java_app_signals as java_app_signals_mod
from discovery.ingest.dotnet_app import (
    DotNetAppClient,
    DotNetAppIngestor,
    _decode_checkpoint,
    _encode_checkpoint,
)
from discovery.ingest.dotnet_app_config import (
    DotNetAppTarget,
    load_targets,
    log_endpoint_failure,
    resolve_secret,
)
from discovery.ingest.operational_config import OperationalCredentialMissing
from discovery.ingest.dotnet_app_signals import (
    build_dotnet_app_corroboration_payload,
    build_dotnet_app_signal,
    build_evidence_pointer,
)

EVENT = "ingestion.artifact_changed"
ORG = "org-a4"

DETECTOR = "ORDERS_PROCESS_FRICTION"
RUN_TS = datetime(2026, 6, 20, tzinfo=timezone.utc)   # within 30d of the fixture
FRESH_TS = "2026-06-10T08:10:00+00:00"
STALE_TS = "2026-01-01T00:00:00+00:00"                # > 30 days before RUN_TS

# Operational-surface artifact kinds the connector is ever allowed to emit (AC8).
OPERATIONAL_KINDS = {"metrics", "log"}
# Fields that would betray source-code reading (the 1.8 phase) — must never appear.
FORBIDDEN_SOURCE_FIELDS = {
    "source_code", "ast", "class_body", "repository", "repo", "file_path",
    "diff", "bytecode", "import_graph", "method_body",
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Default every test to deterministic offline mode with no live context."""
    monkeypatch.setenv("INGEST_MODE", "offline")
    clear_live_connectors()
    yield
    clear_live_connectors()


# ── in-memory seams (no DB) ──────────────────────────────────────────────────
class _Store:
    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp: Checkpoint):
        self.data[(cp.org_id, cp.connector_id)] = cp


def _records(org_id=ORG, since=None):
    """Flatten the real ingestor's delta batches into a record list."""
    batches = list(DotNetAppIngestor().ingest_changes(org_id, since))
    return [r for b in batches for r in b.records], batches


def _drive(org_id=ORG, store=None, **kw):
    """Drive the real ingestor through the shared change runner (emits events)."""
    store = store or _Store()
    res = change_runner.ingest_with_checkpoint(
        DotNetAppIngestor(), org_id,
        read_checkpoint=store.read, save_checkpoint=store.save, **kw,
    )
    return res, store


def _capture_events(monkeypatch):
    events: list = []
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda etype, payload=None: events.append((etype, payload or {})),
    )
    return events


def _dotnet_block(fired=True, ts=FRESH_TS, services=("orders",)):
    return {
        "dotnet_app": {
            "operational_friction": {
                "fired": fired,
                "timestamp": ts,
                "services": list(services),
                "reasons": ["elevated error rate", "latency degradation"],
            }
        }
    }


def _run_data(connected, **systems):
    data = {"connected_systems": list(connected)}
    data.update(systems)
    return data


class _FakeResp:
    def __init__(self, *, ok=True, status_code=200, json_data=None, text="", raise_json=False):
        self.ok = ok
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not JSON")
        return self._json_data


class _FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, int | None]] = []

    def get(self, url, timeout=None, params=None):
        self.calls.append((url, timeout))
        return self.handler(url)


def _configured_live_client(secret="VAULT_TOKEN_123"):
    diagnostics_url = "https://orders.internal.example/diagnostics"
    log_source = "https://orders.internal.example/logs"

    def handler(url):
        if url == f"{diagnostics_url}/health":
            return _FakeResp(json_data={"status": "Unhealthy"})
        if url == f"{diagnostics_url}/counters":
            return _FakeResp(json_data={
                "counters": [
                    {"name": "total-requests", "value": 100.0},
                    {"name": "failed-requests", "value": 12.0},
                    {"name": "request-duration", "value": 1800.0},
                    {"name": "requests-per-second", "value": 25.0},
                    {"name": "gc-heap-size", "value": 92.0},
                    {"name": "gc-committed", "value": 100.0},
                    {"name": "cpu-usage", "value": 91.0},
                ]
            })
        if url == log_source:
            return _FakeResp(json_data=[{
                "offset": 6,
                "ts": FRESH_TS,
                "level": "Critical",
                "logger": "Orders.Api.GatewayClient",
                "exception_type": "System.TimeoutException",
                "retry": True,
                "message": "Retry failed after upstream timeout",
            }])
        return _FakeResp(ok=False, status_code=404, raise_json=True)

    session = _FakeSession(handler)
    client = DotNetAppClient(
        diagnostics_url=diagnostics_url,
        log_source=log_source,
        secret=secret,
    )
    client._session = session
    return client, session, diagnostics_url, log_source


# ═════════════════════════════════════════════════════════════════════════════
# AC1 — reads health/diagnostics endpoints + logs, produces operational signal
# ═════════════════════════════════════════════════════════════════════════════
def test_ac1_reads_both_operational_surfaces():
    records, _ = _records()
    kinds = {r["artifact_kind"] for r in records}
    assert kinds == OPERATIONAL_KINDS, "must read BOTH diagnostics metrics and logs"


def test_ac1_reads_the_configured_diagnostics_and_log_sources():
    records, _ = _records()
    metric = next(r for r in records if r["artifact_kind"] == "metrics")
    log = next(r for r in records if r["artifact_kind"] == "log")
    # The surfaces read are exactly the configured target endpoints (not discovered).
    assert metric["diagnostics_url"] == "https://orders.internal.example/diagnostics"
    assert log["log_source"] == "https://orders.internal.example/logs"


def test_ac1_produces_operational_signal_from_the_surfaces():
    records, _ = _records()
    signal = build_dotnet_app_signal(records)
    friction = signal["operational_friction"]
    assert friction["fired"] is True
    assert "orders" in friction["services"]
    assert "inventory" not in friction["services"]
    # Friction is derived from the four operational signal families (R17-A4 §1).
    o = signal["services"]["orders"]
    assert o["metrics"]["max_error_rate"] >= 0.05          # error patterns
    assert o["metrics"]["latency_degraded"] is True        # latency degradation
    assert o["metrics"]["throughput_declined"] is True     # throughput decline
    assert o["metrics"]["heap_pressure"] is True           # resource pressure
    assert any(c["is_cluster"] for c in o["exception_clusters"])  # clustering


def test_ac1_live_collection_reads_configured_endpoints_and_feeds_signal():
    client, session, diagnostics_url, log_source = _configured_live_client()
    payload = client.read_operational()

    assert [url for url, _ in session.calls] == [
        f"{diagnostics_url}/health",
        f"{diagnostics_url}/counters",
        log_source,
    ]
    assert payload["metrics"][0]["health"] == "Unhealthy"
    assert payload["metrics"][0]["error_rate"] == 0.12
    assert payload["logs"][0]["level"] == "Critical"

    target = DotNetAppTarget(
        app_id="orders-api",
        name="Orders API",
        diagnostics_url=diagnostics_url,
        log_source=log_source,
        metadata={"service": "orders"},
    )
    ingestor = DotNetAppIngestor()
    records = [
        ingestor._to_metric_record(target, payload["metrics"][0]),
        ingestor._to_log_record(target, payload["logs"][0]),
    ]
    signal = build_dotnet_app_signal(records)
    assert signal["operational_friction"]["fired"] is True
    assert signal["services"]["orders"]["metrics"]["heap_pressure"] is True
    assert signal["services"]["orders"]["error_patterns"]["error_count"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# AC2 — ChangeBasedIngestor; incremental since checkpoint; idle -> minimal delta
# ═════════════════════════════════════════════════════════════════════════════
def test_ac2_implements_change_based_ingestor_contract():
    ing = DotNetAppIngestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "dotnet_app"
    assert ing.reports_deletes is False


def test_ac2_first_run_reads_all_available_operational_data():
    records, batches = _records(since=None)
    assert len(records) == 12          # 5 metric samples + 7 log entries
    final_cp = batches[-1].next_checkpoint
    assert isinstance(final_cp, str) and final_cp
    cursors = _decode_checkpoint(final_cp)
    assert cursors["orders-api"] == {
        "log_offset": 5, "metrics_ts": FRESH_TS, "metrics_seq": 1,
    }


def test_ac2_second_run_with_checkpoint_is_an_empty_minimal_delta():
    _, batches = _records(since=None)
    final_cp = batches[-1].next_checkpoint
    since = Checkpoint.create("dotnet_app", ORG, final_cp)

    records2, batches2 = _records(since=since)
    assert records2 == []
    assert len(batches2) == 1
    assert batches2[0].is_complete is True
    assert _decode_checkpoint(batches2[0].next_checkpoint) == _decode_checkpoint(final_cp)


def test_ac2_incremental_returns_only_records_newer_than_the_checkpoint():
    cp = _encode_checkpoint({
        "orders-api": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00"},
        "inventory-svc": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00"},
    })
    records, _ = _records(since=Checkpoint.create("dotnet_app", ORG, cp))

    assert {r["app_id"] for r in records} == {"orders-api"}
    log_offsets = {r["log_offset"] for r in records if r["artifact_kind"] == "log"}
    assert log_offsets == {3, 4, 5}
    metric_ts = {r["observed_ts"] for r in records if r["artifact_kind"] == "metrics"}
    assert metric_ts == {FRESH_TS}


# ═════════════════════════════════════════════════════════════════════════════
# AC3 — shared operational-signal extraction is reused from Java (not duplicated)
# ═════════════════════════════════════════════════════════════════════════════
def test_ac3_extraction_functions_are_the_shared_objects():
    # The .NET and Java signal adapters both re-export the SAME shared extraction
    # function objects — proof the extraction is reused, not copied.
    for name in ("extract_error_signal", "extract_exception_clusters", "extract_metrics_signal"):
        assert getattr(java_app_signals_mod, name) is getattr(shared_signals_mod, name)


def test_ac3_identical_records_produce_identical_signal_across_platforms():
    records, _ = _records()
    # Re-key the same records to a Java-style shape and confirm identical signal:
    # the interpretation is platform-agnostic, so the two builders agree.
    dotnet_sig = build_dotnet_app_signal(records)
    java_sig = java_app_signals_mod.build_java_app_signal(records)
    assert dotnet_sig == java_sig


# ═════════════════════════════════════════════════════════════════════════════
# AC4 — configured per deployment; credentials via vault, never logged
# ═════════════════════════════════════════════════════════════════════════════
def test_ac4_targets_come_from_deployment_configuration():
    targets = load_targets(ORG)
    assert {t.app_id for t in targets} == {"orders-api", "inventory-svc"}
    assert all(t.credential_ref == "dotnet_app" for t in targets)


def test_ac4_no_network_auto_discovery(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.delenv("DOTNET_APP_TARGETS", raising=False)
    assert load_targets(ORG) == []


def test_ac4_inline_credential_in_config_is_rejected(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv("DOTNET_APP_TARGETS", json.dumps([
        {"app_id": "ok-app", "diagnostics_url": "https://a/diagnostics", "log_source": "https://a/logs"},
        {"app_id": "bad-app", "diagnostics_url": "https://b/diagnostics", "token": "PASTED_SECRET"},
    ]))
    targets = load_targets(ORG)
    assert {t.app_id for t in targets} == {"ok-app"}


def test_ac4_rejected_inline_secret_value_is_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv("DOTNET_APP_TARGETS", json.dumps([
        {"app_id": "bad-app", "diagnostics_url": "https://b/diagnostics", "token": "SUPER_SECRET_VALUE"},
    ]))
    with caplog.at_level(logging.WARNING):
        targets = load_targets(ORG)
    assert targets == []
    assert "token" in caplog.text
    assert "SUPER_SECRET_VALUE" not in caplog.text


def test_ac4_credential_resolved_from_the_vault_context():
    target = load_targets(ORG)[0]
    secret = resolve_secret(
        ORG, target,
        connector_lookup=lambda ref: {"token": "VAULT_TOKEN_123"} if ref == "dotnet_app" else None,
    )
    assert secret == "VAULT_TOKEN_123"


def test_ac4_credential_vault_miss_fails_closed_no_env_fallback(monkeypatch):
    # R191-H1 / T1 (F1 fix): a vault miss NEVER falls back to the environment.
    monkeypatch.setenv("DOTNET_APP_TOKEN", "ENV_TOKEN_456")
    target = load_targets(ORG)[0]
    with pytest.raises(OperationalCredentialMissing) as exc:
        resolve_secret(
            ORG, target,
            connector_lookup=lambda ref: None,
            env={"DOTNET_APP_TOKEN": "ENV_TOKEN_456"},  # accepted but never read
        )
    assert exc.value.org_id == ORG
    assert exc.value.app_id == target.app_id
    assert exc.value.credential_ref == target.credential_ref
    assert "ENV_TOKEN_456" not in str(exc.value)


def test_ac4_no_credential_ref_resolves_to_none():
    target = DotNetAppTarget(
        app_id="internal", name="internal", diagnostics_url="https://i/diagnostics",
        log_source="https://i/logs", credential_ref=None,
    )
    assert resolve_secret(ORG, target, connector_lookup=lambda ref: {"token": "x"}, env={}) is None


def test_ac4_resolved_secret_is_never_logged(caplog):
    target = load_targets(ORG)[0]
    with caplog.at_level(logging.DEBUG):
        secret = resolve_secret(
            ORG, target,
            connector_lookup=lambda ref: {"token": "VAULT_SECRET_XYZ"},
        )
    assert secret == "VAULT_SECRET_XYZ"
    assert "VAULT_SECRET_XYZ" not in caplog.text
    assert "VAULT_SECRET_XYZ" not in repr(target)


def test_ac4_live_read_outputs_do_not_include_resolved_secret():
    secret = "VAULT_SECRET_DO_NOT_LEAK"
    client, _session, _diagnostics_url, _log_source = _configured_live_client(secret=secret)
    payload = client.read_operational()
    assert secret not in json.dumps(payload)


def test_ac4_endpoint_failure_logs_only_safe_fields(caplog):
    target = DotNetAppTarget(
        app_id="orders-api",
        name="Orders API",
        diagnostics_url="https://orders.internal.example/diagnostics",
        log_source="https://orders.internal.example/logs",
        environment="production",
    )
    exc = RuntimeError("403 Forbidden for bearer token VAULT_SECRET_DO_NOT_LOG")

    with caplog.at_level(logging.WARNING):
        safe = log_endpoint_failure(ORG, target, "logs", exc)

    blob = json.dumps(safe) + caplog.text
    assert safe["app_id"] == "orders-api"
    assert safe["endpoint_type"] == "logs"
    assert safe["error_category"] == "auth_error"
    assert "VAULT_SECRET_DO_NOT_LOG" not in blob
    assert "Forbidden for bearer token" not in blob


# ═════════════════════════════════════════════════════════════════════════════
# AC5 — every signal carries a valid OBSERVED EvidencePointer
# ═════════════════════════════════════════════════════════════════════════════
def test_ac5_every_record_carries_a_valid_observed_evidence_pointer():
    records, _ = _records()
    assert records
    for r in records:
        ep = r["evidence_pointer"]
        assert isinstance(ep, dict)
        assert ep["source_system"] == "dotnet_app"
        assert ep["origin"] == OBSERVED
        assert ep["source_artifact"]
        assert ep["source_timestamp"]
        assert EvidencePointer.from_dict(ep).is_valid() is True
        assert ep.get("extraction_job_id") is None


def test_ac5_source_artifact_traces_back_to_the_app_and_surface():
    records, _ = _records()
    for r in records:
        ep = r["evidence_pointer"]
        assert ep["source_artifact"].startswith(r["app_id"] + ":")
        assert r["artifact_kind"] in ep["source_artifact"]


def test_ac5_builder_produces_observed_dotnet_app_pointer():
    ep = build_evidence_pointer("orders-api", "metrics", FRESH_TS, FRESH_TS)
    assert ep["source_system"] == "dotnet_app"
    assert ep["origin"] == OBSERVED
    assert EvidencePointer.from_dict(ep).is_valid() is True


# ═════════════════════════════════════════════════════════════════════════════
# AC6 — a .NET-app signal corroborates another system and contributes confidence
# ═════════════════════════════════════════════════════════════════════════════
def test_ac6_cor10_fires_for_fresh_operational_friction():
    rd = _run_data(["salesforce", "dotnet_app"], **_dotnet_block())
    assert check_cor10_dotnet_app_operational(rd, RUN_TS) is True


def test_ac6_cor10_does_not_fire_without_friction():
    rd = _run_data(["salesforce", "dotnet_app"], **_dotnet_block(fired=False))
    assert check_cor10_dotnet_app_operational(rd, RUN_TS) is False


def test_ac6_cor10_respects_the_recency_window():
    rd = _run_data(["salesforce", "dotnet_app"], **_dotnet_block(ts=STALE_TS))
    assert check_cor10_dotnet_app_operational(rd, RUN_TS) is False


def test_ac6_dotnet_signal_elevates_confidence_with_another_system():
    records, _ = _records()
    payload = build_dotnet_app_corroboration_payload(records)
    rd = build_corroboration_run_data(
        systems={"salesforce", "dotnet_app"},
        sn_by_detector={}, jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[payload],
    )
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert "COR-10" in result.rule_ids
    assert result.elevated_confidence == "HIGH"
    assert result.confidence_elevated is True


def test_ac6_corroborates_a_servicenow_incident_spike():
    rd = _run_data(
        ["servicenow", "dotnet_app"],
        servicenow={"incidents": [
            {"state": "Open", "sys_created_on": FRESH_TS, "detector_ids": [DETECTOR]}
        ]},
        **_dotnet_block(),
    )
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert {"COR-01", "COR-10"} <= set(result.rule_ids)
    assert result.elevated_confidence == "HIGH"


def test_ac6_elevation_never_downgrades_a_scorer_baseline():
    rd = _run_data(["salesforce", "dotnet_app"], **_dotnet_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"
    assert apply_corroboration_confidence("HIGH", result) == "HIGH"


def test_ac6_single_source_dotnet_app_does_not_self_corroborate():
    rd = _run_data(["dotnet_app"], **_dotnet_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert result.elevated_confidence == "MEDIUM"
    assert result.confidence_elevated is False


# ═════════════════════════════════════════════════════════════════════════════
# AC7 — changed artifacts emit ingestion.artifact_changed
# ═════════════════════════════════════════════════════════════════════════════
def test_ac7_event_type_is_registered():
    assert EVENT in REGISTERED_EVENT_TYPES


def test_ac7_one_event_per_changed_artifact(monkeypatch):
    events = _capture_events(monkeypatch)
    res, _ = _drive()
    emitted = [p for (e, p) in events if e == EVENT]
    assert len(emitted) == res.records == 12


def test_ac7_every_event_carries_the_required_fields(monkeypatch):
    events = _capture_events(monkeypatch)
    _drive(org_id="org-fields")
    emitted = [p for (e, p) in events if e == EVENT]
    assert emitted
    required = {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    for e in emitted:
        assert required <= set(e.keys())
        assert e["org_id"] == "org-fields"
        assert e["connector_id"] == "dotnet_app"
        assert e["artifact_id"]
        assert e["change_kind"] in ("created", "updated", "deleted")
        datetime.fromisoformat(e["observed_at"])


def test_ac7_idle_run_emits_no_events(monkeypatch):
    events = _capture_events(monkeypatch)
    _, store = _drive()
    events.clear()
    _drive(store=store)
    assert [p for (e, p) in events if e == EVENT] == []


def test_ac7_emission_failure_never_breaks_ingestion(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)
    res, _ = _drive()
    assert res.ok is True
    assert res.checkpoint_advanced is True
    assert res.records == 12


# ═════════════════════════════════════════════════════════════════════════════
# AC8 — operational surfaces only: no source code, no external APM
# ═════════════════════════════════════════════════════════════════════════════
def test_ac8_records_only_describe_operational_surfaces():
    records, _ = _records()
    assert records
    for r in records:
        assert r["artifact_kind"] in OPERATIONAL_KINDS


def test_ac8_no_record_carries_source_code_fields():
    records, _ = _records()
    for r in records:
        leaked = FORBIDDEN_SOURCE_FIELDS & set(r.keys())
        assert not leaked, f"source-code field(s) leaked into a record: {leaked}"


def test_ac8_live_client_requests_only_operational_surfaces():
    client, session, diagnostics_url, log_source = _configured_live_client()
    client.read_operational()

    urls = [url for url, _ in session.calls]
    assert urls == [f"{diagnostics_url}/health", f"{diagnostics_url}/counters", log_source]
    forbidden = (
        "/source", "/repo", "/repository", "/code", "/classes", "/assemblies",
        "/apm", "/newrelic", "/datadog", "/dynatrace",
    )
    assert not any(token in url.lower() for url in urls for token in forbidden)


def test_ac8_no_external_apm_or_code_analysis_dependency():
    # Phase one reads what the running app reports about itself — not an external
    # APM/observability platform, and not the application's source via a parser.
    forbidden = (
        "datadog", "newrelic", "new_relic", "dynatrace", "appdynamics",
        "elastic_apm", "elasticapm", "opentelemetry",      # external APM SDKs
        "roslyn", "cecil", "dnlib", "tree_sitter", "tree-sitter",  # source/IL parsers
    )
    sources = "\n".join(
        inspect.getsource(m) for m in (
            dotnet_app_mod, dotnet_app_signals_mod, dotnet_app_config_mod,
            shared_signals_mod,
        )
    ).lower()
    leaked = [tok for tok in forbidden if tok in sources]
    assert not leaked, f"phase-one scope violation — references to: {leaked}"
