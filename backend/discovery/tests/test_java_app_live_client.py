"""
R17-A3 — live JavaAppClient: Actuator metric mapping (H1), session close (M1),
and tolerant log parsing (M4).

The live HTTP path is never exercised by the offline fixture suite, so these
tests inject a fake ``requests``-style session and assert:

  * H1 — ``_sample_actuator`` reads the standard Actuator metric endpoints and
    populates error_rate / latency_p95_ms / throughput_rpm / jvm_memory_used_ratio
    / system_cpu_usage (not just ``health``), so friction is detectable live.
  * M1 — the HTTP session is closed after use (context manager / ``close``), so a
    long-lived process does not leak pooled connections.
  * M4 — ``_read_logs`` accepts a JSON array, an ``{"entries": []}`` wrapper,
    NDJSON, and plain-text lines.
"""
from __future__ import annotations

import json

from discovery.ingest.java_app import JavaAppClient


class FakeResp:
    def __init__(self, *, ok=True, status=200, json_data=None, text="", raise_json=False):
        self.ok = ok
        self.status_code = status
        self._json = json_data
        self.text = text
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._json


class FakeSession:
    """Minimal stand-in for requests.Session, tracking close()."""

    def __init__(self, handler):
        self._handler = handler
        self.headers: dict = {}
        self.closed = False

    def get(self, url, params=None, timeout=None):
        return self._handler(url, params or {})

    def close(self):
        self.closed = True


def _client_with(handler, *, actuator="https://svc.internal/actuator", log="https://svc.internal/logs"):
    client = JavaAppClient(actuator_url=actuator, log_source=log, secret="tok")
    client._session = FakeSession(handler)   # bypass real requests.Session
    return client


# ── H1: Actuator metric endpoint mapping ─────────────────────────────────────
def _actuator_handler(url, params):
    if url.endswith("/logs"):
        return FakeResp(json_data=[])          # log tail (used by read_operational)
    if url.endswith("/health"):
        return FakeResp(json_data={"status": "DOWN"})
    if url.endswith("/metrics/http.server.requests"):
        if params.get("tag") == "outcome:SERVER_ERROR":
            return FakeResp(json_data={"measurements": [{"statistic": "COUNT", "value": 120.0}]})
        return FakeResp(json_data={"measurements": [
            {"statistic": "COUNT", "value": 1000.0},
            {"statistic": "MAX", "value": 1.8},          # seconds
        ]})
    if url.endswith("/metrics/jvm.memory.used"):
        return FakeResp(json_data={"measurements": [{"statistic": "VALUE", "value": 910.0}]})
    if url.endswith("/metrics/jvm.memory.max"):
        return FakeResp(json_data={"measurements": [{"statistic": "VALUE", "value": 1000.0}]})
    if url.endswith("/metrics/system.cpu.usage"):
        return FakeResp(json_data={"measurements": [{"statistic": "VALUE", "value": 0.88}]})
    return FakeResp(ok=False, status=404, raise_json=True)


def test_h1_sample_actuator_populates_all_metric_fields():
    client = _client_with(_actuator_handler)
    sample = client._sample_actuator()
    assert sample["health"] == "DOWN"
    assert sample["error_rate"] == 0.12            # 120 / 1000
    assert sample["latency_p95_ms"] == 1800.0      # 1.8s -> ms (MAX proxy)
    assert sample["throughput_rpm"] == 1000.0      # cumulative request COUNT
    assert sample["jvm_memory_used_ratio"] == 0.91  # 910 / 1000
    assert sample["system_cpu_usage"] == 0.88
    assert sample["sample_ts"]


def test_h1_missing_metric_endpoints_yield_none_not_false_zero():
    # A minimal deployment exposing only /health must not report false zeros.
    client = _client_with(lambda url, params:
                          FakeResp(json_data={"status": "UP"}) if url.endswith("/health")
                          else FakeResp(ok=False, status=404, raise_json=True))
    sample = client._sample_actuator()
    assert sample["health"] == "UP"
    assert sample["error_rate"] is None
    assert sample["latency_p95_ms"] is None
    assert sample["jvm_memory_used_ratio"] is None
    assert sample["system_cpu_usage"] is None


# ── M1: session is closed after use ──────────────────────────────────────────
def test_m1_context_manager_closes_session():
    client = _client_with(_actuator_handler)
    session = client._session
    with client as c:
        c.read_operational()
    assert session.closed is True
    assert client._session is None


def test_m1_close_is_idempotent():
    client = _client_with(_actuator_handler)
    client.close()
    client.close()   # must not raise even with no live session


# ── M4: tolerant log parsing (JSON / NDJSON / plain text) ────────────────────
def _logs_client(resp):
    return _client_with(lambda url, params: resp)


def test_m4_reads_json_array():
    entries = [{"offset": 1, "level": "ERROR", "message": "boom"}]
    logs = _logs_client(FakeResp(json_data=entries))._read_logs()
    assert logs == entries


def test_m4_reads_entries_wrapper():
    resp = FakeResp(json_data={"entries": [{"offset": 7, "message": "x"}]})
    assert _logs_client(resp)._read_logs() == [{"offset": 7, "message": "x"}]


def test_m4_reads_ndjson():
    text = "\n".join(json.dumps(o) for o in (
        {"level": "ERROR", "message": "timeout"},
        {"level": "WARN", "message": "slow"},
    ))
    resp = FakeResp(raise_json=True, text=text)   # a bare NDJSON body is not valid JSON
    logs = _logs_client(resp)._read_logs()
    assert [r["message"] for r in logs] == ["timeout", "slow"]
    assert all("offset" in r for r in logs)       # offsets synthesised per line


def test_m4_reads_plain_text_lines():
    text = "2026-06-10 08:00 ERROR GatewayClient upstream timeout\nINFO started"
    logs = _logs_client(FakeResp(raise_json=True, text=text))._read_logs()
    assert logs[0]["level"] == "ERROR"
    assert "upstream timeout" in logs[0]["message"]
    assert logs[1]["level"] == "INFO"
