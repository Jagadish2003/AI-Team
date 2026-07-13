from __future__ import annotations

import logging
import os
import re
from typing import Dict

from dotenv import load_dotenv

from app.auth.models import ConnectorAuthConfig

logger = logging.getLogger(__name__)

# Recognised Microsoft identity tenant segments: a directory (tenant) GUID, or one
# of the well-known aliases. Used to surface a malformed TEAMS_TENANT_ID /
# SHAREPOINT_TENANT_ID at import (startup) rather than only when a user connects
# and Microsoft rejects the authorize URL (M4).
_MS_TENANT_ALIASES = {"common", "organizations", "consumers"}
_MS_TENANT_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _warn_if_malformed_ms_tenant(env_var: str, value: str) -> None:
    """Log a WARNING at import if a Microsoft tenant value is neither a GUID nor a
    known alias — the authorize/token URLs are built from it at import, so a typo
    would otherwise only surface as an OAuth failure at connect time (M4)."""
    if value not in _MS_TENANT_ALIASES and not _MS_TENANT_GUID_RE.match(value):
        logger.warning(
            "%s=%r is neither a directory (tenant) GUID nor a known alias "
            "(common/organizations/consumers); the Microsoft OAuth authorize/token "
            "URLs built from it may be invalid and connecting will fail.",
            env_var,
            value,
        )

# Load backend/.env so the per-connector instance/tenant values below can be
# supplied there (idempotent; other app modules call this too). An already-set
# process env var still wins (override=False), matching the rest of the app.
load_dotenv()

# client_id values are non-sensitive per T1-S10-A spec and can be in code.
# os.getenv() allows override from environment (production); the fallback is a
# stable dev/CI placeholder so tests never receive None and CI needs no .env file.

# ---------------------------------------------------------------------------
# Per-connector instance / tenant settings
#
# These are the only host values that change between deployments. Edit them
# here to point the OAuth flows at a different instance — no .env changes
# required. The URLs below are built from these variables, so changing a value
# here updates the token / revocation / authorization URLs together.
#
# Note: configs are evaluated once at import, so a backend restart is required
# for a change to take effect (these are not hot-reloaded per request).
#
# Only Salesforce, ServiceNow, SAP, Dynamics365 and Teams have a per-customer
# instance/tenant (Teams via its Microsoft Entra tenant segment). Jira, Confluence,
# GitHub and Slack use fixed global hosts (auth.atlassian.com, github.com,
# slack.com) that never vary per deployment.
# ---------------------------------------------------------------------------

# Each value reads from backend/.env first (so a deployment can point the OAuth
# flows at a different instance/tenant without editing code), falling back to the
# documented dev/sandbox default. The token / revocation / authorization URLs
# below are built from these, so one value drives all three together.

# Salesforce login host: "test.salesforce.com" (sandbox),
# "login.salesforce.com" (production), or your My Domain host
# e.g. "mycompany.my.salesforce.com".
SALESFORCE_INSTANCE = os.getenv("SALESFORCE_INSTANCE", "test.salesforce.com")

# ServiceNow instance subdomain only, e.g. "dev198195" → dev198195.service-now.com
SERVICENOW_INSTANCE = os.getenv("SERVICENOW_INSTANCE", "dev198195")

# SAP BTP subaccount subdomain + region, e.g. "05e6c258trial" + "us10"
# → 05e6c258trial.authentication.us10.hana.ondemand.com
SAP_SUBDOMAIN = os.getenv("SAP_SUBDOMAIN", "05e6c258trial")
SAP_REGION = os.getenv("SAP_REGION", "us10")

# Microsoft Entra ID (Azure AD) tenant id (GUID) for Dynamics 365.
DYNAMICS365_TENANT_ID = os.getenv("DYNAMICS365_TENANT_ID", "bb612c49-03be-4da1-9974-49f0c8704eb8")

# Microsoft Entra ID (Azure AD) tenant for the Microsoft Teams / Graph OAuth app
# (R17-A1 / AT-434). A specific tenant GUID locks the connector to one customer
# tenant (most restrictive); "organizations" allows any work/school tenant; "common"
# also allows personal Microsoft accounts. Teams is a work/school product, so the
# default is "organizations" — never "common" (no personal-account sign-in).
# The trailing ``or "organizations"`` guards the empty-string trap: a blank
# ``TEAMS_TENANT_ID=""`` in .env is present-but-empty, so os.getenv returns ""
# (not the default), which would build a malformed authorize URL with a double
# slash (login.microsoftonline.com//oauth2/...). Coerce blank → the default.
TEAMS_TENANT_ID = os.getenv("TEAMS_TENANT_ID", "organizations").strip() or "organizations"

# Microsoft Entra ID (Azure AD) tenant for the SharePoint / Microsoft Graph OAuth
# app (R17-A2 / AT-462). SharePoint reuses the Graph app registration set up for
# the Teams connector (R17-A1 / R17-A2 §6), so its tenant DEFAULTS to
# TEAMS_TENANT_ID — a deployment only needs a distinct SHAREPOINT_TENANT_ID if it
# registers a separate Graph app for SharePoint. The trailing ``or TEAMS_TENANT_ID``
# guards the empty-string trap: a blank ``SHAREPOINT_TENANT_ID=""`` in .env is
# present-but-empty, so os.getenv returns "" (not the default), which would build a
# malformed authorize URL with a double slash — coerce blank → the Teams tenant.
SHAREPOINT_TENANT_ID = os.getenv("SHAREPOINT_TENANT_ID", "").strip() or TEAMS_TENANT_ID

# Surface a malformed Microsoft tenant at import/startup (M4) rather than only at
# connect time. Both the Teams and SharePoint authorize/token URLs are built from
# these below, so a typo'd tenant would otherwise fail silently until a user tries
# to connect. (SharePoint defaults to the Teams tenant, so validate both.)
_warn_if_malformed_ms_tenant("TEAMS_TENANT_ID", TEAMS_TENANT_ID)
_warn_if_malformed_ms_tenant("SHAREPOINT_TENANT_ID", SHAREPOINT_TENANT_ID)

CONNECTOR_AUTH_CONFIGS: Dict[str, ConnectorAuthConfig] = {
    "salesforce": ConnectorAuthConfig(
        connector_id="salesforce",
        flow="authorization_code",
        # R18-A3: authorization_code (default) + jwt_bearer (AT-555) — Salesforce's
        # outbound-only, no-callback headless path (signed assertion → access
        # token; refresh by re-assertion). The cert private key lives in the vault.
        supported_auth_modes=["authorization_code", "jwt_bearer"],
        client_id=os.getenv("SALESFORCE_CLIENT_ID", "salesforce-dev-client-id"),
        secret_key="SALESFORCE_CLIENT_SECRET",
        token_url=f"https://{SALESFORCE_INSTANCE}/services/oauth2/token",
        revocation_url=f"https://{SALESFORCE_INSTANCE}/services/oauth2/revoke",
        scopes=["openid", "id", "profile", "email", "address", "phone", "web", "full", "api", "refresh_token", "offline_access"],
        authorization_url=f"https://{SALESFORCE_INSTANCE}/services/oauth2/authorize",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
        # prompt=consent makes Salesforce (re)issue a refresh_token so the access
        # token can be auto-refreshed. The connected app must also have the
        # "Perform requests at any time (refresh_token, offline_access)" scope.
        authorize_params={"prompt": "consent"},
    ),
    "servicenow": ConnectorAuthConfig(
        connector_id="servicenow",
        flow="authorization_code",
        # R18-A3: authorization_code (default) + client_credentials (AT-557 — outbound-only,
        # no callback) + static (R17-D3 Addendum A — user/password vault path).
        supported_auth_modes=["authorization_code", "client_credentials", "static"],
        client_id=os.getenv("SERVICENOW_CLIENT_ID", "servicenow-dev-client-id"),
        secret_key="SERVICENOW_CLIENT_SECRET",
        token_url=f"https://{SERVICENOW_INSTANCE}.service-now.com/oauth_token.do",
        revocation_url=f"https://{SERVICENOW_INSTANCE}.service-now.com/oauth_revoke.do",
        scopes=["user", "admin"],
        authorization_url=f"https://{SERVICENOW_INSTANCE}.service-now.com/oauth_auth.do",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
    ),
    "jira": ConnectorAuthConfig(
        connector_id="jira",
        flow="authorization_code",
        # R18-A3 T1: authorization_code (default) + static (API-token vault path,
        # the pragmatic outbound-only option for Atlassian Cloud — matrix §1).
        supported_auth_modes=["authorization_code", "static"],
        client_id=os.getenv("JIRA_CLIENT_ID", "jira-dev-client-id"),
        secret_key="JIRA_CLIENT_SECRET",
        token_url="https://auth.atlassian.com/oauth/token",
        revocation_url="https://auth.atlassian.com/oauth/token/revoke",
        # offline_access is required for Atlassian to issue a refresh token —
        # without it the ~1h access token cannot be auto-refreshed by the vault
        # and live Jira ingest would stop working after expiry. The Atlassian
        # OAuth (3LO) app must have these scopes enabled (SME-owned).
        scopes=["read:jira-work", "read:jira-user", "offline_access"],
        authorization_url="https://auth.atlassian.com/authorize",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
        # Atlassian 3LO: audience selects the API, and prompt=consent guarantees a
        # rotating refresh_token is returned (with offline_access above) so the ~1h
        # access token auto-refreshes. The vault preserves/rotates it on refresh.
        authorize_params={"audience": "api.atlassian.com", "prompt": "consent"},
    ),
    "confluence": ConnectorAuthConfig(
        connector_id="confluence",
        flow="authorization_code",
        # R18-A3 T1: authorization_code (default) + static (Atlassian API token).
        supported_auth_modes=["authorization_code", "static"],
        client_id=os.getenv("CONFLUENCE_CLIENT_ID", "confluence-dev-client-id"),
        secret_key="CONFLUENCE_CLIENT_SECRET",
        token_url="https://auth.atlassian.com/oauth/token",
        revocation_url="https://auth.atlassian.com/oauth/token/revoke",
        scopes=[
            "read:confluence-content.all",
            "read:confluence-space.summary",
            "offline_access",
        ],
        authorization_url="https://auth.atlassian.com/authorize",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
        # Atlassian 3LO: audience + prompt=consent → rotating refresh_token issued
        # (with offline_access) so the access token auto-refreshes.
        authorize_params={"audience": "api.atlassian.com", "prompt": "consent"},
    ),
    "github": ConnectorAuthConfig(
        connector_id="github",
        flow="authorization_code",
        supported_auth_modes=["authorization_code"],
        client_id=os.getenv("GITHUB_CLIENT_ID", "github-dev-client-id"),
        secret_key="GITHUB_CLIENT_SECRET",
        token_url="https://github.com/login/oauth/access_token",
        revocation_url=None,
        scopes=["repo:status", "read:org", "read:user"],
        authorization_url="https://github.com/login/oauth/authorize",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
    ),
    "slack": ConnectorAuthConfig(
        connector_id="slack",
        flow="authorization_code",
        supported_auth_modes=["authorization_code"],
        client_id=os.getenv("SLACK_CLIENT_ID", "slack-dev-client-id"),
        secret_key="SLACK_CLIENT_SECRET",
        token_url="https://slack.com/api/oauth.v2.access",
        revocation_url=None,                                                        # Slack uses auth.revoke Web API — see vault.py revoke_token()
        # R16-A2 §3 / AT-420 (AC4): minimal, public-channels-only scopes — only
        # what the reach-phase ingestor needs to read public channel messages.
        #   channels:read    → list the public channels AgentIQ was invited to
        #   channels:history → read those public channels' messages
        # Deliberately NO private-channel (groups:*), DM (im:*) or group-DM
        # (mpim:*) scopes, and no write scopes — so private channels and DMs can
        # never be accessed (the privacy guarantee is enforced at the scope level,
        # in addition to the SlackIngestor public-only channel filter). Slack's
        # own OAuth consent screen shows exactly these scopes to the admin.
        scopes=["channels:read", "channels:history"],
        authorization_url="https://slack.com/oauth/v2/authorize",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
    ),
    "teams": ConnectorAuthConfig(
        connector_id="teams",
        flow="authorization_code",
        # R18-A3: authorization_code (default) + client_credentials (AT-556 —
        # Microsoft Graph application permissions, outbound-only, no callback). An
        # org in a no-public-inbound deployment selects client_credentials so Teams
        # authenticates under a service identity without a browser redirect.
        supported_auth_modes=["authorization_code", "client_credentials"],
        # Graph client-credentials (application-permission) tokens are requested with
        # the single resource scope .default — the actual permissions come from the
        # admin-consented app registration, NOT from granular scopes in the request
        # (Microsoft rejects delegated scopes in this grant). The delegated ``scopes``
        # below still drive the authorization_code flow. See docs/INTEGRATE_GRAPH_CLIENT_CREDENTIALS.md
        # for the admin-consent setup (application permissions + tenant-wide consent).
        client_credentials_scopes=["https://graph.microsoft.com/.default"],
        client_id=os.getenv("TEAMS_CLIENT_ID") or "teams-dev-client-id",
        secret_key="TEAMS_CLIENT_SECRET",
        # Microsoft identity platform (v2.0) endpoints. The tenant segment is
        # driven by TEAMS_TENANT_ID above so a deployment can lock the OAuth flow
        # to a single customer tenant without a code change.
        token_url=f"https://login.microsoftonline.com/{TEAMS_TENANT_ID}/oauth2/v2.0/token",
        # Microsoft identity has no RFC-7009 token-revocation endpoint (sign-out /
        # admin token revocation is a portal/Graph action, not a token POST), so —
        # like GitHub and Slack — there is no revocation_url; revoke_token() simply
        # removes the credential from the vault.
        revocation_url=None,
        # R17-A1 / AT-434 (AC4): minimal, channels-only Microsoft Graph scopes —
        # exactly what the reach-phase TeamsIngestor needs to read channel messages
        # and their metadata, and nothing more:
        #   Team.ReadBasic.All     → list the teams AgentIQ has joined (/me/joinedTeams)
        #   Channel.ReadBasic.All  → list a team's channels + their metadata
        #   ChannelMessage.Read.All→ read those channels' messages (the delta query)
        #   offline_access         → issue a refresh token so the access token
        #                            auto-refreshes (vault) instead of expiring for good
        # Deliberately NO Chat.* / ChatMessage.* scopes (the 1:1 / group-DM surface)
        # and NO write scopes (*.ReadWrite, *.Send), so private chats and DMs can
        # NEVER be accessed — the AC4 privacy guarantee is enforced at the scope
        # level, in addition to the TeamsIngestor's standard-channels-only filter.
        # Microsoft's own consent screen shows exactly these scopes to the admin.
        scopes=[
            "offline_access",
            "Team.ReadBasic.All",
            "Channel.ReadBasic.All",
            "ChannelMessage.Read.All",
        ],
        authorization_url=f"https://login.microsoftonline.com/{TEAMS_TENANT_ID}/oauth2/v2.0/authorize",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
        # NOTE: deliberately NO prompt=consent here. The Teams Graph scopes
        # (ChannelMessage.Read.All, Team/Channel.ReadBasic.All) require Entra
        # ADMIN consent. Microsoft's `prompt=consent` FORCES the consent/approval
        # screen on every authorize request, so a non-admin user is shown
        # "Approval required" again on every (re)connect even after an admin has
        # already granted tenant-wide consent — the repeated-approval bug. With
        # prompt omitted, Microsoft skips the screen once consent is granted, and
        # offline_access (above) still yields a refresh token, so nothing is lost.
        # The one-time tenant admin-consent grant is an Entra setup step (Azure
        # portal → Enterprise applications → AgentIQ → Permissions → Grant admin
        # consent, or the /adminconsent endpoint), not a per-connect prompt.
    ),
    "sharepoint": ConnectorAuthConfig(
        connector_id="sharepoint",
        flow="authorization_code",
        # R18-A3: authorization_code (default) + client_credentials (AT-556 —
        # Microsoft Graph application permissions, outbound-only, no callback).
        # SharePoint reuses the same Graph app registration as Teams, so it gains the
        # client-credentials mode identically; an org in a no-public-inbound deployment
        # selects it to authenticate under a service identity with no browser redirect.
        supported_auth_modes=["authorization_code", "client_credentials"],
        # Graph client-credentials tokens use the single resource scope .default (the
        # granted app permissions — e.g. Sites.Read.All as an APPLICATION permission —
        # are resolved from the admin-consented app registration, not sent in the
        # request). The delegated ``scopes`` below still drive the authorization_code
        # flow. See docs/INTEGRATE_GRAPH_CLIENT_CREDENTIALS.md for the admin-consent setup.
        client_credentials_scopes=["https://graph.microsoft.com/.default"],
        # SharePoint reuses the Microsoft Graph app registration set up for Teams
        # (R17-A2 §6 / AT-462): the client id defaults to TEAMS_CLIENT_ID so a
        # single Graph app can serve both connectors, while SHAREPOINT_CLIENT_ID
        # can point at a dedicated app registration if a deployment prefers one.
        client_id=os.getenv("SHAREPOINT_CLIENT_ID")
        or os.getenv("TEAMS_CLIENT_ID")
        or "sharepoint-dev-client-id",
        secret_key="SHAREPOINT_CLIENT_SECRET",
        # Microsoft identity platform (v2.0) endpoints; the tenant segment is driven
        # by SHAREPOINT_TENANT_ID above (defaulting to the shared Teams tenant) so a
        # deployment can lock the OAuth flow to a single customer tenant.
        token_url=f"https://login.microsoftonline.com/{SHAREPOINT_TENANT_ID}/oauth2/v2.0/token",
        # Microsoft identity has no RFC-7009 token-revocation endpoint (same as
        # Teams), so — like Teams/GitHub/Slack — there is no revocation_url;
        # revoke_token() simply removes the credential from the vault.
        revocation_url=None,
        # R17-A2 / AT-462 (AC4): minimal, READ-ONLY Microsoft Graph scopes — exactly
        # what the reach-phase SharePointIngestor needs to enumerate granted sites,
        # their document libraries, and changed driveItems (the drive delta query),
        # and NOTHING more:
        #   Sites.Read.All → read items in all site collections the token is granted
        #                    (the /sites, /sites/{id}/drives and /drives/{id}/root/delta
        #                    metadata reads the connector performs)
        #   offline_access → issue a refresh token so the access token auto-refreshes
        #                    (vault) instead of expiring for good
        # Deliberately NO write scope (*.ReadWrite, Sites.Manage.All,
        # Sites.FullControl.All) and NO Files.* scope — the connector only reads
        # document activity/metadata SIGNAL; it never mutates SharePoint and never
        # reads document bodies (that is the 1.8 deep-content story). Microsoft Graph
        # only returns the sites/libraries the token is scoped to, so ungranted
        # sites can never be read — the AC4 least-privilege guarantee is enforced at
        # the scope level, in addition to the SharePointIngestor's granted-only
        # library filter. Microsoft's own consent screen shows exactly these scopes.
        scopes=[
            "offline_access",
            "Sites.Read.All",
        ],
        authorization_url=f"https://login.microsoftonline.com/{SHAREPOINT_TENANT_ID}/oauth2/v2.0/authorize",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
        # NOTE: deliberately NO prompt=consent (same rationale as Teams).
        # Sites.Read.All is an admin-consent Graph permission; Microsoft's
        # prompt=consent forces the approval screen on every authorize request, so a
        # non-admin user would be shown "Approval required" again on every
        # (re)connect even after an admin has granted tenant-wide consent. With
        # prompt omitted, Microsoft skips the screen once consent is granted, and
        # offline_access still yields a refresh token so nothing is lost.
    ),
    "sap": ConnectorAuthConfig(
        connector_id="sap",
        flow="client_credentials",
        supported_auth_modes=["client_credentials"],
        client_id=os.getenv("SAP_CLIENT_ID", "sap-dev-client-id"),
        secret_key="SAP_CLIENT_SECRET",
        token_url=f"https://{SAP_SUBDOMAIN}.authentication.{SAP_REGION}.hana.ondemand.com/oauth/token",
        revocation_url=None,                                                        # client_credentials — no user token to revoke
        scopes=["uaa.resource"],
        redirect_uri=None,                                                          # client_credentials — no browser redirect
        authorization_url=None,
    ),
    "dynamics365": ConnectorAuthConfig(
        connector_id="dynamics365",
        flow="client_credentials",
        supported_auth_modes=["client_credentials"],
        client_id=os.getenv("DYNAMICS365_CLIENT_ID", "dynamics365-dev-client-id"),
        secret_key="DYNAMICS365_CLIENT_SECRET",
        token_url=f"https://login.microsoftonline.com/{DYNAMICS365_TENANT_ID}/oauth2/v2.0/token",
        revocation_url=None,                                                        # client_credentials — no user token to revoke
        scopes=["default"],
        redirect_uri=None,                                                          # client_credentials — no browser redirect
        authorization_url=None,
    ),
}