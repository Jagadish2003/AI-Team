"""
R17-A4 — live DotNetAppClient: health + EventCounters mapping, session close, and
tolerant/normalised log parsing (the .NET COLLECTION edge).

The live HTTP path is never exercised by the offline fixture suite, so these tests
inject a fake ``requests``-style session and assert:

  * ``_sample_diagnostics`` reads the ASP.NET Core health-checks endpoint and the
    EventCounters surface and maps them onto the NEUTRAL sample fields the shared
    extractor consumes (error_rate / latency_p95_ms / throughput_rpm /
    memory_used_ratio / cpu_usage), so friction is detectable live;
  * the HTTP session is closed after use (no leaked pooled connections);
  * ``_read_logs`` accepts JSON / NDJSON / plain text (shared parser) and the .NET
    LogLevel normalisation maps ``Critical`` → ``CRITICAL`` etc.
"""
from __future__ import annotations

import json

from discovery.ingest.dotnet_app import (
    DotNetAppClient,
    _normalize_dotnet_level,
)


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


def _client_with(handler, *, diag="https://svc.internal/diagnostics", log="https://svc.internal/logs"):
    client = DotNetAppClient(diagnostics_url=diag, log_source=log, secret="tok")
    client._session = FakeSession(handler)   # bypass real requests.Session
    return client


# ── health + EventCounters mapping ───────────────────────────────────────────
def _diagnostics_handler(url, params):
    if url.endswith("/logs"):
        return FakeResp(json_data=[])          # log tail (used by read_operational)
    if url.endswith("/health"):
        return FakeResp(json_data={"status": "Unhealthy"})
    if url.endswith("/counters"):
        return FakeResp(json_data={"counters": [
            {"name": "cpu-usage", "value": 88.0},
            {"name": "gc-heap-size", "value": 512.0},
            {"name": "gc-committed", "value": 640.0},
            {"name": "requests-per-second", "value": 25.0},
            {"name": "total-requests", "value": 100000.0},
            {"name": "failed-requests", "value": 12000.0},
            {"name": "request-duration", "value": 1800.0},
        ]})
    return FakeResp(ok=False, status=404, raise_json=True)


def test_sample_diagnostics_populates_all_metric_fields():
    client = _client_with(_diagnostics_handler)
    sample = client._sample_diagnostics()
    assert sample["health"] == "Unhealthy"
    assert sample["error_rate"] == 0.12            # 12000 / 100000
    assert sample["latency_p95_ms"] == 1800.0      # request-duration (ms)
    assert sample["throughput_rpm"] == 1500.0      # 25 rps * 60
    assert sample["memory_used_ratio"] == 0.8      # 512 / 640 (managed GC heap)
    assert sample["cpu_usage"] == 0.88             # 88% -> 0..1
    assert sample["sample_ts"]


def test_missing_counters_yield_none_not_false_zero():
    # A minimal deployment exposing only /health must not report false zeros.
    client = _client_with(lambda url, params:
                          FakeResp(json_data={"status": "Healthy"}) if url.endswith("/health")
                          else FakeResp(ok=False, status=404, raise_json=True))
    sample = client._sample_diagnostics()
    assert sample["health"] == "Healthy"
    assert sample["error_rate"] is None
    assert sample["latency_p95_ms"] is None
    assert sample["throughput_rpm"] is None
    assert sample["memory_used_ratio"] is None
    assert sample["cpu_usage"] is None


def test_counters_accept_flat_object_shape():
    def handler(url, params):
        if url.endswith("/health"):
            return FakeResp(json_data={"status": "Degraded"})
        if url.endswith("/counters"):
            return FakeResp(json_data={"cpu-usage": 50.0, "total-requests": 200.0,
                                       "failed-requests": 10.0})
        return FakeResp(ok=False, status=404, raise_json=True)
    sample = _client_with(handler)._sample_diagnostics()
    assert sample["cpu_usage"] == 0.5
    assert sample["error_rate"] == 0.05


# ── session is closed after use ──────────────────────────────────────────────
def test_context_manager_closes_session():
    client = _client_with(_diagnostics_handler)
    session = client._session
    with client as c:
        c.read_operational()
    assert session.closed is True
    assert client._session is None


def test_close_is_idempotent():
    client = _client_with(_diagnostics_handler)
    client.close()
    client.close()   # must not raise even with no live session


# ── tolerant + normalised log parsing (JSON / NDJSON / plain text) ───────────
def _logs_client(resp):
    return _client_with(lambda url, params: resp)


def test_reads_json_array():
    entries = [{"offset": 1, "level": "Error", "message": "boom"}]
    logs = _logs_client(FakeResp(json_data=entries))._read_logs()
    assert logs == entries


def test_reads_entries_wrapper():
    resp = FakeResp(json_data={"entries": [{"offset": 7, "message": "x"}]})
    assert _logs_client(resp)._read_logs() == [{"offset": 7, "message": "x"}]


def test_reads_ndjson():
    text = "\n".join(json.dumps(o) for o in (
        {"level": "Critical", "message": "timeout"},
        {"level": "Warning", "message": "slow"},
    ))
    resp = FakeResp(raise_json=True, text=text)   # a bare NDJSON body is not valid JSON
    logs = _logs_client(resp)._read_logs()
    assert [r["message"] for r in logs] == ["timeout", "slow"]
    assert all("offset" in r for r in logs)


def test_reads_plain_text_lines_with_dotnet_levels():
    text = "2026-06-10 08:00 Critical GatewayClient upstream timeout\nInformation started"
    logs = _logs_client(FakeResp(raise_json=True, text=text))._read_logs()
    assert logs[0]["level"] == "CRITICAL"          # .NET 'Critical' -> canonical
    assert "upstream timeout" in logs[0]["message"]
    assert logs[1]["level"] == "INFO"              # .NET 'Information' -> INFO


# ── .NET LogLevel normalisation (the collection edge) ────────────────────────
def test_normalize_dotnet_level_maps_native_spellings():
    assert _normalize_dotnet_level("Critical") == "CRITICAL"
    assert _normalize_dotnet_level("Error") == "ERROR"
    assert _normalize_dotnet_level("Warning") == "WARN"
    assert _normalize_dotnet_level("Information") == "INFO"
    assert _normalize_dotnet_level("Trace") == "TRACE"
    assert _normalize_dotnet_level(None) == ""
