"""
R17-A4 — .NET application change-based ingestor (operational surface).

The .NET counterpart to the Java ingestor (R17-A3). Implements the
:class:`~discovery.ingest.base.ChangeBasedIngestor` contract from R16-A1 to read
a running .NET application's OPERATIONAL surface — its health / diagnostics
surfaces (ASP.NET Core health checks, runtime metrics / EventCounters) and its
application logs — and produce observed operational SIGNAL where the application
shows runtime friction (R17-A4 §1).

This subtask's focus is **provenance (T4 / AC5)**: every signal record this
ingestor emits carries a valid, OBSERVED :class:`EvidencePointer` built through
the shared Evidence & Identity Spine (R16-B1) via
:mod:`discovery.ingest.dotnet_app_provenance` — ``source_system='dotnet_app'``, a
meaningful artifact id pinning the exact log entry/position or diagnostics/metric
sample, the observation timestamp, and ``origin='observed'``. The ingestor never
invents its own provenance format; a record's top-level ``artifact_id`` equals its
pointer's ``source_artifact`` so a .NET-supported finding traces straight back to
the operational evidence behind it.

Shared extraction (R17-A4 §2 / AC3): the signal-extraction logic is identical
between Java and .NET, so it is reused verbatim from
:mod:`discovery.ingest.java_app_signals` (:func:`build_dotnet_app_signal`); only
the collection layer here is .NET-specific, normalising .NET-native readings onto
the canonical fields the shared extractor consumes.

Built on the change-based foundation: the opaque checkpoint encodes a per-app
``{log_offset, metrics_ts, metrics_seq}`` map, so each run reads only new samples
/ log entries; an idle deployment yields an empty delta echoing its position;
a first load streams as resumable checkpointed batches; a same-timestamp sample
is ingested exactly once. ``artifact_id`` + ``change_kind`` on every record let
the shared runner emit ``ingestion.artifact_changed`` events. Offline (default)
reads ``fixtures/dotnet_app_sample.json``.

Phase-one boundary (AC8): operational surface only — never application source
code (the separate 1.8 code-and-structure phase).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import get_live_connector, is_live
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .dotnet_app_provenance import (
    SOURCE_SYSTEM,
    build_log_evidence_pointer,
    build_metric_evidence_pointer,
    log_artifact_id,
    metric_artifact_id,
)

# SHARED operational-signal extraction, reused verbatim from the Java ingestor
# (R17-A4 §2 / AC3) — the extraction shape is identical; only collection differs.
from .java_app_signals import build_java_app_signal

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dotnet_app_sample.json"

_CHECKPOINT_VERSION = 1
_DEFAULT_BATCH_SIZE = 100
_REQUEST_TIMEOUT = 30

_ENV_APP_ID = "DOTNET_APP_ID"
_ENV_DIAGNOSTICS_URL = "DOTNET_APP_DIAGNOSTICS_URL"
_ENV_LOG_SOURCE = "DOTNET_APP_LOG_SOURCE"
_ENV_TOKEN = "DOTNET_APP_TOKEN"


class DotNetAppIngestError(Exception):
    """Raised when live .NET-app ingestion fails with a clear, actionable message."""


def build_dotnet_app_signal(records: Any) -> Dict[str, Any]:
    """Operational signal for .NET records — the SHARED extraction (AC3).

    Delegates to :func:`java_app_signals.build_java_app_signal` (platform-neutral);
    the .NET records carry the canonical operational-reading fields it consumes.
    """
    return build_java_app_signal(records)


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── opaque checkpoint: per-app {log_offset, metrics_ts, metrics_seq} map ──────

def _encode_checkpoint(cursors: Dict[str, Dict[str, Any]]) -> str:
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "apps": cursors},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Tolerant decode: missing/empty/garbage → {} (safe full re-read), never raise."""
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning("dotnet_app: undecodable checkpoint; treating as first run")
        return {}
    apps = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(apps, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for app_id, cur in apps.items():
        if isinstance(cur, dict):
            decoded = {
                "log_offset": int(cur.get("log_offset", 0) or 0),
                "metrics_ts": str(cur.get("metrics_ts", "") or ""),
            }
            if cur.get("metrics_seq") is not None:
                decoded["metrics_seq"] = int(cur.get("metrics_seq") or 0)
            out[str(app_id)] = decoded
    return out


def _app_cursor(cursors: Dict[str, Dict[str, Any]], app_id: str) -> Dict[str, Any]:
    cur = cursors.get(app_id) or {}
    out: Dict[str, Any] = {
        "log_offset": int(cur.get("log_offset", 0) or 0),
        "metrics_ts": str(cur.get("metrics_ts", "") or ""),
    }
    if cur.get("metrics_seq") is not None:
        out["metrics_seq"] = int(cur.get("metrics_seq") or 0)
    return out


class DotNetAppIngestor(ChangeBasedIngestor):
    """Change-based .NET application ingestor.

    Encodes its position as a per-app ``{log_offset, metrics_ts, metrics_seq}``
    cursor map (opaque to the runner) and yields only new metric samples / log
    entries per app. Every emitted record carries an OBSERVED provenance pointer
    (T4 / AC5) built through the shared spine. Operational surface only (AC8).
    ``reports_deletes = False``: operational artifacts are forward-only.
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
        cursors = _decode_checkpoint(since.value if since else None)
        running = {app_id: dict(cur) for app_id, cur in cursors.items()}

        targets = self._configured_dotnet_apps(org_id)
        logger.info(
            "dotnet_app: org=%s %s — %d configured application(s)",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(targets),
        )

        pending: List[tuple] = []
        for target in targets:
            app_id = str(target.get("app_id") or "")
            if not app_id:
                continue
            cursor = _app_cursor(cursors, app_id)
            records, new_cursor = self._read_operational(org_id, target, cursor)
            if records:
                pending.append((target, records, new_cursor))
            else:
                running.setdefault(app_id, new_cursor)

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
            app_id = str(target.get("app_id"))
            for start in range(0, len(records), self.batch_size):
                page = records[start : start + self.batch_size]
                running[app_id] = self._advance_cursor(running.get(app_id), page)
                emitted += 1
                yield DeltaBatch(
                    records=page,
                    next_checkpoint=_encode_checkpoint(running),
                    is_complete=(emitted == total_batches),
                )
            running[app_id] = new_cursor

    # ── operational read (.NET diagnostics samples + logs) ───────────────────
    def _read_operational(
        self, org_id: str, target: Dict[str, Any], cursor: Dict[str, Any]
    ) -> "tuple[List[Dict[str, Any]], Dict[str, Any]]":
        app_id = str(target.get("app_id"))
        raw = self._raw_operational(org_id, target)

        last_ts = str(cursor.get("metrics_ts", "") or "")
        last_seq = cursor.get("metrics_seq")
        samples = sorted(raw.get("metrics", []), key=lambda s: str(s.get("sample_ts", "")))
        newer = [s for s in samples if str(s.get("sample_ts", "")) > last_ts]
        if last_ts and last_seq is not None:
            same_ts = [s for s in samples if str(s.get("sample_ts", "")) == last_ts]
            fresh_same = same_ts[int(last_seq):]
        else:
            fresh_same = []
        fresh_samples = fresh_same + newer

        last_offset = int(cursor.get("log_offset", 0) or 0)
        logs = sorted(raw.get("logs", []), key=lambda e: int(e.get("offset", 0) or 0))
        fresh_logs = [e for e in logs if int(e.get("offset", 0) or 0) > last_offset]

        records: List[Dict[str, Any]] = []
        ts_index: Dict[str, int] = {}
        if last_ts and last_seq is not None:
            ts_index[last_ts] = int(last_seq)
        for sample in fresh_samples:
            ts = str(sample.get("sample_ts", ""))
            idx = ts_index.get(ts, 0)
            records.append(self._to_metric_record(target, sample, idx))
            ts_index[ts] = idx + 1
        for entry in fresh_logs:
            records.append(self._to_log_record(target, entry))

        new_cursor: Dict[str, Any] = {
            "metrics_ts": last_ts,
            "log_offset": int(fresh_logs[-1]["offset"]) if fresh_logs else last_offset,
        }
        if fresh_samples:
            new_ts = str(samples[-1].get("sample_ts", ""))
            new_cursor["metrics_ts"] = new_ts
            new_cursor["metrics_seq"] = sum(
                1 for s in samples if str(s.get("sample_ts", "")) == new_ts
            )
        elif last_seq is not None:
            new_cursor["metrics_seq"] = int(last_seq)
        return records, new_cursor

    def _advance_cursor(
        self, current: Optional[Dict[str, Any]], page: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        cur = dict(current or {"log_offset": 0, "metrics_ts": ""})
        metrics_ts = str(cur.get("metrics_ts", "") or "")
        metrics_seq = int(cur.get("metrics_seq", 0) or 0)
        log_offset = int(cur.get("log_offset", 0) or 0)
        for rec in page:
            if rec.get("artifact_kind") == "metrics":
                ts = str(rec.get("observed_ts", ""))
                if ts > metrics_ts:
                    metrics_ts, metrics_seq = ts, 1
                elif ts == metrics_ts:
                    metrics_seq += 1
            elif rec.get("artifact_kind") == "log":
                off = int(rec.get("log_offset", 0) or 0)
                if off > log_offset:
                    log_offset = off
        advanced: Dict[str, Any] = {"log_offset": log_offset, "metrics_ts": metrics_ts}
        if metrics_ts:
            advanced["metrics_seq"] = metrics_seq
        return advanced

    # ── record shaping: normalise .NET readings + OBSERVED provenance (T4) ────
    def _to_metric_record(
        self, target: Dict[str, Any], sample: Dict[str, Any], seq_index: int = 0
    ) -> Dict[str, Any]:
        """Shape one .NET diagnostics sample into a record.

        ``artifact_id`` references the target app + diagnostics endpoint + sample
        time and EQUALS the provenance pointer's ``source_artifact`` (T4). The
        .NET-native readings are normalised onto the canonical fields the shared
        extractor consumes and also kept for traceability.
        """
        app_id = str(target.get("app_id"))
        ts = str(sample.get("sample_ts", ""))
        rps = _as_float(sample.get("requests_per_second"))
        throughput_rpm = round(rps * 60.0, 2) if rps is not None else None
        return {
            "artifact_id": metric_artifact_id(app_id, ts, seq_index=seq_index),
            "change_kind": ChangeKind.CREATED,
            "source_system": SOURCE_SYSTEM,
            "app_id": app_id,
            "service": target.get("service") or app_id,
            "artifact_kind": "metrics",
            "observed_ts": ts,
            "diagnostics_url": target.get("diagnostics_url"),
            "health": sample.get("health"),
            "error_rate": _as_float(sample.get("request_error_rate")),
            "latency_p95_ms": _as_float(sample.get("latency_p95_ms")),
            "throughput_rpm": throughput_rpm,
            "jvm_memory_used_ratio": _as_float(sample.get("gc_heap_used_ratio")),
            "system_cpu_usage": _as_float(sample.get("cpu_usage")),
            "dotnet_metrics": {
                "request_error_rate": sample.get("request_error_rate"),
                "requests_per_second": sample.get("requests_per_second"),
                "gc_heap_used_ratio": sample.get("gc_heap_used_ratio"),
                "cpu_usage": sample.get("cpu_usage"),
            },
            "evidence_pointer": build_metric_evidence_pointer(
                app_id, ts, seq_index=seq_index
            ),
        }

    def _to_log_record(self, target: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one .NET log entry into a record.

        ``artifact_id`` is the log's stable reference (a native event id when
        present, else the log-stream position) and equals the provenance pointer's
        ``source_artifact`` (T4).
        """
        app_id = str(target.get("app_id"))
        offset = int(entry.get("offset", 0) or 0)
        ts = str(entry.get("ts", ""))
        event_id = entry.get("event_id")
        return {
            "artifact_id": log_artifact_id(app_id, log_offset=offset, event_id=event_id),
            "change_kind": ChangeKind.CREATED,
            "source_system": SOURCE_SYSTEM,
            "app_id": app_id,
            "service": target.get("service") or app_id,
            "artifact_kind": "log",
            "observed_ts": ts,
            "log_offset": offset,
            "event_id": event_id,
            "log_source": target.get("log_source"),
            "level": entry.get("level"),
            "logger": entry.get("logger"),
            "exception_type": entry.get("exception_type"),
            "retry": bool(entry.get("retry", False)),
            "message": entry.get("message", ""),
            "evidence_pointer": build_log_evidence_pointer(
                app_id, log_offset=offset, event_id=event_id, source_timestamp=ts,
            ),
        }

    # ── configuration (minimal inline for T4; full per-deployment config is T3) ─
    def _configured_dotnet_apps(self, org_id: str) -> List[Dict[str, Any]]:
        if not is_live():
            return [self._normalize_target(t) for t in self._fixture().get("targets", [])]
        cred = get_live_connector("dotnet_app")
        diagnostics_url = (cred or {}).get("url") or os.getenv(_ENV_DIAGNOSTICS_URL, "")
        token = (cred or {}).get("token") or os.getenv(_ENV_TOKEN, "")
        if not diagnostics_url:
            logger.info("dotnet_app: no configured diagnostics target for org=%s", org_id)
            return []
        app_id = os.getenv(_ENV_APP_ID, "") or _host_of(diagnostics_url)
        return [{
            "app_id": app_id,
            "service": app_id,
            "diagnostics_url": diagnostics_url.rstrip("/"),
            "log_source": os.getenv(_ENV_LOG_SOURCE, ""),
            "_token": token,  # consumed at read time, never logged
        }]

    @staticmethod
    def _normalize_target(target: Dict[str, Any]) -> Dict[str, Any]:
        meta = target.get("metadata") or {}
        return {
            "app_id": target.get("app_id"),
            "service": meta.get("service") or target.get("service") or target.get("app_id"),
            "diagnostics_url": target.get("diagnostics_url"),
            "log_source": target.get("log_source"),
            "credential_ref": target.get("credential_ref"),
        }

    # ── source access: offline fixture vs live diagnostics / log read ─────────
    def _raw_operational(self, org_id: str, target: Dict[str, Any]) -> Dict[str, Any]:
        if not is_live():
            fixture = self._fixture()
            app_id = str(target.get("app_id"))
            return {
                "metrics": list(fixture.get("metrics", {}).get(app_id, [])),
                "logs": list(fixture.get("logs", {}).get(app_id, [])),
            }
        try:
            with self._client(org_id, target) as client:
                return client.read_operational()
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the run.
            logger.warning(
                "dotnet_app: live read failed for app=%s (%s); skipping",
                target.get("app_id"), type(exc).__name__,
            )
            return {"metrics": [], "logs": []}

    def _fixture(self) -> Dict[str, Any]:
        if not FIXTURE_PATH.exists():
            raise DotNetAppIngestError(f".NET app fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _client(self, org_id: str, target: Dict[str, Any]) -> "DotNetAppClient":
        return DotNetAppClient(
            diagnostics_url=str(target.get("diagnostics_url", "")),
            log_source=str(target.get("log_source", "")),
            secret=target.get("_token"),
        )


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return str(urlparse(url).hostname or url)
    except Exception:  # noqa: BLE001
        return url


class DotNetAppClient:
    """Thin live client for one .NET application's operational surface.

    Reads an ASP.NET Core health-check endpoint and a runtime-metrics endpoint and
    tails the application log source over HTTP. The credential is sent as a Bearer
    header, held only for the request session and never logged. Operational surface
    only (AC8). Use as a context manager so pooled connections are released.
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
        except ImportError:  # pragma: no cover
            raise DotNetAppIngestError("requests library required for live dotnet_app mode")
        if self._session is None:
            self._session = requests.Session()
            if self._secret:
                self._session.headers.update({"Authorization": f"Bearer {self._secret}"})
        return self._session

    def _get_json(self, url: str) -> Optional[Any]:
        resp = self._sess().get(url, timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def read_operational(self) -> Dict[str, Any]:
        metrics = [self._sample_diagnostics()] if self.diagnostics_url else []
        logs = self._read_logs() if self.log_source else []
        return {"metrics": metrics, "logs": logs}

    def _sample_diagnostics(self) -> Dict[str, Any]:
        from datetime import datetime, timezone

        health = self._get_json(f"{self.diagnostics_url}/health")
        status = ""
        if isinstance(health, dict):
            status = str(health.get("status", ""))
        elif isinstance(health, str):
            status = health
        counters = self._get_json(f"{self.diagnostics_url}/metrics")
        counters = counters if isinstance(counters, dict) else {}
        return {
            "sample_ts": datetime.now(timezone.utc).isoformat(),
            "health": status,
            "request_error_rate": counters.get("request-error-rate"),
            "latency_p95_ms": counters.get("request-latency-p95-ms"),
            "requests_per_second": counters.get("requests-per-second"),
            "gc_heap_used_ratio": counters.get("gc-heap-used-ratio"),
            "cpu_usage": counters.get("cpu-usage"),
        }

    def _read_logs(self) -> List[Dict[str, Any]]:
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
            records.append({"offset": offset, "message": line, "level": _plain_text_level(line)})
        return records


def _plain_text_level(line: str) -> str:
    upper = line.upper()
    for level in ("FATAL", "CRITICAL", "SEVERE", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"):
        if level in upper:
            return "ERROR" if level in ("CRITICAL", "SEVERE") else level
    return ""
