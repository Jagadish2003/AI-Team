"""
Ingestion package.

INGEST_MODE environment variable controls data source:
  offline  (default) — reads from fixtures/*.json
  live               — calls real APIs

Per-run live credentials (DB-sourced, multi-tenant safe)
--------------------------------------------------------
Live connector credentials (instance URL + OAuth Bearer token) are read from the
DATABASE per org — the credential vault (`credentials` table) for tokens and the
captured instance URLs (`kv` table) — NOT from backend/.env. They are handed to
the ingest layer through a ``contextvars.ContextVar`` rather than process-global
``os.environ``.

Why a context var and not env: several users' Discovery Runs can execute
concurrently in background threads. Process-global env would let one run's
credentials clobber another's mid-flight, so one tenant's run could read another
tenant's instance/token. Each Discovery Run executes in its own copied context
(Starlette runs the background task via ``copy_context().run(...)``), so
credentials set for one run are invisible to any other concurrent run.

``_get_client()`` in each connector reads this per-run context first and falls
back to the corresponding env var only for CLI / standalone (non-run) use.
"""
import contextvars
import os
from typing import Dict, Optional

# Per-run connector credentials: {connector_id: {"url": ..., "token": ...}}.
# Default None means "no run context active" → env fallback (CLI/standalone).
_live_connectors: contextvars.ContextVar[Optional[Dict[str, Dict[str, str]]]] = (
    contextvars.ContextVar("aiq_live_connectors", default=None)
)


def set_live_connectors(connectors: Dict[str, Dict[str, str]]) -> None:
    """Set the per-run live connector credentials for the current context.

    Isolated per run via contextvars — safe under concurrent multi-tenant runs.
    Pass an empty dict to clear any previously-set credentials for this context.
    """
    _live_connectors.set(dict(connectors or {}))


def get_live_connector(connector_id: str) -> Optional[Dict[str, str]]:
    """Return {"url", "token"} for a connector from the per-run context, or None."""
    creds = _live_connectors.get()
    if not creds:
        return None
    return creds.get(connector_id)


def clear_live_connectors() -> None:
    """Clear the per-run live connector credentials (teardown / tests)."""
    _live_connectors.set(None)


def is_live() -> bool:
    INGEST_MODE = os.getenv("INGEST_MODE", "").strip().lower()
    IS_LIVE = (INGEST_MODE == "live")
    return IS_LIVE
