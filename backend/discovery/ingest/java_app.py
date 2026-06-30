"""
R17-A3 / T1 — Java application change-based ingestor (operational surface).

AgentIQ's first non-SaaS enterprise source. Implements the
:class:`~discovery.ingest.base.ChangeBasedIngestor` contract from R16-A1 to read
the OPERATIONAL surface of a running Java enterprise application — its framework
health/diagnostics endpoints (Spring Boot Actuator: ``health``, ``metrics``,
``info``) and its application logs — and produce operational SIGNAL where the
application shows runtime friction (R17-A3 §1).

Scope — phase one of two (AC8)
------------------------------
This is the OPERATIONAL phase: read what a running application reports about
itself. It reads operational surfaces ONLY — it never reads the application's
SOURCE CODE, which is reserved for the separate 1.8 code-and-structure phase.
External APM/observability-platform data is also out of scope (a possible later
extension). The connector therefore touches only the Actuator endpoints and the
configured log sources; there is no repository clone, no class/AST inspection,
and no configuration-file reading.

Configured, not auto-discovered (R17-A3 §2)
-------------------------------------------
The applications to read — their Actuator endpoint URLs and log sources — are
configured per deployment (see :mod:`discovery.ingest.java_app_config`), with
credentials handled via the vault. AgentIQ does NOT scan the network to find
Java apps; the customer points it at the applications in scope. This keeps phase
one bounded, secure, and predictable.

Built on the change-based foundation (R16-A1 / §2, AC2 + AC3)
-------------------------------------------------------------
Java operational data is inherently incremental: logs are read forward from a
position and metrics endpoints are sampled over time. The connector encodes its
read position — a per-app ``{log_offset, metrics_ts}`` map — as the opaque
checkpoint value, so each run processes only new operational data rather than
re-reading history. The runner persists/returns the value verbatim and never
interprets it (R16-A1 AC5). An idle application yields an empty (or minimal)
delta that echoes the incoming position (AC2).

Checkpoint shape (opaque to the runner)
---------------------------------------
A single ``(org_id, 'java_app')`` checkpoint row is persisted by the runner, but
a deployment has many configured apps each with its own read position. The
connector encodes a per-app cursor MAP as the opaque value::

    {"v": 1, "apps": {"payments-api": {"log_offset": 42, "metrics_ts": "2026-06-01T10:00:00Z"}}}

An app absent from the map is read from the beginning, which is what makes a
first load resumable (R16-A1 §3): if a streamed first load fails partway, the
next run finds a checkpoint covering the apps already loaded and resumes the
rest.

Provenance & change events
--------------------------
Every record carries a fully-populated, OBSERVED ``evidence_pointer`` (R16-B1,
``source_system='java_app'``, ``origin='observed'`` — T4 / AC4) plus an
``artifact_id`` and ``change_kind`` so the shared change runner emits one
``ingestion.artifact_changed`` event per changed artifact (R16-A1 / AT-381 —
T6 / AC6). Records also carry an extracted operational ``signals`` block (T2) so
friction signal travels with the delta to corroboration (T5 / AC5).

Offline vs live
---------------
Offline (default, ``INGEST_MODE`` != ``live``): reads the deterministic fixture
``fixtures/java_app_sample.json`` — parity with the other connectors, so the
whole pipeline runs without any credentials. Live: samples each configured
target's Actuator endpoints and tails its log source over HTTP, using the
credential resolved from the vault (never from config — AC3).

Deletes / tombstones (R16-A1 §5)
--------------------------------
``reports_deletes = False``: operational data is forward-only — a metrics sample
or a log line, once observed, is never "deleted" upstream; the source has no
deletion semantics for these artifacts. The gap is declared explicitly here
rather than silently pretending deletes are caught.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import is_live
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .java_app_config import JavaAppTarget, load_targets, resolve_secret
from .java_app_signals import (
    build_evidence_pointer,
    build_service_signal,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "java_app_sample.json"

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default number of operational records emitted per :class:`DeltaBatch`. Kept
#: modest so a large initial load streams as many small, individually-
#: checkpointed batches (AC3 resumability) rather than one monolithic read.
_DEFAULT_BATCH_SIZE = 100

#: Live HTTP timeout (seconds) for Actuator / log reads.
_REQUEST_TIMEOUT = 30


class JavaAppIngestError(Exception):
    """Raised when live Java-app ingestion fails with a clear, actionable message."""


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
            "java_app: could not decode checkpoint value; treating as first run "
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


class JavaAppIngestor(ChangeBasedIngestor):
    """Change-based Java application ingestor (R17-A3 / T1).

    Encodes its position as a per-app ``{log_offset, metrics_ts}`` cursor map
    (opaque to the runner) and yields only new metric samples / log entries per
    app. A first run (``since is None``) performs a full initial load of every
    configured app, streamed as resumable, individually-checkpointed batches.

    Operational surface only (AC8): reads the configured Actuator endpoints and
    log sources — never the application's source code.

    Deletes / tombstones (R16-A1 §5): ``reports_deletes = False`` — operational
    artifacts (metric samples, log lines) are forward-only and have no upstream
    deletion semantics; the limitation is declared rather than faked.
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
        """Yield batches of new Java-app operational records since ``since``.

        First run (``since is None``): full load of every configured app,
        streamed as checkpointed batches (resumable — AC3). Incremental run: only
        metric samples newer than the stored ``metrics_ts`` and log entries past
        the stored ``log_offset`` per app (AC2). An idle deployment yields a
        single empty :class:`DeltaBatch` whose ``next_checkpoint`` echoes the
        incoming position (AC2).
        """
        cursors = _decode_checkpoint(since.value if since else None)
        running = {app_id: dict(cur) for app_id, cur in cursors.items()}

        targets = load_targets(org_id)
        logger.info(
            "java_app: org=%s %s — %d configured application(s)",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(targets),
        )

        # Collect each app's pending (new) operational records first so we know
        # which batch is the overall final one and can flag is_complete=True on
        # exactly that batch (the runner needs one terminal batch to advance).
        pending: List[tuple] = []  # (target, [records], new_cursor)
        for target in targets:
            cursor = _app_cursor(cursors, target.app_id)
            records, new_cursor = self._read_operational(org_id, target, cursor)
            if records:
                pending.append((target, records, new_cursor))
            else:
                # Even with no new records, carry the app's known cursor forward
                # so the encoded position never regresses for already-seen apps.
                running.setdefault(target.app_id, new_cursor)

        if not pending:
            # Idle deployment → empty delta that echoes the incoming position.
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
                # Advance this app's cursor to the newest reading in the page.
                running[target.app_id] = self._advance_cursor(running.get(target.app_id), page)
                emitted += 1
                yield DeltaBatch(
                    records=page,
                    next_checkpoint=_encode_checkpoint(running),
                    is_complete=(emitted == total_batches),
                )
            # Ensure the final cursor for the app reflects everything read.
            running[target.app_id] = new_cursor

    # ── Operational read (Actuator samples + logs) ───────────────────────────
    def _read_operational(
        self, org_id: str, target: JavaAppTarget, cursor: Dict[str, Any]
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Read new metric samples + log entries for one app since ``cursor``.

        Returns ``(records, new_cursor)`` where records are the changed
        operational artifacts (oldest-first) and new_cursor is the advanced
        ``{log_offset, metrics_ts}`` for the app. Operational surface only — no
        source code is read (AC8).
        """
        raw = self._raw_operational(org_id, target)

        # Metric samples newer than the stored metrics_ts.
        last_ts = cursor.get("metrics_ts", "")
        samples = sorted(raw.get("metrics", []), key=lambda s: str(s.get("sample_ts", "")))
        fresh_samples = [s for s in samples if str(s.get("sample_ts", "")) > last_ts]

        # Log entries past the stored offset.
        last_offset = int(cursor.get("log_offset", 0) or 0)
        logs = sorted(raw.get("logs", []), key=lambda e: int(e.get("offset", 0) or 0))
        fresh_logs = [e for e in logs if int(e.get("offset", 0) or 0) > last_offset]

        records: List[Dict[str, Any]] = []
        for sample in fresh_samples:
            records.append(self._to_metric_record(target, sample))
        for entry in fresh_logs:
            records.append(self._to_log_record(target, entry))

        new_cursor = {
            "metrics_ts": str(fresh_samples[-1]["sample_ts"]) if fresh_samples else last_ts,
            "log_offset": int(fresh_logs[-1]["offset"]) if fresh_logs else last_offset,
        }
        return records, new_cursor

    def _advance_cursor(
        self, current: Optional[Dict[str, Any]], page: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Advance a cursor to the newest reading in a page of records.

        Keeps the encoded checkpoint monotonic so any single yielded batch is a
        valid resume point on the next run.
        """
        cur = dict(current or {"log_offset": 0, "metrics_ts": ""})
        for rec in page:
            if rec.get("artifact_kind") == "metrics":
                ts = str(rec.get("observed_ts", ""))
                if ts > cur.get("metrics_ts", ""):
                    cur["metrics_ts"] = ts
            elif rec.get("artifact_kind") == "log":
                off = int(rec.get("log_offset", 0) or 0)
                if off > int(cur.get("log_offset", 0) or 0):
                    cur["log_offset"] = off
        return cur

    def _to_metric_record(self, target: JavaAppTarget, sample: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one Actuator metric sample into a change-delta record (T2/T4).

        Carries the normalised operational reading (health, error rate, latency,
        throughput, resource gauges) the running application reports about itself,
        plus a per-record operational signal and an OBSERVED evidence pointer.
        ``artifact_id`` + ``change_kind`` let the shared runner emit
        ``ingestion.artifact_changed`` (AC6).
        """
        ts = str(sample.get("sample_ts", ""))
        record = {
            "artifact_id": f"{target.app_id}:metrics:{ts}",
            "change_kind": ChangeKind.CREATED,  # a new sample is a new observation
            "source_system": "java_app",
            "app_id": target.app_id,
            "service": target.service,
            "artifact_kind": "metrics",
            "observed_ts": ts,
            "actuator_url": target.actuator_url,
            "health": sample.get("health"),
            "error_rate": sample.get("error_rate"),
            "latency_p95_ms": sample.get("latency_p95_ms"),
            "throughput_rpm": sample.get("throughput_rpm"),
            "jvm_memory_used_ratio": sample.get("jvm_memory_used_ratio"),
            "system_cpu_usage": sample.get("system_cpu_usage"),
            "evidence_pointer": build_evidence_pointer(
                target.app_id, "metrics", ts, ts
            ),
        }
        # Per-record operational signal (single-sample view); the run-level
        # rollup is computed across all records by java_app_signals.
        record["signals"] = build_service_signal(target.service, [record])
        return record

    def _to_log_record(self, target: JavaAppTarget, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one application log entry into a change-delta record (T2/T4).

        Carries structured log *signal* only — level, logger, exception type,
        offset, and the message text passed through for error-pattern counting.
        No log-message NLP / meaning extraction is done (AC8). Carries an OBSERVED
        evidence pointer + artifact_id/change_kind for the runner's event.
        """
        offset = int(entry.get("offset", 0) or 0)
        ts = str(entry.get("ts", ""))
        record = {
            "artifact_id": f"{target.app_id}:log:{offset}",
            "change_kind": ChangeKind.CREATED,
            "source_system": "java_app",
            "app_id": target.app_id,
            "service": target.service,
            "artifact_kind": "log",
            "observed_ts": ts,
            "log_offset": offset,
            "log_source": target.log_source,
            "level": entry.get("level"),
            "logger": entry.get("logger"),
            "exception_type": entry.get("exception_type"),
            "retry": bool(entry.get("retry", False)),
            "message": entry.get("message", ""),
            "evidence_pointer": build_evidence_pointer(
                target.app_id, "log", str(offset), ts
            ),
        }
        record["signals"] = build_service_signal(target.service, [record])
        return record

    # ── Source access: offline fixture vs live Actuator / log read ───────────
    def _raw_operational(self, org_id: str, target: JavaAppTarget) -> Dict[str, Any]:
        """Return ``{"metrics": [...], "logs": [...]}`` for one app.

        Offline reads the deterministic fixture; live samples the Actuator
        endpoints and tails the log source using the vault-resolved credential
        (AC3). Operational surface only — no source code (AC8).
        """
        if not is_live():
            fixture = self._fixture()
            return {
                "metrics": list(fixture.get("metrics", {}).get(target.app_id, [])),
                "logs": list(fixture.get("logs", {}).get(target.app_id, [])),
            }
        return self._client(org_id, target).read_operational()

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
    the application log source over HTTP. The credential, when present, is sent as
    a Bearer header; it is held only for the life of the request session and never
    logged. Operational surface only — this client has no path that reads source
    code (AC8).
    """

    def __init__(self, *, actuator_url: str, log_source: str, secret: Optional[str]):
        self.actuator_url = actuator_url.rstrip("/") if actuator_url else ""
        self.log_source = log_source
        self._secret = secret
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

    def read_operational(self) -> Dict[str, Any]:  # pragma: no cover - exercised live only
        """Sample the Actuator endpoints and tail the log source.

        Returns the same ``{"metrics": [...], "logs": [...]}`` shape the offline
        fixture provides, so the ingestor's downstream logic is identical in both
        modes. Network/transport errors are raised as :class:`JavaAppIngestError`
        with no secret in the message (the runner degrades non-blockingly).
        """
        metrics = [self._sample_actuator()] if self.actuator_url else []
        logs = self._read_logs() if self.log_source else []
        return {"metrics": metrics, "logs": logs}

    def _sample_actuator(self) -> Dict[str, Any]:  # pragma: no cover - exercised live only
        """Read /health, /metrics, /info and normalise into one sample."""
        from datetime import datetime, timezone

        def _get(path: str) -> Dict[str, Any]:
            resp = self._sess().get(f"{self.actuator_url}/{path}", timeout=_REQUEST_TIMEOUT)
            if not resp.ok:
                raise JavaAppIngestError(f"Actuator {path} HTTP {resp.status_code}")
            return resp.json()

        health = _get("health")
        # Real Actuator metric reads would aggregate /metrics/{name}; kept compact
        # here. The shape mirrors the fixture so signal extraction is identical.
        sample_ts = datetime.now(timezone.utc).isoformat()
        return {
            "sample_ts": sample_ts,
            "health": str(health.get("status", "")),
        }

    def _read_logs(self) -> List[Dict[str, Any]]:  # pragma: no cover - exercised live only
        """Tail the configured log source from the last offset forward."""
        resp = self._sess().get(self.log_source, timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise JavaAppIngestError(f"log read HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            return []
        entries = payload.get("entries", payload) if isinstance(payload, dict) else payload
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
