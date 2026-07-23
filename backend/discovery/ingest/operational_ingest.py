"""
R17-A4 / T1+T2 — Shared operational change-ingestion base (platform-agnostic).

Java (R17-A3) and .NET (R17-A4) read the OPERATIONAL surface of a running
enterprise application incrementally: logs are read forward from a position and
health/diagnostics endpoints are sampled over time. The change-based *foundation*
for that — the opaque per-app checkpoint cursor, the delta selection of only-new
samples/logs, and the resumable, individually-checkpointed batch streaming — is
identical between the two platforms. Only the COLLECTION edges differ (which
endpoints, which log formats, which native metric names).

This module owns that shared foundation once (R17-A4 §2, Architectural Note
"Share the extraction, not just the idea"). A concrete ingestor subclasses
:class:`OperationalChangeIngestor` and implements only the collection hooks:
where its targets come from (:meth:`_load_targets`), how to read one app's raw
operational surface (:meth:`_raw_operational`), and how to shape a normalised
metric sample / log entry into a change-delta record (:meth:`_to_metric_record` /
:meth:`_to_log_record`, using the shared :meth:`_metric_record` /
:meth:`_log_record` builders). Everything else — cursor math, delta windowing,
batch streaming, provenance stamping — is shared.

Checkpoint shape (opaque to the runner)
---------------------------------------
A single ``(org_id, connector_id)`` checkpoint row is persisted by the runner,
but a deployment has many configured apps each with its own read position. The
connector encodes a per-app cursor MAP as the opaque value::

    {"v": 1, "apps": {"payments-api": {"log_offset": 42, "metrics_ts": "…Z", "metrics_seq": 1}}}

``metrics_seq`` is the number of samples already consumed AT ``metrics_ts`` — so a
second sample sharing the same timestamp (rapid polling / coarse-resolution
clocks) is still ingested exactly once rather than lost or re-read. A cursor
without ``metrics_seq`` (a hand-set or legacy checkpoint) means "everything at
``metrics_ts`` is already consumed" — the original strict-greater-than semantics.
An app absent from the map is read from the beginning, which makes a first load
resumable (R16-A1 §3). The runner persists/returns the value verbatim and never
interprets it (R16-A1 AC5).
"""

from __future__ import annotations

import abc
import json
import logging
from typing import Any, Callable, Dict, Iterator, List, Optional

from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .operational_config import (
    OperationalCredentialMissing,
    credential_missing_health,
)
from .operational_signals import build_evidence_pointer

logger = logging.getLogger(__name__)

#: Opaque-checkpoint schema version, so a future shape change can be detected.
CHECKPOINT_VERSION = 1

#: Default number of operational records emitted per :class:`DeltaBatch`. Kept
#: modest so a large initial load streams as many small, individually-
#: checkpointed batches (resumability) rather than one monolithic read.
DEFAULT_BATCH_SIZE = 100


# ─────────────────────────────────────────────────────────────────────────────
# Opaque per-app cursor machinery (shared by every operational ingestor)
# ─────────────────────────────────────────────────────────────────────────────

def encode_checkpoint(cursors: Dict[str, Dict[str, Any]]) -> str:
    """Encode the per-app cursor map as the opaque checkpoint value.

    ``sort_keys`` keeps the encoding deterministic so two runs over identical
    state produce byte-identical checkpoints (testable, diff-friendly).
    """
    return json.dumps(
        {"v": CHECKPOINT_VERSION, "apps": cursors},
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_checkpoint(value: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Decode an opaque checkpoint value back into the per-app cursor map.

    Tolerant by design: a missing, empty, or unparseable value yields an empty
    map (read every app from the beginning) rather than raising — a degenerate
    checkpoint must degrade to a safe full re-read, never crash the run.

    ``metrics_seq`` is preserved only when present; a cursor without it keeps the
    strict "everything at metrics_ts already consumed" semantics.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning(
            "operational ingest: could not decode checkpoint value; treating as "
            "first run (full re-read)."
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


def app_cursor(cursors: Dict[str, Dict[str, Any]], app_id: str) -> Dict[str, Any]:
    """Return the cursor for one app, defaulting to the beginning of time."""
    cur = cursors.get(app_id) or {}
    out: Dict[str, Any] = {
        "log_offset": int(cur.get("log_offset", 0) or 0),
        "metrics_ts": str(cur.get("metrics_ts", "") or ""),
    }
    if cur.get("metrics_seq") is not None:
        out["metrics_seq"] = int(cur.get("metrics_seq") or 0)
    return out


class OperationalIngestError(Exception):
    """Raised when operational ingestion fails with a clear, actionable message."""


# ─────────────────────────────────────────────────────────────────────────────
# Tolerant log-body parsing (shared by every live operational client)
# ─────────────────────────────────────────────────────────────────────────────

def parse_log_payload(resp: Any, *, plain_text_level: Callable[[str], str]) -> List[Dict[str, Any]]:
    """Parse a log-source HTTP response into log entries, accepting three shapes.

    Application logs are commonly a structured JSON array / ``{"entries": []}``
    wrapper (Spring Boot, Serilog/MEL JSON console), NDJSON (one JSON object per
    line), or plain-text lines. All three are handled so live log signal is
    actually ingested regardless of framework; a truly unparseable body yields no
    entries rather than raising. ``plain_text_level`` maps a raw text line to a
    best-effort level token — the one genuinely platform-specific bit of parsing
    (Java vs .NET level vocabularies), injected by the caller. No log-message NLP
    is done (AC8): only the level token is inferred from plain text.
    """
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
        records.append({"offset": offset, "message": line, "level": plain_text_level(line)})
    return records


# ─────────────────────────────────────────────────────────────────────────────
# The shared change-ingestion base
# ─────────────────────────────────────────────────────────────────────────────

class OperationalChangeIngestor(ChangeBasedIngestor):
    """Shared change-based ingestor for operational enterprise-app sources.

    Owns the platform-neutral foundation: the per-app ``{log_offset, metrics_ts,
    metrics_seq}`` cursor map (opaque to the runner), the delta selection of
    only-new samples/logs, resumable batch streaming, and OBSERVED provenance
    stamping. Subclasses supply only the COLLECTION edges (targets, raw surface
    read, and normalised-record shaping).

    Operational surface only (AC8): a concrete subclass reads the configured
    health/diagnostics endpoints and log sources — never the application's source
    code.

    Deletes / tombstones (R16-A1 §5): ``reports_deletes = False`` — operational
    artifacts (metric samples, log lines) are forward-only and have no upstream
    deletion semantics; the limitation is declared rather than faked.
    """

    #: The provenance ``source_system`` stamped on every record (defaults to the
    #: connector id; subclasses may leave it and just set ``connector_id``).
    source_system: str = ""
    reports_deletes = False

    #: Human-facing connector name used in connector-health records for a
    #: fail-closed target (R191-H1 / T1, AC1). Subclasses set it (e.g.
    #: ``"Java Application"`` / ``".NET Application"``); it defaults to the
    #: connector id so a record is always attributable.
    health_system: str = ""

    def __init__(self, batch_size: int = DEFAULT_BATCH_SIZE):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size
        if not self.source_system:
            self.source_system = self.connector_id
        if not self.health_system:
            self.health_system = self.connector_id
        #: Connector-health records for targets skipped this ingest pass because
        #: their vault credential was missing (fail-closed — R191-H1 / T1, AC1).
        #: Populated during :meth:`ingest_changes`; the runner reads it after the
        #: pass and surfaces it in the run's ``connector_health`` KV. One entry per
        #: skipped target; the shape matches ``ConnectorHealth.to_dict()``.
        self.credential_health: List[Dict[str, Any]] = []

    def reset_credential_health(self) -> None:
        """Start a distinct run with an empty credential-health slate."""
        self.credential_health = []

    # ── Collection hooks (implemented per platform) ─────────────────────────
    @abc.abstractmethod
    def _load_targets(self, org_id: str) -> List[Any]:
        """Return the configured targets for ``org_id`` (config, never scanning)."""
        raise NotImplementedError

    @abc.abstractmethod
    def _raw_operational(self, org_id: str, target: Any) -> Dict[str, Any]:
        """Return ``{"metrics": [...normalised samples...], "logs": [...]}`` for one app."""
        raise NotImplementedError

    @abc.abstractmethod
    def _to_metric_record(self, target: Any, sample: Dict[str, Any], seq_index: int = 0) -> Dict[str, Any]:
        """Shape one normalised metric sample into a change-delta record."""
        raise NotImplementedError

    @abc.abstractmethod
    def _to_log_record(self, target: Any, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one application log entry into a change-delta record."""
        raise NotImplementedError

    # ── ChangeBasedIngestor contract (shared) ────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of new operational records since ``since``.

        First run (``since is None``): full load of every configured app, streamed
        as checkpointed batches (resumable — AC2). Incremental run: only metric
        samples newer than the stored position and log entries past the stored
        ``log_offset`` per app (AC2). An idle deployment yields a single empty
        :class:`DeltaBatch` whose ``next_checkpoint`` echoes the incoming position.

        Fail-closed on a vault miss (R191-H1 / T1, AC1): if a target declares a
        credential its vault does not hold, reading it raises
        :class:`OperationalCredentialMissing`; that ONE target is skipped (its
        cursor is left untouched so it is retried once the credential is connected)
        and a connector-health record is appended to :attr:`credential_health` for
        the runner to surface. The pass continues for every other target.
        """
        cursors = decode_checkpoint(since.value if since else None)
        running = {app_id: dict(cur) for app_id, cur in cursors.items()}
        targets = self._load_targets(org_id)
        logger.info(
            "%s: org=%s %s — %d configured application(s)",
            self.connector_id,
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(targets),
        )

        # Collect each app's pending (new) operational records first so we know
        # which batch is the overall final one and can flag is_complete=True on
        # exactly that batch (the runner needs one terminal batch to advance).
        pending: List[tuple] = []  # (target, [records], new_cursor)
        for target in targets:
            cursor = app_cursor(cursors, target.app_id)
            try:
                records, new_cursor = self._read_operational(org_id, target, cursor)
            except OperationalCredentialMissing as miss:
                # Fail closed: vault has no credential for this target. Skip it,
                # record actionable connector health (org / target / credential
                # ref), and leave its cursor untouched so it is retried once the
                # credential is connected. No env fallback, no secret in the log.
                logger.warning(
                    "%s: skipping target (fail-closed, no vault credential) "
                    "org=%s app_id=%s credential_ref=%s",
                    self.connector_id,
                    miss.org_id,
                    miss.app_id,
                    miss.credential_ref,
                )
                self.credential_health.append(
                    credential_missing_health(system=self.health_system, exc=miss)
                )
                continue
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
                next_checkpoint=encode_checkpoint(running),
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
                    next_checkpoint=encode_checkpoint(running),
                    is_complete=(emitted == total_batches),
                )
            # Ensure the final cursor for the app reflects everything read.
            running[target.app_id] = new_cursor

    # ── Operational read (samples + logs) — shared cursor/seq windowing ──────
    def _read_operational(
        self, org_id: str, target: Any, cursor: Dict[str, Any]
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Read new metric samples + log entries for one app since ``cursor``.

        Returns ``(records, new_cursor)`` where records are the changed
        operational artifacts (oldest-first) and new_cursor is the advanced
        ``{log_offset, metrics_ts, metrics_seq}`` for the app. Operational surface
        only — no source code is read (AC8).

        Metric selection is sequence-aware: samples strictly newer than
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
        # artifact ids. Boundary samples continue from the consumed count.
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
        a batch boundary.
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

    # ── Shared record builders (provenance-stamped; endpoint field per platform) ─
    def _metric_record(
        self,
        target: Any,
        sample: Dict[str, Any],
        seq_index: int,
        *,
        endpoint_field: str,
        endpoint_url: Optional[str],
    ) -> Dict[str, Any]:
        """Build a normalised metric change-delta record (shared shape).

        Carries the normalised operational reading (health, error rate, latency,
        throughput, resource gauges) plus an OBSERVED evidence pointer.
        ``artifact_id`` + ``change_kind`` let the shared runner emit
        ``ingestion.artifact_changed`` (AC7). ``seq_index`` disambiguates samples
        that share a timestamp so their artifact ids stay unique; the first sample
        at a timestamp keeps the bare id. ``endpoint_field`` names the platform's
        diagnostics endpoint (``actuator_url`` / ``diagnostics_url``).

        Operational SIGNAL is NOT computed here — it is a window operation over the
        whole delta (:mod:`operational_signals`), so a single-sample view would be
        meaningless.
        """
        ts = str(sample.get("sample_ts", ""))
        ref = ts if seq_index <= 0 else f"{ts}:{seq_index}"
        return {
            "artifact_id": f"{target.app_id}:metrics:{ref}",
            "change_kind": ChangeKind.CREATED,  # a new sample is a new observation
            "source_system": self.source_system,
            "app_id": target.app_id,
            "service": target.service,
            "artifact_kind": "metrics",
            "observed_ts": ts,
            endpoint_field: endpoint_url,
            "health": sample.get("health"),
            "error_rate": sample.get("error_rate"),
            "latency_p95_ms": sample.get("latency_p95_ms"),
            "throughput_rpm": sample.get("throughput_rpm"),
            "memory_used_ratio": sample.get("memory_used_ratio"),
            "cpu_usage": sample.get("cpu_usage"),
            "evidence_pointer": build_evidence_pointer(
                self.source_system, target.app_id, "metrics", ref, ts
            ),
        }

    def _log_record(
        self,
        target: Any,
        entry: Dict[str, Any],
        *,
        log_source: Optional[str],
        level: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a normalised log change-delta record (shared shape).

        Carries structured log *signal* only — level (canonical upper-case),
        logger/category, exception type, offset, retry flag, and the message text
        passed through for error-pattern counting. No log-message NLP / meaning
        extraction is done (AC8). ``level`` lets a platform pass its already-
        normalised level (e.g. .NET ``Critical`` → ``CRITICAL``); when omitted the
        entry's own ``level`` is used verbatim.

        A native log ``event_id``, when the entry carries one, is preferred as the
        artifact reference (``{app}:log:event:{event_id}``) — the most precise
        handle on the exact log event; otherwise the log-stream ``offset`` pins the
        reading. The cursor math always uses ``log_offset`` either way.
        """
        offset = int(entry.get("offset", 0) or 0)
        ts = str(entry.get("ts", ""))
        event_id = str(entry.get("event_id") or "").strip()
        ref = f"event:{event_id}" if event_id else str(offset)
        return {
            "artifact_id": f"{target.app_id}:log:{ref}",
            "change_kind": ChangeKind.CREATED,
            "source_system": self.source_system,
            "app_id": target.app_id,
            "service": target.service,
            "artifact_kind": "log",
            "observed_ts": ts,
            "log_offset": offset,
            "log_source": log_source,
            "event_id": event_id or None,
            "level": level if level is not None else entry.get("level"),
            "logger": entry.get("logger"),
            "exception_type": entry.get("exception_type"),
            "retry": bool(entry.get("retry", False)),
            "message": entry.get("message", ""),
            "evidence_pointer": build_evidence_pointer(
                self.source_system, target.app_id, "log", ref, ts
            ),
        }
