"""CS-2 live ingestion wiring — bridge the OAuth vault to discovery ingest.

Background
----------
The Integration Hub OAuth Connect flow (CS-2) stores per-org access/refresh
tokens for Salesforce / ServiceNow / Jira in the credential vault
(``app.auth.vault`` → ``credentials`` table). The discovery ingest layer
(``discovery/ingest/*``), however, is offline-first and reads its credentials
from process **env vars**:

    salesforce  →  SF_ACCESS_TOKEN   (+ SF_INSTANCE_URL)
    servicenow  →  SERVICENOW_TOKEN  (+ SERVICENOW_URL)
    jira        →  JIRA_TOKEN        (+ JIRA_URL, the api.atlassian.com gateway base)

Until now nothing connected the two: a WebApp run ignored the stored OAuth
tokens and fell back to fixtures / token-generation files. This module closes
that gap.

What it does
------------
``resolve_live_systems(org_id)`` inspects the vault for each of the three
OAuth connectors. For every connector that

  1. has a valid (auto-refreshed) token in the vault for ``org_id``, AND
  2. has a resolvable instance/site URL,

it publishes the decrypted access token + URL to the per-run ingest context and
includes the connector id in the returned list. The discovery run then ingests
**live** from exactly those connectors and skips the rest.

How the instance URL is resolved (no env required)
--------------------------------------------------
OAuth tokens carry the bearer access token but not always the instance/site URL,
so the URL is resolved in this order — none of it needs backend/.env config:

  1. The URL captured during the OAuth Connect flow (org-scoped KV in the DB):
     Salesforce's ``instance_url`` from the token response, ServiceNow's host
     from its OAuth config, Jira's api.atlassian.com gateway from the cloudId.
  2. A CLI/standalone env fallback (``SF_INSTANCE_URL`` / ``SERVICENOW_URL`` /
     ``JIRA_URL``) for non-WebApp use.
  3. OAuth-only derivation when neither of the above exists, so a connector
     authenticated purely through Integration Hub still ingests live:
     ServiceNow is derived from its OAuth config host and Jira's gateway is
     discovered live from the token (then persisted for next time).

A connector that is authenticated but whose URL still cannot be resolved is left
out rather than promoted to live, so it degrades to "skipped" instead of
hard-failing the whole run (per the project's "degrade, don't crash" rule).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx

from app import db
from app.auth.models import ConnectorNotAuthenticatedError
from app.auth.vault import get_token

logger = logging.getLogger(__name__)

# Jira Cloud OAuth (3LO): tokens are not used against the site URL. The cloudId
# is discovered via the accessible-resources endpoint, then all REST calls go
# through the api.atlassian.com gateway.
ATLASSIAN_ACCESSIBLE_RESOURCES_URL = (
    "https://api.atlassian.com/oauth/token/accessible-resources"
)
JIRA_CLOUD_GATEWAY_TEMPLATE = "https://api.atlassian.com/ex/jira/{cloud_id}"

# connector_id -> instance-URL env var used ONLY as a CLI/standalone fallback
# when no URL was captured at connect time. Tokens always come from the vault
# (DB); URLs come from the captured KV (DB) first, this env var second.
_LIVE_CONNECTORS: Dict[str, str] = {
    "salesforce": "SF_INSTANCE_URL",
    "servicenow": "SERVICENOW_URL",
    "jira": "JIRA_URL",
}


# ---------------------------------------------------------------------------
# Instance-URL capture (discovered during the OAuth Connect flow)
# ---------------------------------------------------------------------------
# OAuth tokens carry the bearer access token but not the instance/site URL.
# Some of it can still be discovered at connect time:
#   - Salesforce returns ``instance_url`` in the token-exchange response.
#   - ServiceNow's host is encoded in its connector config token URL.
#   - Jira Cloud does NOT expose its site URL in the token response — it needs a
#     separate accessible-resources lookup against api.atlassian.com, so it is
#     not captured here and still relies on JIRA_URL.
# The captured value is stashed in an org-scoped KV entry (not the connectors
# API response) and preferred over env config when resolving live ingest.


def _instance_url_kv_key(org_id: str, connector_id: str) -> str:
    return f"connector_instance_url:{org_id}:{connector_id}"


def store_connector_instance_url(org_id: str, connector_id: str, url: str) -> None:
    """Persist the instance/site URL discovered for an org+connector at connect time."""
    db.kv_set(_instance_url_kv_key(org_id, connector_id), url)


def get_connector_instance_url(org_id: str, connector_id: str) -> Optional[str]:
    """Return the captured instance URL for an org+connector, or None. Never raises."""
    try:
        return db.kv_get(_instance_url_kv_key(org_id, connector_id))
    except Exception:
        return None


def capture_instance_url(
    connector_id: str,
    token_response: Optional[dict],
    token_url: Optional[str] = None,
) -> Optional[str]:
    """Derive the instance/site URL for a connector from its OAuth result.

    Salesforce: ``instance_url`` is returned in the token response.
    ServiceNow: the host is taken from the connector's configured ``token_url``.
    Other connectors (incl. Jira): returns None — not derivable here.
    """
    url = (token_response or {}).get("instance_url")
    if url:
        return url.rstrip("/")

    if connector_id == "servicenow" and token_url:
        parsed = urlparse(token_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"

    return None


async def fetch_jira_gateway_base(
    access_token: str,
    *,
    _transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Optional[str]:
    """Resolve the Jira Cloud API gateway base URL for an OAuth (3LO) token.

    Jira Cloud OAuth tokens are not used against the site URL — REST calls go
    through ``https://api.atlassian.com/ex/jira/{cloudId}``. The cloudId is
    discovered via the accessible-resources endpoint. Returns the gateway base
    (suitable as JIRA_URL for the Bearer-based JiraClient) or None on any
    failure. ``_transport`` is injected only in tests.
    """
    if not access_token:
        return None

    try:
        async with httpx.AsyncClient(timeout=30, transport=_transport) as client:
            resp = await client.get(
                ATLASSIAN_ACCESSIBLE_RESOURCES_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
    except Exception:
        logger.exception("Jira accessible-resources request failed")
        return None

    if resp.status_code != 200:
        logger.warning("Jira accessible-resources returned HTTP %s", resp.status_code)
        return None

    try:
        resources = resp.json()
    except Exception:
        logger.warning("Jira accessible-resources returned a non-JSON body")
        return None

    if not resources:
        logger.warning("Jira OAuth token has no accessible sites")
        return None

    if len(resources) > 1:
        logger.warning(
            "Jira OAuth token grants %d sites; using the first (%s)",
            len(resources),
            resources[0].get("url"),
        )

    cloud_id = resources[0].get("id")
    if not cloud_id:
        return None

    return JIRA_CLOUD_GATEWAY_TEMPLATE.format(cloud_id=cloud_id)


def _run_coro(coro):
    """Run an async coroutine from sync code, safely.

    ``run_trackb_and_persist`` executes inside a Starlette BackgroundTask, which
    runs sync callables in a worker thread with no active event loop — so
    ``asyncio.run`` is the normal path. The running-loop branch keeps this safe
    if the function is ever invoked from within an active loop (e.g. an async
    test) by running the coroutine on its own loop in a separate thread.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is not None and running.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


def _derive_oauth_instance_url(connector_id: str, access_token: str) -> Optional[str]:
    """Derive a connector's instance/site URL purely from OAuth — no env config.

    Used when nothing was captured at connect time and no CLI env var is set, so a
    connector authenticated through Integration Hub still ingests live:

      * ServiceNow — the instance host is fully determined by the OAuth config
        (``SERVICENOW_INSTANCE`` → token_url), so the base is derived from it.
      * Jira Cloud — the api.atlassian.com gateway base depends on the token's
        cloudId, discovered live via the accessible-resources endpoint.
      * Salesforce — the instance_url always rides in the token response and is
        captured at connect, so there is no static fallback here.

    Never raises; returns None when nothing can be derived.
    """
    try:
        if connector_id == "servicenow":
            from app.auth.configs import CONNECTOR_AUTH_CONFIGS

            cfg = CONNECTOR_AUTH_CONFIGS.get("servicenow")
            return capture_instance_url(
                "servicenow", None, cfg.token_url if cfg else None
            )
        if connector_id == "jira":
            return _run_coro(fetch_jira_gateway_base(access_token))
    except Exception:
        logger.exception("OAuth instance-URL derivation failed for %s", connector_id)
    return None


def resolve_live_systems(org_id: str) -> List[str]:
    """Return the connectors that should ingest live for ``org_id``.

    Reads everything from the DATABASE for this org — OAuth access tokens from
    the credential vault (auto-refreshed) and instance/site URLs captured at
    connect time — and publishes them to the per-run ingest context
    (``discovery.ingest.set_live_connectors``). The context is isolated per run
    (contextvars), so concurrent runs for different users never see each other's
    credentials. Nothing is written to process-global ``os.environ``.

    The returned list is the subset of {salesforce, servicenow, jira} that is
    both authenticated in the vault and has an instance URL (captured, or env as
    a CLI fallback), plus ``slack``, ``teams`` and ``github`` when authenticated
    (all URL-less, resolved by token alone — see :func:`_resolve_slack` /
    :func:`_resolve_teams` / :func:`_resolve_github`). Never raises — any failure
    for a single connector simply excludes it.
    """
    from discovery.ingest import set_live_connectors

    live: List[str] = []
    connectors: Dict[str, Dict[str, str]] = {}

    for connector_id, url_env in _LIVE_CONNECTORS.items():
        try:
            record = _run_coro(get_token(org_id, connector_id))
        except ConnectorNotAuthenticatedError:
            # Not connected (or token expired and could not be refreshed) for this
            # org — leave it out. Logged (not silent) so a run that drops a
            # connector explains why: reconnect it in the Integration Hub.
            logger.info(
                "Connector %s not authenticated for org %s (no token, or expired and "
                "refresh failed) — skipping live ingest; reconnect in Integration Hub.",
                connector_id,
                org_id,
            )
            continue
        except Exception:
            logger.exception(
                "Failed to load vault token for connector %s (org=%s); skipping live ingest",
                connector_id,
                org_id,
            )
            continue

        # Prefer the instance URL captured during the OAuth Connect flow (DB);
        # fall back to env only for CLI/standalone use.
        url = get_connector_instance_url(org_id, connector_id) or os.getenv(url_env)
        if not url:
            # OAuth-only derivation: a connector authenticated via Integration Hub
            # should ingest live without any SERVICENOW_URL / JIRA_URL env config.
            url = _derive_oauth_instance_url(connector_id, record.access_token)
            if url:
                url = url.rstrip("/")
                # Persist so later runs (and the connector status view) reuse it
                # without re-deriving (a per-token network call for Jira).
                try:
                    store_connector_instance_url(org_id, connector_id, url)
                except Exception:
                    logger.exception(
                        "Failed to persist derived instance URL for %s (org=%s)",
                        connector_id,
                        org_id,
                    )
        if not url:
            logger.warning(
                "Connector %s is authenticated but no instance URL could be "
                "captured, derived, or read from %s; skipping live ingest for it.",
                connector_id,
                url_env,
            )
            continue

        connectors[connector_id] = {"url": url.rstrip("/"), "token": record.access_token}
        live.append(connector_id)
        logger.info("Live ingest enabled for connector %s (org=%s)", connector_id, org_id)

    # Slack — URL-less SaaS connector (R16-A2). Slack's Web API host is global
    # (slack.com), so it has no per-org instance URL and does NOT go through the
    # instance-URL loop above (which excludes any connector without a URL). When
    # Slack is authenticated in the vault, publish just its access token to the
    # per-run context and include it in the live set, so the discovery run
    # ingests it through the change runner (emitting ingestion.artifact_changed
    # and advancing the checkpoint) and feeds its escalation signal to
    # corroboration. The Slack MEDIUM ceiling is enforced downstream by the
    # corroboration engine — nothing here elevates confidence.
    _resolve_slack(org_id, connectors, live)

    # Microsoft Teams — URL-less SaaS connector (R17-A1). Like Slack, the Graph
    # host is global (graph.microsoft.com), so it has no per-org instance URL and
    # does NOT go through the instance-URL loop above. When Teams is authenticated
    # in the vault, publish its access token to the per-run context (which
    # TeamsIngestor._client reads via get_live_connector('teams')) and include it
    # in the live set, so the discovery run ingests it through the change runner.
    # The Teams MEDIUM ceiling is enforced downstream by the corroboration engine.
    _resolve_teams(org_id, connectors, live)

    # GitHub — URL-less SaaS connector (T1-S12). Unlike Slack it reads its own
    # token straight from the vault inside the connector, so nothing is published
    # to the per-run context; it only needs the run promoted to live (see below).
    _resolve_github(org_id, live)

    # Java application — R17-A3, AgentIQ's first non-SaaS source. Like Slack it is
    # URL-less here (each target carries its own actuator_url/log_source in the
    # per-deployment config); when a Java-app credential is in the vault, publish
    # its token to the per-run context so the ingestor resolves the secret safely
    # (never from config — AC3) and include it in the live set.
    _resolve_java_app(org_id, connectors, live)

    # .NET application — R17-A4, the .NET counterpart to the Java source above. Same
    # URL-less, per-deployment-config, vault-credential shape (each target carries
    # its own diagnostics_url/log_source; only the credential is resolved here).
    _resolve_dotnet_app(org_id, connectors, live)

    # Publish to the per-run context (empty dict clears any prior credentials).
    set_live_connectors(connectors)
    return live


def _resolve_github(org_id: str, live: List[str]) -> None:
    """Add GitHub to the live set when authenticated (T1-S12 live-path wiring).

    GitHub is URL-less AND fetches its own token directly from the vault in
    ``connectors/saas/github.ingest()`` (``get_token(org, 'github')``) — it does
    NOT read the per-run context — so nothing is published to ``connectors`` here.
    Its only requirement to ingest live data is that the run's ``INGEST_MODE`` is
    'live', which materialize sets when ``resolve_live_systems`` returns a
    non-empty list. Including ``'github'`` here is what flips a GitHub-connected
    run to live; the ``github_engineering`` pack gating in the runner still decides
    whether GitHub is actually ingested. Mutates ``live`` in place; never raises.
    """
    try:
        _run_coro(get_token(org_id, "github"))
    except ConnectorNotAuthenticatedError:
        logger.info(
            "Connector github not authenticated for org %s — skipping live ingest; "
            "connect it in the Integration Hub.",
            org_id,
        )
        return
    except Exception:
        logger.exception(
            "Failed to load vault token for connector github (org=%s); skipping live ingest",
            org_id,
        )
        return

    live.append("github")
    logger.info("Live ingest enabled for connector github (org=%s)", org_id)


def _resolve_slack(
    org_id: str,
    connectors: Dict[str, Dict[str, str]],
    live: List[str],
) -> None:
    """Add Slack to the live set when authenticated, keyed by token only (no URL).

    Mutates ``connectors`` and ``live`` in place. Never raises — any failure
    simply leaves Slack out (degrade, don't crash), matching the systems-of-record
    handling above.
    """
    try:
        record = _run_coro(get_token(org_id, "slack"))
    except ConnectorNotAuthenticatedError:
        logger.info(
            "Connector slack not authenticated for org %s — skipping live ingest; "
            "connect it in the Integration Hub.",
            org_id,
        )
        return
    except Exception:
        logger.exception(
            "Failed to load vault token for connector slack (org=%s); skipping live ingest",
            org_id,
        )
        return

    connectors["slack"] = {"token": record.access_token}
    live.append("slack")
    logger.info("Live ingest enabled for connector slack (org=%s)", org_id)


def _resolve_java_app(
    org_id: str,
    connectors: Dict[str, Dict[str, str]],
    live: List[str],
) -> None:
    """Add the Java application source to the live set when a credential exists (R17-A3).

    The Java applications in scope are configured per deployment (their Actuator
    URLs + log sources live in ``JAVA_APP_TARGETS``, never here); this resolver
    only handles the credential. When a ``java_app`` token is in the vault for the
    org, its decrypted value is published to the per-run context under the
    ``java_app`` key so the ingestor's ``resolve_secret`` reads it safely (AC3) —
    the secret never touches config or logs. AgentIQ does NOT scan the network:
    a deployment with no configured targets simply yields no Java records.

    Mutates ``connectors`` and ``live`` in place. Never raises — any failure
    leaves Java out (degrade, don't crash), matching the other resolvers.
    """
    try:
        record = _run_coro(get_token(org_id, "java_app"))
    except ConnectorNotAuthenticatedError:
        logger.info(
            "Connector java_app not authenticated for org %s — skipping live ingest; "
            "configure its credential in the vault.",
            org_id,
        )
        return
    except Exception:
        logger.exception(
            "Failed to load vault token for connector java_app (org=%s); skipping live ingest",
            org_id,
        )
        return

    connectors["java_app"] = {"token": record.access_token}
    live.append("java_app")
    logger.info("Live ingest enabled for connector java_app (org=%s)", org_id)


def _resolve_dotnet_app(
    org_id: str,
    connectors: Dict[str, Dict[str, str]],
    live: List[str],
) -> None:
    """Add the .NET application source to the live set when a credential exists (R17-A4).

    The .NET counterpart to :func:`_resolve_java_app`. The .NET applications in scope
    are configured per deployment (their diagnostics URLs + log sources live in
    ``DOTNET_APP_TARGETS``, never here); this resolver only handles the credential.
    When a ``dotnet_app`` token is in the vault for the org, its decrypted value is
    published to the per-run context under the ``dotnet_app`` key so the ingestor's
    ``resolve_secret`` reads it safely — the secret never touches config or logs.
    AgentIQ does NOT scan the network: a deployment with no configured targets simply
    yields no .NET records.

    Mutates ``connectors`` and ``live`` in place. Never raises — any failure leaves
    .NET out (degrade, don't crash), matching the other resolvers.
    """
    try:
        record = _run_coro(get_token(org_id, "dotnet_app"))
    except ConnectorNotAuthenticatedError:
        logger.info(
            "Connector dotnet_app not authenticated for org %s — skipping live ingest; "
            "configure its credential in the vault.",
            org_id,
        )
        return
    except Exception:
        logger.exception(
            "Failed to load vault token for connector dotnet_app (org=%s); skipping live ingest",
            org_id,
        )
        return

    connectors["dotnet_app"] = {"token": record.access_token}
    live.append("dotnet_app")
    logger.info("Live ingest enabled for connector dotnet_app (org=%s)", org_id)


def _resolve_teams(
    org_id: str,
    connectors: Dict[str, Dict[str, str]],
    live: List[str],
) -> None:
    """Add Microsoft Teams to the live set when authenticated, keyed by token only.

    Teams is a URL-less SaaS connector (the Microsoft Graph host is global), so
    like Slack it does not go through the instance-URL loop. When Teams is
    authenticated in the vault, publish its access token to the per-run context
    (read by ``TeamsIngestor._client`` via ``get_live_connector('teams')``) and
    include it in the live set, so the discovery run ingests Teams through the
    change runner. Mutates ``connectors`` and ``live`` in place. Never raises —
    any failure simply leaves Teams out (degrade, don't crash)."""
    try:
        record = _run_coro(get_token(org_id, "teams"))
    except ConnectorNotAuthenticatedError:
        logger.info(
            "Connector teams not authenticated for org %s — skipping live ingest; "
            "connect it in the Integration Hub.",
            org_id,
        )
        return
    except Exception:
        logger.exception(
            "Failed to load vault token for connector teams (org=%s); skipping live ingest",
            org_id,
        )
        return

    connectors["teams"] = {"token": record.access_token}
    live.append("teams")
    logger.info("Live ingest enabled for connector teams (org=%s)", org_id)
