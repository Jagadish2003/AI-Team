from __future__ import annotations

import os
from typing import Dict

from app.auth.models import ConnectorAuthConfig

# client_id values are non-sensitive per T1-S10-A spec and can be in code.
# os.getenv() allows override from environment (production); the fallback is a
# stable dev/CI placeholder so tests never receive None and CI needs no .env file.
CONNECTOR_AUTH_CONFIGS: Dict[str, ConnectorAuthConfig] = {
    "salesforce": ConnectorAuthConfig(
        connector_id="salesforce",
        flow="authorization_code",
        client_id=os.getenv("SALESFORCE_CLIENT_ID", "salesforce-dev-client-id"),
        secret_key="SALESFORCE_CLIENT_SECRET",
        token_url="https://test.salesforce.com/services/oauth2/token",
        revocation_url="https://test.salesforce.com/services/oauth2/revoke",
        scopes=["api", "refresh_token", "offline_access"],
        authorization_url="https://test.salesforce.com/services/oauth2/authorize",
        redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", ""),
    ),
    "servicenow": ConnectorAuthConfig(
        connector_id="servicenow",
        flow="authorization_code",
        client_id=os.getenv("SERVICENOW_CLIENT_ID", "servicenow-dev-client-id"),
        secret_key="SERVICENOW_CLIENT_SECRET",
        token_url="https://dev198195.service-now.com/oauth_token.do",
        revocation_url="https://dev198195.service-now.com/oauth_revoke.do",
        scopes=["user", "admin"],
        authorization_url="https://dev198195.service-now.com/oauth_auth.do",
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
        token_url="https://05e6c258trial.authentication.us10.hana.ondemand.com/oauth/token",
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
        token_url="https://login.microsoftonline.com/bb612c49-03be-4da1-9974-49f0c8704eb8/oauth2/v2.0/token",
        revocation_url=None,                                                        # client_credentials — no user token to revoke
        scopes=["default"],
        redirect_uri=None,                                                          # client_credentials — no browser redirect
        authorization_url=None,
    ),
}