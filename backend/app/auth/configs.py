from __future__ import annotations

from typing import Dict

from app.auth.models import ConnectorAuthConfig

CONNECTOR_AUTH_CONFIGS: Dict[str, ConnectorAuthConfig] = {
    "salesforce": ConnectorAuthConfig(
        connector_id="salesforce",
        flow="authorization_code",
        client_id="REPLACE_WITH_SALESFORCE_CLIENT_ID",
        secret_key="SALESFORCE_CLIENT_SECRET",
        token_url="https://{instance}.salesforce.com/services/oauth2/token",
        revocation_url="https://{instance}.salesforce.com/services/oauth2/revoke",
        scopes=["api", "refresh_token", "offline_access"],
        # {instance} substituted per-org at call time (A4 — resolved in vault layer)
        authorization_url="https://login.salesforce.com/services/oauth2/authorize",
    ),
    "servicenow": ConnectorAuthConfig(
        connector_id="servicenow",
        flow="authorization_code",
        client_id="REPLACE_WITH_SERVICENOW_CLIENT_ID",
        secret_key="SERVICENOW_CLIENT_SECRET",
        token_url="https://{instance}.service-now.com/oauth_token.do",
        revocation_url="https://{instance}.service-now.com/oauth_revoke.do",
        scopes=["useraccount"],
        # {instance} substituted per-org at call time (A4 — resolved in vault layer)
        authorization_url="https://{instance}.service-now.com/oauth_auth.do",
    ),
    "jira": ConnectorAuthConfig(
        connector_id="jira",
        flow="authorization_code",
        client_id="REPLACE_WITH_JIRA_CLIENT_ID",
        secret_key="JIRA_CLIENT_SECRET",
        token_url="https://auth.atlassian.com/oauth/token",
        revocation_url="https://auth.atlassian.com/oauth/token/revoke",
        scopes=["read:jira-work", "read:jira-user", "offline_access"],
        authorization_url="https://auth.atlassian.com/authorize",
    ),
    "github": ConnectorAuthConfig(
        connector_id="github",
        flow="authorization_code",
        client_id="REPLACE_WITH_GITHUB_CLIENT_ID",
        secret_key="GITHUB_CLIENT_SECRET",
        token_url="https://github.com/login/oauth/access_token",
        revocation_url=None,
        scopes=["repo", "read:user", "read:org"],
        authorization_url="https://github.com/login/oauth/authorize",
    ),
    "confluence": ConnectorAuthConfig(
        connector_id="confluence",
        flow="authorization_code",
        client_id="REPLACE_WITH_CONFLUENCE_CLIENT_ID",
        secret_key="CONFLUENCE_CLIENT_SECRET",
        token_url="https://auth.atlassian.com/oauth/token",
        revocation_url="https://auth.atlassian.com/oauth/token/revoke",
        scopes=["read:confluence-space.summary", "read:confluence-content.all", "offline_access"],
        authorization_url="https://auth.atlassian.com/authorize",
    ),
    "slack": ConnectorAuthConfig(
        connector_id="slack",
        flow="authorization_code",
        client_id="REPLACE_WITH_SLACK_CLIENT_ID",
        secret_key="SLACK_CLIENT_SECRET",
        token_url="https://slack.com/api/oauth.v2.access",
        revocation_url=None,  # Slack-specific revocation added in T1-S12-C
        scopes=["channels:read", "users:read", "team:read"],
        authorization_url="https://slack.com/oauth/v2/authorize",
    ),
    "sap": ConnectorAuthConfig(
        connector_id="sap",
        flow="client_credentials",
        client_id="REPLACE_WITH_SAP_CLIENT_ID",
        secret_key="SAP_CLIENT_SECRET",
        token_url="https://{tenant}.authentication.sap.hana.ondemand.com/oauth/token",
        revocation_url=None,  # client_credentials — no user token
        scopes=["uaa.resource"],
        redirect_uri=None,
        authorization_url=None,  # client_credentials — no browser redirect
    ),
    "d365": ConnectorAuthConfig(
        connector_id="d365",
        flow="client_credentials",
        client_id="REPLACE_WITH_D365_CLIENT_ID",
        secret_key="D365_CLIENT_SECRET",
        token_url="https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        revocation_url=None,  # client_credentials — no user token
        scopes=["https://dynamics.microsoft.com/.default"],
        redirect_uri=None,
        authorization_url=None,  # client_credentials — no browser redirect
    ),
}
