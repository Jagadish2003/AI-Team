"""
R17-A4 / T1 — .NET application change-based ingestor (operational surface).

The .NET counterpart to the Java ingestor (R17-A3), completing the operational
phase of enterprise-application ingestion. Implements the
:class:`~discovery.ingest.base.ChangeBasedIngestor` contract from R16-A1 to read
the OPERATIONAL surface of a running .NET enterprise application — its health /
diagnostics surfaces (ASP.NET Core health checks, runtime metrics /
EventCounters) and its application logs — and produce operational SIGNAL where
the application shows runtime friction (R17-A4 §1).

Deliberately parallel to Java (R17-A4 §2 / AC3)
-----------------------------------------------
The logic that turns operational data into signal — error/exception clustering,
latency-degradation and throughput-decline detection, resource-pressure flags — is
**identical** between Java and .NET, so it is SHARED, not duplicated: this module
reuses :mod:`discovery.ingest.java_app_signals` verbatim (see
:func:`build_dotnet_app_signal`). Only the COLLECTION layer differs — which
endpoints are sampled and how .NET's native readings are normalised. The .NET
collection maps its health-check status and EventCounter readings
(``request_error_rate``, ``requests_per_second``, ``gc_heap_used_ratio``,
``cpu_usage`` …) onto the canonical operational-reading fields the shared
extractor consumes (``error_rate``, ``throughput_rpm``, ``jvm_memory_used_ratio``
— read as the platform-neutral "memory used ratio" —, ``system_cpu_usage`` …).

Scope — phase one of two (AC8)
------------------------------
Operational surface ONLY. This connector reads what the *running* .NET
application reports about itself — health status, runtime metrics, and the lines
it writes to its own log. It never reads the application's SOURCE CODE (reserved
for the paired 1.8 code-and-structure phase) and never reads external APM
platforms. No repository clone, no assembly/IL inspection, no config-file reading.

Built on the change-based foundation (R16-A1 / §2, AC2)
-------------------------------------------------------
.NET operational data is incremental: logs are read forward from a position and
metrics are sampled over time. The connector encodes its read position — a
per-app ``{log_offset, metrics_ts, metrics_seq}`` map — as the opaque checkpoint
value, so each run processes only new operational data. ``metrics_seq`` is the
number of samples already consumed AT ``metrics_ts``, so a second sample sharing
a timestamp (rapid polling / coarse clocks) is ingested exactly once. An app
absent from the map is read from the beginning (resumable first load). The runner
persists/returns the value verbatim and never interprets it (R16-A1 AC5). An idle
deployment yields an empty delta echoing the incoming position.

Provenance & change events
--------------------------
Every record carries a fully-populated, OBSERVED ``evidence_pointer`` (R16-B1,
``source_system='dotnet_app'``, ``origin='observed'`` — T4 / AC5) built through
the shared Evidence & Identity Spine (not a bespoke model), plus an
``artifact_id`` and ``change_kind`` so the shared change runner emits one
``ingestion.artifact_changed`` event per changed artifact (T6 / AC7). Operational
SIGNAL is a window operation over the whole delta (:func:`build_dotnet_app_signal`,
which reuses the Java extractor), NOT per-record — a single sample cannot show a
degradation *trend*.

Configured, not auto-discovered (R17-A4 §2)
-------------------------------------------
The applications to read — their diagnostics URL and log source — are configured
per deployment; live credentials are resolved from the vault-backed per-run
context, never from config and never logged. Offline (default) reads the
deterministic ``fixtures/dotnet_app_sample.json`` so the whole pipeline runs
without credentials. (The full per-deployment config plumbing is T3; T1 uses a
minimal inline target loader over the same fixture/context shape.)

Deletes / tombstones (R16-A1 §5)
--------------------------------
``reports_deletes = False``: operational data is forward-only — a metric sample
or log line, once observed, is never deleted upstream. The gap is declared
explicitly rather than faked.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import get_live_connector, is_live
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch

# ── SHARED operational-signal extraction (R17-A4 §2 / AC3) ───────────────────
# Reused verbatim from the Java ingestor — the extraction shape is identical, so
# it is genuinely shared code, not copied. Only this module's collection layer is
# .NET-specific.
from .java_app_signals import build_java_app_signal

try:  # spine lives in the app package; tests run with backend/ on sys.path
    from app.provenance import EvidencePointer, utc_now_iso
except ModuleNotFoundError:  # pragma: no cover - repo-root import fallback
    from backend.app.provenance import EvidencePointer, utc_now_iso  # type: ignore

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dotnet_app_sample.json"

SOURCE_SYSTEM = "dotnet_app"

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default operational records per :class:`DeltaBatch` — a large first load
#: streams as many small, individually-checkpointed batches (resumable).
_DEFAULT_BATCH_SIZE = 100

_REQUEST_TIMEOUT = 30

# CLI / standalone env fallbacks (used only when no per-run credential context is
# active). Live secrets are NEVER logged.
_ENV_APP_ID = "DOTNET_APP_ID"
_ENV_DIAGNOSTICS_URL = "DOTNET_APP_DIAGNOSTICS_URL"
_ENV_LOG_SOURCE = "DOTNET_APP_LOG_SOURCE"
_ENV_TOKEN = "DOTNET_APP_TOKEN"


class DotNetAppIngestError(Exception):
    """Raised when live .NET-app ingestion fails with a clear, actionable message."""


# ─────────────────────────────────────────────────────────────────────────────
# Operational signal — REUSES the shared Java extraction (AC3)
# ─────────────────────────────────────────────────────────────────────────────

def build_dotnet_app_signal(records: Any) -> Dict[str, Any]:
    """Operational signal for .NET records — the SHARED extraction (AC3).

    Delegates to :func:`java_app_signals.build_java_app_signal`, which is platform
    -neutral (it groups by service and derives error/latency/throughput/resource/
    exception signal from the canonical operational-reading fields). The .NET
    records this ingestor emits carry those exact canonical fields, so the same
    extraction runs unchanged — shared, not duplicated (R17-A4 §2 / AC3).
    """
    return build_java_app_signal(records)


def _build_evidence_pointer(
    app_id: str, artifact_kind: str, artifact_ref: str, source_timestamp: Optional[str]
) -> Dict[str, Any]:
    """Build the R16-B1 OBSERVED EvidencePointer for one .NET operational signal.

    Uses the shared spine (:class:`app.provenance.EvidencePointer`), not a bespoke
    model. ``source_system='dotnet_app'`` distinguishes .NET operational evidence;
    ``source_artifact='{app_id}:{kind}:{ref}'`` pins the exact reading (metric
    sample time or log offset) so a .NET-grounded finding traces back to it;
    ``origin='observed'`` — measured directly, never inferred (AC5).
    """
    return EvidencePointer.observed(
        source_system=SOURCE_SYSTEM,
        source_artifact=f"{app_id}:{artifact_kind}:{artifact_ref}",
        source_timestamp=source_timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# Opaque checkpoint: a per-app {log_offset, metrics_ts, metrics_seq} map.
# (Same shape/semantics as the Java ingestor so the pair stays consistent.)
# ─────────────────────────────────────────────────────────────────────────────

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


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class DotNetAppIngestor(ChangeBasedIngestor):
    """Change-based .NET application ingestor (R17-A4 / T1).

    Encodes its position as a per-app ``{log_offset, metrics_ts, metrics_seq}``
    cursor map (opaque to the runner) and yields only new metric samples / log
    entries per app. Operational surface only (AC8) — reads the configured .NET
    diagnostics endpoints and log sources, never source code. ``reports_deletes =
    False``: operational artifacts are forward-only with no upstream deletion
    semantics.
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
        as resumable, individually-checkpointed batches. Incremental run: only
        metric samples newer than the stored position and log entries past the
        stored ``log_offset`` per app (AC2). An idle deployment yields a single
        empty :class:`DeltaBatch` echoing the incoming position.
        """
        cursors = _decode_checkpoint(since.value if since else None)
        running = {app_id: dict(cur) for app_id, cur in cursors.items()}

        targets = self._configured_dotnet_apps(org_id)
        logger.info(
            "dotnet_app: org=%s %s — %d configured application(s)",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(targets),
        )

        pending: List[tuple] = []  # (target, [records], new_cursor)
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

    # ── Operational read (.NET diagnostics samples + logs) ───────────────────
    def _read_operational(
        self, org_id: str, target: Dict[str, Any], cursor: Dict[str, Any]
    ) -> "tuple[List[Dict[str, Any]], Dict[str, Any]]":
        """Read new metric samples + log entries for one app since ``cursor``.

        Returns ``(records, new_cursor)``. Metric selection is sequence-aware:
        samples strictly newer than ``metrics_ts`` are fresh; samples sharing
        ``metrics_ts`` are fresh only beyond the ``metrics_seq`` already consumed
        (so a same-timestamp late arrival is ingested once). Operational surface
        only — no source code (AC8).
        """
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
        """Advance a cursor to the newest reading in a page (keeps checkpoint monotonic)."""
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

    # ── Record shaping: normalise .NET readings into the canonical shared shape ─
    def _to_metric_record(
        self, target: Dict[str, Any], sample: Dict[str, Any], seq_index: int = 0
    ) -> Dict[str, Any]:
        """Shape one .NET diagnostics sample into a change-delta record (T1/T4).

        The COLLECTION-layer normalisation (R17-A4 §2): .NET-native readings
        (``request_error_rate``, ``requests_per_second``, ``gc_heap_used_ratio``,
        ``cpu_usage``, health-check status) are mapped onto the CANONICAL fields
        the shared extractor consumes (``error_rate``, ``throughput_rpm``,
        ``jvm_memory_used_ratio`` = the platform-neutral memory-used ratio,
        ``system_cpu_usage``, ``health``) so the same extraction runs unchanged.
        The native readings are also kept for .NET traceability. Carries an
        OBSERVED evidence pointer + artifact_id/change_kind for the runner's event.
        """
        app_id = str(target.get("app_id"))
        ts = str(sample.get("sample_ts", ""))
        ref = ts if seq_index <= 0 else f"{ts}:{seq_index}"

        rps = _as_float(sample.get("requests_per_second"))
        throughput_rpm = round(rps * 60.0, 2) if rps is not None else None

        return {
            "artifact_id": f"{app_id}:metrics:{ref}",
            "change_kind": ChangeKind.CREATED,  # a new sample is a new observation
            "source_system": SOURCE_SYSTEM,
            "app_id": app_id,
            "service": target.get("service") or app_id,
            "artifact_kind": "metrics",
            "observed_ts": ts,
            "diagnostics_url": target.get("diagnostics_url"),
            # Canonical operational-reading fields the SHARED extractor consumes:
            "health": sample.get("health"),
            "error_rate": _as_float(sample.get("request_error_rate")),
            "latency_p95_ms": _as_float(sample.get("latency_p95_ms")),
            "throughput_rpm": throughput_rpm,
            "jvm_memory_used_ratio": _as_float(sample.get("gc_heap_used_ratio")),
            "system_cpu_usage": _as_float(sample.get("cpu_usage")),
            # .NET-native readings kept for traceability (collection layer):
            "dotnet_metrics": {
                "request_error_rate": sample.get("request_error_rate"),
                "requests_per_second": sample.get("requests_per_second"),
                "gc_heap_used_ratio": sample.get("gc_heap_used_ratio"),
                "cpu_usage": sample.get("cpu_usage"),
            },
            "evidence_pointer": _build_evidence_pointer(app_id, "metrics", ref, ts),
        }

    def _to_log_record(self, target: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one .NET application log entry into a change-delta record (T1/T4).

        Carries structured log signal only — level, logger, exception type, offset,
        the message text passed through for error-pattern counting. No log-message
        NLP (AC8). Carries an OBSERVED evidence pointer + artifact_id/change_kind.
        """
        app_id = str(target.get("app_id"))
        offset = int(entry.get("offset", 0) or 0)
        ts = str(entry.get("ts", ""))
        return {
            "artifact_id": f"{app_id}:log:{offset}",
            "change_kind": ChangeKind.CREATED,
            "source_system": SOURCE_SYSTEM,
            "app_id": app_id,
            "service": target.get("service") or app_id,
            "artifact_kind": "log",
            "observed_ts": ts,
            "log_offset": offset,
            "log_source": target.get("log_source"),
            "level": entry.get("level"),
            "logger": entry.get("logger"),
            "exception_type": entry.get("exception_type"),
            "retry": bool(entry.get("retry", False)),
            "message": entry.get("message", ""),
            "evidence_pointer": _build_evidence_pointer(app_id, "log", str(offset), ts),
        }

    # ── Configuration (minimal inline for T1; full per-deployment config is T3) ─
    def _configured_dotnet_apps(self, org_id: str) -> List[Dict[str, Any]]:
        """Return the .NET applications configured for this deployment.

        Offline (default): the fixture's ``targets`` list, each carrying its
        ``app_id``, service metadata, diagnostics URL and log source. Live: the
        single target published to the per-run credential context for
        ``dotnet_app`` (diagnostics URL + token, vault-backed), with a
        CLI/standalone env fallback. AgentIQ never scans the network — the customer
        points it at the applications in scope (R17-A4 §2). Credentials are read at
        use-time and never logged.
        """
        if not is_live():
            targets = list(self._fixture().get("targets", []))
            return [self._normalize_target(t) for t in targets]

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
        """Flatten a fixture/config target into the shape the ingestor reads."""
        meta = target.get("metadata") or {}
        return {
            "app_id": target.get("app_id"),
            "service": meta.get("service") or target.get("service") or target.get("app_id"),
            "diagnostics_url": target.get("diagnostics_url"),
            "log_source": target.get("log_source"),
            "credential_ref": target.get("credential_ref"),
        }

    # ── Source access: offline fixture vs live diagnostics / log read ─────────
    def _raw_operational(self, org_id: str, target: Dict[str, Any]) -> Dict[str, Any]:
        """Return ``{"metrics": [...], "logs": [...]}`` for one app.

        Offline reads the deterministic fixture; live samples the .NET diagnostics
        endpoints and tails the log source using the vault-resolved credential.
        Operational surface only — no source code (AC8). The live client's HTTP
        session is always closed after the read (no connection leak).
        """
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

    Reads an ASP.NET Core health-check endpoint (``/health`` → ``{status,
    entries}``) and a runtime-metrics / EventCounters endpoint, and tails the
    application log source over HTTP. The credential, when present, is sent as a
    Bearer header, held only for the life of the request session and never logged.
    Operational surface only — no path reads source code (AC8). Use as a context
    manager so the pooled connections are released after the read.
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
        """Sample the diagnostics endpoints and tail the log source.

        Returns the same ``{"metrics": [...], "logs": [...]}`` shape the offline
        fixture provides (in .NET-native field names, normalised later by the
        ingestor), so downstream logic is identical in both modes.
        """
        metrics = [self._sample_diagnostics()] if self.diagnostics_url else []
        logs = self._read_logs() if self.log_source else []
        return {"metrics": metrics, "logs": logs}

    def _sample_diagnostics(self) -> Dict[str, Any]:
        """Read health-check status + EventCounter readings into one native sample.

        ASP.NET Core health checks expose ``/health`` (status: Healthy | Degraded |
        Unhealthy). Runtime metrics are read from a metrics endpoint returning a
        ``{counter_name: value}`` map (dotnet-counters / prometheus-style export).
        Any absent reading is left None — "not reported", not a false zero.
        """
        from datetime import datetime, timezone

        health = self._get_json(f"{self.diagnostics_url}/health")
        status = ""
        if isinstance(health, dict):
            status = str(health.get("status", ""))
        elif isinstance(health, str):
            status = health

        counters = self._get_json(f"{self.diagnostics_url}/metrics")
        counters = counters if isinstance(counters, dict) else {}

        def _c(name: str) -> Any:
            return counters.get(name)

        return {
            "sample_ts": datetime.now(timezone.utc).isoformat(),
            "health": status,
            "request_error_rate": _c("request-error-rate"),
            "latency_p95_ms": _c("request-latency-p95-ms"),
            "requests_per_second": _c("requests-per-second"),
            "gc_heap_used_ratio": _c("gc-heap-used-ratio"),
            "cpu_usage": _c("cpu-usage"),
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
            records.append({"offset": offset, "message": line, "level": _plain_text_level(line)})
        return records


def _plain_text_level(line: str) -> str:
    """Best-effort log level from a plain-text log line (no NLP — AC8)."""
    upper = line.upper()
    for level in ("FATAL", "CRITICAL", "SEVERE", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"):
        if level in upper:
            return "ERROR" if level in ("CRITICAL", "SEVERE") else level
    return ""
