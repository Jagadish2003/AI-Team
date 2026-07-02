"""
R17-A4 / T1 — .NET application change-based ingestor (operational surface).

The .NET counterpart to R17-A3's Java ingestor, completing the OPERATIONAL phase
of the Java/.NET enterprise-application scope. Implements the shared
:class:`~discovery.ingest.operational_ingest.OperationalChangeIngestor` (a R16-A1
:class:`~discovery.ingest.base.ChangeBasedIngestor`) to read the OPERATIONAL
surface of a running .NET enterprise application — its health/diagnostics surface
(ASP.NET Core health checks + EventCounters/diagnostics) and its application logs —
and produce operational SIGNAL where the application shows runtime friction
(R17-A4 §1).

Shared foundation, .NET collection edge (R17-A4 / AC3)
------------------------------------------------------
Deliberately parallel to the Java story: the change-based foundation (opaque
per-app checkpoint cursor, delta windowing, resumable batch streaming, provenance
stamping) and the signal *interpretation* (:mod:`operational_signals`) are shared
VERBATIM with the Java ingestor — the interpretation of an error-rate rise,
latency degradation, throughput drop, resource pressure, or a recurring exception
cluster is identical whatever the platform. This module supplies only the .NET
COLLECTION edge:

  * which endpoints — ASP.NET Core health checks + EventCounters, mapped onto the
    neutral sample the shared extractor consumes (:class:`DotNetAppClient`);
  * which log formats — the tolerant JSON/NDJSON/plain-text parser (shared) plus
    .NET ``LogLevel`` normalisation (``Critical`` → ``CRITICAL`` etc.); and
  * the .NET endpoint field name (``diagnostics_url``).

Nothing about the interpretation is duplicated here, so future fixes/improvements
to the extraction never drift between Java and .NET.

Scope — phase one of two (AC8)
------------------------------
OPERATIONAL phase only: read what a running .NET application reports about itself.
It reads operational surfaces ONLY — never the application's SOURCE CODE (reserved
for the separate 1.8 code-and-structure phase) and no external APM/observability
platform. There is no repository clone, no class/AST inspection, and no
configuration-file reading.

Configured, not auto-discovered (R17-A4 §3)
-------------------------------------------
The applications to read — their diagnostics endpoint URLs and log sources — are
configured per deployment (see :mod:`discovery.ingest.dotnet_app_config`), with
credentials handled via the vault. AgentIQ does NOT scan the network to find .NET
apps.

Provenance & change events
--------------------------
Every record carries a fully-populated, OBSERVED ``evidence_pointer`` (R16-B1,
``source_system='dotnet_app'``, ``origin='observed'`` — T4/AC5) plus an
``artifact_id`` and ``change_kind`` so the shared change runner emits one
``ingestion.artifact_changed`` event per changed artifact (R16-A1 — T6/AC7).

Deletes / tombstones (R16-A1 §5)
--------------------------------
``reports_deletes = False``: operational data is forward-only — a metrics sample
or a log line, once observed, is never "deleted" upstream.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import is_live
from .dotnet_app_config import DotNetAppTarget, load_targets, resolve_secret
from .operational_ingest import (
    OperationalChangeIngestor,
    OperationalIngestError,
    app_cursor as _app_cursor,  # noqa: F401 — re-exported for callers/tests
    decode_checkpoint as _decode_checkpoint,
    encode_checkpoint as _encode_checkpoint,
    parse_log_payload,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dotnet_app_sample.json"

#: Live HTTP timeout (seconds) for health / EventCounters / log reads.
_REQUEST_TIMEOUT = 30

#: Map a .NET ``Microsoft.Extensions.Logging`` LogLevel (or Serilog level) onto the
#: shared canonical, upper-case level vocabulary the extractor counts as an error.
#: This is a genuinely .NET-specific bit of COLLECTION: ``Critical``/``Warning``/
#: ``Information`` are .NET spellings that map onto CRITICAL/WARN/INFO.
_DOTNET_LEVEL_MAP: Dict[str, str] = {
    "TRACE": "TRACE",
    "DEBUG": "DEBUG",
    "INFORMATION": "INFO",
    "INFO": "INFO",
    "WARNING": "WARN",
    "WARN": "WARN",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
    "FATAL": "FATAL",
    "NONE": "",
}


class DotNetAppIngestError(OperationalIngestError):
    """Raised when live .NET-app ingestion fails with a clear, actionable message."""


def _normalize_dotnet_level(level: Any) -> str:
    """Normalise a .NET LogLevel onto the shared canonical level vocabulary."""
    token = str(level or "").strip().upper()
    return _DOTNET_LEVEL_MAP.get(token, token)


def _dotnet_plain_text_level(line: str) -> str:
    """Best-effort canonical level from a plain-text .NET log line (no NLP — AC8)."""
    upper = line.upper()
    # .NET writes 'Information'/'Warning'/'Critical'; check the longer tokens first.
    for token in ("CRITICAL", "FATAL", "ERROR", "WARNING", "WARN",
                  "INFORMATION", "INFO", "DEBUG", "TRACE"):
        if token in upper:
            return _DOTNET_LEVEL_MAP.get(token, token)
    return ""


class DotNetAppIngestor(OperationalChangeIngestor):
    """Change-based .NET application ingestor (R17-A4 / T1).

    Subclasses the shared :class:`OperationalChangeIngestor`, supplying only the
    .NET collection edge: health-check + EventCounters reads and the
    ``diagnostics_url`` endpoint field. The per-app ``{log_offset, metrics_ts,
    metrics_seq}`` cursor map, delta windowing, resumable batching, and provenance
    are all inherited — identical to Java.

    Operational surface only (AC8): reads the configured health/diagnostics
    endpoints and log sources — never the application's source code.
    """

    connector_id = "dotnet_app"
    source_system = "dotnet_app"
    reports_deletes = False

    # ── Collection hooks ─────────────────────────────────────────────────────
    def _load_targets(self, org_id: str) -> List[DotNetAppTarget]:
        return load_targets(org_id)

    def _to_metric_record(
        self, target: DotNetAppTarget, sample: Dict[str, Any], seq_index: int = 0
    ) -> Dict[str, Any]:
        """Shape one .NET diagnostics sample into a change-delta record (T2/T4).

        The normalised reading (health/error-rate/latency/throughput/heap/CPU) and
        provenance are built by the shared base; only the .NET endpoint field
        (``diagnostics_url``) is .NET-specific.
        """
        return self._metric_record(
            target, sample, seq_index,
            endpoint_field="diagnostics_url", endpoint_url=target.diagnostics_url,
        )

    def _to_log_record(self, target: DotNetAppTarget, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one .NET log entry into a change-delta record (T2/T4).

        Normalises the .NET LogLevel (``Critical`` → ``CRITICAL``, ``Information``
        → ``INFO`` …) onto the shared vocabulary so the shared error/exception
        extraction reads it identically to a Java log — this level mapping is the
        .NET collection edge.
        """
        return self._log_record(
            target, entry,
            log_source=target.log_source,
            level=_normalize_dotnet_level(entry.get("level")),
        )

    # ── Source access: offline fixture vs live health/EventCounters read ─────
    def _raw_operational(self, org_id: str, target: DotNetAppTarget) -> Dict[str, Any]:
        """Return ``{"metrics": [...], "logs": [...]}`` for one app.

        Offline reads the deterministic fixture; live samples the health checks +
        EventCounters and tails the log source using the vault-resolved credential
        (AC4). Operational surface only — no source code (AC8). The live client's
        HTTP session is always closed after the read (no connection leak).
        """
        if not is_live():
            fixture = self._fixture()
            return {
                "metrics": list(fixture.get("metrics", {}).get(target.app_id, [])),
                "logs": list(fixture.get("logs", {}).get(target.app_id, [])),
            }
        with self._client(org_id, target) as client:
            return client.read_operational()

    def _fixture(self) -> Dict[str, Any]:
        if not FIXTURE_PATH.exists():
            raise DotNetAppIngestError(f".NET app fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _client(self, org_id: str, target: DotNetAppTarget) -> "DotNetAppClient":
        """Build a live client for one app from the vault-resolved credential.

        The secret is resolved from the credential vault via the per-run context
        (AC4) and handed to the client; it is never read from the target config
        nor logged.
        """
        secret = resolve_secret(org_id, target)
        return DotNetAppClient(
            diagnostics_url=target.diagnostics_url,
            log_source=target.log_source,
            secret=secret,
        )


class DotNetAppClient:
    """Thin live client for one .NET application's operational surface.

    Reads the ASP.NET Core health-checks endpoint and the EventCounters/diagnostics
    surface over HTTP, and normalises them onto the neutral sample/log shape the
    shared extractor consumes — so the ingestor's downstream logic is identical to
    Java's. The credential, when present, is sent as a Bearer header; it is held
    only for the life of the request session and never logged. Operational surface
    only — this client has no path that reads source code (AC8).

    Use as a context manager (or call :meth:`close`) so the underlying
    ``requests.Session`` — and its pooled TCP connections — is released after the
    read; a long-lived FastAPI process reads many targets across many runs.
    """

    def __init__(self, *, diagnostics_url: str, log_source: str, secret: Optional[str]):
        self.diagnostics_url = diagnostics_url.rstrip("/") if diagnostics_url else ""
        self.log_source = log_source
        self._secret = secret
        self._session = None

    def __enter__(self) -> "DotNetAppClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP session and release its pooled connections."""
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def _sess(self):
        try:
            import requests
        except ImportError:  # pragma: no cover - requests ships in requirements
            raise DotNetAppIngestError(
                "requests library required for live .NET-app mode: pip install requests"
            )
        if self._session is None:
            self._session = requests.Session()
            if self._secret:
                self._session.headers.update({"Authorization": f"Bearer {self._secret}"})
        return self._session

    def read_operational(self) -> Dict[str, Any]:
        """Sample the health/EventCounters surface and tail the log source.

        Returns the same ``{"metrics": [...], "logs": [...]}`` shape the offline
        fixture provides, so the ingestor's downstream logic is identical in both
        modes. Network/transport errors are raised as :class:`DotNetAppIngestError`
        with no secret in the message (the runner degrades non-blockingly).
        """
        metrics = [self._sample_diagnostics()] if self.diagnostics_url else []
        logs = self._read_logs() if self.log_source else []
        return {"metrics": metrics, "logs": logs}

    # ── Health + EventCounters reads ─────────────────────────────────────────
    def _get_json(self, path: str) -> Optional[Any]:
        """GET a diagnostics endpoint and return parsed JSON, or None on non-OK."""
        resp = self._sess().get(f"{self.diagnostics_url}/{path}", timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _counters(self) -> Dict[str, float]:
        """Read the EventCounters surface into a ``{counter-name: value}`` map.

        Accepts the common shapes a .NET diagnostics endpoint exposes: a
        ``dotnet-counters`` style ``{"counters"|"Events": [{"name","value"}]}``
        list, or a flat ``{"cpu-usage": 88.0, ...}`` object. Unknown shapes yield
        an empty map (every derived field then reads as "not reported", never a
        false zero).
        """
        body = self._get_json("counters")
        out: Dict[str, float] = {}
        if isinstance(body, dict):
            series = body.get("counters")
            if series is None:
                series = body.get("Events")
            if isinstance(series, list):
                for c in series:
                    if isinstance(c, dict) and c.get("name") is not None:
                        val = _as_float(c.get("value"))
                        if val is not None:
                            out[str(c["name"])] = val
            else:
                # Flat {name: value} object.
                for name, value in body.items():
                    val = _as_float(value)
                    if val is not None:
                        out[str(name)] = val
        return out

    def _sample_diagnostics(self) -> Dict[str, Any]:
        """Read health + EventCounters into one normalised sample.

        Maps the .NET operational surface onto the neutral reading fields the
        shared signal extractor consumes:

          * ``health``            — ASP.NET Core health-checks ``status``
                                    (Healthy/Degraded/Unhealthy).
          * ``error_rate``        — ``failed-requests`` / ``total-requests``
                                    (ASP.NET Core hosting EventCounters).
          * ``latency_p95_ms``    — ``request-duration`` EventCounter (ms).
          * ``throughput_rpm``    — ``requests-per-second`` × 60 (falls back to the
                                    cumulative ``total-requests`` counter).
          * ``memory_used_ratio`` — ``gc-heap-size`` / ``gc-committed`` (the .NET
                                    managed GC heap, mapped onto the neutral field).
          * ``cpu_usage``         — ``cpu-usage`` EventCounter (percent → 0..1).

        Any counter that is absent simply yields None for that field, which the
        signal extractor treats as "not reported" rather than a false zero.
        """
        from datetime import datetime, timezone

        health_body = self._get_json("health")
        if isinstance(health_body, dict):
            health = str(health_body.get("status", ""))
        elif isinstance(health_body, str):
            health = health_body
        else:
            health = ""

        c = self._counters()

        total = c.get("total-requests")
        failed = c.get("failed-requests")
        error_rate = (
            round(failed / total, 4) if total and total > 0 and failed is not None else None
        )

        latency = c.get("request-duration")
        latency_p95_ms = round(latency, 2) if latency is not None else None

        rps = c.get("requests-per-second")
        if rps is not None:
            throughput_rpm = round(rps * 60.0, 2)
        else:
            throughput_rpm = total

        heap_used = c.get("gc-heap-size")
        heap_committed = c.get("gc-committed")
        memory_used_ratio = (
            round(heap_used / heap_committed, 4)
            if heap_used is not None and heap_committed and heap_committed > 0
            else None
        )

        cpu = c.get("cpu-usage")
        cpu_usage = round(cpu / 100.0, 4) if cpu is not None else None

        return {
            "sample_ts": datetime.now(timezone.utc).isoformat(),
            "health": health,
            "error_rate": error_rate,
            "latency_p95_ms": latency_p95_ms,
            "throughput_rpm": throughput_rpm,
            "memory_used_ratio": memory_used_ratio,
            "cpu_usage": cpu_usage,
        }

    # ── Log tail ─────────────────────────────────────────────────────────────
    def _read_logs(self) -> List[Dict[str, Any]]:
        """Tail the configured log source, accepting JSON, NDJSON, or plain text.

        .NET application logs are commonly a structured JSON array / ``{"entries":
        []}`` wrapper (Serilog / ``Microsoft.Extensions.Logging`` JSON console),
        NDJSON, or plain-text lines. The tolerant parsing is the shared
        :func:`operational_ingest.parse_log_payload`; only the plain-text level
        vocabulary is .NET-specific.
        """
        resp = self._sess().get(self.log_source, timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise DotNetAppIngestError(f"log read HTTP {resp.status_code}")
        return parse_log_payload(resp, plain_text_level=_dotnet_plain_text_level)


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
