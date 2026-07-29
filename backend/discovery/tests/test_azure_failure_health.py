"""
MSP-B2 T6 (AT-653) — per-subscription failure loudness + retry/backoff.

Offline / DB-free: clients, token, and sleep are injected. Verifies failure
classification, bounded backoff retry for TRANSIENT failures only, checkpoint
preservation on failure (no silent thinning), failure isolation, and loud run-health
reporting via the `ingestion.subscription_health` telemetry event.

Acceptance criteria:
  T6-AC1 — Subscription-specific failures are reported.
  T6-AC2 — Authentication failures are visible in run health.
  T6-AC3 — Retry/backoff is implemented without silently dropping events.
  T6-AC4 — Healthy subscriptions continue processing independently.
"""
from __future__ import annotations

import pytest

from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg


SUB_A = "aaaaaaaa-0000-0000-0000-000000000001"
SUB_B = "bbbbbbbb-0000-0000-0000-000000000002"


# ── exception fakes (carry the shape classify_failure inspects) ─────────────────


class HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class ReadTimeout(Exception):        # type name contains "timeout"
    pass


class ConnectError(Exception):       # type name contains "connect"
    pass


def _alert(sub, aid, fired):
    return {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {"essentials": {
            "alertId": f"/subscriptions/{sub}/providers/Microsoft.AlertsManagement/alerts/{aid}",
            "alertRule": "r", "severity": "Sev2", "signalType": "Metric",
            "monitorCondition": "Fired",
            "alertTargetIDs": [f"/subscriptions/{sub}/resourceGroups/rg/providers/Microsoft.Web/sites/app"],
            "firedDateTime": fired, "description": "seeded",
        }},
    }


class ScriptedAlertsClient:
    """Alerts client whose per-subscription behaviour is scripted.

    by_sub[sub] is either a list (returned) or a callable() invoked each fetch
    (may raise or return a list) — lets a test fail N times then succeed.
    """

    def __init__(self, by_sub):
        self.by_sub = dict(by_sub)
        self.calls = {}

    def fetch_alerts(self, *, token, subscription_id, environment, since_iso):
        self.calls[subscription_id] = self.calls.get(subscription_id, 0) + 1
        behaviour = self.by_sub.get(subscription_id, [])
        if callable(behaviour):
            return behaviour()
        return list(behaviour)


def _flaky(exc_factory, fail_times, then):
    """A callable that raises for the first ``fail_times`` calls, then returns ``then``."""
    state = {"n": 0}

    def _call():
        if state["n"] < fail_times:
            state["n"] += 1
            raise exc_factory()
        return list(then)

    return _call


def _ingestor(subs, client, *, sleep_fn=None, retry=None, token_fn=None, vault_reader=None):
    return ae.AzureEventIngestor(
        "org1", cfg.AzureEventConfig(
            environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
            mode=cfg.MODE_LIGHTHOUSE, subscriptions=list(subs),
        ),
        alerts_client=client,
        sleep_fn=sleep_fn or (lambda s: None),
        retry_policy=retry,
        token_fn=token_fn,
        vault_reader=vault_reader,
    )


# ── failure classification table ─────────────────────────────────────────────────


class TestClassification:

    @pytest.mark.parametrize("exc,expected,retryable", [
        (HttpError(429), ae.CATEGORY_THROTTLED, True),
        (HttpError(500), ae.CATEGORY_SERVER_ERROR, True),
        (HttpError(503), ae.CATEGORY_SERVER_ERROR, True),
        (ReadTimeout(), ae.CATEGORY_TIMEOUT, True),
        (ConnectError(), ae.CATEGORY_NETWORK, True),
        (HttpError(401), ae.CATEGORY_AUTHENTICATION, False),
        (HttpError(403), ae.CATEGORY_AUTHORIZATION, False),
        (HttpError(404), ae.CATEGORY_NOT_FOUND, False),
        (HttpError(400), ae.CATEGORY_CLIENT_ERROR, False),
        (ValueError("bad json"), ae.CATEGORY_MALFORMED, False),
        (ae.AzureAuthError("no sp"), ae.CATEGORY_AUTHENTICATION, False),
        (RuntimeError("weird"), ae.CATEGORY_UNEXPECTED, False),
    ])
    def test_classify_and_retryability(self, exc, expected, retryable):
        assert ae.classify_failure(exc) == expected
        assert ae.is_retryable(expected) is retryable


# ── T6-AC3 — retry/backoff for transient failures, no dropped events ─────────────


class TestRetryBackoff:

    def test_transient_then_success_no_events_lost(self):
        slept = []
        client = ScriptedAlertsClient({
            SUB_A: _flaky(lambda: HttpError(429), fail_times=2,
                          then=[_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")]),
        })
        result = _ingestor([SUB_A], client, sleep_fn=slept.append,
                           retry=ae.RetryPolicy(max_retries=3, base_seconds=0.5)).ingest_alerts(token="T")
        assert result.emitted_count == 1                      # event NOT dropped (AC3)
        assert result.subscription_status[SUB_A]["status"] == "ok"
        assert result.subscription_status[SUB_A]["attempts"] == 3   # 2 failures + success
        assert slept == [0.5, 1.0]                            # exponential backoff between retries

    def test_retry_exhaustion_reports_error_and_preserves_checkpoint(self):
        slept = []
        client = ScriptedAlertsClient({SUB_A: lambda: (_ for _ in ()).throw(HttpError(503))})
        result = _ingestor([SUB_A], client, sleep_fn=slept.append,
                           retry=ae.RetryPolicy(max_retries=2, base_seconds=1.0, max_seconds=8.0)).ingest_alerts(token="T")
        st = result.subscription_status[SUB_A]
        assert st["status"] == "error"
        assert st["category"] == ae.CATEGORY_SERVER_ERROR
        assert st["attempts"] == 3                            # 1 + 2 retries
        assert st["retryable"] is True and st["recoverable"] is True
        assert len(slept) == 2                                # backed off before each retry
        assert ae.decode_checkpoints(result.next_checkpoint) == {}   # nothing advanced (no silent thinning)

    def test_non_transient_is_not_retried(self):
        slept = []
        client = ScriptedAlertsClient({SUB_A: lambda: (_ for _ in ()).throw(HttpError(403))})
        result = _ingestor([SUB_A], client, sleep_fn=slept.append).ingest_alerts(token="T")
        st = result.subscription_status[SUB_A]
        assert st["status"] == "error" and st["category"] == ae.CATEGORY_AUTHORIZATION
        assert st["attempts"] == 1                            # no retry
        assert st["retryable"] is False and st["recoverable"] is False
        assert slept == []

    def test_backoff_is_bounded_by_max(self):
        p = ae.RetryPolicy(max_retries=10, base_seconds=1.0, max_seconds=4.0)
        assert p.backoff_seconds(1) == 1.0
        assert p.backoff_seconds(2) == 2.0
        assert p.backoff_seconds(3) == 4.0
        assert p.backoff_seconds(9) == 4.0                    # capped


# ── T6-AC1 — subscription-specific failures reported into run health ─────────────


class TestRunHealthReporting:

    def _capture(self, monkeypatch):
        events = []
        import app.telemetry as telemetry
        monkeypatch.setattr(telemetry, "record_event", lambda et, payload: events.append((et, payload)))
        return events

    def test_failure_emits_subscription_health_event(self, monkeypatch):
        events = self._capture(monkeypatch)
        client = ScriptedAlertsClient({SUB_A: lambda: (_ for _ in ()).throw(HttpError(429))})
        _ingestor([SUB_A], client, retry=ae.RetryPolicy(max_retries=1)).ingest_alerts(token="T")
        health = [p for (et, p) in events if et == "ingestion.subscription_health"]
        assert len(health) == 1
        p = health[0]
        assert p["connector_id"] == "azure_events"
        assert p["account_scope"] == SUB_A
        assert p["stream"] == "alerts"
        assert p["source_system"] == "azure_monitor"
        assert p["category"] == ae.CATEGORY_THROTTLED
        assert p["status"] == "error" and p["retryable"] is True
        assert "429" in p["error_summary"]

    def test_health_event_carries_no_secret(self, monkeypatch):
        events = self._capture(monkeypatch)
        client = ScriptedAlertsClient({SUB_A: lambda: (_ for _ in ()).throw(HttpError(500))})
        _ingestor([SUB_A], client, retry=ae.RetryPolicy(max_retries=0)).ingest_alerts(token="SECRET-TOKEN")
        p = [pl for (et, pl) in events if et == "ingestion.subscription_health"][0]
        assert "SECRET-TOKEN" not in str(p)


# ── T6-AC2 — authentication failures visible in run health ───────────────────────


class TestAuthFailureVisible:

    def test_auth_failure_reported_for_all_subscriptions_no_crash(self, monkeypatch):
        events = []
        import app.telemetry as telemetry
        monkeypatch.setattr(telemetry, "record_event", lambda et, p: events.append((et, p)))

        # token acquisition fails (no SP in vault) — a connector-level auth failure.
        ing = ae.AzureEventIngestor(
            "org1", cfg.AzureEventConfig(
                environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
                mode=cfg.MODE_DIRECT, subscriptions=[SUB_A, SUB_B],
            ),
            vault_reader=lambda o, c: None,      # no service principal → AzureAuthError
            sleep_fn=lambda s: None,
        )
        result = ing.ingest_all(checkpoint='{"alerts": {"%s": "2026-06-01T00:00:00Z"}}' % SUB_A)

        # No crash; every pinned subscription reported unhealthy with auth category.
        assert result.emitted_count == 0
        auth_entries = [s for s in result.subscription_status.values()
                        if s["category"] == ae.CATEGORY_AUTHENTICATION]
        assert len(auth_entries) == len(ae.V1_STREAMS) * 2   # 3 streams × 2 subs
        # Loud in run health.
        health = [p for (et, p) in events if et == "ingestion.subscription_health"]
        assert all(p["category"] == ae.CATEGORY_AUTHENTICATION for p in health)
        assert len(health) == len(ae.V1_STREAMS) * 2
        # Checkpoints preserved (not advanced, not cleared).
        ns = ae.decode_stream_checkpoints(result.next_checkpoint)
        assert ns["alerts"][SUB_A] == "2026-06-01T00:00:00Z"

    def test_auth_failure_not_retried(self, monkeypatch):
        import app.telemetry as telemetry
        monkeypatch.setattr(telemetry, "record_event", lambda et, p: None)
        slept = []
        calls = {"n": 0}

        async def _bad_token(**kw):
            calls["n"] += 1
            raise ae.AzureAuthError("bad credential")

        ing = ae.AzureEventIngestor(
            "org1", cfg.AzureEventConfig(
                environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
                mode=cfg.MODE_DIRECT, subscriptions=[SUB_A],
            ),
            vault_reader=lambda o, c: type("R", (), {"username": "c", "secret": "s", "base_url": "t"})(),
            token_fn=_bad_token, sleep_fn=slept.append,
        )
        ing.ingest_all()
        assert slept == []               # auth is never retried


# ── T6-AC4 — healthy subscriptions continue independently ────────────────────────


class TestFailureIsolation:

    def test_healthy_subscription_continues_when_another_fails(self):
        client = ScriptedAlertsClient({
            SUB_A: lambda: (_ for _ in ()).throw(HttpError(403)),      # permanent failure
            SUB_B: [_alert(SUB_B, "b1", "2026-06-01T12:00:00Z")],      # healthy
        })
        result = _ingestor([SUB_A, SUB_B], client).ingest_alerts(token="T")
        assert result.subscription_status[SUB_A]["status"] == "error"
        assert result.subscription_status[SUB_B]["status"] == "ok"
        assert result.emitted_count == 1
        assert result.records[0]["account_scope"] == SUB_B

    def test_failed_sub_checkpoint_frozen_healthy_advances(self):
        client = ScriptedAlertsClient({
            SUB_A: lambda: (_ for _ in ()).throw(HttpError(500)),
            SUB_B: [_alert(SUB_B, "b1", "2026-06-01T12:00:00Z")],
        })
        result = _ingestor([SUB_A, SUB_B], client, retry=ae.RetryPolicy(max_retries=0)).ingest_alerts(token="T")
        cps = ae.decode_checkpoints(result.next_checkpoint)
        assert SUB_A not in cps                        # failed → not advanced
        assert cps[SUB_B] == "2026-06-01T12:00:00Z"    # healthy → advanced

    def test_transient_recovery_on_one_sub_does_not_affect_other(self):
        client = ScriptedAlertsClient({
            SUB_A: _flaky(lambda: HttpError(429), 1, [_alert(SUB_A, "a1", "2026-06-01T12:00:00Z")]),
            SUB_B: [_alert(SUB_B, "b1", "2026-06-01T12:30:00Z")],
        })
        result = _ingestor([SUB_A, SUB_B], client).ingest_alerts(token="T")
        assert result.all_ok
        assert result.emitted_count == 2
