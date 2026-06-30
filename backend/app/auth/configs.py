from __future__ import annotations

import os
from typing import Dict

from dotenv import load_dotenv

from app.auth.models import ConnectorAuthConfig

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
# instance/tenant. Jira, Confluence, GitHub and Slack use fixed global hosts
# (auth.atlassian.com, github.com, slack.com) that never vary per deployment.
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

# Microsoft Entra ID (Azure AD) tenant for Microsoft Teams (Microsoft Graph).
# "common" lets any work/school tenant's admin consent for their organisation;
# set a specific tenant GUID to lock the Teams connector to one Microsoft 365
# tenant. Teams authenticates against Microsoft Graph via the Microsoft identity
# platform v2.0 endpoints built from this value.
TEAMS_TENANT_ID = os.getenv("TEAMS_TENANT_ID", "common")

CONNECTOR_AUTH_CONFIGS: Dict[str, ConnectorAuthConfig] = {
    "salesforce": ConnectorAuthConfig(
        connector_id="salesforce",
        flow="authorization_code",
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
        client_id=os.getenv("GITHUB_CLIENT_ID", "github-dev-client-id"),
        secret_key="GITHUB_CLIENT_SECRET",
        token_url="https://github.com/login/oauth/access_token",
        revocation_url=None,
        scopes=["repo:status", "read:org", "read:user"],
        authorization_url="https://github.com/login/oauth/authorize",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
    ),
    "teams": ConnectorAuthConfig(
        connector_id="teams",
        flow="authorization_code",
        client_id=os.getenv("TEAMS_CLIENT_ID", "teams-dev-client-id"),
        secret_key="TEAMS_CLIENT_SECRET",
        token_url=f"https://login.microsoftonline.com/{TEAMS_TENANT_ID}/oauth2/v2.0/token",
        revocation_url=None,                                                        # Microsoft identity platform has no OAuth2 revoke endpoint; tokens expire + refresh
        # R17-A1 §3 / AT-434: least-privilege, READ-ONLY Microsoft Graph scopes —
        # only what the reach-phase Teams ingestor needs to list the granted teams
        # / channels and read those channels' messages. Deliberately NO Chat.*
        # scopes (1:1 / group direct messages) and NO write/send scopes, so
        # private chats and DMs can never be accessed (AC4) — the privacy
        # guarantee is enforced at the OAuth-scope level, and Microsoft's own
        # consent screen shows the admin exactly these permissions. offline_access
        # yields a refresh token so the access token auto-refreshes in the vault.
        scopes=[
            "offline_access",
            "https://graph.microsoft.com/Team.ReadBasic.All",
            "https://graph.microsoft.com/Channel.ReadBasic.All",
            "https://graph.microsoft.com/ChannelMessage.Read.All",
        ],
        authorization_url=f"https://login.microsoftonline.com/{TEAMS_TENANT_ID}/oauth2/v2.0/authorize",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
        # Microsoft v2.0: prompt=consent guarantees the admin sees the requested
        # scopes and a refresh_token is issued (with offline_access above).
        authorize_params={"prompt": "consent"},
    ),
    "slack": ConnectorAuthConfig(
        connector_id="slack",
        flow="authorization_code",
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
    "sap": ConnectorAuthConfig(
        connector_id="sap",
        flow="client_credentials",
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
        client_id=os.getenv("DYNAMICS365_CLIENT_ID", "dynamics365-dev-client-id"),
        secret_key="DYNAMICS365_CLIENT_SECRET",
        token_url=f"https://login.microsoftonline.com/{DYNAMICS365_TENANT_ID}/oauth2/v2.0/token",
        revocation_url=None,                                                        # client_credentials — no user token to revoke
        scopes=["default"],
        redirect_uri=None,                                                          # client_credentials — no browser redirect
        authorization_url=None,
    ),
}