"""
R17-A4 / T1 (change-based) + T6 (change events) — .NET application ingestor.

The .NET counterpart to :mod:`discovery.ingest.java_app` (R17-A3), deliberately
parallel to it (R17-A4 §6). Implements the R16-A1
:class:`~discovery.ingest.base.ChangeBasedIngestor` contract to read the
OPERATIONAL surface of a running .NET enterprise application — its
health/diagnostics endpoints (ASP.NET Core health checks, runtime metrics /
EventCounters) and its application logs — and produce operational SIGNAL where
the application shows runtime friction (R17-A4 §1).

Scope this file owns for the change-event story (T6 / AC7)
----------------------------------------------------------
This connector exists so .NET ingestion participates in the EXISTING change-based
telemetry model. It REUSES the R16-A1 mechanism rather than minting its own event
path: every record it yields carries an ``artifact_id`` and a ``change_kind``, so
when the shared change runner (``change_runner.ingest_with_checkpoint``) drives
this ingestor it emits one ``ingestion.artifact_changed`` event per changed log
artifact / fresh diagnostics sample, identifying:

    org_id, connector_id='dotnet_app', artifact_id, change_kind.

This module never imports the telemetry layer and never emits events itself — the
shared runner owns emission, which guarantees events fire ONLY for
fully-processed batches (emission happens after ``process_batch`` succeeds) and
the per-``(org_id, 'dotnet_app')`` checkpoint advances only on success.

Configured, not auto-discovered (R17-A4 §2)
-------------------------------------------
The applications to read are configured per deployment (see
:mod:`discovery.ingest.dotnet_app_config`), with credentials resolved from the
vault — never scanned from the network, never hardcoded.

Operational surface only (AC8): reads diagnostics endpoints + logs, never the
application's source code (reserved for the 1.8 code-and-structure phase).

Offline vs live
---------------
Offline (default): reads the deterministic fixture ``dotnet_app_sample.json``.
Live: samples each configured target's diagnostics endpoint and tails its log
source using the vault-resolved credential; endpoint failures are reported with
:func:`dotnet_app_config.safe_endpoint_error` (no credentials / connection
strings in logs — AC4).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.provenance import EvidencePointer, utc_now_iso

from . import is_live
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .dotnet_app_config import (
    DotNetAppTarget,
    load_targets,
    log_endpoint_failure,
    resolve_secret,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dotnet_app_sample.json"

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default number of operational records emitted per :class:`DeltaBatch`. Kept
#: modest so a large first load streams as many small, individually-checkpointed
#: batches rather than one monolithic read.
_DEFAULT_BATCH_SIZE = 100

#: Live HTTP timeout (seconds) for diagnostics / log reads.
_REQUEST_TIMEOUT = 30


class DotNetAppIngestError(Exception):
    """Raised when live .NET-app ingestion fails with a clear, actionable message."""


def _encode_checkpoint(cursors: Dict[str, Dict[str, Any]]) -> str:
    """Encode the per-app cursor map as the opaque checkpoint value.

    ``sort_keys`` keeps the encoding deterministic so two runs over identical
    state produce byte-identical checkpoints (testable, diff-friendly).
    """
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "apps": cursors},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Decode an opaque checkpoint value back into the per-app cursor map.

    Tolerant by design: a missing, empty, or unparseable value yields an empty
    map (read every app from the beginning) rather than raising — a degenerate
    checkpoint must degrade to a safe full re-read, never crash the run.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning(
            "dotnet_app: could not decode checkpoint value; treating as first run "
            "(full re-read)."
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
                "sample_ts": str(cur.get("sample_ts", "") or ""),
            }
    return out


def _app_cursor(cursors: Dict[str, Dict[str, Any]], app_id: str) -> Dict[str, Any]:
    """Return the cursor for one app, defaulting to the beginning of time."""
    cur = cursors.get(app_id) or {}
    return {
        "log_offset": int(cur.get("log_offset", 0) or 0),
        "sample_ts": str(cur.get("sample_ts", "") or ""),
    }


def _build_evidence_pointer(artifact_id: str, source_timestamp: Optional[str]) -> Dict[str, Any]:
    """Build the R16-B1 OBSERVED EvidencePointer for one .NET operational signal.

    ``source_system='dotnet_app'``, ``origin='observed'`` — operational signals
    are directly measured, so they are first-class observed evidence, never
    inferred. ``source_artifact`` is the record's own artifact id (stable), so no
    ``extraction_job_id`` is required.
    """
    return EvidencePointer.observed(
        source_system="dotnet_app",
        source_artifact=artifact_id,
        source_timestamp=source_timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


class DotNetAppIngestor(ChangeBasedIngestor):
    """Change-based .NET application ingestor (R17-A4).

    Encodes its position as a per-app ``{log_offset, sample_ts}`` cursor map
    (opaque to the runner) and yields only new diagnostics samples / log entries
    per app. A first run (``since is None``) performs a full initial load of every
    configured app, streamed as resumable, individually-checkpointed batches.

    Every record carries ``artifact_id`` + ``change_kind`` so the shared R16-A1
    change runner emits one ``ingestion.artifact_changed`` per changed artifact —
    this connector never emits events itself (T6 / AC7).

    Deletes / tombstones (R16-A1 §5): ``reports_deletes = False`` — operational
    artifacts (diagnostics samples, log lines) are forward-only and have no
    upstream deletion semantics; the limitation is declared rather than faked.
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
        """Yield batches of new .NET operational records since ``since``.

        First run (``since is None``): full load of every configured app, streamed
        as checkpointed batches. Incremental run: only diagnostics samples newer
        than the stored ``sample_ts`` and log entries past the stored
        ``log_offset`` per app (AC2). An idle deployment yields a single empty
        :class:`DeltaBatch` whose ``next_checkpoint`` echoes the incoming position.
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

        # Collect each app's pending (new) records first so we know which batch is
        # the overall final one and can flag is_complete=True on exactly that batch
        # (the runner needs one terminal batch to advance the checkpoint).
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
        """Read new diagnostics samples + log entries for one app since ``cursor``.

        Returns ``(records, new_cursor)``. Operational surface only — no source
        code (AC8). A live read failure is reported via safe error handling (no
        credentials / connection strings logged — AC4) and degrades to no records.
        """
        raw = self._raw_operational(org_id, target)

        last_ts = cursor.get("sample_ts", "")
        samples = sorted(raw.get("metrics", []), key=lambda s: str(s.get("sample_ts", "")))
        fresh_samples = [s for s in samples if str(s.get("sample_ts", "")) > last_ts]

        last_offset = int(cursor.get("log_offset", 0) or 0)
        logs = sorted(raw.get("logs", []), key=lambda e: int(e.get("offset", 0) or 0))
        fresh_logs = [e for e in logs if int(e.get("offset", 0) or 0) > last_offset]

        records: List[Dict[str, Any]] = []
        for sample in fresh_samples:
            records.append(self._to_diagnostics_record(target, sample))
        for entry in fresh_logs:
            records.append(self._to_log_record(target, entry))

        new_cursor = {
            "sample_ts": str(fresh_samples[-1]["sample_ts"]) if fresh_samples else last_ts,
            "log_offset": int(fresh_logs[-1]["offset"]) if fresh_logs else last_offset,
        }
        return records, new_cursor

    def _advance_cursor(
        self, current: Optional[Dict[str, Any]], page: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Advance a cursor to the newest reading in a page (keeps checkpoint monotonic)."""
        cur = dict(current or {"log_offset": 0, "sample_ts": ""})
        for rec in page:
            if rec.get("surface") == "diagnostics":
                ts = str(rec.get("observed_ts", ""))
                if ts > cur.get("sample_ts", ""):
                    cur["sample_ts"] = ts
            elif rec.get("surface") == "logs":
                off = int(rec.get("log_offset", 0) or 0)
                if off > int(cur.get("log_offset", 0) or 0):
                    cur["log_offset"] = off
        return cur

    def _to_diagnostics_record(self, target: DotNetAppTarget, sample: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one diagnostics sample into a change-delta record.

        Carries the normalised diagnostics reading the running .NET app reports
        about itself, plus an OBSERVED evidence pointer. ``artifact_id`` +
        ``change_kind`` let the shared runner emit ``ingestion.artifact_changed``.
        """
        ts = str(sample.get("sample_ts", ""))
        artifact_id = f"{target.app_id}:diagnostics:{ts}"
        return {
            "artifact_id": artifact_id,
            "change_kind": ChangeKind.CREATED,  # a new sample is a new observation
            "source_system": "dotnet_app",
            "app_id": target.app_id,
            "service": target.service,
            "environment": target.environment,
            "surface": "diagnostics",
            "observed_ts": ts,
            "diagnostics_url": target.diagnostics_url,
            "health": sample.get("health"),
            "error_rate": sample.get("error_rate"),
            "latency_p95_ms": sample.get("latency_p95_ms"),
            "throughput_rpm": sample.get("throughput_rpm"),
            "gc_pause_ratio": sample.get("gc_pause_ratio"),
            "cpu_usage": sample.get("cpu_usage"),
            "evidence_pointer": _build_evidence_pointer(artifact_id, ts),
        }

    def _to_log_record(self, target: DotNetAppTarget, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one application log entry into a change-delta record.

        Structured log *signal* only — level, logger, exception type, offset, and
        the message text passed through; no log-message NLP (AC8). Carries an
        OBSERVED evidence pointer + artifact_id/change_kind for the runner's event.
        """
        offset = int(entry.get("offset", 0) or 0)
        ts = str(entry.get("ts", ""))
        artifact_id = f"{target.app_id}:log:{offset}"
        return {
            "artifact_id": artifact_id,
            "change_kind": ChangeKind.CREATED,
            "source_system": "dotnet_app",
            "app_id": target.app_id,
            "service": target.service,
            "environment": target.environment,
            "surface": "logs",
            "observed_ts": ts,
            "log_offset": offset,
            "log_source": target.log_source,
            "level": entry.get("level"),
            "logger": entry.get("logger"),
            "exception_type": entry.get("exception_type"),
            "retry": bool(entry.get("retry", False)),
            "message": entry.get("message", ""),
            "evidence_pointer": _build_evidence_pointer(artifact_id, ts),
        }

    # ── Source access: offline fixture vs live diagnostics / log read ────────
    def _raw_operational(self, org_id: str, target: DotNetAppTarget) -> Dict[str, Any]:
        """Return ``{"metrics": [...], "logs": [...]}`` for one app.

        Offline reads the deterministic fixture; live samples the diagnostics
        endpoint and tails the log source using the vault-resolved credential
        (AC4). A live failure is reported safely (no credentials/connection
        strings) and degrades to empty, so one bad endpoint never aborts the run.
        """
        if not is_live():
            fixture = self._fixture()
            return {
                "metrics": list(fixture.get("metrics", {}).get(target.app_id, [])),
                "logs": list(fixture.get("logs", {}).get(target.app_id, [])),
            }
        try:
            return self._client(org_id, target).read_operational()
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash; log safely.
            log_endpoint_failure(org_id, target, "operational", exc)
            return {"metrics": [], "logs": []}

    def _fixture(self) -> Dict[str, Any]:
        if not FIXTURE_PATH.exists():
            raise DotNetAppIngestError(f".NET app fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _client(self, org_id: str, target: DotNetAppTarget) -> "DotNetAppClient":
        """Build a live client from the vault-resolved credential (AC4)."""
        secret = resolve_secret(org_id, target)
        return DotNetAppClient(
            diagnostics_url=target.diagnostics_url,
            log_source=target.log_source,
            secret=secret,
        )


class DotNetAppClient:
    """Thin live client for one .NET application's operational surface.

    Reads the health/diagnostics endpoint and the application log source over
    HTTP. The credential, when present, is sent as a Bearer header; it is held
    only for the life of the request session and never logged. Operational surface
    only — no path reads source code (AC8).
    """

    def __init__(self, *, diagnostics_url: str, log_source: str, secret: Optional[str]):
        self.diagnostics_url = diagnostics_url.rstrip("/") if diagnostics_url else ""
        self.log_source = log_source
        self._secret = secret
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

    def read_operational(self) -> Dict[str, Any]:  # pragma: no cover - exercised live only
        """Sample the diagnostics endpoint and tail the log source."""
        metrics = [self._sample_diagnostics()] if self.diagnostics_url else []
        logs = self._read_logs() if self.log_source else []
        return {"metrics": metrics, "logs": logs}

    def _sample_diagnostics(self) -> Dict[str, Any]:  # pragma: no cover - exercised live only
        from datetime import datetime, timezone

        resp = self._sess().get(f"{self.diagnostics_url}/health", timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise DotNetAppIngestError(f"diagnostics health HTTP {resp.status_code}")
        health = resp.json()
        return {
            "sample_ts": datetime.now(timezone.utc).isoformat(),
            "health": str(health.get("status", "")),
        }

    def _read_logs(self) -> List[Dict[str, Any]]:  # pragma: no cover - exercised live only
        resp = self._sess().get(self.log_source, timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise DotNetAppIngestError(f"log read HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            return []
        entries = payload.get("entries", payload) if isinstance(payload, dict) else payload
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
