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
read position — a per-app ``{log_offset, metrics_ts, metrics_seq}`` map — as the
opaque checkpoint value, so each run processes only new operational data rather
than re-reading history. The runner persists/returns the value verbatim and
never interprets it (R16-A1 AC5). An idle application yields an empty (or
minimal) delta that echoes the incoming position (AC2).

Checkpoint shape (opaque to the runner)
---------------------------------------
A single ``(org_id, 'java_app')`` checkpoint row is persisted by the runner, but
a deployment has many configured apps each with its own read position. The
connector encodes a per-app cursor MAP as the opaque value::

    {"v": 1, "apps": {"payments-api": {"log_offset": 42, "metrics_ts": "2026-06-01T10:00:00Z", "metrics_seq": 1}}}

``metrics_seq`` is the number of samples already consumed AT ``metrics_ts`` — so a
second sample sharing the same timestamp (rapid polling / coarse-resolution
clocks) is still ingested exactly once rather than lost or re-read (M2). A cursor
without ``metrics_seq`` (a hand-set or legacy checkpoint) means "everything at
``metrics_ts`` is already consumed" — the original strict-greater-than semantics.
An app absent from the map is read from the beginning, which is what makes a
first load resumable (R16-A1 §3).

Provenance & change events
--------------------------
Every record carries a fully-populated, OBSERVED ``evidence_pointer`` (R16-B1,
``source_system='java_app'``, ``origin='observed'`` — T4 / AC4) plus an
``artifact_id`` and ``change_kind`` so the shared change runner emits one
``ingestion.artifact_changed`` event per changed artifact (R16-A1 / AT-381 —
T6 / AC6). Operational SIGNAL is extracted as a window operation over the whole
delta by :func:`java_app_signals.build_java_app_corroboration_payload` (the
runner calls it over the collected records — T2 / T5), NOT per-record: a single
sample cannot show a degradation *trend*, so per-record signal would be
meaningless.

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
from .java_app_signals import build_evidence_pointer

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

    ``metrics_seq`` is preserved only when present; a cursor without it keeps the
    strict "everything at metrics_ts already consumed" semantics (M2).
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
            decoded = {
                "log_offset": int(cur.get("log_offset", 0) or 0),
                "metrics_ts": str(cur.get("metrics_ts", "") or ""),
            }
            if cur.get("metrics_seq") is not None:
                decoded["metrics_seq"] = int(cur.get("metrics_seq") or 0)
            out[str(app_id)] = decoded
    return out


def _app_cursor(cursors: Dict[str, Dict[str, Any]], app_id: str) -> Dict[str, Any]:
    """Return the cursor for one app, defaulting to the beginning of time."""
    cur = cursors.get(app_id) or {}
    out: Dict[str, Any] = {
        "log_offset": int(cur.get("log_offset", 0) or 0),
        "metrics_ts": str(cur.get("metrics_ts", "") or ""),
    }
    if cur.get("metrics_seq") is not None:
        out["metrics_seq"] = int(cur.get("metrics_seq") or 0)
    return out


class JavaAppIngestor(ChangeBasedIngestor):
    """Change-based Java application ingestor (R17-A3 / T1).

    Encodes its position as a per-app ``{log_offset, metrics_ts, metrics_seq}``
    cursor map (opaque to the runner) and yields only new metric samples / log
    entries per app. A first run (``since is None``) performs a full initial load
    of every configured app, streamed as resumable, individually-checkpointed
    batches.

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
        metric samples newer than the stored position and log entries past the
        stored ``log_offset`` per app (AC2). An idle deployment yields a single
        empty :class:`DeltaBatch` whose ``next_checkpoint`` echoes the incoming
        position (AC2).
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
        ``{log_offset, metrics_ts, metrics_seq}`` for the app. Operational surface
        only — no source code is read (AC8).

        Metric selection is sequence-aware (M2): samples strictly newer than
        ``metrics_ts`` are always fresh; samples sharing ``metrics_ts`` are fresh
        only beyond the ``metrics_seq`` already consumed at that timestamp — so a
        late-arriving same-timestamp sample is ingested once, never dropped or
        re-read. A cursor with no ``metrics_seq`` keeps the strict-> semantics.
        """
        raw = self._raw_operational(org_id, target)

        # Metric samples: strictly newer, plus not-yet-consumed same-timestamp ones.
        last_ts = str(cursor.get("metrics_ts", "") or "")
        last_seq = cursor.get("metrics_seq")  # None → legacy: strict-> (no boundary carry)
        samples = sorted(raw.get("metrics", []), key=lambda s: str(s.get("sample_ts", "")))
        newer = [s for s in samples if str(s.get("sample_ts", "")) > last_ts]
        if last_ts and last_seq is not None:
            same_ts = [s for s in samples if str(s.get("sample_ts", "")) == last_ts]
            fresh_same = same_ts[int(last_seq):]
        else:
            fresh_same = []
        fresh_samples = fresh_same + newer  # same-ts (== last_ts) first, then newer

        # Log entries past the stored offset.
        last_offset = int(cursor.get("log_offset", 0) or 0)
        logs = sorted(raw.get("logs", []), key=lambda e: int(e.get("offset", 0) or 0))
        fresh_logs = [e for e in logs if int(e.get("offset", 0) or 0) > last_offset]

        records: List[Dict[str, Any]] = []
        # Per-timestamp index so duplicate-timestamp samples get distinct
        # artifact ids (M2). Boundary samples continue from the consumed count.
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
        """Advance a cursor to the newest reading in a page of records.

        Keeps the encoded checkpoint monotonic so any single yielded batch is a
        valid resume point on the next run. ``metrics_seq`` accumulates across
        pages: a new max timestamp resets it to 1, a repeat of the current max
        increments it — so the final streamed checkpoint matches the authoritative
        cursor from :meth:`_read_operational` even when duplicate timestamps span
        a batch boundary (M2).
        """
        cur = dict(current or {"log_offset": 0, "metrics_ts": ""})
        metrics_ts = str(cur.get("metrics_ts", "") or "")
        metrics_seq = int(cur.get("metrics_seq", 0) or 0)
        log_offset = int(cur.get("log_offset", 0) or 0)
        for rec in page:
            if rec.get("artifact_kind") == "metrics":
                ts = str(rec.get("observed_ts", ""))
                if ts > metrics_ts:
                    metrics_ts = ts
                    metrics_seq = 1
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

    def _to_metric_record(
        self, target: JavaAppTarget, sample: Dict[str, Any], seq_index: int = 0
    ) -> Dict[str, Any]:
        """Shape one Actuator metric sample into a change-delta record (T2/T4).

        Carries the normalised operational reading (health, error rate, latency,
        throughput, resource gauges) the running application reports about itself,
        plus an OBSERVED evidence pointer. ``artifact_id`` + ``change_kind`` let
        the shared runner emit ``ingestion.artifact_changed`` (AC6). ``seq_index``
        disambiguates samples that share a timestamp so their artifact ids stay
        unique (M2); the first sample at a timestamp keeps the bare id.

        Operational SIGNAL is NOT computed here — it is a window operation over
        the whole delta (java_app_signals), so a single-sample view would be
        meaningless.
        """
        ts = str(sample.get("sample_ts", ""))
        ref = ts if seq_index <= 0 else f"{ts}:{seq_index}"
        record = {
            "artifact_id": f"{target.app_id}:metrics:{ref}",
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
                target.app_id, "metrics", ref, ts
            ),
        }
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
        return record

    # ── Source access: offline fixture vs live Actuator / log read ───────────
    def _raw_operational(self, org_id: str, target: JavaAppTarget) -> Dict[str, Any]:
        """Return ``{"metrics": [...], "logs": [...]}`` for one app.

        Offline reads the deterministic fixture; live samples the Actuator
        endpoints and tails the log source using the vault-resolved credential
        (AC3). Operational surface only — no source code (AC8). The live client's
        HTTP session is always closed after the read (M1 — no connection leak).
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
    the application log source over HTTP. The credential, when present, is sent as
    a Bearer header; it is held only for the life of the request session and never
    logged. Operational surface only — this client has no path that reads source
    code (AC8).

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

        Maps the standard Actuator endpoints to the operational reading fields the
        signal extractor consumes:

          * ``error_rate``            — ``http.server.requests`` SERVER_ERROR count
                                        / total count.
          * ``latency_p95_ms``        — ``http.server.requests`` MAX (seconds→ms):
                                        the high-percentile proxy Actuator exposes
                                        without a percentile histogram.
          * ``throughput_rpm``        — ``http.server.requests`` cumulative COUNT
                                        (rate windowing across samples is a later
                                        enhancement; single-sample reads only see
                                        the running total).
          * ``jvm_memory_used_ratio`` — ``jvm.memory.used`` / ``jvm.memory.max``.
          * ``system_cpu_usage``      — ``system.cpu.usage`` (0..1).

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
            "jvm_memory_used_ratio": heap_ratio,
            "system_cpu_usage": self._metric("system.cpu.usage"),
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

        # 1. Structured JSON: a list or an {"entries": [...]} wrapper.
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
            # 2. NDJSON: one JSON object per line.
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        obj.setdefault("offset", offset)
                        records.append(obj)
                        continue
                except ValueError:
                    pass
            # 3. Plain-text line: keep the raw message; level parsed best-effort.
            records.append({"offset": offset, "message": line, "level": _plain_text_level(line)})
        return records


def _plain_text_level(line: str) -> str:
    """Best-effort log level from a plain-text log line (no NLP — AC8)."""
    upper = line.upper()
    for level in ("FATAL", "SEVERE", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"):
        if level in upper:
            return level
    return ""
