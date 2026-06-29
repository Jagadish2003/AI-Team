"""
R17-A3 / T2 — Java application operational signal extraction.

Turns the raw operational surface of a running Java enterprise application —
its **application logs** and its **framework health/diagnostics samples** (Spring
Boot Actuator: health, metrics, info) — into structured operational SIGNAL. This
is the layer that makes the source useful: reading logs and metrics is not enough
by itself; AgentIQ needs the data converted into meaningful patterns that explain
*where runtime friction exists* so they can later become findings, corroboration
evidence, and confidence boosters for opportunities detected in other systems.

This module is the R17-A3 analog of :mod:`discovery.ingest.slack_signals` — pure,
deterministic extraction functions that the Java ingestor (T1,
``discovery.ingest.java_app``) calls once it has read new log entries and fresh
Actuator samples. Keeping the extraction here (separate from the ingestor) means
it can be unit-tested against fixed inputs without any HTTP/IO.

The four operational signal families (story Section 2 / the T2 task)
-------------------------------------------------------------------
1. **Error patterns from logs** — repeated failures, recurring error messages,
   retry loops, timeout messages, and failed downstream calls are turned into
   structured pattern records instead of remaining raw text. See
   :func:`extract_error_patterns`.
2. **Latency & throughput degradation from diagnostics/metrics** — rising
   response time, dropping request volume, rising error rate, or a service going
   unhealthy is captured as operational evidence. See
   :func:`extract_degradation_signals`.
3. **Exception clustering** — related exceptions are grouped by type + originating
   stack frame so recurring problem areas surface as one cluster rather than many
   separate lines. See :func:`cluster_exceptions`.
4. **Resource pressure** — memory, CPU, thread-pool, and queue pressure exposed by
   the running application are captured where available. See
   :func:`extract_resource_pressure`.

Provenance (R16-B1 / story Section 3, T4 / AC4)
-----------------------------------------------
Every signal this module produces carries a fully-populated, OBSERVED
:class:`~app.provenance.EvidencePointer`: ``source_system='java_app'``, a stable
artifact id, a source timestamp, and ``origin='observed'``. Operational signals
are *directly measured* — they are first-class observed evidence, never inferred —
so they need no ``extraction_job_id`` and can corroborate and elevate findings in
other systems. See :func:`build_evidence_pointer`.

Reach/depth boundary (story Section 0 / AC8)
--------------------------------------------
Phase one reads the **operational surface only** — what the running application
reports about its own behaviour (logs + diagnostics endpoints). It does NOT read
the application's source code (that is the separate 1.8 code-and-structure phase),
and it does not pull external APM/observability-platform data. This module only
consumes log entries and metric samples; it never touches source.

Input shapes (produced by the T1 ingestor)
-------------------------------------------
``log_entries`` — a list of structured log records::

    {
      "timestamp": "2026-06-29T10:15:00Z",        # ISO-8601 (optional)
      "level": "ERROR",                           # log level
      "logger": "com.acme.payments.PaymentService",
      "message": "Read timed out calling billing-api",
      "thread": "http-nio-8080-exec-3",           # optional
      "exception": {                              # optional structured throwable
        "type": "java.net.SocketTimeoutException",
        "message": "Read timed out",
        "stack_trace": ["com.acme.payments.HttpClient.call(HttpClient.java:88)", ...]
      }
    }

``metric_samples`` — a list of normalized point-in-time diagnostic readings, each
one Actuator scrape mapped to the fields below (all metric fields optional; the
extractors degrade gracefully when a field is absent)::

    {
      "service": "payments-service",
      "endpoint": "http://app:8080/actuator",
      "timestamp": "2026-06-29T10:15:00Z",
      "health": "UP",                  # UP | DOWN | OUT_OF_SERVICE | UNKNOWN
      "latency_p95_ms": 120.0,         # response time
      "throughput_rpm": 5000.0,        # request volume
      "error_rate": 0.002,             # fraction of requests erroring (0..1)
      "memory_used_bytes": 1_500_000_000,
      "memory_max_bytes": 2_000_000_000,
      "cpu_usage": 0.42,               # 0..1
      "thread_pool_active": 18,
      "thread_pool_size": 20,
      "queue_depth": 5,
      "queue_capacity": 100
    }
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.provenance import EvidencePointer, utc_now_iso

# ─────────────────────────────────────────────────────────────────────────────
# Source identity
# ─────────────────────────────────────────────────────────────────────────────
#: The source_system stamped on every Java-app evidence pointer and the key the
#: corroboration engine reads the signal block under (T5). A Java-app signal must
#: always be reported under this id so cross-system corroboration can find it.
JAVA_APP_SYSTEM = "java_app"
JAVA_APP_CORROBORATION_KEY = JAVA_APP_SYSTEM

#: Log levels that count as a failure/problem signal. Lower levels (INFO/DEBUG)
#: are operational noise and never become an error pattern.
ERROR_LEVELS = frozenset({"WARN", "WARNING", "ERROR", "FATAL", "SEVERE", "CRITICAL"})

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds — error-pattern extraction
# ─────────────────────────────────────────────────────────────────────────────
#: A normalized message template seen at least this many times is "recurring"
#: (covers both "repeated failures" and "recurring error messages").
RECURRING_MIN_COUNT = 3
#: A retry-marked template needs at least this many occurrences (or an explicit
#: attempt number >= 2) to be flagged as a retry *loop* rather than a one-off.
RETRY_LOOP_MIN_COUNT = 2

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds — exception clustering
# ─────────────────────────────────────────────────────────────────────────────
#: An exception cluster of at least this size marks a recurring problem area.
EXCEPTION_RECURRING_MIN_COUNT = 2

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds — latency / throughput / error-rate degradation
# ─────────────────────────────────────────────────────────────────────────────
#: Recent p95 latency must exceed the baseline by this fraction to degrade (+30%).
LATENCY_DEGRADATION_PCT = 0.30
#: Recent request volume must fall by this fraction below the baseline (-30%).
THROUGHPUT_DROP_PCT = 0.30
#: Recent error rate must rise by at least this absolute amount (2 percentage pts)
#: AND clear the floor below before it is reported, so trivial noise is ignored.
ERROR_RATE_RISE_ABS = 0.02
ERROR_RATE_FLOOR = 0.01
#: Health states that are NOT "healthy" — any of these in the latest sample fires
#: a ``service_unhealthy`` degradation signal.
HEALTHY_STATE = "UP"

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds — resource pressure (utilization ratios, 0..1)
# ─────────────────────────────────────────────────────────────────────────────
MEMORY_PRESSURE_RATIO = 0.85
CPU_PRESSURE_RATIO = 0.85
THREAD_POOL_PRESSURE_RATIO = 0.85
QUEUE_PRESSURE_RATIO = 0.80

# ─────────────────────────────────────────────────────────────────────────────
# Pattern-matching markers (structured detection over message text — NOT NLP)
# ─────────────────────────────────────────────────────────────────────────────
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_HEX0X_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_LONGHEX_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_WS_RE = re.compile(r"\s+")

#: Timeout markers (message text or exception class name).
_TIMEOUT_RE = re.compile(
    r"(?:tim(?:ed|e)\s*[\s-]?out|time-?out|timeoutexception)", re.IGNORECASE
)
#: Retry-loop markers, including explicit "attempt N of M" / "attempt N".
_RETRY_RE = re.compile(
    r"\b(?:retry|retrying|re-?attempt|back-?off|giving up after|"
    r"attempt\s+\d+(?:\s*(?:of|/)\s*\d+)?)\b",
    re.IGNORECASE,
)
_ATTEMPT_RE = re.compile(r"attempt\s+(\d+)|(\d+)\s*(?:of|/)\s*\d+", re.IGNORECASE)
#: Failed-downstream-call markers: upstream/downstream failures, refused/reset
#: connections, circuit breaker trips, and 5xx gateway responses.
_DOWNSTREAM_FAIL_RE = re.compile(
    r"\b(?:downstream|upstream|connection refused|connection reset|unreachable|"
    r"service unavailable|circuit breaker|bad gateway|gateway timeout|"
    r"no route to host|failed to (?:call|connect|reach|invoke|fetch)|"
    r"50[234])\b",
    re.IGNORECASE,
)
#: Best-effort downstream target extraction (the named dependency that failed).
_DS_TARGET_RES = (
    re.compile(
        r"(?:calling|invoking|reach(?:ing)?|connect(?:ing)?\s+to)\s+"
        r"(?:the\s+)?(?:downstream\s+)?(?:service\s+)?['\"]?([A-Za-z][\w.-]{1,60})['\"]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"downstream (?:service|call|dependency)\s+['\"]?([A-Za-z][\w.-]{1,60})['\"]?",
        re.IGNORECASE,
    ),
)
#: Java exception class names: fully-qualified (``java.net.SocketTimeoutException``)
#: and bare (``NullPointerException``). Used when a log line has no structured
#: ``exception`` block but names a throwable in its message.
_EXC_FQCN_RE = re.compile(r"\b((?:[a-zA-Z_]\w*\.)+[A-Z]\w*(?:Exception|Error))\b")
_EXC_SIMPLE_RE = re.compile(r"\b([A-Z]\w*(?:Exception|Error))\b")


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``); None if unparseable."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _stable_id(*parts: Any) -> str:
    """Deterministic short id from the given parts (stable across runs, diff-friendly)."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _mean(values: Iterable[float]) -> Optional[float]:
    """Mean of the numeric values, or None when the iterable is empty."""
    vals = [float(v) for v in values if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else None


def _num(value: Any) -> Optional[float]:
    """Coerce to float, or None if not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _normalize_message(message: str) -> str:
    """Collapse a raw log message to a template by masking variable tokens.

    UUIDs, IPs/ports, hex ids, quoted strings, and numbers are replaced with
    placeholders so e.g. "timed out to 10.0.0.5:8080 after 1200ms" and
    "timed out to 10.0.0.7:9090 after 1500ms" collapse to one template. This is
    structured pattern matching, not interpretation of meaning.
    """
    if not message or not isinstance(message, str):
        return ""
    text = _UUID_RE.sub("<uuid>", message)
    text = _IP_RE.sub("<ip>", text)
    text = _HEX0X_RE.sub("<hex>", text)
    text = _LONGHEX_RE.sub("<hex>", text)
    text = _QUOTED_RE.sub("<str>", text)
    text = _NUM_RE.sub("<n>", text)
    return _WS_RE.sub(" ", text).strip()


def _iso_range(timestamps: Iterable[Optional[datetime]]) -> Tuple[Optional[str], Optional[str]]:
    """Return (first_seen, last_seen) ISO strings from a set of parsed timestamps."""
    present = [t for t in timestamps if t is not None]
    if not present:
        return None, None
    return min(present).isoformat(), max(present).isoformat()


def _extract_downstream_target(message: str) -> Optional[str]:
    """Best-effort name of the failed downstream dependency, or None."""
    for rx in _DS_TARGET_RES:
        m = rx.search(message or "")
        if m and m.group(1):
            return m.group(1)
    return None


def build_evidence_pointer(
    artifact_id: str, source_timestamp: Optional[str]
) -> Dict[str, Any]:
    """Build the R16-B1 OBSERVED EvidencePointer for one Java-app signal (T4 / AC4).

    Every operational signal is traceable back to the application it was measured
    on, so each carries an observed pointer:

      * ``source_system`` = ``'java_app'``
      * ``source_artifact`` = the stable signal artifact id (service + signal kind
        + discriminator), so ``source_artifact_type`` is ``'record_id'``
      * ``source_timestamp`` = when the signal was observed (the last log
        occurrence / the latest metric sample); falls back to now only if no
        timestamp is available, so the mandatory spine is always populated
      * ``origin`` = ``'observed'`` — directly measured runtime behaviour, never
        inferred, so no ``extraction_job_id`` is required

    Returned as a JSON-serialisable dict ready to attach to the signal record.
    """
    return EvidencePointer.observed(
        source_system=JAVA_APP_SYSTEM,
        source_artifact=artifact_id,
        source_timestamp=source_timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# Signal family 1 — error patterns from logs
# ─────────────────────────────────────────────────────────────────────────────
def extract_error_patterns(
    log_entries: Iterable[Dict[str, Any]], *, application_id: str = JAVA_APP_SYSTEM
) -> List[Dict[str, Any]]:
    """Turn raw error/warn log lines into structured error-pattern signals.

    Error and warning level entries are grouped by a normalized message template
    (so the same failure with different ids/numbers collapses into one group),
    then each group is categorised:

      * ``recurring`` — seen at least :data:`RECURRING_MIN_COUNT` times (repeated
        failures / recurring error messages)
      * ``timeout`` — the message or its exception names a timeout
      * ``failed_downstream_call`` — an upstream/downstream call failure (refused/
        reset connection, circuit breaker, 5xx, "failed to call …")
      * ``retry_loop`` — retry markers with repetition (or an explicit attempt
        number >= 2)

    A group is returned only when it matches at least one category, so a lone
    benign error never becomes a "pattern". Each pattern carries an observed
    evidence pointer. Results are sorted by count (desc), then template (asc).
    """
    groups: Dict[str, Dict[str, Any]] = {}
    for entry in log_entries or []:
        if not isinstance(entry, dict):
            continue
        level = str(entry.get("level", "")).upper()
        if level not in ERROR_LEVELS:
            continue
        message = str(entry.get("message", "") or "")
        exc = entry.get("exception") if isinstance(entry.get("exception"), dict) else {}
        exc_type = str(exc.get("type", "") or "")
        # The text we match markers against = message + exception type/message.
        marker_text = " ".join(
            t for t in (message, exc_type, str(exc.get("message", "") or "")) if t
        )
        template = _normalize_message(message) or _normalize_message(marker_text) or "(empty)"

        g = groups.setdefault(
            template,
            {
                "messages": [],
                "marker_texts": [],
                "levels": Counter(),
                "loggers": Counter(),
                "timestamps": [],
                "downstream_targets": Counter(),
                "max_attempt": 0,
            },
        )
        g["messages"].append(message)
        g["marker_texts"].append(marker_text)
        g["levels"][level] += 1
        if entry.get("logger"):
            g["loggers"][str(entry["logger"])] += 1
        g["timestamps"].append(_parse_iso(entry.get("timestamp")))
        target = _extract_downstream_target(message)
        if target:
            g["downstream_targets"][target] += 1
        for m in _ATTEMPT_RE.finditer(marker_text):
            attempt = next((int(x) for x in m.groups() if x), 0)
            g["max_attempt"] = max(g["max_attempt"], attempt)

    patterns: List[Dict[str, Any]] = []
    for template, g in groups.items():
        count = len(g["messages"])
        joined = " \n ".join(g["marker_texts"])
        downstream_target = (
            g["downstream_targets"].most_common(1)[0][0]
            if g["downstream_targets"]
            else None
        )
        categories: List[str] = []

        if count >= RECURRING_MIN_COUNT:
            categories.append("recurring")
        if _TIMEOUT_RE.search(joined):
            categories.append("timeout")
        # A failed downstream call is either an explicit failure marker (refused/
        # reset/circuit-breaker/5xx) OR any error that names an outbound call to a
        # dependency — e.g. a timeout "calling billing-api" is a downstream failure.
        if _DOWNSTREAM_FAIL_RE.search(joined) or downstream_target:
            categories.append("failed_downstream_call")
        if _RETRY_RE.search(joined) and (
            count >= RETRY_LOOP_MIN_COUNT or g["max_attempt"] >= 2
        ):
            categories.append("retry_loop")

        if not categories:
            continue

        first_seen, last_seen = _iso_range(g["timestamps"])
        level = g["levels"].most_common(1)[0][0] if g["levels"] else "ERROR"
        logger = g["loggers"].most_common(1)[0][0] if g["loggers"] else None
        pattern_id = _stable_id(application_id, "log_pattern", template)
        artifact_id = f"{application_id}:log_pattern:{pattern_id}"
        patterns.append(
            {
                "pattern_id": pattern_id,
                "source_system": JAVA_APP_SYSTEM,
                "application_id": application_id,
                "template": template,
                "sample_message": g["messages"][0],
                "count": count,
                "categories": sorted(categories),
                "level": level,
                "logger": logger,
                "downstream_target": downstream_target,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "evidence_pointer": build_evidence_pointer(artifact_id, last_seen),
            }
        )

    patterns.sort(key=lambda p: (-p["count"], p["template"]))
    return patterns


# ─────────────────────────────────────────────────────────────────────────────
# Signal family 3 — exception clustering
# ─────────────────────────────────────────────────────────────────────────────
def _top_frame(exception: Dict[str, Any]) -> Optional[str]:
    """Return the top (most-recent) stack frame as a normalized string, or None."""
    stack = exception.get("stack_trace")
    frame: Optional[str] = None
    if isinstance(stack, list) and stack:
        frame = str(stack[0])
    elif isinstance(stack, str) and stack.strip():
        # First non-empty line; prefer a line that looks like a frame ("at …").
        lines = [ln.strip() for ln in stack.splitlines() if ln.strip()]
        at_lines = [ln for ln in lines if ln.lower().startswith("at ")]
        frame = (at_lines or lines)[0] if (at_lines or lines) else None
    if not frame:
        return None
    frame = frame.strip()
    if frame.lower().startswith("at "):
        frame = frame[3:].strip()
    return frame


def _frame_signature(frame: Optional[str]) -> str:
    """Class.method portion of a frame, with the ``(File.java:NN)`` suffix dropped.

    Dropping the line number keeps a cluster stable across the small line drift a
    code change causes, so the same fault still clusters together.
    """
    if not frame:
        return ""
    return frame.split("(", 1)[0].strip()


def _exception_type_from_entry(entry: Dict[str, Any]) -> Optional[str]:
    """Resolve the exception class for a log entry, structured first then message."""
    exc = entry.get("exception")
    if isinstance(exc, dict) and exc.get("type"):
        return str(exc["type"])
    message = str(entry.get("message", "") or "")
    m = _EXC_FQCN_RE.search(message)
    if m:
        return m.group(1)
    m = _EXC_SIMPLE_RE.search(message)
    return m.group(1) if m else None


def cluster_exceptions(
    log_entries: Iterable[Dict[str, Any]], *, application_id: str = JAVA_APP_SYSTEM
) -> List[Dict[str, Any]]:
    """Group related exceptions so recurring problem areas surface as one cluster.

    Every log entry that carries (or names) an exception is keyed by
    ``exception_type`` + originating stack-frame signature (line numbers dropped),
    falling back to the normalized exception/message text when there is no stack
    trace. Related exceptions therefore collapse into a single cluster with a
    count, instead of being treated as many separate issues.

    Each cluster carries an observed evidence pointer and a ``recurring`` flag
    (count >= :data:`EXCEPTION_RECURRING_MIN_COUNT`). Sorted by count (desc), then
    signature (asc).
    """
    clusters: Dict[str, Dict[str, Any]] = {}
    for entry in log_entries or []:
        if not isinstance(entry, dict):
            continue
        exc_type = _exception_type_from_entry(entry)
        if not exc_type:
            continue
        exc = entry.get("exception") if isinstance(entry.get("exception"), dict) else {}
        top_frame = _top_frame(exc)
        frame_sig = _frame_signature(top_frame)
        if frame_sig:
            signature = f"{exc_type}@{frame_sig}"
        else:
            exc_msg = str(exc.get("message", "") or entry.get("message", "") or "")
            signature = f"{exc_type}@{_normalize_message(exc_msg)}"

        c = clusters.setdefault(
            signature,
            {
                "exception_type": exc_type,
                "top_frame": top_frame,
                "loggers": set(),
                "messages": [],
                "timestamps": [],
            },
        )
        if entry.get("logger"):
            c["loggers"].add(str(entry["logger"]))
        c["messages"].append(str(exc.get("message", "") or entry.get("message", "") or ""))
        c["timestamps"].append(_parse_iso(entry.get("timestamp")))
        # Keep the first non-empty top frame we ever see for this signature.
        if top_frame and not c["top_frame"]:
            c["top_frame"] = top_frame

    out: List[Dict[str, Any]] = []
    for signature, c in clusters.items():
        count = len(c["messages"])
        first_seen, last_seen = _iso_range(c["timestamps"])
        cluster_id = _stable_id(application_id, "exception", signature)
        artifact_id = f"{application_id}:exception:{cluster_id}"
        out.append(
            {
                "cluster_id": cluster_id,
                "source_system": JAVA_APP_SYSTEM,
                "application_id": application_id,
                "exception_type": c["exception_type"],
                "signature": signature,
                "top_frame": c["top_frame"],
                "count": count,
                "recurring": count >= EXCEPTION_RECURRING_MIN_COUNT,
                "loggers": sorted(c["loggers"]),
                "representative_message": next((m for m in c["messages"] if m), ""),
                "first_seen": first_seen,
                "last_seen": last_seen,
                "evidence_pointer": build_evidence_pointer(artifact_id, last_seen),
            }
        )

    out.sort(key=lambda x: (-x["count"], x["signature"]))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Signal family 2 — latency / throughput / error-rate / health degradation
# ─────────────────────────────────────────────────────────────────────────────
def _samples_by_service(
    metric_samples: Iterable[Dict[str, Any]], application_id: str
) -> Dict[str, List[Dict[str, Any]]]:
    """Group samples by service id (falling back to ``application_id``), time-sorted."""
    by_service: Dict[str, List[Tuple[Tuple[int, Any, int], Dict[str, Any]]]] = {}
    for idx, sample in enumerate(metric_samples or []):
        if not isinstance(sample, dict):
            continue
        service = str(sample.get("service") or application_id)
        parsed = _parse_iso(sample.get("timestamp"))
        # Sort key keeps timestamped samples in chronological order and pushes
        # undated samples to the end in stable arrival order (deterministic).
        sort_key = (0, parsed, idx) if parsed is not None else (1, None, idx)
        by_service.setdefault(service, []).append((sort_key, sample))

    ordered: Dict[str, List[Dict[str, Any]]] = {}
    for service, items in by_service.items():
        items.sort(key=lambda it: (it[0][0], it[0][1] or datetime.min.replace(tzinfo=timezone.utc), it[0][2]))
        ordered[service] = [s for _, s in items]
    return ordered


def _split_baseline_recent(
    samples: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split time-ordered samples into (baseline, recent) windows.

    The earlier half is the baseline; the later half (including the midpoint for
    odd counts) is "recent". Degradation is the move from baseline to recent.
    """
    mid = len(samples) // 2
    return samples[:mid], samples[mid:]


def _degradation_record(
    *,
    kind: str,
    service: str,
    application_id: str,
    metric: str,
    baseline_value: Optional[float],
    current_value: Optional[float],
    direction: str,
    change_pct: Optional[float],
    first_seen: Optional[str],
    last_seen: Optional[str],
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    artifact_id = f"{service}:metric:{kind}"
    rec = {
        "signal_id": _stable_id(application_id, service, kind),
        "kind": kind,
        "source_system": JAVA_APP_SYSTEM,
        "application_id": application_id,
        "service": service,
        "metric": metric,
        "baseline_value": baseline_value,
        "current_value": current_value,
        "direction": direction,
        "change_pct": change_pct,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "evidence_pointer": build_evidence_pointer(artifact_id, last_seen),
    }
    if detail:
        rec.update(detail)
    return rec


def extract_degradation_signals(
    metric_samples: Iterable[Dict[str, Any]], *, application_id: str = JAVA_APP_SYSTEM
) -> List[Dict[str, Any]]:
    """Detect latency / throughput / error-rate / health degradation from samples.

    Per service, samples are time-ordered and split into a baseline (earlier) and
    recent (later) window. A signal fires when:

      * ``latency_degradation`` — recent p95 latency exceeds baseline by
        :data:`LATENCY_DEGRADATION_PCT`
      * ``throughput_degradation`` — recent request volume falls below baseline by
        :data:`THROUGHPUT_DROP_PCT`
      * ``error_rate_rise`` — recent error rate rises by at least
        :data:`ERROR_RATE_RISE_ABS` and clears :data:`ERROR_RATE_FLOOR`
      * ``service_unhealthy`` — the latest sample's health is not ``UP``

    Each signal carries baseline/current values, percent change, and an observed
    evidence pointer. Sorted by service, then kind.
    """
    results: List[Dict[str, Any]] = []
    for service, samples in _samples_by_service(metric_samples, application_id).items():
        if not samples:
            continue
        first_seen, last_seen = _iso_range(_parse_iso(s.get("timestamp")) for s in samples)
        latest = samples[-1]

        # ── Health: latest sample not UP ────────────────────────────────────
        health = latest.get("health")
        if health is not None and str(health).upper() != HEALTHY_STATE:
            results.append(
                _degradation_record(
                    kind="service_unhealthy",
                    service=service,
                    application_id=application_id,
                    metric="health",
                    baseline_value=None,
                    current_value=None,
                    direction="down",
                    change_pct=None,
                    first_seen=first_seen,
                    last_seen=last_seen,
                    detail={"health": str(health).upper()},
                )
            )

        # The trend signals need at least a baseline and a recent window.
        if len(samples) < 2:
            continue
        baseline, recent = _split_baseline_recent(samples)

        # ── Latency degradation (p95 up) ────────────────────────────────────
        base_lat = _mean(v for v in (_num(s.get("latency_p95_ms")) for s in baseline) if v is not None)
        cur_lat = _mean(v for v in (_num(s.get("latency_p95_ms")) for s in recent) if v is not None)
        if base_lat and cur_lat and base_lat > 0 and cur_lat >= base_lat * (1 + LATENCY_DEGRADATION_PCT):
            results.append(
                _degradation_record(
                    kind="latency_degradation",
                    service=service,
                    application_id=application_id,
                    metric="latency_p95_ms",
                    baseline_value=round(base_lat, 3),
                    current_value=round(cur_lat, 3),
                    direction="up",
                    change_pct=round((cur_lat - base_lat) / base_lat * 100, 2),
                    first_seen=first_seen,
                    last_seen=last_seen,
                )
            )

        # ── Throughput degradation (request volume down) ────────────────────
        base_tp = _mean(v for v in (_num(s.get("throughput_rpm")) for s in baseline) if v is not None)
        cur_tp = _mean(v for v in (_num(s.get("throughput_rpm")) for s in recent) if v is not None)
        if base_tp and cur_tp is not None and base_tp > 0 and cur_tp <= base_tp * (1 - THROUGHPUT_DROP_PCT):
            results.append(
                _degradation_record(
                    kind="throughput_degradation",
                    service=service,
                    application_id=application_id,
                    metric="throughput_rpm",
                    baseline_value=round(base_tp, 3),
                    current_value=round(cur_tp, 3),
                    direction="down",
                    change_pct=round((cur_tp - base_tp) / base_tp * 100, 2),
                    first_seen=first_seen,
                    last_seen=last_seen,
                )
            )

        # ── Error-rate rise ─────────────────────────────────────────────────
        base_er = _mean(v for v in (_num(s.get("error_rate")) for s in baseline) if v is not None)
        cur_er = _mean(v for v in (_num(s.get("error_rate")) for s in recent) if v is not None)
        if (
            base_er is not None
            and cur_er is not None
            and cur_er >= ERROR_RATE_FLOOR
            and (cur_er - base_er) >= ERROR_RATE_RISE_ABS
        ):
            change_pct = round((cur_er - base_er) / base_er * 100, 2) if base_er > 0 else None
            results.append(
                _degradation_record(
                    kind="error_rate_rise",
                    service=service,
                    application_id=application_id,
                    metric="error_rate",
                    baseline_value=round(base_er, 5),
                    current_value=round(cur_er, 5),
                    direction="up",
                    change_pct=change_pct,
                    first_seen=first_seen,
                    last_seen=last_seen,
                )
            )

    results.sort(key=lambda r: (r["service"], r["kind"]))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Signal family 4 — resource pressure
# ─────────────────────────────────────────────────────────────────────────────
def _pressure_record(
    *,
    kind: str,
    service: str,
    application_id: str,
    resource: str,
    utilization: float,
    threshold: float,
    current: Optional[float],
    capacity: Optional[float],
    timestamp: Optional[str],
) -> Dict[str, Any]:
    artifact_id = f"{service}:pressure:{kind}"
    return {
        "signal_id": _stable_id(application_id, service, kind),
        "kind": kind,
        "source_system": JAVA_APP_SYSTEM,
        "application_id": application_id,
        "service": service,
        "resource": resource,
        "utilization": round(utilization, 4),
        "threshold": threshold,
        "current": current,
        "capacity": capacity,
        "timestamp": timestamp,
        "evidence_pointer": build_evidence_pointer(artifact_id, timestamp),
    }


def extract_resource_pressure(
    metric_samples: Iterable[Dict[str, Any]], *, application_id: str = JAVA_APP_SYSTEM
) -> List[Dict[str, Any]]:
    """Capture memory / CPU / thread-pool / queue pressure where exposed.

    The most recent sample per service is evaluated (current pressure state). A
    signal fires when a utilization ratio crosses its threshold:

      * ``memory_pressure`` — used/max heap >= :data:`MEMORY_PRESSURE_RATIO`
      * ``cpu_pressure`` — CPU usage >= :data:`CPU_PRESSURE_RATIO`
      * ``thread_pool_pressure`` — active/size >= :data:`THREAD_POOL_PRESSURE_RATIO`
      * ``queue_pressure`` — depth/capacity >= :data:`QUEUE_PRESSURE_RATIO`

    Resource metrics are optional; a family is simply skipped when the sample does
    not expose it. Each signal carries an observed evidence pointer. Sorted by
    service, then kind.
    """
    results: List[Dict[str, Any]] = []
    for service, samples in _samples_by_service(metric_samples, application_id).items():
        if not samples:
            continue
        latest = samples[-1]
        ts = _parse_iso(latest.get("timestamp"))
        timestamp = ts.isoformat() if ts else None

        # ── Memory ──────────────────────────────────────────────────────────
        used = _num(latest.get("memory_used_bytes"))
        max_mem = _num(latest.get("memory_max_bytes"))
        if used is not None and max_mem and max_mem > 0:
            util = used / max_mem
            if util >= MEMORY_PRESSURE_RATIO:
                results.append(
                    _pressure_record(
                        kind="memory_pressure", service=service,
                        application_id=application_id, resource="memory",
                        utilization=util, threshold=MEMORY_PRESSURE_RATIO,
                        current=used, capacity=max_mem, timestamp=timestamp,
                    )
                )

        # ── CPU ─────────────────────────────────────────────────────────────
        cpu = _num(latest.get("cpu_usage"))
        if cpu is not None and cpu >= CPU_PRESSURE_RATIO:
            results.append(
                _pressure_record(
                    kind="cpu_pressure", service=service,
                    application_id=application_id, resource="cpu",
                    utilization=cpu, threshold=CPU_PRESSURE_RATIO,
                    current=cpu, capacity=1.0, timestamp=timestamp,
                )
            )

        # ── Thread pool ─────────────────────────────────────────────────────
        active = _num(latest.get("thread_pool_active"))
        size = _num(latest.get("thread_pool_size"))
        if active is not None and size and size > 0:
            util = active / size
            if util >= THREAD_POOL_PRESSURE_RATIO:
                results.append(
                    _pressure_record(
                        kind="thread_pool_pressure", service=service,
                        application_id=application_id, resource="thread_pool",
                        utilization=util, threshold=THREAD_POOL_PRESSURE_RATIO,
                        current=active, capacity=size, timestamp=timestamp,
                    )
                )

        # ── Queue ───────────────────────────────────────────────────────────
        depth = _num(latest.get("queue_depth"))
        capacity = _num(latest.get("queue_capacity"))
        if depth is not None and capacity and capacity > 0:
            util = depth / capacity
            if util >= QUEUE_PRESSURE_RATIO:
                results.append(
                    _pressure_record(
                        kind="queue_pressure", service=service,
                        application_id=application_id, resource="queue",
                        utilization=util, threshold=QUEUE_PRESSURE_RATIO,
                        current=depth, capacity=capacity, timestamp=timestamp,
                    )
                )

    results.sort(key=lambda r: (r["service"], r["kind"]))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation — the downstream signal block
# ─────────────────────────────────────────────────────────────────────────────
def build_java_app_signal(
    *,
    log_entries: Optional[Iterable[Dict[str, Any]]] = None,
    metric_samples: Optional[Iterable[Dict[str, Any]]] = None,
    application_id: str = JAVA_APP_SYSTEM,
) -> Dict[str, Any]:
    """Aggregate all four operational signal families into one structured block.

    This is the "produces operational signal" deliverable (AC1): given the raw
    operational surface (logs + diagnostics samples), it returns the structured
    signal that downstream discovery/corroboration consumes — never raw text. The
    ``summary.has_friction`` flag is the quick "this application shows runtime
    friction" indicator.
    """
    log_entries = list(log_entries or [])
    metric_samples = list(metric_samples or [])

    error_patterns = extract_error_patterns(log_entries, application_id=application_id)
    exception_clusters = cluster_exceptions(log_entries, application_id=application_id)
    degradations = extract_degradation_signals(metric_samples, application_id=application_id)
    resource_pressure = extract_resource_pressure(metric_samples, application_id=application_id)

    services = sorted(
        {str(s.get("service")) for s in metric_samples if isinstance(s, dict) and s.get("service")}
    )

    return {
        "application_id": application_id,
        "source_system": JAVA_APP_SYSTEM,
        "error_patterns": error_patterns,
        "exception_clusters": exception_clusters,
        "degradations": degradations,
        "resource_pressure": resource_pressure,
        "summary": {
            "services": services,
            "error_pattern_count": len(error_patterns),
            "exception_cluster_count": len(exception_clusters),
            "degradation_count": len(degradations),
            "resource_pressure_count": len(resource_pressure),
            "has_friction": bool(
                error_patterns or exception_clusters or degradations or resource_pressure
            ),
        },
    }


def build_java_app_corroboration_payload(
    *,
    log_entries: Optional[Iterable[Dict[str, Any]]] = None,
    metric_samples: Optional[Iterable[Dict[str, Any]]] = None,
    application_id: str = JAVA_APP_SYSTEM,
) -> Dict[str, Any]:
    """Package Java-app signal into the corroboration-engine input block (feeds T5).

    Wraps :func:`build_java_app_signal` under the ``'java_app'`` key the
    corroboration engine reads, and surfaces the compact ``corroboration_markers``
    a cross-system rule needs: the affected service names plus ``fired`` + latest
    ``timestamp`` blocks for the signals that naturally corroborate other systems
    (a rising error rate / latency degradation / unhealthy service corroborating a
    ServiceNow incident spike for the same service — story Section 3 / AC5).

    This function only *produces* the signal in a shape the engine can consume; it
    attaches no confidence and performs no elevation. Wiring the corroboration
    rule itself is the separate T5 task. Operational signals are OBSERVED evidence,
    so — unlike the Slack MEDIUM ceiling — they are first-class corroborators.
    """
    signal = build_java_app_signal(
        log_entries=log_entries, metric_samples=metric_samples, application_id=application_id
    )

    def _marker(kinds: set) -> Dict[str, Any]:
        hits = [d for d in signal["degradations"] if d["kind"] in kinds]
        timestamp = max((d["last_seen"] for d in hits if d.get("last_seen")), default=None)
        return {
            "fired": bool(hits),
            "timestamp": timestamp,
            "services": sorted({d["service"] for d in hits}),
        }

    recurring_exc = [c for c in signal["exception_clusters"] if c["recurring"]]
    markers = {
        "services": signal["summary"]["services"],
        "error_rate_rise": _marker({"error_rate_rise"}),
        "latency_degradation": _marker({"latency_degradation"}),
        "service_unhealthy": _marker({"service_unhealthy"}),
        "recurring_exceptions": {
            "fired": bool(recurring_exc),
            "count": len(recurring_exc),
            "exception_types": sorted({c["exception_type"] for c in recurring_exc}),
        },
        "friction": {"fired": signal["summary"]["has_friction"]},
    }

    return {JAVA_APP_CORROBORATION_KEY: {**signal, "corroboration_markers": markers}}
