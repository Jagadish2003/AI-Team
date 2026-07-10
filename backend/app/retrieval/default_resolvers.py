"""Default production content resolvers for retrieval freshness (R18-B2).

The freshness worker can only turn a queued ``artifact_changed`` event back into
fresh chunks when it knows how to re-extract the current artifact. This module
registers the resolvers for the source systems whose change events are emitted by
the ingestion foundation in this repo.

Registration verifies each resolver's backing connector module IMPORTS at startup
and skips (with a loud warning) any resolver whose module is unavailable — a
misconfigured deployment surfaces once, at startup, instead of as an endless
stream of per-artifact resolver errors at refresh time. Everything else stays
lazy: credentials and source reads are touched only when a specific artifact is
refreshed. An unregistered source's artifacts are left queued (``no_resolver``)
with their chunks stale — never served as current — until the module is fixed.
"""
from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from app.retrieval import refresh
from app.retrieval.ingest import ContentArtifact

logger = logging.getLogger(__name__)

Resolver = Callable[[str, str], Optional[ContentArtifact]]


def register_default_content_resolvers() -> None:
    """Register the built-in source resolvers used by the refresh worker.

    Each resolver's backing connector module is imported ONCE here, before the
    resolver is registered. A module that fails to import is a deployment
    problem, and it must surface as one clear startup warning — not as a silent
    per-artifact failure loop at refresh time (a broken import inside the resolver
    body would be caught per call by the worker, burning a retry attempt per
    queued artifact per tick with no visible signal beyond debug noise). A skipped
    source's artifacts stay queued as ``no_resolver`` (chunks stale, never served
    as current) and start refreshing on the next startup after the module is fixed.

    Idempotent: ``refresh.register_content_resolver`` replaces a previous resolver
    for the same source system, so calling this from every app startup is safe.
    """
    registered = 0
    for source_system, resolver in _DEFAULT_RESOLVERS.items():
        module_name = _RESOLVER_MODULES[source_system]
        try:
            _import_module(module_name)
        except Exception:  # noqa: BLE001 — a module-level failure of any kind
            logger.warning(
                "retrieval freshness: NOT registering content resolver for %r — "
                "backing module %r failed to import. Its queued artifacts will "
                "stay pending (no_resolver) with chunks stale until the module "
                "imports cleanly.",
                source_system,
                module_name,
                exc_info=True,
            )
            continue
        refresh.register_content_resolver(source_system, resolver)
        registered += 1
    logger.info(
        "retrieval freshness: registered %d of %d default content resolver(s)",
        registered,
        len(_DEFAULT_RESOLVERS),
    )


def _import_module(name: str):
    """Import a connector module under either backend/ or repo-root execution."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        return importlib.import_module(f"backend.{name}")


def _artifact(
    source_system: str,
    source_artifact: str,
    content: str,
    content_type: str,
    *,
    source_timestamp: Optional[str] = None,
    provenance: Optional[dict[str, Any]] = None,
) -> ContentArtifact:
    return ContentArtifact(
        source_system=source_system,
        source_artifact=source_artifact,
        content=content,
        content_type=content_type,
        source_timestamp=source_timestamp,
        provenance=provenance,
    )


def _text_lines(*pairs: tuple[str, Any]) -> str:
    lines = []
    for label, value in pairs:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            lines.append(f"{label}: {text}")
    return "\n".join(lines)


def _slack_ts_to_iso(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


def _resolve_slack(org_id: str, source_artifact: str) -> Optional[ContentArtifact]:
    channel_id, sep, ts = source_artifact.partition(":")
    if not sep or not channel_id or not ts:
        return None
    mod = _import_module("discovery.ingest.slack")
    ingestor = mod.SlackIngestor()
    for msg in ingestor._raw_messages(org_id, {"id": channel_id}):
        if str(msg.get("ts", "")) != ts:
            continue
        edited = msg.get("edited") or {}
        timestamp = _slack_ts_to_iso(edited.get("ts") or msg.get("ts"))
        return _artifact(
            "slack",
            source_artifact,
            str(msg.get("text", "") or ""),
            "conversation",
            source_timestamp=timestamp,
            provenance={
                "channel_id": channel_id,
                "thread_ts": msg.get("thread_ts"),
                "user": msg.get("user"),
                "reply_count": msg.get("reply_count", 0),
                "reactions": msg.get("reactions", []),
            },
        )
    return None


def _resolve_teams(org_id: str, source_artifact: str) -> Optional[ContentArtifact]:
    container, sep, message_id = source_artifact.partition(":")
    team_id, slash, channel_id = container.partition("/")
    if not sep or not slash or not team_id or not channel_id or not message_id:
        return None
    mod = _import_module("discovery.ingest.teams")
    ingestor = mod.TeamsIngestor()
    messages, _next_token = ingestor._channel_delta(
        org_id, {"team_id": team_id, "id": channel_id}, None
    )
    for msg in messages:
        if str(msg.get("id", "")) != message_id:
            continue
        body = msg.get("body") or {}
        text = body.get("content", "") if isinstance(body, dict) else ""
        timestamp = msg.get("lastModifiedDateTime") or msg.get("createdDateTime")
        sender = (msg.get("from") or {}).get("user") or {}
        return _artifact(
            "teams",
            source_artifact,
            str(text or ""),
            "conversation",
            source_timestamp=timestamp,
            provenance={
                "team_id": team_id,
                "channel_id": channel_id,
                "message_id": message_id,
                "user": sender.get("id"),
                "user_display_name": sender.get("displayName"),
                "reply_count": msg.get("reply_count", 0),
                "mentions": msg.get("mentions", []),
                "reactions": msg.get("reactions", []),
            },
        )
    return None


def _resolve_confluence(org_id: str, source_artifact: str) -> Optional[ContentArtifact]:
    space_key, sep, content_id = source_artifact.partition(":")
    if not sep or not space_key or not content_id:
        return None
    mod = _import_module("discovery.ingest.confluence")
    ingestor = mod.ConfluenceIngestor()
    for item in ingestor._raw_content(org_id, {"key": space_key}):
        if str(item.get("id", "")) != content_id:
            continue
        version = item.get("version") or {}
        by = version.get("by") or {}
        links = item.get("_links") or {}
        modified = version.get("when") or item.get("lastModified")
        content = _text_lines(
            ("Title", item.get("title")),
            ("Type", item.get("type")),
            ("Status", item.get("status")),
            ("Version", version.get("number")),
            ("Modified by", by.get("accountId") or by.get("displayName")),
            ("URL", links.get("webui")),
        )
        return _artifact(
            "confluence",
            source_artifact,
            content,
            "prose",
            source_timestamp=modified,
            provenance={
                "space_key": space_key,
                "content_id": content_id,
                "url": links.get("webui"),
                "version_number": version.get("number"),
            },
        )
    return None


def _resolve_sharepoint(org_id: str, source_artifact: str) -> Optional[ContentArtifact]:
    container, sep, item_id = source_artifact.partition(":")
    site_id, slash, drive_id = container.partition("/")
    if not sep or not slash or not site_id or not drive_id or not item_id:
        return None
    mod = _import_module("discovery.ingest.sharepoint")
    ingestor = mod.SharePointIngestor()
    library = {"site_id": site_id, "id": drive_id}
    for item in ingestor._raw_items(org_id, library):
        if item.get("deleted") or str(item.get("id", "")) != item_id:
            continue
        item_type = "folder" if "folder" in item else "file" if "file" in item else "item"
        created_by = (item.get("createdBy") or {}).get("user") or {}
        modified_by = (item.get("lastModifiedBy") or {}).get("user") or {}
        parent = item.get("parentReference") or {}
        timestamp = item.get("lastModifiedDateTime") or item.get("createdDateTime")
        content = _text_lines(
            ("Name", item.get("name")),
            ("Type", item_type),
            ("Size", item.get("size")),
            ("Parent", parent.get("path")),
            ("Modified by", modified_by.get("displayName") or modified_by.get("id")),
            ("URL", item.get("webUrl")),
        )
        return _artifact(
            "sharepoint",
            source_artifact,
            content,
            "prose",
            source_timestamp=timestamp,
            provenance={
                "site_id": site_id,
                "drive_id": drive_id,
                "item_id": item_id,
                "item_type": item_type,
                "web_url": item.get("webUrl"),
                "parent_path": parent.get("path"),
                "created_by": created_by.get("id"),
                "last_modified_by": modified_by.get("id"),
            },
        )
    return None


def _find_operational_record(ingestor: Any, org_id: str, source_artifact: str) -> Optional[dict]:
    app_id, sep, _rest = source_artifact.partition(":")
    if not sep or not app_id:
        return None
    for target in ingestor._load_targets(org_id):
        if str(getattr(target, "app_id", "")) != app_id:
            continue
        records, _cursor = ingestor._read_operational(org_id, target, {})
        for rec in records:
            if rec.get("artifact_id") == source_artifact:
                return rec
    return None


def _operational_content(record: dict) -> str:
    if record.get("artifact_kind") == "log":
        return _text_lines(
            ("Service", record.get("service")),
            ("Level", record.get("level")),
            ("Logger", record.get("logger")),
            ("Exception", record.get("exception_type")),
            ("Message", record.get("message")),
        )
    return _text_lines(
        ("Service", record.get("service")),
        ("Health", record.get("health")),
        ("Error rate", record.get("error_rate")),
        ("Latency p95 ms", record.get("latency_p95_ms")),
        ("Throughput rpm", record.get("throughput_rpm")),
        ("Memory used ratio", record.get("memory_used_ratio")),
        ("CPU usage", record.get("cpu_usage")),
    )


def _resolve_operational(
    org_id: str,
    source_artifact: str,
    *,
    module_name: str,
    class_name: str,
    source_system: str,
) -> Optional[ContentArtifact]:
    mod = _import_module(module_name)
    ingestor = getattr(mod, class_name)()
    record = _find_operational_record(ingestor, org_id, source_artifact)
    if record is None:
        return None
    return _artifact(
        source_system,
        source_artifact,
        _operational_content(record),
        "prose",
        source_timestamp=record.get("observed_ts"),
        provenance={
            "app_id": record.get("app_id"),
            "service": record.get("service"),
            "artifact_kind": record.get("artifact_kind"),
            "log_offset": record.get("log_offset"),
            "event_id": record.get("event_id"),
        },
    )


def _resolve_java_app(org_id: str, source_artifact: str) -> Optional[ContentArtifact]:
    return _resolve_operational(
        org_id,
        source_artifact,
        module_name="discovery.ingest.java_app",
        class_name="JavaAppIngestor",
        source_system="java_app",
    )


def _resolve_dotnet_app(org_id: str, source_artifact: str) -> Optional[ContentArtifact]:
    return _resolve_operational(
        org_id,
        source_artifact,
        module_name="discovery.ingest.dotnet_app",
        class_name="DotNetAppIngestor",
        source_system="dotnet_app",
    )


_DEFAULT_RESOLVERS: Dict[str, Resolver] = {
    "slack": _resolve_slack,
    "teams": _resolve_teams,
    "confluence": _resolve_confluence,
    "sharepoint": _resolve_sharepoint,
    "java_app": _resolve_java_app,
    "dotnet_app": _resolve_dotnet_app,
}

# The connector module each resolver re-extracts through. Imported once at
# registration time so a missing/broken module disqualifies its resolver with one
# startup warning instead of failing silently on every refresh attempt.
_RESOLVER_MODULES: Dict[str, str] = {
    "slack": "discovery.ingest.slack",
    "teams": "discovery.ingest.teams",
    "confluence": "discovery.ingest.confluence",
    "sharepoint": "discovery.ingest.sharepoint",
    "java_app": "discovery.ingest.java_app",
    "dotnet_app": "discovery.ingest.dotnet_app",
}


__all__ = [
    "register_default_content_resolvers",
]
