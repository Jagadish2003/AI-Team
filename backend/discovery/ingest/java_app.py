"""
R17-A3 / T1 — Java application change-based ingestor (operational surface).

Implements the :class:`~discovery.ingest.base.ChangeBasedIngestor` contract from
R16-A1 for custom enterprise **Java applications** — AgentIQ's first non-SaaS,
non-database enterprise source (R17-A3, the 2.0 "discovery across at least one
non-SaaS enterprise data source" release-gate criterion).

What it reads — the OPERATIONAL surface only (doc Section 1)
-----------------------------------------------------------
1. **Framework health / diagnostics endpoints** — Spring Boot Actuator
   (``/health``, ``/metrics``, ``/info``): live service health, runtime metrics
   (error rate, latency, throughput, heap/CPU pressure) and application metadata.
2. **Application logs** — error patterns, exceptions, retry/failure signals and
   process-level friction visible in what the app logs over time.

From these two surfaces it produces *observed* operational SIGNAL: where a running
Java application shows runtime friction an agent could help address.

Phase-one boundary (AC8) — NO source code
------------------------------------------
This ingestor reads ONLY what a *running* application exposes about itself
(diagnostics endpoints + logs). It never reads the application's source code or
analyses its structure — that is the separate Release 1.8 code-and-structure
story. External APM/observability platforms are likewise out of phase one (a
possible later extension). The surfaces this connector will ever emit are fixed
to :data:`OPERATIONAL_SURFACES`.

Built on the change-based foundation (doc Section 2)
----------------------------------------------------
Logs are inherently incremental (read forward from an offset) and metrics
endpoints are sampled over time, so the connector encodes its read position — a
per-application ``(log_offset, last_sample_ts)`` pair — as the OPAQUE checkpoint
value. Each run processes only new log entries and fresh Actuator samples since
the checkpoint rather than re-reading history (AC2). An idle application yields no
records (an empty delta that echoes the incoming position).

Provenance (doc Section 3 / R16-B1)
-----------------------------------
Every record carries a fully-populated, OBSERVED :class:`EvidencePointer`
(``source_system='java_app'``, the artifact id, the sample/log timestamp,
``origin='observed'``) — operational signal is directly measured, so it is
first-class evidence, never inferred. ``artifact_id`` + ``change_kind`` on every
record let the shared runner (``change_runner.py``, AT-381) emit
``ingestion.artifact_changed`` events; this connector never emits them itself.

Configuration, not auto-discovery (doc Section 2)
-------------------------------------------------
AgentIQ does not scan the network for Java apps. The applications in scope — their
Actuator base URL and log source — are configured per deployment; live
credentials are read from the per-run credential context (vault-backed) and are
never logged. Offline (the default) reads the deterministic
``fixtures/java_app_sample.json`` for parity with the other connectors.

Scope note: the deeper signal extraction (trend/clustering — T2), the
corroboration feed (T5), the per-deployment config plumbing into the run
(T3/runner wiring) and event emission (T6, handled by the shared runner) are
separate subtasks. This file is T1: the ingestor that reads the two operational
surfaces incrementally and emits observed signal records.
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

try:  # provenance lives in the app package; tests run with backend/ on sys.path
    from app.provenance import EvidencePointer
except ModuleNotFoundError:  # pragma: no cover - repo-root import fallback
    from backend.app.provenance import EvidencePointer  # type: ignore

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "java_app_sample.json"

#: The operational surfaces this connector reads — and the ONLY surfaces it will
#: ever emit. Application source code is deliberately absent (AC8): it belongs to
#: the separate Release 1.8 code-and-structure phase. A test pins this set so the
#: phase-one boundary cannot regress.
OPERATIONAL_SURFACES = ("actuator", "logs")

#: Opaque-checkpoint schema version, so a future shape change is detectable.
_CHECKPOINT_VERSION = 1

#: Records emitted per :class:`DeltaBatch`. Kept modest so a large first load
#: streams as many small, individually-checkpointed batches (resumable) rather
#: than one monolithic read.
_DEFAULT_BATCH_SIZE = 100

#: Baseline friction thresholds for the operational signal block. T1 produces a
#: light, observed classification (enough to ground a finding — AC1); the deeper
#: trend/clustering analysis is T2. These are not per-run configurable here.
_ERROR_RATE_WARN = 0.05      # fraction of requests failing
_LATENCY_P99_WARN_MS = 1000.0
_HEAP_RATIO_WARN = 0.85
_CPU_USAGE_WARN = 0.85

#: Log levels that represent runtime friction (the signal in logs).
_ERROR_LEVELS = frozenset({"ERROR", "FATAL", "SEVERE"})

#: CLI / standalone env fallbacks (used only when no per-run credential context
#: is active — e.g. running the connector outside a Discovery Run). Live tokens
#: are NEVER logged.
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

    Shape::

        {"v": 1, "apps": {"payments-api": {"log_offset": 4,
                                            "sample_ts": "2026-06-28T10:00:00+00:00"}}}

    ``sort_keys`` keeps the encoding deterministic so identical state yields a
    byte-identical checkpoint (testable, diff-friendly). The runner treats the
    returned string as opaque and never interprets it (R16-A1).
    """
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "apps": apps},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Decode an opaque checkpoint back into the per-application position map.

    Tolerant by design: a missing, empty, or unparseable value yields an empty
    map (read every application from the beginning) rather than raising — a
    degenerate checkpoint must degrade to a safe full re-read, never crash a run.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning(
            "java_app: could not decode checkpoint value; treating as first run "
            "(full re-read)."
        )
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
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None."""
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
    """True when sample timestamp ``ts`` is strictly newer than ``cursor``.

    Compares parsed datetimes (robust to formatting); falls back to string
    comparison if either is unparseable. An empty cursor means "read from the
    beginning" so every sample is newer.
    """
    if not cursor:
        return True
    a, b = _parse_iso(ts), _parse_iso(cursor)
    if a is not None and b is not None:
        return a > b
    return str(ts) > str(cursor)


def _iso_sort_key(value: str) -> Any:
    """Deterministic sort key for sample timestamps (parsed dt, else the string)."""
    dt = _parse_iso(value)
    return (0, dt.isoformat()) if dt is not None else (1, str(value))


class JavaAppIngestor(ChangeBasedIngestor):
    """Change-based ingestor for a running Java application's operational surface.

    Encodes its read position as a per-application ``{log_offset, sample_ts}`` map
    (opaque to the runner) and yields only new log entries and fresh Actuator
    samples per application. A first run (``since is None``) performs a full load
    of every configured application's available operational data, streamed as
    resumable, individually-checkpointed batches; an incremental run yields only
    what changed since the checkpoint (AC2).

    Deletes / tombstones (R16-A1 §5)
    --------------------------------
    ``reports_deletes = False``: the connector polls Actuator samples forward in
    time and tails logs forward from an offset. Neither surface can observe the
    *deletion* of a previously-seen log line or sample — history only ever grows
    forward — so there are no deletions to report. The limitation is declared
    explicitly rather than silently pretending deletes are caught.
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

        First run (``since is None``): full load of each configured application's
        logs + Actuator samples, streamed as checkpointed batches. Incremental
        run: only log entries with a higher offset and Actuator samples with a
        newer timestamp than the stored per-application cursor (AC2). An idle set
        of applications yields a single empty :class:`DeltaBatch` whose
        ``next_checkpoint`` echoes the incoming position (no regression).
        """
        cursors = _decode_checkpoint(since.value if since else None)
        # Working copy advanced as records are emitted; each yielded
        # next_checkpoint encodes the cumulative map so any batch is a valid
        # resume point on the next run.
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

        # (record, app_id, cursor_field, cursor_value-after-this-record). Logs
        # before samples within an app, apps in configured order, so the running
        # cursor advances monotonically and every batch is a clean resume point.
        pending: List[tuple] = []
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
            # Idle: nothing new anywhere → single empty delta echoing the incoming
            # position. next_checkpoint must be non-empty so the runner persists it.
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

    # ── Configuration (per-deployment; doc Section 2 / T3) ───────────────────
    def _configured_java_apps(self, org_id: str) -> List[Dict[str, Any]]:
        """Return the Java applications configured for this deployment.

        Offline (default): the deterministic fixture's ``apps`` list. Live: the
        single target published to the per-run credential context for
        ``java_app`` (Actuator base URL + bearer token, vault-backed), with a
        CLI/standalone env fallback. AgentIQ never scans the network — the
        customer points it at the applications in scope (doc Section 2).
        """
        if not is_live():
            return list(self._fixture().get("apps", []))

        cred = get_live_connector("java_app")
        actuator_base = (cred or {}).get("url") or os.getenv(_ENV_ACTUATOR_URL, "")
        token = (cred or {}).get("token") or os.getenv(_ENV_TOKEN, "")
        if not actuator_base:
            # Authenticated-but-untargetable / not configured → no apps, no crash.
            logger.info("java_app: no configured Actuator target for org=%s", org_id)
            return []
        app_id = os.getenv(_ENV_APP_ID, "") or _host_of(actuator_base)
        return [{
            "app_id": app_id,
            "actuator_base": actuator_base.rstrip("/"),
            "log_source": os.getenv(_ENV_LOG_SOURCE, ""),
            "_token": token,  # consumed at read time, never logged
        }]

    # ── Record shaping (observed operational signal + provenance) ────────────
    def _to_log_record(self, app_id: str, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one new log entry into an observed operational-signal record.

        Carries the log's identity/level/message and a light friction
        classification (``is_error`` / exception type) — the deeper exception
        clustering is T2. ``artifact_id`` + ``change_kind`` make it event-ready;
        ``evidence_pointer`` (top-level, never nested) traces it to the source log.
        """
        offset = int(entry.get("offset", 0) or 0)
        ts = str(entry.get("ts", ""))
        artifact_id = f"{app_id}:log:{offset}"
        return {
            "artifact_id": artifact_id,
            "change_kind": ChangeKind.CREATED,
            "source_system": "java_app",
            "app_id": app_id,
            "surface": "logs",
            "log_offset": offset,
            "ts": ts,
            "level": str(entry.get("level", "")).upper(),
            "logger": entry.get("logger", ""),
            "message": entry.get("message", ""),
            "exception": entry.get("exception"),
            "signals": _log_signals(entry),
            "evidence_pointer": EvidencePointer.observed(
                source_system="java_app",
                source_artifact=artifact_id,
                source_timestamp=ts or None,
                source_artifact_type="record_id",
            ).to_dict(),
        }

    def _to_actuator_record(self, app_id: str, sample: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one Actuator sample (health + metrics + info) into a record.

        One record per sampled observation of the running app's operational state,
        read from the ``/health``, ``/metrics`` and ``/info`` endpoints (AC1). The
        ``signals`` block is a baseline observed friction classification (T2 adds
        trend/clustering). ``evidence_pointer`` is observed and top-level.
        """
        observed_at = str(sample.get("observed_at", ""))
        health = sample.get("health") or {}
        metrics = sample.get("metrics") or {}
        info = sample.get("info") or {}
        artifact_id = f"{app_id}:actuator:{observed_at}"
        return {
            "artifact_id": artifact_id,
            "change_kind": ChangeKind.CREATED,
            "source_system": "java_app",
            "app_id": app_id,
            "surface": "actuator",
            "observed_at": observed_at,
            "health": health,
            "metrics": metrics,
            "info": info,
            "signals": _actuator_signals(health, metrics),
            "evidence_pointer": EvidencePointer.observed(
                source_system="java_app",
                source_artifact=artifact_id,
                source_timestamp=observed_at or None,
                source_artifact_type="record_id",
            ).to_dict(),
        }

    # ── Source access: offline fixture vs live application ───────────────────
    def _read_actuator_samples(
        self, org_id: str, app: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Actuator samples for one application — fixture offline, live otherwise."""
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
        """Log entries for one application — fixture offline, live otherwise."""
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
        """Build a best-effort live reader for one configured application.

        The bearer token is read from the per-deployment config (vault-backed,
        resolved into the app dict) and is never stored on the ingestor nor logged.
        """
        return JavaAppClient(
            actuator_base=str(app.get("actuator_base", "")).rstrip("/"),
            log_source=str(app.get("log_source", "")),
            token=str(app.get("_token", "")),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Operational signal classification (baseline, observed). T2 adds trend/cluster.
# ─────────────────────────────────────────────────────────────────────────────

def _actuator_signals(health: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Baseline observed friction read from an Actuator sample (doc Section 1)."""
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
    """Baseline observed friction read from a single log entry (doc Section 1)."""
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
    """Best-effort host label from a URL, for use as a fallback app id."""
    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or url
        return str(host)
    except Exception:  # noqa: BLE001
        return url


class JavaAppClient:
    """Thin best-effort reader for a live Java application's operational surface.

    Only used in live mode (``INGEST_MODE=live``). Reads Spring Boot Actuator
    ``/health``, ``/info`` and a small set of known metric names, plus a log
    source. Every failure raises :class:`JavaAppIngestError`, which the ingestor
    catches and degrades to "skip this app" — a Java application being unreachable
    must never break the whole discovery run. The bearer token is held only for
    the lifetime of the request and never logged.
    """

    #: Actuator metric names mapped to the canonical keys the signal block reads.
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
        except ImportError:  # pragma: no cover - requests ships in requirements
            raise JavaAppIngestError("requests library required for live java_app mode")
        if self._session is None:
            self._session = requests.Session()
            if self._token:
                self._session.headers.update(
                    {"Authorization": f"Bearer {self._token}"}
                )
        return self._session

    def _get(self, path: str) -> Dict[str, Any]:
        if not self.actuator_base:
            raise JavaAppIngestError("no Actuator base URL configured")
        resp = self._sess().get(f"{self.actuator_base}/{path}", timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            # Status only — never include the URL (it can carry credentials).
            raise JavaAppIngestError(f"Actuator /{path} HTTP {resp.status_code}")
        return resp.json()

    def fetch_actuator_sample(self) -> List[Dict[str, Any]]:
        """Read /health, /info (+ best-effort metrics) as one current sample."""
        health = self._get("health")
        try:
            info = self._get("info")
        except JavaAppIngestError:
            info = {}
        metrics: Dict[str, Any] = {}
        # Actuator exposes metrics individually under /metrics/{name}; read the
        # known ones best-effort so a missing meter does not fail the sample.
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
        """Live log reading is deployment-specific (file/agent/endpoint).

        Phase one ships the offline fixture path as the deterministic
        implementation; a live log adapter is wired per deployment. Returns an
        empty list when no live log adapter is configured rather than guessing.
        """
        return []
