from __future__ import annotations

import os
from typing import Dict

from app.auth.models import ConnectorAuthConfig

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
# Only Salesforce, ServiceNow, SAP and Dynamics365 have a per-customer
# instance/tenant. Jira, Confluence, GitHub and Slack use fixed global hosts
# (auth.atlassian.com, github.com, slack.com) that never vary per deployment.
# ---------------------------------------------------------------------------

# Salesforce login host: "test.salesforce.com" (sandbox),
# "login.salesforce.com" (production), or your My Domain host
# e.g. "mycompany.my.salesforce.com".
SALESFORCE_INSTANCE = "test.salesforce.com"

# ServiceNow instance subdomain only, e.g. "dev198195" → dev198195.service-now.com
SERVICENOW_INSTANCE = "dev198195"

# SAP BTP subaccount subdomain + region, e.g. "05e6c258trial" + "us10"
# → 05e6c258trial.authentication.us10.hana.ondemand.com
SAP_SUBDOMAIN = "05e6c258trial"
SAP_REGION = "us10"

# Microsoft Entra ID (Azure AD) tenant id (GUID) for Dynamics 365.
DYNAMICS365_TENANT_ID = "bb612c49-03be-4da1-9974-49f0c8704eb8"

CONNECTOR_AUTH_CONFIGS: Dict[str, ConnectorAuthConfig] = {
    "salesforce": ConnectorAuthConfig(
        connector_id="salesforce",
        flow="authorization_code",
        client_id=os.getenv("SALESFORCE_CLIENT_ID", "salesforce-dev-client-id"),
        secret_key="SALESFORCE_CLIENT_SECRET",
        token_url=f"https://{SALESFORCE_INSTANCE}/services/oauth2/token",
        revocation_url=f"https://{SALESFORCE_INSTANCE}/services/oauth2/revoke",
        scopes=["api", "refresh_token", "offline_access"],
        authorization_url=f"https://{SALESFORCE_INSTANCE}/services/oauth2/authorize",
        redirect_uri=os.environ.get("SALESFORCE_OAUTH_REDIRECT_URI", ""),
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
        scopes=["read:jira-work", "read:jira-user", "offline_access"],
        authorization_url="https://auth.atlassian.com/authorize",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
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
    "slack": ConnectorAuthConfig(
        connector_id="slack",
        flow="authorization_code",
        client_id=os.getenv("SLACK_CLIENT_ID", "slack-dev-client-id"),
        secret_key="SLACK_CLIENT_SECRET",
        token_url="https://slack.com/api/oauth.v2.access",
        revocation_url=None,                                                        # Slack uses auth.revoke Web API — see vault.py revoke_token()
        scopes=["channels:read", "channels:history", "users:read", "team:read"],
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