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

# Per-run org id (R17-D3 Addendum A, T11). Set by ``resolve_live_systems`` so an
# ingestor with NO pre-resolved per-run credential can still resolve THIS run's
# credential per-org from the vault — never from a process-global env credential.
# This is "the org_id the run context carries". Default None → fall back to the
# tenancy context / default org (CLI/standalone).
_run_org_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "aiq_run_org_id", default=None
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
    _run_org_id.set(None)


def set_ingest_org(org_id: Optional[str]) -> None:
    """Record the org this run ingests for, so ingestors can resolve credentials
    per-org from the vault (R17-D3 Addendum A, T11). Isolated per run via
    contextvars — safe under concurrent multi-tenant runs."""
    _run_org_id.set(org_id)


def get_ingest_org() -> str:
    """Return the org id for the current ingest, for per-org vault resolution.

    Precedence: the per-run org set by ``resolve_live_systems`` (the run context
    the runner carries) → the request tenancy context → the dev default org
    (CLI/standalone). Never raises."""
    org = _run_org_id.get()
    if org:
        return org
    try:
        from app.middleware.tenancy import DEV_DEFAULT_ORG, get_current_org_id_optional

        return get_current_org_id_optional() or DEV_DEFAULT_ORG
    except Exception:
        return "default"


def resolve_vault_connector(
    connector_id: str, org_id: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """Resolve a connector's credential for an org from the vault.

    The single, env-free credential fallback for ingestors when no pre-resolved
    per-run credential is present (CLI/standalone, or a static-credential
    connector). Resolves via ``get_connector_credentials`` (R17-D3 Addendum A,
    T9/T11) — the one credential path — and normalises the record to the same
    ``{"url", "token", "username"}`` shape the per-run context uses:

      * OAuth  → ``{"token": access_token}`` (no url — the OAuth instance URL is
        captured separately, e.g. ``SF_INSTANCE_URL`` / ``JIRA_URL``; a URL is
        instance config, not a credential).
      * static → ``{"token": secret, "username": username, "url": base_url}``.

    ``org_id`` scopes the lookup; when omitted it defaults to the run's org
    (:func:`get_ingest_org`). Returns ``None`` when the connector is not
    configured for that org. Reads NO environment credential (AC8/AC11) and never
    raises. Credentials are decrypted at use and never cached.
    """
    try:
        from app.auth.credentials import try_get_connector_credentials
        from app.auth.models import StaticCredentialRecord

        record = try_get_connector_credentials(org_id or get_ingest_org(), connector_id)
    except Exception:
        # Best-effort fallback: a vault/DB failure (no DB, no vault key, etc.)
        # degrades to "not configured" rather than crashing the run — the
        # project's "degrade, don't crash" rule. Never surfaces the credential.
        import logging

        logging.getLogger(__name__).debug(
            "vault credential resolution unavailable for %s (returning none)",
            connector_id,
            exc_info=True,
        )
        return None

    if record is None:
        return None

    if isinstance(record, StaticCredentialRecord):
        creds: Dict[str, str] = {"token": record.secret}
        if record.username:
            creds["username"] = record.username
        if record.base_url:
            creds["url"] = record.base_url
        return creds
    return {"token": record.access_token}


def is_live() -> bool:
    INGEST_MODE = os.getenv("INGEST_MODE", "").strip().lower()
    IS_LIVE = (INGEST_MODE == "live")
    return IS_LIVE
