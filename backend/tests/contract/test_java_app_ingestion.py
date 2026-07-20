"""
R17-A3 / T7 — Contract tests for Java Application Ingestion (Operational), Section 5.

These prove the merged R17-A3 implementation satisfies every acceptance criterion
(AC1-AC8) at the contract layer. They run in the ``contract-tests`` CI gate
(``backend/tests/contract``) and exercise the REAL ingestor / signals / config /
corroboration engine OFFLINE against the deterministic fixture
(``discovery/ingest/fixtures/java_app_sample.json``), with in-memory checkpoint
and telemetry seams — so no database and no live credentials are required.

The offline fixture deliberately models two services:
  * ``payments-api`` (service "payments") — degrades across its samples (rising
    error rate, latency degradation, throughput decline, heap+CPU pressure, DOWN
    health, recurring TimeoutException cluster) so operational friction FIRES.
  * ``ledger-svc`` (service "ledger") — stays healthy, so a quiet service yields
    no friction.

AC map (R17-A3 §5):
  AC1  reads health/diagnostics endpoints + logs and produces operational signal
  AC2  ChangeBasedIngestor: incremental since checkpoint; idle -> empty/minimal delta
  AC3  targets from deployment config; credentials via vault, never logged
  AC4  every signal carries a valid OBSERVED EvidencePointer (source_system='java_app')
  AC5  a Java-app signal corroborates another system and contributes to confidence
  AC6  changed artifacts emit ingestion.artifact_changed
  AC7  a discovery finding grounded in Java operational data (non-SaaS DoD)
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
    check_cor09_java_app_operational,
    evaluate_corroboration,
)
from app.provenance import OBSERVED, EvidencePointer
from app.telemetry import REGISTERED_EVENT_TYPES
from discovery.ingest import change_runner, clear_live_connectors
from discovery.ingest.base import ChangeBasedIngestor, Checkpoint
from discovery.ingest import java_app as java_app_mod
from discovery.ingest import java_app_config as java_app_config_mod
from discovery.ingest import java_app_signals as java_app_signals_mod
from discovery.ingest.java_app import JavaAppIngestor, _decode_checkpoint, _encode_checkpoint
from discovery.ingest.java_app_config import (
    JavaAppTarget,
    load_targets,
    resolve_secret,
)
from discovery.ingest.operational_config import OperationalCredentialMissing
from discovery.ingest.java_app_signals import (
    build_evidence_pointer,
    build_java_app_corroboration_payload,
    build_java_app_signal,
)

EVENT = "ingestion.artifact_changed"
ORG = "org-t7"

DETECTOR = "PAYMENTS_PROCESS_FRICTION"
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
    batches = list(JavaAppIngestor().ingest_changes(org_id, since))
    return [r for b in batches for r in b.records], batches


def _drive(org_id=ORG, store=None, **kw):
    """Drive the real ingestor through the shared change runner (emits events)."""
    store = store or _Store()
    res = change_runner.ingest_with_checkpoint(
        JavaAppIngestor(), org_id,
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


def _java_block(fired=True, ts=FRESH_TS, services=("payments",)):
    return {
        "java_app": {
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


# ═════════════════════════════════════════════════════════════════════════════
# AC1 — reads health/diagnostics endpoints + logs, produces operational signal
# ═════════════════════════════════════════════════════════════════════════════
def test_ac1_reads_both_operational_surfaces():
    records, _ = _records()
    kinds = {r["artifact_kind"] for r in records}
    assert kinds == OPERATIONAL_KINDS, "must read BOTH Actuator metrics and logs"


def test_ac1_reads_the_configured_actuator_and_log_sources():
    records, _ = _records()
    metric = next(r for r in records if r["artifact_kind"] == "metrics")
    log = next(r for r in records if r["artifact_kind"] == "log")
    # The surfaces read are exactly the configured target endpoints (not discovered).
    assert metric["actuator_url"] == "https://payments.internal.example/actuator"
    assert log["log_source"] == "https://payments.internal.example/logs"


def test_ac1_produces_operational_signal_from_the_surfaces():
    records, _ = _records()
    signal = build_java_app_signal(records)
    friction = signal["operational_friction"]
    # The degrading service surfaces runtime friction; the healthy one does not.
    assert friction["fired"] is True
    assert "payments" in friction["services"]
    assert "ledger" not in friction["services"]
    # Friction is derived from the four operational signal families (R17-A3 §1).
    pay = signal["services"]["payments"]
    assert pay["metrics"]["max_error_rate"] >= 0.05          # error patterns
    assert pay["metrics"]["latency_degraded"] is True        # latency degradation
    assert pay["metrics"]["throughput_declined"] is True     # throughput decline
    assert pay["metrics"]["heap_pressure"] is True           # resource pressure
    assert any(c["is_cluster"] for c in pay["exception_clusters"])  # clustering


def test_ac1_operational_signal_is_aggregated_over_the_whole_delta():
    # Signal is a WINDOW operation (trend across samples), computed over all
    # collected records by java_app_signals — not per single-sample record.
    records, _ = _records()
    assert records
    signal = build_java_app_signal(records)
    assert signal["operational_friction"]["fired"] is True
    assert "payments" in signal["services"]


# ═════════════════════════════════════════════════════════════════════════════
# AC2 — ChangeBasedIngestor; incremental since checkpoint; idle -> minimal delta
# ═════════════════════════════════════════════════════════════════════════════
def test_ac2_implements_change_based_ingestor_contract():
    ing = JavaAppIngestor()
    assert isinstance(ing, ChangeBasedIngestor)
    assert ing.connector_id == "java_app"
    # Operational artifacts are forward-only: the connector declares it cannot
    # observe deletes rather than silently faking them.
    assert ing.reports_deletes is False


def test_ac2_first_run_reads_all_available_operational_data():
    records, batches = _records(since=None)
    assert len(records) == 12          # 5 metric samples + 7 log entries
    final_cp = batches[-1].next_checkpoint
    assert isinstance(final_cp, str) and final_cp        # opaque, persistable
    cursors = _decode_checkpoint(final_cp)
    # metrics_seq = number of samples consumed AT the newest timestamp (M2).
    assert cursors["payments-api"] == {
        "log_offset": 5, "metrics_ts": FRESH_TS, "metrics_seq": 1,
    }


def test_ac2_second_run_with_checkpoint_is_an_empty_minimal_delta():
    _, batches = _records(since=None)
    final_cp = batches[-1].next_checkpoint
    since = Checkpoint.create("java_app", ORG, final_cp)

    records2, batches2 = _records(since=since)
    assert records2 == []                                   # nothing new
    assert len(batches2) == 1                               # a single minimal delta
    assert batches2[0].is_complete is True
    # The idle delta echoes the incoming position (no regression).
    assert _decode_checkpoint(batches2[0].next_checkpoint) == _decode_checkpoint(final_cp)


def test_ac2_incremental_returns_only_records_newer_than_the_checkpoint():
    # Caught up to payments offset 2 / 08:05; ledger fully caught up.
    cp = _encode_checkpoint({
        "payments-api": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00"},
        "ledger-svc": {"log_offset": 2, "metrics_ts": "2026-06-10T08:05:00+00:00"},
    })
    records, _ = _records(since=Checkpoint.create("java_app", ORG, cp))

    # Only the newer payments artifacts come back; nothing already-seen, nothing ledger.
    assert {r["app_id"] for r in records} == {"payments-api"}
    log_offsets = {r["log_offset"] for r in records if r["artifact_kind"] == "log"}
    assert log_offsets == {3, 4, 5}                          # strictly > offset 2
    metric_ts = {r["observed_ts"] for r in records if r["artifact_kind"] == "metrics"}
    assert metric_ts == {FRESH_TS}                           # strictly > 08:05


# ═════════════════════════════════════════════════════════════════════════════
# AC3 — configured per deployment; credentials via vault, never logged
# ═════════════════════════════════════════════════════════════════════════════
def test_ac3_targets_come_from_deployment_configuration():
    targets = load_targets(ORG)
    assert {t.app_id for t in targets} == {"payments-api", "ledger-svc"}
    # Config carries a vault REFERENCE, never an inline secret.
    assert all(t.credential_ref == "java_app" for t in targets)


def test_ac3_no_network_auto_discovery(monkeypatch):
    # Live mode with nothing configured must yield NO targets — AgentIQ never
    # scans the network to find Java apps (R17-A3 §2).
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.delenv("JAVA_APP_TARGETS", raising=False)
    assert load_targets(ORG) == []


def test_ac3_inline_credential_in_config_is_rejected(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv("JAVA_APP_TARGETS", json.dumps([
        {"app_id": "ok-app", "actuator_url": "https://a/actuator", "log_source": "https://a/logs"},
        {"app_id": "bad-app", "actuator_url": "https://b/actuator", "token": "PASTED_SECRET"},
    ]))
    targets = load_targets(ORG)
    # The clean target loads; the one with an inline secret is dropped.
    assert {t.app_id for t in targets} == {"ok-app"}


def test_ac3_rejected_inline_secret_value_is_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("INGEST_MODE", "live")
    monkeypatch.setenv("JAVA_APP_TARGETS", json.dumps([
        {"app_id": "bad-app", "actuator_url": "https://b/actuator", "token": "SUPER_SECRET_VALUE"},
    ]))
    with caplog.at_level(logging.WARNING):
        targets = load_targets(ORG)
    assert targets == []
    # The offending KEY is named so the deployment can fix it, but the VALUE is not.
    assert "token" in caplog.text
    assert "SUPER_SECRET_VALUE" not in caplog.text


def test_ac3_credential_resolved_from_the_vault_context():
    target = load_targets(ORG)[0]
    # The vault lookup (the per-run credential context) supplies the secret.
    secret = resolve_secret(
        ORG, target,
        connector_lookup=lambda ref: {"token": "VAULT_TOKEN_123"} if ref == "java_app" else None,
    )
    assert secret == "VAULT_TOKEN_123"


def test_ac3_credential_vault_miss_fails_closed_no_env_fallback(monkeypatch):
    # R191-H1 / T1 (F1 fix): a vault miss NEVER falls back to the environment.
    # An env token present is irrelevant — resolution fails closed.
    monkeypatch.setenv("JAVA_APP_TOKEN", "ENV_TOKEN_456")
    target = load_targets(ORG)[0]
    with pytest.raises(OperationalCredentialMissing) as exc:
        resolve_secret(
            ORG, target,
            connector_lookup=lambda ref: None,        # nothing in the vault context
            env={"JAVA_APP_TOKEN": "ENV_TOKEN_456"},  # accepted but never read
        )
    # The exception is actionable: it names the org, the target, and the ref.
    assert exc.value.org_id == ORG
    assert exc.value.app_id == target.app_id
    assert exc.value.credential_ref == target.credential_ref
    assert "ENV_TOKEN_456" not in str(exc.value)


def test_ac3_no_credential_ref_resolves_to_none():
    target = JavaAppTarget(
        app_id="internal", name="internal", actuator_url="https://i/actuator",
        log_source="https://i/logs", credential_ref=None,
    )
    assert resolve_secret(ORG, target, connector_lookup=lambda ref: {"token": "x"}, env={}) is None


def test_ac3_resolved_secret_is_never_logged(caplog):
    target = load_targets(ORG)[0]
    with caplog.at_level(logging.DEBUG):
        secret = resolve_secret(
            ORG, target,
            connector_lookup=lambda ref: {"token": "VAULT_SECRET_XYZ"},
        )
    assert secret == "VAULT_SECRET_XYZ"
    assert "VAULT_SECRET_XYZ" not in caplog.text
    # The credential is also never attached to the (frozen, non-secret) target.
    assert "VAULT_SECRET_XYZ" not in repr(target)


# ═════════════════════════════════════════════════════════════════════════════
# AC4 — every signal carries a valid OBSERVED EvidencePointer
# ═════════════════════════════════════════════════════════════════════════════
def test_ac4_every_record_carries_a_valid_observed_evidence_pointer():
    records, _ = _records()
    assert records
    for r in records:
        ep = r["evidence_pointer"]
        assert isinstance(ep, dict)
        assert ep["source_system"] == "java_app"
        assert ep["origin"] == OBSERVED
        assert ep["source_artifact"]
        assert ep["source_timestamp"]
        # Round-trips through the spine validator (observed needs no extraction job).
        assert EvidencePointer.from_dict(ep).is_valid() is True
        assert ep.get("extraction_job_id") is None


def test_ac4_source_artifact_traces_back_to_the_app_and_surface():
    records, _ = _records()
    for r in records:
        ep = r["evidence_pointer"]
        assert ep["source_artifact"].startswith(r["app_id"] + ":")
        assert r["artifact_kind"] in ep["source_artifact"]


def test_ac4_builder_produces_observed_java_app_pointer():
    ep = build_evidence_pointer("payments-api", "metrics", FRESH_TS, FRESH_TS)
    assert ep["source_system"] == "java_app"
    assert ep["origin"] == OBSERVED
    assert EvidencePointer.from_dict(ep).is_valid() is True


# ═════════════════════════════════════════════════════════════════════════════
# AC5 — a Java-app signal corroborates another system and contributes confidence
# ═════════════════════════════════════════════════════════════════════════════
def test_ac5_cor09_fires_for_fresh_operational_friction():
    rd = _run_data(["salesforce", "java_app"], **_java_block())
    assert check_cor09_java_app_operational(rd, RUN_TS) is True


def test_ac5_cor09_does_not_fire_without_friction():
    rd = _run_data(["salesforce", "java_app"], **_java_block(fired=False))
    assert check_cor09_java_app_operational(rd, RUN_TS) is False


def test_ac5_cor09_respects_the_recency_window():
    rd = _run_data(["salesforce", "java_app"], **_java_block(ts=STALE_TS))
    assert check_cor09_java_app_operational(rd, RUN_TS) is False


def test_ac5_java_signal_elevates_confidence_with_another_system():
    # Real ingested operational signal + a second connected system → elevation.
    records, _ = _records()
    payload = build_java_app_corroboration_payload(records)
    rd = build_corroboration_run_data(
        systems={"salesforce", "java_app"},
        sn_by_detector={}, jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[payload],
    )
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert "COR-09" in result.rule_ids
    assert result.elevated_confidence == "HIGH"
    assert result.confidence_elevated is True


def test_ac5_corroborates_a_servicenow_incident_spike():
    # The story's worked example: a Java error-rate rise corroborating a
    # ServiceNow incident spike for the same service.
    rd = _run_data(
        ["servicenow", "java_app"],
        servicenow={"incidents": [
            {"state": "Open", "sys_created_on": FRESH_TS, "detector_ids": [DETECTOR]}
        ]},
        **_java_block(),
    )
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert {"COR-01", "COR-09"} <= set(result.rule_ids)
    assert result.elevated_confidence == "HIGH"


def test_ac5_elevation_never_downgrades_a_scorer_baseline():
    rd = _run_data(["salesforce", "java_app"], **_java_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"
    assert apply_corroboration_confidence("HIGH", result) == "HIGH"


def test_ac5_single_source_java_app_does_not_self_corroborate():
    rd = _run_data(["java_app"], **_java_block())
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert result.elevated_confidence == "MEDIUM"
    assert result.confidence_elevated is False


# ═════════════════════════════════════════════════════════════════════════════
# AC6 — changed artifacts emit ingestion.artifact_changed
# ═════════════════════════════════════════════════════════════════════════════
def test_ac6_event_type_is_registered():
    assert EVENT in REGISTERED_EVENT_TYPES


def test_ac6_one_event_per_changed_artifact(monkeypatch):
    events = _capture_events(monkeypatch)
    res, _ = _drive()
    emitted = [p for (e, p) in events if e == EVENT]
    assert len(emitted) == res.records == 12


def test_ac6_every_event_carries_the_required_fields(monkeypatch):
    events = _capture_events(monkeypatch)
    _drive(org_id="org-fields")
    emitted = [p for (e, p) in events if e == EVENT]
    assert emitted
    required = {"org_id", "connector_id", "artifact_id", "change_kind", "observed_at"}
    for e in emitted:
        assert required <= set(e.keys())
        assert e["org_id"] == "org-fields"
        assert e["connector_id"] == "java_app"
        assert e["artifact_id"]
        assert e["change_kind"] in ("created", "updated", "deleted")
        datetime.fromisoformat(e["observed_at"])   # valid UTC ISO timestamp


def test_ac6_idle_run_emits_no_events(monkeypatch):
    events = _capture_events(monkeypatch)
    _, store = _drive()                 # first run emits all
    events.clear()
    _drive(store=store)                 # nothing changed
    assert [p for (e, p) in events if e == EVENT] == []


def test_ac6_emission_failure_never_breaks_ingestion(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.telemetry.record_event", _boom)
    res, _ = _drive()
    assert res.ok is True
    assert res.checkpoint_advanced is True
    assert res.records == 12


# ═════════════════════════════════════════════════════════════════════════════
# AC7 — a discovery finding grounded in Java operational data (non-SaaS DoD)
# ═════════════════════════════════════════════════════════════════════════════
def test_ac7_discovery_finding_grounded_in_java_operational_data(monkeypatch):
    monkeypatch.setattr("app.telemetry.record_event", lambda *a, **k: None)

    # 1. Ingest the operational surface (Actuator samples + logs) through the
    #    shared change runner — exactly as a discovery run does.
    collected: list = []
    store = _Store()
    change_runner.ingest_with_checkpoint(
        JavaAppIngestor(), ORG,
        process_batch=lambda b: collected.extend(b.records),
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    assert collected, "expected operational records from the Java application"

    # 2. Build the corroboration block from the ingested operational signal.
    payload = build_java_app_corroboration_payload(collected)
    assert payload["java_app"]["operational_friction"]["fired"] is True

    # 3. A run with the Java app connected alongside another system yields a
    #    finding whose confidence is elevated by the Java signal — discovery
    #    across a non-SaaS enterprise source.
    rd = build_corroboration_run_data(
        systems={"salesforce", "java_app"},
        sn_by_detector={}, jira_by_detector={},
        run_timestamp_iso=RUN_TS.isoformat(),
        source_payloads=[payload],
    )
    result = evaluate_corroboration(DETECTOR, "service_cloud", rd, RUN_TS, ORG)
    assert "COR-09" in result.rule_ids
    assert result.elevated_confidence == "HIGH"
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"


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


def test_ac8_no_external_apm_or_code_analysis_dependency():
    # Phase one reads what the running app reports about itself — not an external
    # APM/observability platform, and not the application's source via a parser.
    forbidden = (
        "datadog", "newrelic", "new_relic", "dynatrace", "appdynamics",
        "elastic_apm", "elasticapm", "opentelemetry",      # external APM SDKs
        "javalang", "tree_sitter", "tree-sitter",          # source/AST parsers
    )
    sources = "\n".join(
        inspect.getsource(m) for m in (
            java_app_mod, java_app_signals_mod, java_app_config_mod,
        )
    ).lower()
    leaked = [tok for tok in forbidden if tok in sources]
    assert not leaked, f"phase-one scope violation — references to: {leaked}"
