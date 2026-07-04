/**
 * R17-D3 Addendum A (T12 / AC10) — the connectors that authenticate with STATIC
 * credentials (URL + username + token/password) entered by an Owner in the
 * Integration Hub, rather than the OAuth Connect flow.
 *
 * Per the addendum: Jira, ServiceNow, and the native DB connectors (SQL Server /
 * Oracle / PostgreSQL). Jira and ServiceNow also support OAuth (the tile's
 * Connect button); this static path is the addendum's credential-form
 * alternative for them and the primary path for the DB connectors.
 *
 * This is the FRONTEND source of truth for that set. It must stay in lock-step
 * with the backend allow-list `STATIC_CREDENTIAL_CONNECTORS` in
 * `app/routes_connector_auth.py` (which is the authority that actually accepts or
 * rejects a POST). SQL Server is `sql_server` in the connector catalog; the
 * `sqlserver` alias mirrors the backend so the id used by the DB-driver layer
 * also resolves.
 */

export interface StaticCredentialFields {
  /** Non-secret instance location. */
  urlLabel: string;
  urlPlaceholder: string;
  usernameLabel: string;
  usernamePlaceholder: string;
  /** The write-only secret — "API token" for Jira, "Password" for the rest. */
  secretLabel: string;
  /** One-line helper shown under the form title. */
  hint: string;
}

const DB_FIELDS = (placeholder: string): StaticCredentialFields => ({
  urlLabel: "Host / connection URL",
  urlPlaceholder: placeholder,
  usernameLabel: "Username",
  usernamePlaceholder: "service_account",
  secretLabel: "Password",
  hint: "Connection credentials are encrypted into your workspace vault.",
});

export const STATIC_CREDENTIAL_CONNECTORS: Record<string, StaticCredentialFields> = {
  jira: {
    urlLabel: "Jira base URL",
    urlPlaceholder: "https://your-domain.atlassian.net",
    usernameLabel: "Email",
    usernamePlaceholder: "you@company.com",
    secretLabel: "API token",
    hint: "Create an API token in Atlassian account settings, then enter it here.",
  },
  servicenow: {
    urlLabel: "ServiceNow instance URL",
    urlPlaceholder: "https://your-instance.service-now.com",
    usernameLabel: "Username",
    usernamePlaceholder: "integration.user",
    secretLabel: "Password",
    hint: "Use a dedicated integration user's credentials.",
  },
  postgresql: DB_FIELDS("db.internal:5432/analytics"),
  sql_server: DB_FIELDS("sqlserver.internal:1433"),
  sqlserver: DB_FIELDS("sqlserver.internal:1433"),
  oracle_db: DB_FIELDS("oracle.internal:1521/ORCL"),
};

/** True when a connector authenticates with static credentials (has a form). */
export function isStaticCredentialConnector(connectorId: string): boolean {
  return connectorId in STATIC_CREDENTIAL_CONNECTORS;
}

/** Field metadata for a connector's credential form, or null if not static. */
export function staticCredentialFields(
  connectorId: string,
): StaticCredentialFields | null {
  return STATIC_CREDENTIAL_CONNECTORS[connectorId] ?? null;
}
