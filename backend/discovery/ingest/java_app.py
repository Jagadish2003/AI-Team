"""
R17-A3 / T1 — Java application change-based ingestor (operational surface).

AgentIQ's first non-SaaS enterprise source. Implements the shared
:class:`~discovery.ingest.operational_ingest.OperationalChangeIngestor` (itself a
:class:`~discovery.ingest.base.ChangeBasedIngestor` from R16-A1) to read the
OPERATIONAL surface of a running Java enterprise application — its framework
health/diagnostics endpoints (Spring Boot Actuator: ``health``, ``metrics``,
``info``) and its application logs — and produce operational SIGNAL where the
application shows runtime friction (R17-A3 §1).

Shared foundation, Java collection edge (R17-A4 / AC3)
------------------------------------------------------
The change-based foundation (opaque per-app checkpoint cursor, delta windowing,
resumable batch streaming, provenance stamping) and the signal *interpretation*
(:mod:`operational_signals`) are shared verbatim with the .NET ingestor. This
module supplies only the Java COLLECTION edge: where targets come from
(:mod:`java_app_config`), how to read one app's Actuator surface + logs
(``_raw_operational`` / :class:`JavaAppClient`), and the Java endpoint field name
(``actuator_url``). Nothing about the interpretation is duplicated here.

Scope — phase one of two (AC8)
------------------------------
This is the OPERATIONAL phase: read what a running application reports about
itself. It reads operational surfaces ONLY — it never reads the application's
SOURCE CODE, which is reserved for the separate 1.8 code-and-structure phase.
External APM/observability-platform data is also out of scope. The connector
touches only the Actuator endpoints and the configured log sources; there is no
repository clone, no class/AST inspection, and no configuration-file reading.

Configured, not auto-discovered (R17-A3 §2)
-------------------------------------------
The applications to read — their Actuator endpoint URLs and log sources — are
configured per deployment (see :mod:`discovery.ingest.java_app_config`), with
credentials handled via the vault. AgentIQ does NOT scan the network to find Java
apps; the customer points it at the applications in scope.

Provenance & change events
--------------------------
Every record carries a fully-populated, OBSERVED ``evidence_pointer`` (R16-B1,
``source_system='java_app'``, ``origin='observed'`` — T4/AC4) plus an
``artifact_id`` and ``change_kind`` so the shared change runner emits one
``ingestion.artifact_changed`` event per changed artifact (R16-A1 — T6/AC6).
Operational SIGNAL is extracted as a window operation over the whole delta by
:func:`java_app_signals.build_java_app_corroboration_payload`, NOT per-record.

Offline vs live
---------------
Offline (default, ``INGEST_MODE`` != ``live``): reads the deterministic fixture
``fixtures/java_app_sample.json``. Live: samples each configured target's Actuator
endpoints and tails its log source over HTTP, using the credential resolved from
the vault (never from config — AC3).

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
from .java_app_config import JavaAppTarget, load_targets, resolve_secret
from .operational_ingest import (
    OperationalChangeIngestor,
    OperationalIngestError,
    app_cursor as _app_cursor,  # noqa: F401 — re-exported for callers/tests
    decode_checkpoint as _decode_checkpoint,
    encode_checkpoint as _encode_checkpoint,
    parse_log_payload,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "java_app_sample.json"

#: Live HTTP timeout (seconds) for Actuator / log reads.
_REQUEST_TIMEOUT = 30


class JavaAppIngestError(OperationalIngestError):
    """Raised when live Java-app ingestion fails with a clear, actionable message."""


class JavaAppIngestor(OperationalChangeIngestor):
    """Change-based Java application ingestor (R17-A3 / T1).

    Subclasses the shared :class:`OperationalChangeIngestor`, supplying only the
    Java collection edge: Actuator + log reads and the ``actuator_url`` endpoint
    field. The per-app ``{log_offset, metrics_ts, metrics_seq}`` cursor map, delta
    windowing, resumable batching, and provenance are all inherited.

    Operational surface only (AC8): reads the configured Actuator endpoints and
    log sources — never the application's source code.
    """

    connector_id = "java_app"
    source_system = "java_app"
    reports_deletes = False
    #: Human-facing name for a fail-closed credential-miss health record (AC1).
    health_system = "Java Application"

    # ── Collection hooks ─────────────────────────────────────────────────────
    def _load_targets(self, org_id: str) -> List[JavaAppTarget]:
        return load_targets(org_id)

    def _to_metric_record(
        self, target: JavaAppTarget, sample: Dict[str, Any], seq_index: int = 0
    ) -> Dict[str, Any]:
        """Shape one Actuator metric sample into a change-delta record (T2/T4).

        The normalised reading (health/error-rate/latency/throughput/heap/CPU) and
        provenance are built by the shared base; only the Java endpoint field
        (``actuator_url``) is Java-specific.
        """
        return self._metric_record(
            target, sample, seq_index,
            endpoint_field="actuator_url", endpoint_url=target.actuator_url,
        )

    def _to_log_record(self, target: JavaAppTarget, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one application log entry into a change-delta record (T2/T4)."""
        return self._log_record(target, entry, log_source=target.log_source)

    # ── Source access: offline fixture vs live Actuator / log read ───────────
    def _raw_operational(self, org_id: str, target: JavaAppTarget) -> Dict[str, Any]:
        """Return ``{"metrics": [...], "logs": [...]}`` for one app.

        Offline reads the deterministic fixture; live samples the Actuator
        endpoints and tails the log source using the vault-resolved credential
        (AC3). Operational surface only — no source code (AC8). The live client's
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
            raise JavaAppIngestError(f"Java app fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _client(self, org_id: str, target: JavaAppTarget) -> "JavaAppClient":
        """Build a live client for one app from the vault-resolved credential.

        The secret is resolved from the credential vault via the per-run context
        (AC3) and handed to the client; it is never read from the target config
        nor logged.
        """
        secret = resolve_secret(org_id, target)
        return JavaAppClient(
            actuator_url=target.actuator_url,
            log_source=target.log_source,
            secret=secret,
        )


class JavaAppClient:
    """Thin live client for one Java application's operational surface.

    Reads the framework health/diagnostics endpoints (Spring Boot Actuator) and
    the application log source over HTTP, and normalises them onto the neutral
    sample/log shape the shared extractor consumes. The credential, when present,
    is sent as a Bearer header; it is held only for the life of the request session
    and never logged. Operational surface only — this client has no path that reads
    source code (AC8).

    Use as a context manager (or call :meth:`close`) so the underlying
    ``requests.Session`` — and its pooled TCP connections — is released after the
    read; a long-lived FastAPI process reads many targets across many runs (M1).
    """

    def __init__(self, *, actuator_url: str, log_source: str, secret: Optional[str]):
        self.actuator_url = actuator_url.rstrip("/") if actuator_url else ""
        self.log_source = log_source
        self._secret = secret
        self._session = None

    def __enter__(self) -> "JavaAppClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP session and release its pooled connections (M1)."""
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def _sess(self):
        try:
            import requests
        except ImportError:  # pragma: no cover - requests ships in requirements
            raise JavaAppIngestError(
                "requests library required for live Java-app mode: pip install requests"
            )
        if self._session is None:
            self._session = requests.Session()
            if self._secret:
                self._session.headers.update({"Authorization": f"Bearer {self._secret}"})
        return self._session

    def read_operational(self) -> Dict[str, Any]:
        """Sample the Actuator endpoints and tail the log source.

        Returns the same ``{"metrics": [...], "logs": [...]}`` shape the offline
        fixture provides, so the ingestor's downstream logic is identical in both
        modes. Network/transport errors are raised as :class:`JavaAppIngestError`
        with no secret in the message (the runner degrades non-blockingly).
        """
        metrics = [self._sample_actuator()] if self.actuator_url else []
        logs = self._read_logs() if self.log_source else []
        return {"metrics": metrics, "logs": logs}

    # ── Actuator metric reads (H1) ───────────────────────────────────────────
    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """GET an Actuator endpoint and return parsed JSON, or None on non-OK."""
        resp = self._sess().get(
            f"{self.actuator_url}/{path}", params=params, timeout=_REQUEST_TIMEOUT
        )
        if not resp.ok:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _metric(
        self, name: str, statistic: str = "VALUE", tag: Optional[str] = None
    ) -> Optional[float]:
        """Read one measurement from ``GET /metrics/{name}`` (optionally tag-filtered).

        Spring Boot Actuator returns ``{"name", "measurements": [{"statistic",
        "value"}], ...}``. Returns the value for ``statistic`` (COUNT / VALUE /
        TOTAL_TIME / MAX), or None when the metric/statistic is unavailable.
        """
        body = self._get_json(f"metrics/{name}", params={"tag": tag} if tag else None)
        if not isinstance(body, dict):
            return None
        for m in body.get("measurements", []) or []:
            if isinstance(m, dict) and m.get("statistic") == statistic:
                try:
                    return float(m.get("value"))
                except (TypeError, ValueError):
                    return None
        return None

    def _sample_actuator(self) -> Dict[str, Any]:
        """Read health + request/JVM/CPU metrics into one normalised sample (H1).

        Maps the standard Actuator endpoints onto the neutral operational reading
        fields the shared signal extractor consumes:

          * ``error_rate``          — ``http.server.requests`` SERVER_ERROR count
                                      / total count.
          * ``latency_p95_ms``      — ``http.server.requests`` MAX (seconds→ms):
                                      the high-percentile proxy Actuator exposes.
          * ``throughput_rpm``      — ``http.server.requests`` cumulative COUNT.
          * ``memory_used_ratio``   — ``jvm.memory.used`` / ``jvm.memory.max``
                                      (the JVM heap, mapped onto the neutral field).
          * ``cpu_usage``           — ``system.cpu.usage`` (0..1).

        Any endpoint that is absent simply yields None for that field, which the
        signal extractor treats as "not reported" rather than a false zero.
        """
        from datetime import datetime, timezone

        health = self._get_json("health") or {}

        total = self._metric("http.server.requests", "COUNT")
        errors = self._metric("http.server.requests", "COUNT", tag="outcome:SERVER_ERROR")
        error_rate = (
            round(errors / total, 4) if total and total > 0 and errors is not None else None
        )
        max_seconds = self._metric("http.server.requests", "MAX")
        latency_p95_ms = round(max_seconds * 1000.0, 2) if max_seconds is not None else None

        heap_used = self._metric("jvm.memory.used")
        heap_max = self._metric("jvm.memory.max")
        heap_ratio = (
            round(heap_used / heap_max, 4)
            if heap_used is not None and heap_max and heap_max > 0
            else None
        )

        return {
            "sample_ts": datetime.now(timezone.utc).isoformat(),
            "health": str(health.get("status", "")),
            "error_rate": error_rate,
            "latency_p95_ms": latency_p95_ms,
            "throughput_rpm": total,
            "memory_used_ratio": heap_ratio,
            "cpu_usage": self._metric("system.cpu.usage"),
        }

    # ── Log tail (M4) ────────────────────────────────────────────────────────
    def _read_logs(self) -> List[Dict[str, Any]]:
        """Tail the configured log source, accepting JSON, NDJSON, or plain text.

        Spring Boot application logs are commonly a JSON array / ``{"entries": []}``
        wrapper, NDJSON (one JSON object per line), or plain-text lines. All three
        are handled so live log signal is actually ingested (M4); a truly
        unparseable body yields no entries rather than raising.
        """
        resp = self._sess().get(self.log_source, timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise JavaAppIngestError(f"log read HTTP {resp.status_code}")
        return parse_log_payload(resp, plain_text_level=_plain_text_level)


def _plain_text_level(line: str) -> str:
    """Best-effort log level from a plain-text log line (no NLP — AC8)."""
    upper = line.upper()
    for level in ("FATAL", "SEVERE", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"):
        if level in upper:
            return level
    return ""
