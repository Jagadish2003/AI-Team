from __future__ import annotations

from typing import Dict

from app.auth.models import ConnectorAuthConfig
import os

CONNECTOR_AUTH_CONFIGS: Dict[str, ConnectorAuthConfig] = {
    "salesforce": ConnectorAuthConfig(
        connector_id="salesforce",
        flow="authorization_code",
        client_id=os.getenv("SALESFORCE_CLIENT_ID"),
        secret_key="SALESFORCE_SECRET_KEY",
        token_url="https://test.salesforce.com/services/oauth2/token",
        revocation_url="https://test.salesforce.com/services/oauth2/revoke",
        scopes=["api", "refresh_token", "offline_access"],
        # {instance} substituted per-org at call time (A4 — resolved in vault layer)
        authorization_url="https://test.salesforce.com/services/oauth2/authorize",
        redirect_uri="https://agentiq.app/api/connectors/oauth/callback",
    ),
    "servicenow": ConnectorAuthConfig(
        connector_id="servicenow",
        flow="authorization_code",
        client_id=os.getenv("SERVICENOW_CLIENT_ID"),
        secret_key="SERVICENOW_SECRET_KEY",
        token_url="https://dev198195.service-now.com/oauth_token.do",
        revocation_url="https://dev198195.service-now.com/oauth_revoke.do",
        scopes=["useraccount"],
        # {instance} substituted per-org at call time (A4 — resolved in vault layer)
        authorization_url="https://dev198195.service-now.com/oauth_auth.do",
        redirect_uri="https://agentiq.app/api/connectors/oauth/callback",
    ),
    "jira": ConnectorAuthConfig(
        connector_id="jira",
        flow="authorization_code",
        client_id=os.getenv("JIRA_CLIENT_ID"),
        secret_key="JIRA_SECRET_KEY",
        token_url="https://auth.atlassian.com/oauth/token",
        revocation_url="https://auth.atlassian.com/oauth/token/revoke",
        scopes=["read:jira-work", "read:jira-user"],
        authorization_url="https://auth.atlassian.com/authorize",
        redirect_uri="https://agentiq.app/api/connectors/oauth/callback",
    ),
    "github": ConnectorAuthConfig(
        connector_id="github",
        flow="authorization_code",
        client_id=os.getenv("GITHUB_CLIENT_ID"),
        secret_key="GITHUB_CLIENT_SECRET",
        token_url="https://github.com/login/oauth/access_token",
        revocation_url=None,
        scopes=["repo", "read:user", "user:email"],
        authorization_url="https://github.com/login/oauth/authorize",
        redirect_uri="https://app.example.com/auth/callbacks/github",
    ),
    "confluence": ConnectorAuthConfig(
        connector_id="confluence",
        flow="authorization_code",
        client_id=os.getenv("CONFLUENCE_CLIENT_ID"),
        secret_key="CONFLUENCE_CLIENT_SECRET",
        token_url="https://auth.atlassian.com/oauth/token",
        revocation_url="https://auth.atlassian.com/oauth/token/revoke",
        scopes=["read:confluence-space.summary", "read:confluence-content.all", "offline_access"],
        authorization_url="https://auth.atlassian.com/authorize",
        redirect_uri="https://app.example.com/auth/callbacks/confluence",
    ),
    "slack": ConnectorAuthConfig(
        connector_id="slack",
        flow="authorization_code",
        client_id=os.getenv("SLACK_CLIENT_ID"),
        secret_key="SLACK_CLIENT_SECRET",
        token_url="https://slack.com/api/oauth.v2.access",
        revocation_url=None,  # Slack-specific revocation added in T1-S12-C
        scopes=["channels:read", "chat:write", "users:read", "team:read"],
        authorization_url="https://slack.com/oauth/v2/authorize",
        redirect_uri="https://app.example.com/auth/callbacks/slack",
    ),
    "sap": ConnectorAuthConfig(
        connector_id="sap",
        flow="client_credentials",
        client_id=os.getenv("SAP_CLIENT_ID"),
        secret_key="SAP_CLIENT_SECRET",
        token_url="https://05e6c258trial.authentication.us10.hana.ondemand.com/oauth/token%22",
        revocation_url=None,  # client_credentials — no user token
        scopes=["uaa.resource"],
        redirect_uri=None,  # client_credentials — no browser redirect
        authorization_url=None,  # client_credentials — no browser redirect
    ),
    "d365": ConnectorAuthConfig(
        connector_id="d365",
        flow="client_credentials",
        client_id=os.getenv("D365_CLIENT_ID"),
        secret_key="D365_CLIENT_SECRET",
        token_url="https://login.microsoftonline.com/bb612c49-03be-4da1-9974-49f0c8704eb8/oauth2/v2.0/token",
        revocation_url=None,  # client_credentials — no user token
        scopes=["https://org64f3b8f9.crm8.dynamics.com/.default"],
        redirect_uri=None,  # client_credentials — no browser redirect
        authorization_url=None,  # client_credentials — no browser redirect
    ),
}