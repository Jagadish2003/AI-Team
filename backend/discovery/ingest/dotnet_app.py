"""
R17-A4 / T1 — .NET application change-based ingestor (operational surface).

The producer of the .NET operational signals this story feeds into corroboration.
Implements the :class:`~discovery.ingest.base.ChangeBasedIngestor` contract
(R16-A1) to read the OPERATIONAL surface of a running .NET enterprise application —
its health/diagnostics surface (ASP.NET Core health checks + EventCounters) and its
application logs — and emit change-delta records, each stamped with an OBSERVED
:class:`~app.provenance.EvidencePointer` (``source_system='dotnet_app'``). The
corroboration shaping of those records is :mod:`discovery.ingest.dotnet_app_signals`.

Scope — operational surface only (phase one)
--------------------------------------------
Reads what a running .NET application reports *about itself* — never its SOURCE
CODE (the separate 1.8 code-and-structure phase) and never an external APM
platform. Every record is a ``metrics`` sample or a ``log`` entry.

Configured, not auto-discovered
-------------------------------
The applications to read come from per-deployment config
(:mod:`discovery.ingest.dotnet_app_config`), with credentials handled via the
vault. AgentIQ does NOT scan the network to find .NET apps.

Change-based, incremental
-------------------------
.NET operational data is incremental — logs are read forward from a position,
diagnostics are sampled over time — so the connector encodes its read position as
an opaque per-app ``{log_offset, metrics_ts}`` cursor map. Each run processes only
new data; an idle application yields an empty delta. The shared change runner owns
the checkpoint lifecycle and emits one ``ingestion.artifact_changed`` event per
changed operational artifact.

Signal, not per-record
----------------------
Operational SIGNAL is a WINDOW operation over the whole delta
(:mod:`dotnet_app_signals`), never per-record — a single sample cannot show a
degradation *trend*.

Deletes / tombstones (R16-A1 §5)
--------------------------------
``reports_deletes = False``: operational data is forward-only.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import is_live
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .dotnet_app_config import DotNetAppTarget, load_targets, resolve_secret
from .dotnet_app_signals import build_evidence_pointer

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dotnet_app_sample.json"

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default number of operational records emitted per :class:`DeltaBatch`.
_DEFAULT_BATCH_SIZE = 100

#: Live HTTP timeout (seconds) for health / EventCounters / log reads.
_REQUEST_TIMEOUT = 30

#: Map a .NET LogLevel (Microsoft.Extensions.Logging / Serilog) onto the canonical,
#: upper-case level vocabulary the error/exception extraction treats as an error.
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


class DotNetAppIngestError(Exception):
    """Raised when live .NET-app ingestion fails with a clear, actionable message."""


def _normalize_dotnet_level(level: Any) -> str:
    """Normalise a .NET LogLevel onto the canonical level vocabulary."""
    token = str(level or "").strip().upper()
    return _DOTNET_LEVEL_MAP.get(token, token)


def _encode_checkpoint(cursors: Dict[str, Dict[str, Any]]) -> str:
    """Encode the per-app cursor map as the opaque checkpoint value (deterministic)."""
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "apps": cursors},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Decode an opaque checkpoint value back into the per-app cursor map.

    Tolerant: a missing, empty, or unparseable value yields an empty map (read
    every app from the beginning) rather than raising.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning(
            "dotnet_app: could not decode checkpoint value; treating as first run."
        )
        return {}
    apps = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(apps, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for app_id, cur in apps.items():
        if isinstance(cur, dict):
            out[str(app_id)] = {
                "log_offset": int(cur.get("log_offset", 0) or 0),
                "metrics_ts": str(cur.get("metrics_ts", "") or ""),
            }
    return out


def _app_cursor(cursors: Dict[str, Dict[str, Any]], app_id: str) -> Dict[str, Any]:
    """Return the cursor for one app, defaulting to the beginning of time."""
    cur = cursors.get(app_id) or {}
    return {
        "log_offset": int(cur.get("log_offset", 0) or 0),
        "metrics_ts": str(cur.get("metrics_ts", "") or ""),
    }


class DotNetAppIngestor(ChangeBasedIngestor):
    """Change-based .NET application ingestor (R17-A4 / T1).

    Encodes its position as a per-app ``{log_offset, metrics_ts}`` cursor map
    (opaque to the runner) and yields only new metric samples / log entries per app.
    Operational surface only — reads the configured health/diagnostics endpoints and
    log sources, never the application's source code.
    """

    connector_id = "dotnet_app"
    reports_deletes = False

    def __init__(self, batch_size: int = _DEFAULT_BATCH_SIZE):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size

    # ── ChangeBasedIngestor contract ────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of new .NET-app operational records since ``since``.

        First run (``since is None``): full load of every configured app, streamed
        as checkpointed batches. Incremental run: only metric samples newer than the
        stored position and log entries past the stored ``log_offset`` per app. An
        idle deployment yields a single empty :class:`DeltaBatch` whose
        ``next_checkpoint`` echoes the incoming position.
        """
        cursors = _decode_checkpoint(since.value if since else None)
        running = {app_id: dict(cur) for app_id, cur in cursors.items()}

        targets = load_targets(org_id)
        logger.info(
            "dotnet_app: org=%s %s — %d configured application(s)",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(targets),
        )

        pending: List[tuple] = []  # (target, [records], new_cursor)
        for target in targets:
            cursor = _app_cursor(cursors, target.app_id)
            records, new_cursor = self._read_operational(org_id, target, cursor)
            if records:
                pending.append((target, records, new_cursor))
            else:
                running.setdefault(target.app_id, new_cursor)

        if not pending:
            yield DeltaBatch(
                records=[],
                next_checkpoint=_encode_checkpoint(running),
                is_complete=True,
            )
            return

        total_batches = sum(
            (len(recs) + self.batch_size - 1) // self.batch_size for _, recs, _ in pending
        )
        emitted = 0
        for target, records, new_cursor in pending:
            for start in range(0, len(records), self.batch_size):
                page = records[start : start + self.batch_size]
                running[target.app_id] = self._advance_cursor(running.get(target.app_id), page)
                emitted += 1
                yield DeltaBatch(
                    records=page,
                    next_checkpoint=_encode_checkpoint(running),
                    is_complete=(emitted == total_batches),
                )
            running[target.app_id] = new_cursor

    # ── Operational read (diagnostics samples + logs) ────────────────────────
    def _read_operational(
        self, org_id: str, target: DotNetAppTarget, cursor: Dict[str, Any]
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Read new metric samples + log entries for one app since ``cursor``.

        Returns ``(records, new_cursor)`` — records are the changed operational
        artifacts (oldest-first), new_cursor is the advanced ``{log_offset,
        metrics_ts}``. Operational surface only — no source code.
        """
        raw = self._raw_operational(org_id, target)

        last_ts = str(cursor.get("metrics_ts", "") or "")
        samples = sorted(raw.get("metrics", []), key=lambda s: str(s.get("sample_ts", "")))
        fresh_samples = [s for s in samples if str(s.get("sample_ts", "")) > last_ts]

        last_offset = int(cursor.get("log_offset", 0) or 0)
        logs = sorted(raw.get("logs", []), key=lambda e: int(e.get("offset", 0) or 0))
        fresh_logs = [e for e in logs if int(e.get("offset", 0) or 0) > last_offset]

        records: List[Dict[str, Any]] = []
        for sample in fresh_samples:
            records.append(self._to_metric_record(target, sample))
        for entry in fresh_logs:
            records.append(self._to_log_record(target, entry))

        new_cursor = {
            "metrics_ts": str(fresh_samples[-1].get("sample_ts", "")) if fresh_samples else last_ts,
            "log_offset": int(fresh_logs[-1]["offset"]) if fresh_logs else last_offset,
        }
        return records, new_cursor

    def _advance_cursor(
        self, current: Optional[Dict[str, Any]], page: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Advance a cursor to the newest reading in a page, keeping it monotonic."""
        cur = dict(current or {"log_offset": 0, "metrics_ts": ""})
        metrics_ts = str(cur.get("metrics_ts", "") or "")
        log_offset = int(cur.get("log_offset", 0) or 0)
        for rec in page:
            if rec.get("artifact_kind") == "metrics":
                ts = str(rec.get("observed_ts", ""))
                if ts > metrics_ts:
                    metrics_ts = ts
            elif rec.get("artifact_kind") == "log":
                off = int(rec.get("log_offset", 0) or 0)
                if off > log_offset:
                    log_offset = off
        return {"log_offset": log_offset, "metrics_ts": metrics_ts}

    def _to_metric_record(self, target: DotNetAppTarget, sample: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one diagnostics sample into a change-delta record (with provenance).

        Carries the normalised operational reading (health/error-rate/latency/
        throughput/heap/CPU) the running application reports about itself, plus an
        OBSERVED evidence pointer (``source_system='dotnet_app'``) and
        ``artifact_id``/``change_kind`` so the runner can emit
        ``ingestion.artifact_changed``. Resource gauges use neutral field names
        (``memory_used_ratio`` / ``cpu_usage``).
        """
        ts = str(sample.get("sample_ts", ""))
        return {
            "artifact_id": f"{target.app_id}:metrics:{ts}",
            "change_kind": ChangeKind.CREATED,
            "source_system": "dotnet_app",
            "app_id": target.app_id,
            "service": target.service,
            "artifact_kind": "metrics",
            "observed_ts": ts,
            "diagnostics_url": target.diagnostics_url,
            "health": sample.get("health"),
            "error_rate": sample.get("error_rate"),
            "latency_p95_ms": sample.get("latency_p95_ms"),
            "throughput_rpm": sample.get("throughput_rpm"),
            "memory_used_ratio": sample.get("memory_used_ratio"),
            "cpu_usage": sample.get("cpu_usage"),
            "evidence_pointer": build_evidence_pointer(target.app_id, "metrics", ts, ts),
        }

    def _to_log_record(self, target: DotNetAppTarget, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one application log entry into a change-delta record (with provenance).

        Carries structured log *signal* only — level (normalised .NET LogLevel),
        logger/category, exception type, offset, retry flag, and the message text
        passed through for error-pattern counting. No log-message NLP is done.
        """
        offset = int(entry.get("offset", 0) or 0)
        ts = str(entry.get("ts", ""))
        return {
            "artifact_id": f"{target.app_id}:log:{offset}",
            "change_kind": ChangeKind.CREATED,
            "source_system": "dotnet_app",
            "app_id": target.app_id,
            "service": target.service,
            "artifact_kind": "log",
            "observed_ts": ts,
            "log_offset": offset,
            "log_source": target.log_source,
            "level": _normalize_dotnet_level(entry.get("level")),
            "logger": entry.get("logger"),
            "exception_type": entry.get("exception_type"),
            "retry": bool(entry.get("retry", False)),
            "message": entry.get("message", ""),
            "evidence_pointer": build_evidence_pointer(target.app_id, "log", str(offset), ts),
        }

    # ── Source access: offline fixture vs live health/EventCounters read ─────
    def _raw_operational(self, org_id: str, target: DotNetAppTarget) -> Dict[str, Any]:
        """Return ``{"metrics": [...], "logs": [...]}`` for one app.

        Offline reads the deterministic fixture; live samples the health checks +
        EventCounters and tails the log source using the vault-resolved credential.
        The live client's HTTP session is always closed after the read.
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
        """Build a live client from the vault-resolved credential (never from config)."""
        secret = resolve_secret(org_id, target)
        return DotNetAppClient(
            diagnostics_url=target.diagnostics_url,
            log_source=target.log_source,
            secret=secret,
        )


class DotNetAppClient:
    """Thin live client for one .NET application's operational surface.

    Reads the ASP.NET Core health-checks endpoint and the EventCounters/diagnostics
    surface over HTTP, normalising them onto the neutral sample/log shape the signal
    layer consumes. The credential, when present, is sent as a Bearer header, held
    only for the life of the request session and never logged. Operational surface
    only — no path reads source code. Use as a context manager so the underlying
    ``requests.Session`` is released after the read.
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
        fixture provides. Network/transport errors are raised as
        :class:`DotNetAppIngestError` with no secret in the message.
        """
        metrics = [self._sample_diagnostics()] if self.diagnostics_url else []
        logs = self._read_logs() if self.log_source else []
        return {"metrics": metrics, "logs": logs}

    def _get_json(self, path: str) -> Optional[Any]:
        resp = self._sess().get(f"{self.diagnostics_url}/{path}", timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _counters(self) -> Dict[str, float]:
        """Read the EventCounters surface into a ``{counter-name: value}`` map.

        Accepts a ``dotnet-counters`` style ``{"counters"|"Events": [{"name","value"}]}``
        list or a flat ``{"cpu-usage": 88.0, ...}`` object. Unknown shapes yield an
        empty map (every derived field then reads as "not reported").
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
                for name, value in body.items():
                    val = _as_float(value)
                    if val is not None:
                        out[str(name)] = val
        return out

    def _sample_diagnostics(self) -> Dict[str, Any]:
        """Read health + EventCounters into one normalised sample.

        Maps the .NET operational surface onto the neutral reading fields the signal
        layer consumes: ``health`` (health-checks status), ``error_rate``
        (failed-requests / total-requests), ``latency_p95_ms`` (request-duration ms),
        ``throughput_rpm`` (requests-per-second × 60, else total-requests),
        ``memory_used_ratio`` (gc-heap-size / gc-committed), ``cpu_usage`` (cpu-usage
        %, → 0..1). An absent counter yields None (not a false zero).
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
        throughput_rpm = round(rps * 60.0, 2) if rps is not None else total

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

    def _read_logs(self) -> List[Dict[str, Any]]:
        """Tail the configured log source, accepting JSON, NDJSON, or plain text."""
        resp = self._sess().get(self.log_source, timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise DotNetAppIngestError(f"log read HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if payload is not None:
            entries = payload.get("entries", payload) if isinstance(payload, dict) else payload
            if isinstance(entries, list):
                return [e for e in entries if isinstance(e, dict)]

        text = resp.text or ""
        records: List[Dict[str, Any]] = []
        offset = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            offset += 1
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        obj.setdefault("offset", offset)
                        records.append(obj)
                        continue
                except ValueError:
                    pass
            records.append({"offset": offset, "message": line, "level": _dotnet_plain_text_level(line)})
        return records


def _dotnet_plain_text_level(line: str) -> str:
    """Best-effort canonical level from a plain-text .NET log line (no NLP)."""
    upper = line.upper()
    for token in ("CRITICAL", "FATAL", "ERROR", "WARNING", "WARN",
                  "INFORMATION", "INFO", "DEBUG", "TRACE"):
        if token in upper:
            return _DOTNET_LEVEL_MAP.get(token, token)
    return ""


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
