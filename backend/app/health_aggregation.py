"""R18-C2 T1 — Run-Health Dashboard aggregation layer.

Read-only, org-scoped assembly of the operational state the Run-Health Dashboard
renders. The design principle (R18-C2 §Design) is strict: **the dashboard
ASSEMBLES existing truth; it does not invent new instrumentation.** Every value
here is read from records/events the platform already emits — connector records
and health checks, the ingestion checkpoint repository, telemetry events, run
records and run events, the R18-B2 freshness metrics, and the R16-B1 pack
version stamps. This module is a READER, never a second source of truth: it
performs no writes and never computes a state speculatively.

Five view builders, each taking an ``org_id`` that the route resolves from the
tenancy context (never from a request body/query — cross-tenant reads are
impossible):

* ``connectors_view``       — per connected system: connection state, auth mode,
                              last successful ingestion, checkpoint position/age,
                              last error.
* ``runs_view``             — recent discovery runs: status (with non-blocking
                              degradation made visible), duration, systems,
                              detectors evaluated/fired, opportunities, per-stage
                              outcomes.
* ``content_freshness_view`` — retrieval substrate health: indexed volume per
                              source, embedding backlog, stale chunks, refresh/
                              backfill progress (R18-B2), redaction count, and
                              skipped-with-reason items.
* ``packs_view``            — packs executed on the latest run, their versions
                              (R16-B1 stamp) and detector counts.
* ``attention_view``        — deterministic actionable conditions derived from
                              those same records, severity ordered and linked to
                              the supporting dashboard panel.

Resilience: connector/run assembly is defensive per item — one malformed record
never blanks the whole panel. The ONE deliberate exception is the freshness read
inside ``content_freshness_view``: R18-B2 metrics must never degrade to a
false-healthy zero, so a store read failure is allowed to propagate and surface
as an HTTP error (matching ``retrieval/metrics.py``).

Skipped-with-reason items are read from the ``ingestion.artifact_skipped``
telemetry event that R18-C2 T2 emits at origin in the document ingestor
(``discovery/ingest/documents.py``); ``content_freshness_view`` groups them by
reason. An org with no skips simply yields an empty breakdown.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from . import db
from .telemetry import get_telemetry_range


# Attention-rule thresholds are deliberately explicit and version-controlled.
# They are product rules, not inferred heuristics, so identical health records
# always produce the same conditions and severity.
ATTENTION_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
STALLED_CHECKPOINT_SECONDS = 24 * 60 * 60
GROWING_BACKLOG_MIN_CHUNKS = 50
GROWING_BACKLOG_MIN_SPAN_SECONDS = 15 * 60
REPEATED_STAGE_RUN_WINDOW = 5
REPEATED_STAGE_MIN_RUNS = 2

# Freshness + store are imported lazily inside content_freshness_view so this
# module (and the connectors/runs/packs panels) still import cleanly in an
# environment without the pgvector retrieval tables.


# ── time helpers ──────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _age_seconds(value: Any) -> Optional[int]:
    dt = _parse_iso(value if isinstance(value, str) else _to_iso(value))
    if dt is None:
        return None
    return max(0, int((_now() - dt).total_seconds()))


def _duration_seconds(started: Any, updated: Any) -> Optional[int]:
    start = _parse_iso(started)
    end = _parse_iso(updated)
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _wide_range() -> tuple[datetime, datetime]:
    """A whole-history time window for telemetry reads (get_telemetry_range needs
    an explicit range; the dashboard wants "all events for this org")."""
    return (datetime(2000, 1, 1, tzinfo=timezone.utc), _now() + timedelta(days=1))


def _safe_range(org_id: str, event_type: str) -> List[Any]:
    """Org-scoped telemetry read that never breaks a panel: returns [] on error
    or when the (possibly not-yet-emitted) event type has no rows."""
    frm, to = _wide_range()
    try:
        return get_telemetry_range(org_id, event_type, frm, to, limit=1000) or []
    except Exception:
        return []


def _latest_by(events: List[Any], predicate) -> Optional[Any]:
    """Newest matching event (get_telemetry_range is oldest-first, so take the
    last match)."""
    match = None
    for e in events:
        try:
            if predicate(e):
                match = e
        except Exception:
            continue
    return match


# ── Connectors panel ────────────────────────────────────────────────────────

def _auth_mode(org_id: str, connector_id: str) -> Optional[str]:
    """oauth | static | None — read from the credential vault kind, never the
    secret value."""
    try:
        from .auth import vault

        rec = vault.get_credential(org_id, connector_id)
    except Exception:
        return None
    if rec is None:
        return None
    kind = getattr(rec, "kind", None)
    if kind in ("oauth", "static"):
        return kind
    return "static" if type(rec).__name__ == "StaticCredentialRecord" else "oauth"


def _read_checkpoint(org_id: str, connector_id: str):
    try:
        try:
            from discovery.ingest.checkpoint_repository import read_checkpoint
        except ModuleNotFoundError:  # pragma: no cover - repo-root import style
            from backend.discovery.ingest.checkpoint_repository import read_checkpoint  # type: ignore
        return read_checkpoint(org_id, connector_id)
    except Exception:
        return None


_CONNECTED_STATES = {"connected", "live", "needs_refresh", "refresh_failed", "needs_auth"}


def _connector_entry(
    org_id: str,
    record: Dict[str, Any],
    ingest_events: List[Any],
    health_events: List[Any],
) -> Optional[Dict[str, Any]]:
    connector_id = record.get("id")
    if not connector_id:
        return None

    checkpoint = _read_checkpoint(org_id, connector_id)
    auth_mode = _auth_mode(org_id, connector_id)

    status = str(record.get("status") or "").strip().lower()
    configured = bool(record.get("configured"))
    has_credential = auth_mode is not None
    has_checkpoint = checkpoint is not None

    # "Every CONNECTED system in the org": include a connector once the org has
    # established some connection state for it (configured, a live-ish status, a
    # stored credential, or an ingestion checkpoint). Untouched catalog defaults
    # are not shown — the panel answers "is my data flowing", not "what could I
    # connect".
    is_connected = (
        configured
        or status in _CONNECTED_STATES
        or has_credential
        or has_checkpoint
    )
    if not is_connected:
        return None

    # Last successful ingestion: newest db.ingestor_completed event for this
    # connector (existing telemetry). Falls back to the connector record's
    # lastSynced overlay when no completion event has been recorded.
    last_ingest_event = _latest_by(
        ingest_events, lambda e: getattr(e, "connector_id", None) == connector_id
    )
    last_successful_ingestion = (
        _to_iso(getattr(last_ingest_event, "timestamp", None))
        if last_ingest_event is not None
        else None
    )
    if last_successful_ingestion is None:
        synced = record.get("lastSynced")
        if isinstance(synced, str) and synced not in ("", "—", "-"):
            last_successful_ingestion = synced

    # Last error: newest health-check event whose payload status is an error, or a
    # completed ingestion that reported degraded records.
    last_error: Optional[str] = None
    err_health = _latest_by(
        health_events,
        lambda e: getattr(e, "connector_id", None) == connector_id
        and str(_event_payload(e).get("status", "")).lower()
        in ("error", "needs_refresh", "refresh_failed"),
    )
    if err_health is not None:
        payload = _event_payload(err_health)
        last_error = payload.get("message") or payload.get("status")
    elif last_ingest_event is not None:
        payload = _event_payload(last_ingest_event)
        degraded = payload.get("degraded_count")
        if isinstance(degraded, int) and degraded > 0:
            last_error = f"{degraded} record(s) degraded during last ingestion"

    checkpoint_captured_at = getattr(checkpoint, "captured_at", None) if checkpoint else None

    return {
        "connector_id": connector_id,
        "name": record.get("name") or connector_id,
        "tier": record.get("tier"),
        "connection_state": record.get("status") or ("connected" if is_connected else "disconnected"),
        "auth_mode": auth_mode,
        "last_successful_ingestion": last_successful_ingestion,
        "checkpoint_position": getattr(checkpoint, "value", None) if checkpoint else None,
        "checkpoint_captured_at": checkpoint_captured_at,
        "checkpoint_age_seconds": _age_seconds(checkpoint_captured_at),
        "last_error": last_error,
    }


def connectors_view(org_id: str) -> List[Dict[str, Any]]:
    """Per-connector health for every connected system in the org (AC1)."""
    try:
        records = db.org_connectors_list(org_id)
    except Exception:
        records = []

    ingest_events = _safe_range(org_id, "db.ingestor_completed")
    health_events = _safe_range(org_id, "connector.health_check")

    out: List[Dict[str, Any]] = []
    for record in records:
        try:
            entry = _connector_entry(org_id, record, ingest_events, health_events)
        except Exception:
            entry = None
        if entry is not None:
            out.append(entry)
    return out


# ── Runs panel ────────────────────────────────────────────────────────────────

_DEGRADED_EVENT_LEVELS = {"WARNING", "ERROR", "AI_ERROR"}


def _degraded_stages(errors: Dict[str, Any], events: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Affected stage + reason for every non-blocking failure, from the run's
    status ``errors`` map and any WARNING/ERROR/AI_ERROR run events (AC2)."""
    stages: List[Dict[str, str]] = []
    seen: set = set()
    for stage, reason in (errors or {}).items():
        key = (str(stage), str(reason))
        if key in seen:
            continue
        seen.add(key)
        stages.append({"stage": str(stage), "reason": str(reason)})
    for e in events or []:
        if str(e.get("level", "")).upper() in _DEGRADED_EVENT_LEVELS:
            stage = str(e.get("stage") or "")
            reason = str(e.get("message") or e.get("level") or "")
            key = (stage, reason)
            if key in seen:
                continue
            seen.add(key)
            stages.append({"stage": stage, "reason": reason})
    return stages


def _run_health_status(raw_status: str, degraded_stages: List[Dict[str, str]]) -> str:
    """healthy | degraded | failed | <lifecycle> — non-blocking failures are
    surfaced as ``degraded``, never hidden behind a green tick (§Boundaries)."""
    s = (raw_status or "").strip().lower()
    if s == "failed":
        return "failed"
    if s in ("running", "created", "queued", "pending"):
        return s
    # Terminal success states: complete / partial / done.
    if s == "partial" or degraded_stages:
        return "degraded"
    if s in ("complete", "done", "completed"):
        return "healthy"
    return s or "unknown"


def _run_entry(org_id: str, run: Dict[str, Any], snapshot_events: List[Any]) -> Dict[str, Any]:
    run_id = run.get("id")
    status_kv = db.run_kv_get("status", run_id, {}) or {}
    raw_status = status_kv.get("status") or run.get("status") or "unknown"
    errors = status_kv.get("errors") or {}
    counts = status_kv.get("counts") or {}

    events = db.get_run_events(run_id) or []
    stage_outcomes = [
        {
            "stage": e.get("stage"),
            "level": e.get("level"),
            "message": e.get("message"),
        }
        for e in events
    ]
    degraded_stages = _degraded_stages(errors, events)
    health_status = _run_health_status(raw_status, degraded_stages)

    snapshot = _latest_by(snapshot_events, lambda e: getattr(e, "run_id", None) == run_id)
    detectors_evaluated: Optional[int] = None
    detectors_fired: Optional[int] = None
    if snapshot is not None:
        payload = _event_payload(snapshot)
        detectors_evaluated = payload.get("detector_count")
        detectors_fired = payload.get("fired_count")

    opps = db.run_kv_get("opps", run_id, None)
    if isinstance(opps, list):
        opportunities = len(opps)
    else:
        opportunities = counts.get("opportunities")

    systems = (
        run.get("selectedSystemIds")
        or status_kv.get("systemsUsed")
        or []
    )

    return {
        "run_id": run_id,
        "status": raw_status,
        "health_status": health_status,
        "degraded": health_status == "degraded",
        "started_at": run.get("startedAt"),
        "updated_at": run.get("updatedAt") or status_kv.get("updatedAt"),
        "duration_seconds": _duration_seconds(
            run.get("startedAt"), run.get("updatedAt") or status_kv.get("updatedAt")
        ),
        "systems": systems,
        "system_count": len(systems) if systems else run.get("systemCount"),
        "pack_id": run.get("packId"),
        "detectors_evaluated": detectors_evaluated,
        "detectors_fired": detectors_fired,
        "opportunities": opportunities,
        "degraded_stages": degraded_stages,
        "stage_outcomes": stage_outcomes,
    }


def runs_view(org_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Recent discovery runs for the org, newest first, degradation visible (AC2)."""
    try:
        runs = db.tenancy_get_runs(org_id)
    except Exception:
        runs = []
    runs = sorted(runs, key=lambda r: r.get("startedAt") or "", reverse=True)
    if limit and limit > 0:
        runs = runs[:limit]

    snapshot_events = _safe_range(org_id, "run.signal_snapshot")

    out: List[Dict[str, Any]] = []
    for run in runs:
        try:
            out.append(_run_entry(org_id, run, snapshot_events))
        except Exception:
            out.append({"run_id": run.get("id"), "status": run.get("status"), "health_status": "unknown"})
    return out


# ── Content & freshness panel ───────────────────────────────────────────────

def _redaction_count(org_id: str) -> int:
    total = 0
    for e in _safe_range(org_id, "ingestion.secret_redacted"):
        payload = _event_payload(e)
        try:
            total += int(payload.get("redaction_count", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _skipped_breakdown(org_id: str) -> List[Dict[str, Any]]:
    """Skipped-with-reason items grouped by reason.

    Reads the ``ingestion.artifact_skipped`` telemetry event emitted at origin by
    the document ingestor (R18-C2 T2 gap-fill — ``discovery/ingest/documents.py``).
    Each event carries one skipped artifact's ``reason`` (size_capped /
    budget_exceeded / unsupported_format / no_handler / encrypted / scanned_image);
    this groups them into ``{reason, count}`` for the content panel. An org with
    no skips (or a build predating the emission) simply yields an empty list.
    """
    counts: Dict[str, int] = {}
    for e in _safe_range(org_id, "ingestion.artifact_skipped"):
        payload = _event_payload(e)
        reason = payload.get("reason") or "unknown"
        try:
            counts[reason] = counts.get(reason, 0) + int(payload.get("count", 1) or 1)
        except (TypeError, ValueError):
            counts[reason] = counts.get(reason, 0) + 1
    return [{"reason": r, "count": c} for r, c in sorted(counts.items())]


def content_freshness_view(org_id: str) -> Dict[str, Any]:
    """Retrieval substrate health (AC3), live against R18-B2/A1/A2 records.

    The freshness read is deliberately NOT wrapped in a swallow-to-zero guard:
    R18-B2's metrics must never report false-healthy when the store is down.
    """
    try:
        from .retrieval.metrics import freshness_metrics
        from .retrieval import store
    except ModuleNotFoundError:  # pragma: no cover - repo-root import style
        from backend.app.retrieval.metrics import freshness_metrics  # type: ignore
        from backend.app.retrieval import store  # type: ignore

    fresh = freshness_metrics(org_id)
    return {
        "org_id": org_id,
        "generated_at": fresh["generated_at"],
        "indexed_by_source": store.count_chunks_by_source(org_id),
        "chunks_total": fresh["chunks_total"],
        "chunks_embedded": fresh["chunks_embedded"],
        "pending_embeddings": fresh["pending_embeddings"],
        "stale_chunks": fresh["stale_chunks"],
        "pending_change_events": fresh["pending_change_events"],
        "failed_refreshes": fresh["failed_refreshes"],
        "backfill": fresh["backfill"],
        "redaction_count": _redaction_count(org_id),
        "skipped": _skipped_breakdown(org_id),
    }


# ── Packs panel ─────────────────────────────────────────────────────────────

def _latest_run(org_id: str) -> Optional[Dict[str, Any]]:
    try:
        runs = db.tenancy_get_runs(org_id)
    except Exception:
        return None
    if not runs:
        return None
    return sorted(runs, key=lambda r: r.get("startedAt") or "", reverse=True)[0]


def packs_view(org_id: str) -> Dict[str, Any]:
    """Packs executed on the latest run, with versions + detector counts (AC).

    Pack ids and versions come from the run record / pack execution data
    (R16-B1 ``packVersion`` stamp), never recreated in the dashboard layer.
    """
    latest = _latest_run(org_id)
    if latest is None:
        return {"run_id": None, "packs": []}

    run_id = latest.get("id")
    pack_id = latest.get("packId") or db.run_kv_get("pack_id", run_id, None)
    if not pack_id:
        return {"run_id": run_id, "packs": []}

    try:
        from discovery.packs.pack_config import get_pack_version, get_detector_modules, get_pack
    except ModuleNotFoundError:  # pragma: no cover
        from backend.discovery.packs.pack_config import (  # type: ignore
            get_pack_version,
            get_detector_modules,
            get_pack,
        )

    # Prefer the version stamped on the run (the exact version that executed);
    # fall back to the registry version for the pack id.
    pack_version = latest.get("packVersion") or get_pack_version(pack_id)
    try:
        detector_modules = list(get_detector_modules(pack_id))
        detector_count = len(detector_modules)
    except Exception:
        detector_modules = []
        detector_count = 0
    try:
        pack_name = get_pack(pack_id).get("packName")
    except Exception:
        pack_name = None

    return {
        "run_id": run_id,
        "packs": [
            {
                "pack_id": pack_id,
                "pack_name": pack_name,
                "pack_version": pack_version,
                "detector_count": detector_count,
                "detectors": detector_modules,
            }
        ],
    }


# ── Attention strip ──────────────────────────────────────────────────────────

_AUTH_ACTION_STATES = {"needs_auth", "refresh_failed", "error"}
_EPOCH_ISO = "1970-01-01T00:00:00+00:00"


def _event_payload(event: Any) -> Dict[str, Any]:
    payload = getattr(event, "payload", {}) or {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _credential_metadata(org_id: str, connector_id: str) -> Optional[Dict[str, Any]]:
    """Read only the OAuth health metadata needed by the attention rule.

    No secret is returned or decrypted. The query is bound to both org and
    connector, preserving the vault's tenant boundary.
    """
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT kind, expires_at, refresh_failed, refresh_token, updated_at "
            "FROM credentials "
            "WHERE org_id = %s AND connector_id = %s "
            "AND is_deleted = FALSE",
            (org_id, connector_id),
        )
        row = cur.fetchone()
    finally:
        con.close()

    if row is None:
        return None
    return {
        "kind": row[0],
        "expires_at": _to_iso(row[1]),
        "refresh_failed": bool(row[2]),
        "has_refresh_token": row[3] is not None and str(row[3]) != "",
        "updated_at": _to_iso(row[4]),
    }


def _make_attention_item(
    *,
    item_id: str,
    condition: str,
    severity: str,
    title: str,
    explanation: str,
    timestamp: str,
    panel: str,
    href: str,
    connector_id: Optional[str] = None,
    run_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the stable frontend contract for one actionable condition."""
    return {
        "id": item_id,
        "condition": condition,
        "severity": severity,
        "title": title,
        "explanation": explanation,
        "connector_id": connector_id,
        "run_id": run_id,
        "timestamp": timestamp,
        "panel": panel,
        "href": href,
        "details": details or {},
    }


def _auth_attention_items(
    org_id: str,
    connectors: List[Dict[str, Any]],
    health_events: List[Any],
    now: datetime,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for connector in connectors:
        connector_id = str(connector.get("connector_id") or "")
        if not connector_id:
            continue

        latest_health = _latest_by(
            health_events,
            lambda event: getattr(event, "connector_id", None) == connector_id,
        )
        health_status = (
            str(_event_payload(latest_health).get("status") or "").lower()
            if latest_health is not None
            else ""
        )
        metadata = _credential_metadata(org_id, connector_id)
        condition: Optional[str] = None
        condition_time: Optional[str] = None
        auth_status: Optional[str] = None

        if metadata and metadata.get("kind") == "oauth":
            expiry = _parse_iso(metadata.get("expires_at"))
            if metadata.get("refresh_failed"):
                condition = "unusable_authentication"
                auth_status = "refresh_failed"
                condition_time = metadata.get("updated_at") or metadata.get("expires_at")
            elif (
                expiry is not None
                and expiry <= now
                and not metadata.get("has_refresh_token")
            ):
                condition = "expired_authentication"
                auth_status = "needs_auth"
                condition_time = metadata.get("expires_at")

        # A health-check event is authoritative even when the credential row is
        # absent (for example, a provider has revoked it remotely).
        if health_status in _AUTH_ACTION_STATES:
            condition = (
                "unusable_authentication"
                if health_status in {"refresh_failed", "error"}
                else "expired_authentication"
            )
            auth_status = health_status
            condition_time = _to_iso(getattr(latest_health, "timestamp", None))

        if condition is None or condition_time is None:
            continue

        name = str(connector.get("name") or connector_id)
        explanation = (
            f"{name} authentication cannot be used ({auth_status}). "
            "Reconnect the connector before its data can flow again."
        )
        items.append(
            _make_attention_item(
                item_id=f"auth:{connector_id}",
                condition=condition,
                severity="critical",
                title=f"Reconnect {name}",
                explanation=explanation,
                connector_id=connector_id,
                timestamp=condition_time,
                panel="connectors",
                href=(
                    "/run-health?panel=connectors&connector="
                    f"{quote(connector_id, safe='')}"
                ),
                details={"auth_status": auth_status},
            )
        )
    return items


def _checkpoint_attention_items(
    connectors: List[Dict[str, Any]], now: datetime
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for connector in connectors:
        connector_id = str(connector.get("connector_id") or "")
        captured_at = _parse_iso(connector.get("checkpoint_captured_at"))
        if not connector_id or captured_at is None:
            continue
        age_seconds = max(0, int((now - captured_at).total_seconds()))
        if age_seconds < STALLED_CHECKPOINT_SECONDS:
            continue

        hours = age_seconds // 3600
        name = str(connector.get("name") or connector_id)
        items.append(
            _make_attention_item(
                item_id=f"checkpoint:{connector_id}",
                condition="stalled_checkpoint",
                severity="high",
                title=f"{name} checkpoint is stalled",
                explanation=(
                    f"The {name} ingestion checkpoint has not advanced for "
                    f"{hours} hours. Review connector ingestion details."
                ),
                connector_id=connector_id,
                timestamp=captured_at.isoformat(),
                panel="connectors",
                href=(
                    "/run-health?panel=connectors&connector="
                    f"{quote(connector_id, safe='')}"
                ),
                details={
                    "checkpoint_age_seconds": age_seconds,
                    "checkpoint_captured_at": captured_at.isoformat(),
                },
            )
        )
    return items


def _backlog_attention_items(org_id: str, now: datetime) -> List[Dict[str, Any]]:
    try:
        from .retrieval import store
    except ModuleNotFoundError:  # pragma: no cover - repo-root import style
        from backend.app.retrieval import store  # type: ignore

    backlog = store.pending_embedding_backlog(org_id)
    count = int(backlog.get("count", 0) or 0)
    oldest = _parse_iso(backlog.get("oldest_created_at"))
    newest = _parse_iso(backlog.get("newest_created_at"))
    if oldest is None or newest is None:
        return []

    span_seconds = max(0, int((newest - oldest).total_seconds()))
    if (
        count < GROWING_BACKLOG_MIN_CHUNKS
        or span_seconds < GROWING_BACKLOG_MIN_SPAN_SECONDS
    ):
        return []

    oldest_age = max(0, int((now - oldest).total_seconds()))
    severity = "high" if count >= 500 or oldest_age >= 24 * 60 * 60 else "medium"
    return [
        _make_attention_item(
            item_id="content:embedding_backlog",
            condition="growing_embedding_backlog",
            severity=severity,
            title="Embedding backlog is growing",
            explanation=(
                f"{count} content chunks are waiting for embeddings, with pending "
                f"work spanning {span_seconds // 60} minutes."
            ),
            timestamp=oldest.isoformat(),
            panel="content",
            href="/run-health?panel=content",
            details={
                "pending_embeddings": count,
                "oldest_pending_at": oldest.isoformat(),
                "newest_pending_at": newest.isoformat(),
                "backlog_span_seconds": span_seconds,
            },
        )
    ]


def _stage_failure_attention_items(org_id: str) -> List[Dict[str, Any]]:
    recent_runs = runs_view(org_id, limit=REPEATED_STAGE_RUN_WINDOW)
    by_stage: Dict[str, List[Dict[str, Any]]] = {}
    for run in recent_runs:
        seen_in_run: set[str] = set()
        for failure in run.get("degraded_stages") or []:
            stage = str(failure.get("stage") or "").strip()
            stage_key = stage.casefold()
            if not stage or stage_key in seen_in_run:
                continue
            seen_in_run.add(stage_key)
            by_stage.setdefault(stage_key, []).append(
                {
                    "stage": stage,
                    "reason": str(failure.get("reason") or "Stage failed"),
                    "run_id": run.get("run_id"),
                    "timestamp": run.get("updated_at") or run.get("started_at"),
                }
            )

    items: List[Dict[str, Any]] = []
    for stage_key, failures in by_stage.items():
        if len(failures) < REPEATED_STAGE_MIN_RUNS:
            continue
        latest = max(
            failures,
            key=lambda failure: (
                _parse_iso(failure.get("timestamp"))
                or datetime.fromisoformat(_EPOCH_ISO)
            ),
        )
        timestamp = _to_iso(latest.get("timestamp"))
        run_id = latest.get("run_id")
        if timestamp is None or not run_id:
            continue

        stage = str(latest["stage"])
        slug = re.sub(r"[^a-z0-9]+", "-", stage_key).strip("-") or "unknown"
        severity = "high" if len(failures) >= 3 else "medium"
        items.append(
            _make_attention_item(
                item_id=f"stage:{slug}",
                condition="repeated_stage_failure",
                severity=severity,
                title=f"{stage.replace('_', ' ').title()} is failing repeatedly",
                explanation=(
                    f"The {stage} stage failed in {len(failures)} of the last "
                    f"{len(recent_runs)} runs. Latest reason: {latest['reason']}"
                ),
                run_id=str(run_id),
                timestamp=timestamp,
                panel="runs",
                href=(
                    "/run-health?panel=runs&run="
                    f"{quote(str(run_id), safe='')}&stage={quote(stage, safe='')}"
                ),
                details={
                    "stage": stage,
                    "failure_count": len(failures),
                    "run_window": len(recent_runs),
                },
            )
        )
    return items


def _attention_sort_key(item: Dict[str, Any]) -> tuple[Any, ...]:
    """Critical first; then newest condition time; then stable identifier."""
    severity = ATTENTION_SEVERITY_RANK.get(str(item.get("severity")), 0)
    timestamp = _parse_iso(item.get("timestamp"))
    epoch = timestamp.timestamp() if timestamp is not None else 0.0
    return (-severity, -epoch, str(item.get("id") or ""))


def attention_view(org_id: str) -> List[Dict[str, Any]]:
    """Return org-scoped actionable health conditions in deterministic order.

    A single degraded stage remains visible in ``runs_view`` but is not promoted
    here until the same stage appears in at least two of the five latest runs.
    """
    now = _now()
    connectors = connectors_view(org_id)
    health_events = _safe_range(org_id, "connector.health_check")
    items = [
        *_auth_attention_items(org_id, connectors, health_events, now),
        *_checkpoint_attention_items(connectors, now),
        *_backlog_attention_items(org_id, now),
        *_stage_failure_attention_items(org_id),
    ]
    return sorted(items, key=_attention_sort_key)
