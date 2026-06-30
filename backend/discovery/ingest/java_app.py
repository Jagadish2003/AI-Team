"""
R17-A3 — Java application change-based ingestor (operational surface).

Implements the :class:`~discovery.ingest.base.ChangeBasedIngestor` contract from
R16-A1 for custom enterprise Java applications — AgentIQ's first non-SaaS,
non-database enterprise source. It reads the OPERATIONAL surface of a running
Java app (Spring Boot Actuator health/metrics/info samples + application logs)
and produces *observed* operational signal: where the application shows runtime
friction (error rates, latency degradation, recurring exceptions, resource
pressure).

This subtask's focus is **provenance (T4 / AC4)**: every signal record this
ingestor emits carries a valid, OBSERVED :class:`EvidencePointer` built through
the shared Evidence & Identity Spine (R16-B1) via :mod:`discovery.ingest.java_app_provenance`
— ``source_system='java_app'``, a meaningful artifact id pinning the exact log
position/event or diagnostics endpoint+sample, the observation timestamp, and
``origin='observed'``. The ingestor never invents its own provenance format. A
record's top-level ``artifact_id`` equals its pointer's ``source_artifact`` so a
Java-grounded finding is traceable straight back to the runtime source.

Built on the change-based foundation: the opaque checkpoint encodes a
per-application ``(log_offset, last_sample_ts)`` map, so each run reads only new
log entries and fresh Actuator samples; an idle application yields an empty delta
echoing its position. ``artifact_id`` + ``change_kind`` on every record let the
shared runner emit ``ingestion.artifact_changed`` events. Offline (the default)
reads the deterministic ``fixtures/java_app_sample.json``.

Phase-one boundary: reads the operational surface only — never application source
code (the separate Release 1.8 code-and-structure phase).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import get_live_connector, is_live
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .java_app_provenance import (
    SOURCE_SYSTEM,
    actuator_artifact_id,
    build_actuator_evidence_pointer,
    build_log_evidence_pointer,
    log_artifact_id,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "java_app_sample.json"

#: The operational surfaces this connector reads — the ONLY surfaces it emits.
#: Application source code is deliberately absent (the Release 1.8 phase).
OPERATIONAL_SURFACES = ("actuator", "logs")

_CHECKPOINT_VERSION = 1
_DEFAULT_BATCH_SIZE = 100

# Baseline friction thresholds for the operational signal block (deeper trend /
# clustering analysis is the separate T2 subtask).
_ERROR_RATE_WARN = 0.05
_LATENCY_P99_WARN_MS = 1000.0
_HEAP_RATIO_WARN = 0.85
_CPU_USAGE_WARN = 0.85
_ERROR_LEVELS = frozenset({"ERROR", "FATAL", "SEVERE"})

# CLI / standalone env fallbacks (used only when no per-run credential context is
# active). Live tokens are NEVER logged.
_ENV_APP_ID = "JAVA_APP_ID"
_ENV_ACTUATOR_URL = "JAVA_APP_ACTUATOR_URL"
_ENV_LOG_SOURCE = "JAVA_APP_LOG_SOURCE"
_ENV_TOKEN = "JAVA_APP_TOKEN"
_REQUEST_TIMEOUT = 30


class JavaAppIngestError(Exception):
    """Raised when live Java-app ingestion fails with a clear, actionable message."""


# ─────────────────────────────────────────────────────────────────────────────
# Opaque checkpoint: a per-application {log_offset, last sample ts} map.
# ─────────────────────────────────────────────────────────────────────────────

def _encode_checkpoint(apps: Dict[str, Dict[str, Any]]) -> str:
    """Encode the per-application position map as the opaque checkpoint value.

    ``sort_keys`` keeps the encoding deterministic (identical state => identical
    string). The runner treats the value as opaque and never interprets it.
    """
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "apps": apps},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Decode an opaque checkpoint back into the per-application position map.

    Tolerant: a missing, empty, or unparseable value yields an empty map (safe
    full re-read) rather than raising.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning("java_app: undecodable checkpoint; treating as first run")
        return {}
    apps = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(apps, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for app_id, pos in apps.items():
        if not isinstance(pos, dict):
            continue
        out[str(app_id)] = {
            "log_offset": int(pos.get("log_offset", 0) or 0),
            "sample_ts": str(pos.get("sample_ts", "") or ""),
        }
    return out


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ts_newer(ts: str, cursor: Optional[str]) -> bool:
    """True when sample timestamp ``ts`` is strictly newer than ``cursor``."""
    if not cursor:
        return True
    a, b = _parse_iso(ts), _parse_iso(cursor)
    if a is not None and b is not None:
        return a > b
    return str(ts) > str(cursor)


def _iso_sort_key(value: str) -> Any:
    dt = _parse_iso(value)
    return (0, dt.isoformat()) if dt is not None else (1, str(value))


class JavaAppIngestor(ChangeBasedIngestor):
    """Change-based ingestor for a running Java application's operational surface.

    Encodes its read position as a per-application ``{log_offset, sample_ts}`` map
    (opaque to the runner) and yields only new log entries and fresh Actuator
    samples per application. Every emitted record carries an OBSERVED provenance
    pointer (T4 / AC4) built through the shared spine.

    ``reports_deletes = False``: the connector polls samples forward in time and
    tails logs forward from an offset; neither surface can observe the deletion of
    a previously-seen line or sample, so there are no deletions to report. The
    limitation is declared explicitly rather than silently unhandled.
    """

    connector_id = "java_app"
    reports_deletes = False

    def __init__(self, batch_size: int = _DEFAULT_BATCH_SIZE):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size

    # ── ChangeBasedIngestor contract ────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of new operational signal records since ``since``.

        First run (``since is None``) loads each configured application's logs +
        Actuator samples, streamed as resumable checkpointed batches; an
        incremental run yields only what is newer than the stored per-application
        cursor. An idle set of applications yields a single empty
        :class:`DeltaBatch` echoing the incoming position.
        """
        cursors = _decode_checkpoint(since.value if since else None)
        running: Dict[str, Dict[str, Any]] = {
            app_id: dict(pos) for app_id, pos in cursors.items()
        }

        apps = self._configured_java_apps(org_id)
        logger.info(
            "java_app: org=%s %s — %d configured application(s)",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(apps),
        )

        pending: List[tuple] = []  # (record, app_id, cursor_field, value-after)
        for app in apps:
            app_id = str(app.get("app_id") or "")
            if not app_id:
                continue
            pos = cursors.get(app_id, {})
            log_cur = int(pos.get("log_offset", 0) or 0)
            sample_cur = str(pos.get("sample_ts", "") or "")

            new_logs = sorted(
                (e for e in self._read_logs(org_id, app)
                 if int(e.get("offset", 0) or 0) > log_cur),
                key=lambda e: int(e.get("offset", 0) or 0),
            )
            for entry in new_logs:
                pending.append(
                    (self._to_log_record(app_id, entry),
                     app_id, "log_offset", int(entry.get("offset", 0) or 0))
                )

            new_samples = sorted(
                (s for s in self._read_actuator_samples(org_id, app)
                 if _ts_newer(str(s.get("observed_at", "")), sample_cur)),
                key=lambda s: _iso_sort_key(str(s.get("observed_at", ""))),
            )
            for sample in new_samples:
                pending.append(
                    (self._to_actuator_record(app_id, sample),
                     app_id, "sample_ts", str(sample.get("observed_at", "")))
                )

        if not pending:
            yield DeltaBatch(
                records=[],
                next_checkpoint=_encode_checkpoint(running or cursors),
                is_complete=True,
            )
            return

        total_batches = (len(pending) + self.batch_size - 1) // self.batch_size
        emitted = 0
        for start in range(0, len(pending), self.batch_size):
            page = pending[start : start + self.batch_size]
            records = []
            for record, app_id, field, value in page:
                records.append(record)
                slot = running.setdefault(app_id, {"log_offset": 0, "sample_ts": ""})
                slot[field] = value
            emitted += 1
            yield DeltaBatch(
                records=records,
                next_checkpoint=_encode_checkpoint(running),
                is_complete=(emitted == total_batches),
            )

    # ── Configuration (per-deployment; configured, not auto-discovered) ──────
    def _configured_java_apps(self, org_id: str) -> List[Dict[str, Any]]:
        if not is_live():
            return list(self._fixture().get("apps", []))
        cred = get_live_connector("java_app")
        actuator_base = (cred or {}).get("url") or os.getenv(_ENV_ACTUATOR_URL, "")
        token = (cred or {}).get("token") or os.getenv(_ENV_TOKEN, "")
        if not actuator_base:
            logger.info("java_app: no configured Actuator target for org=%s", org_id)
            return []
        app_id = os.getenv(_ENV_APP_ID, "") or _host_of(actuator_base)
        return [{
            "app_id": app_id,
            "actuator_base": actuator_base.rstrip("/"),
            "log_source": os.getenv(_ENV_LOG_SOURCE, ""),
            "_token": token,  # consumed at read time, never logged
        }]

    # ── Record shaping: observed operational signal + spine provenance (T4) ──
    def _to_log_record(self, app_id: str, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one new log entry into an observed operational-signal record.

        ``artifact_id`` is the log's stable artifact reference (a native event id
        when present, else the log position) and equals the provenance pointer's
        ``source_artifact`` so the signal traces straight back to the log source.
        """
        offset = int(entry.get("offset", 0) or 0)
        ts = str(entry.get("ts", ""))
        event_id = entry.get("event_id")
        artifact_id = log_artifact_id(app_id, log_offset=offset, event_id=event_id)
        return {
            "artifact_id": artifact_id,
            "change_kind": ChangeKind.CREATED,
            "source_system": SOURCE_SYSTEM,
            "app_id": app_id,
            "surface": "logs",
            "log_offset": offset,
            "event_id": event_id,
            "ts": ts,
            "level": str(entry.get("level", "")).upper(),
            "logger": entry.get("logger", ""),
            "message": entry.get("message", ""),
            "exception": entry.get("exception"),
            "signals": _log_signals(entry),
            "evidence_pointer": build_log_evidence_pointer(
                app_id, log_offset=offset, event_id=event_id, source_timestamp=ts,
            ),
        }

    def _to_actuator_record(self, app_id: str, sample: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one Actuator sample (health + metrics + info) into a record.

        ``artifact_id`` references the target app + actuator endpoint + sample
        time and equals the provenance pointer's ``source_artifact``.
        """
        observed_at = str(sample.get("observed_at", ""))
        health = sample.get("health") or {}
        metrics = sample.get("metrics") or {}
        info = sample.get("info") or {}
        artifact_id = actuator_artifact_id(app_id, observed_at)
        return {
            "artifact_id": artifact_id,
            "change_kind": ChangeKind.CREATED,
            "source_system": SOURCE_SYSTEM,
            "app_id": app_id,
            "surface": "actuator",
            "observed_at": observed_at,
            "health": health,
            "metrics": metrics,
            "info": info,
            "signals": _actuator_signals(health, metrics),
            "evidence_pointer": build_actuator_evidence_pointer(app_id, observed_at),
        }

    # ── Source access: offline fixture vs live application ───────────────────
    def _read_actuator_samples(
        self, org_id: str, app: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        if not is_live():
            app_id = str(app.get("app_id") or "")
            return list(self._fixture().get("actuator_samples", {}).get(app_id, []))
        try:
            return self._live_client(app).fetch_actuator_sample()
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the run.
            logger.warning(
                "java_app: live Actuator read failed for app=%s (%s); skipping",
                app.get("app_id"), type(exc).__name__,
            )
            return []

    def _read_logs(self, org_id: str, app: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not is_live():
            app_id = str(app.get("app_id") or "")
            return list(self._fixture().get("logs", {}).get(app_id, []))
        try:
            return self._live_client(app).fetch_logs()
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the run.
            logger.warning(
                "java_app: live log read failed for app=%s (%s); skipping",
                app.get("app_id"), type(exc).__name__,
            )
            return []

    def _fixture(self) -> Dict[str, Any]:
        if not FIXTURE_PATH.exists():
            raise JavaAppIngestError(f"Java app fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _live_client(self, app: Dict[str, Any]) -> "JavaAppClient":
        return JavaAppClient(
            actuator_base=str(app.get("actuator_base", "")).rstrip("/"),
            log_source=str(app.get("log_source", "")),
            token=str(app.get("_token", "")),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Operational signal classification (baseline, observed). T2 adds trend/cluster.
# ─────────────────────────────────────────────────────────────────────────────

def _actuator_signals(health: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
    status = str((health or {}).get("status", "UNKNOWN")).upper()
    m = metrics or {}

    def _num(key: str) -> float:
        try:
            return float(m.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    error_rate = _num("http.server.requests.error.rate")
    latency_p99 = _num("http.server.requests.p99.ms")
    throughput = _num("http.server.requests.throughput.rps")
    heap_ratio = _num("jvm.memory.heap.used.ratio")
    cpu_usage = _num("system.cpu.usage")

    unhealthy = status not in ("UP", "UNKNOWN", "")
    friction: List[str] = []
    if unhealthy:
        friction.append("unhealthy")
    if error_rate > _ERROR_RATE_WARN:
        friction.append("error_rate_elevated")
    if latency_p99 > _LATENCY_P99_WARN_MS:
        friction.append("latency_degraded")
    if heap_ratio > _HEAP_RATIO_WARN:
        friction.append("heap_pressure")
    if cpu_usage > _CPU_USAGE_WARN:
        friction.append("cpu_pressure")
    return {
        "health_status": status,
        "unhealthy": unhealthy,
        "error_rate": error_rate,
        "latency_p99_ms": latency_p99,
        "throughput_rps": throughput,
        "heap_used_ratio": heap_ratio,
        "cpu_usage": cpu_usage,
        "friction": friction,
        "has_friction": bool(friction),
    }


def _log_signals(entry: Dict[str, Any]) -> Dict[str, Any]:
    level = str(entry.get("level", "")).upper()
    exception = entry.get("exception")
    is_error = level in _ERROR_LEVELS
    has_exception = bool(exception)
    exception_type = None
    if has_exception:
        exception_type = str(exception).split(":", 1)[0].strip() or None
    if is_error:
        friction = ["error_log"]
    elif level == "WARN":
        friction = ["warning_log"]
    else:
        friction = []
    return {
        "level": level,
        "is_error": is_error,
        "has_exception": has_exception,
        "exception_type": exception_type,
        "friction": friction,
        "has_friction": bool(friction),
    }


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return str(urlparse(url).hostname or url)
    except Exception:  # noqa: BLE001
        return url


class JavaAppClient:
    """Best-effort live reader for a Java application's operational surface.

    Only used in live mode. Every failure raises :class:`JavaAppIngestError`,
    which the ingestor catches and degrades to "skip this app" — an unreachable
    application must never break the whole discovery run. The bearer token is held
    only for the request and never logged.
    """

    _METRIC_MAP = {
        "http.server.requests": "http.server.requests.p99.ms",
        "jvm.memory.used": "jvm.memory.heap.used.ratio",
        "system.cpu.usage": "system.cpu.usage",
    }

    def __init__(self, actuator_base: str, log_source: str, token: str):
        self.actuator_base = actuator_base
        self.log_source = log_source
        self._token = token
        self._session = None

    def _sess(self):
        try:
            import requests
        except ImportError:  # pragma: no cover
            raise JavaAppIngestError("requests library required for live java_app mode")
        if self._session is None:
            self._session = requests.Session()
            if self._token:
                self._session.headers.update({"Authorization": f"Bearer {self._token}"})
        return self._session

    def _get(self, path: str) -> Dict[str, Any]:
        if not self.actuator_base:
            raise JavaAppIngestError("no Actuator base URL configured")
        resp = self._sess().get(f"{self.actuator_base}/{path}", timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise JavaAppIngestError(f"Actuator /{path} HTTP {resp.status_code}")
        return resp.json()

    def fetch_actuator_sample(self) -> List[Dict[str, Any]]:
        health = self._get("health")
        try:
            info = self._get("info")
        except JavaAppIngestError:
            info = {}
        metrics: Dict[str, Any] = {}
        for name, canonical in self._METRIC_MAP.items():
            try:
                payload = self._get(f"metrics/{name}")
                measurements = payload.get("measurements") or []
                if measurements:
                    metrics[canonical] = measurements[0].get("value")
            except JavaAppIngestError:
                continue
        return [{
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "health": health,
            "metrics": metrics,
            "info": info,
        }]

    def fetch_logs(self) -> List[Dict[str, Any]]:
        """Live log reading is deployment-specific; the offline fixture path is the
        deterministic implementation. Returns [] when no live adapter is wired."""
        return []
