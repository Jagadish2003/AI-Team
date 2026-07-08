import { apiGet, apiPost, apiDelete } from "../lib/apiClient";

import type { Connector } from "../types/connector";
import type { UploadedFile } from "../types/upload";
// 1. Add the new types needed for the new functions
import type {
  PermissionRequirement,
  MappingRow,
  ConfidenceExplanation,
} from "../types/normalization";

/**
 * Token status as returned by GET /api/connectors/{id}/token-status.
 *
 * These values mirror the backend contract (AT-77 AC14, enforced by
 * backend/tests/contract/test_connector_auth.py) — do NOT use valid/expired/
 * missing here; the backend never emits those:
 *   - connected      → token present and well beyond the refresh threshold
 *   - needs_refresh   → within the refresh threshold but still valid (auto-refresh)
 *   - needs_auth      → no token, or the token has already expired
 *   - refresh_failed  → a refresh was attempted and failed; user must re-auth
 */
export type TokenStatus = 'connected' | 'needs_refresh' | 'needs_auth' | 'refresh_failed';

export interface TokenStatusResponse {
  status: TokenStatus;
  // Backend currently returns only `status`; kept optional for forward-compat.
  expires_at?: string | null;
}

export function fetchConnectors(): Promise<Connector[]> {
  return apiGet<Connector[]>("/api/connectors");
}

export function fetchTokenStatus(connectorId: string): Promise<TokenStatusResponse> {
  return apiGet<TokenStatusResponse>(`/api/connectors/${connectorId}/token-status`);
}

/**
 * CS-2 / AT-323 (T1): Initiate the real OAuth Connect flow.
 *
 * Instead of marking the connector connected without authentication, this
 * fetches a one-time provider auth URL from the backend
 * (GET /api/connectors/{id}/auth-url — the backend mints a state nonce) and
 * redirects the browser to the provider's login page. The provider then
 * redirects back to OAUTH_REDIRECT_URI, the backend exchanges the code and
 * stores the token, and finally redirects to the frontend /oauth/callback.
 *
 * Does not resolve normally on success — the browser navigates away. The
 * returned promise only settles (rejects) if fetching the auth URL fails.
 */
export async function connectConnectorApi(connectorId: string): Promise<void> {
  // Step 1: Get the OAuth auth URL from the backend (mints a state nonce).
  const { auth_url } = await apiGet<{ auth_url: string; connector_id: string }>(
    `/api/connectors/${connectorId}/auth-url`
  );

  // Step 2: Redirect the browser to the provider login page. The provider
  // redirects back to OAUTH_REDIRECT_URI after authorisation.
  window.location.href = auth_url;
  // Function does not return normally — browser navigates away.
}

export function configureSyncApi(connectorId: string): Promise<Connector> {
  return apiPost<Connector>(`/api/connectors/${connectorId}/configure`, {});
}

/**
 * R17-D3 Addendum A (T12 / AC10) — static-credential entry for connectors that
 * authenticate with URL + username + token/password (Jira, ServiceNow, native
 * DB connectors) rather than OAuth.
 *
 * Values are WRITE-ONLY: {@link saveConnectorCredentials} sends them to the
 * backend, which Fernet-encrypts them into the caller's org vault, and NOTHING
 * ever reads them back. {@link ConnectorCredentialStatus} carries only non-secret
 * metadata (whether a credential is configured, its instance base_url, and
 * whether a username is present) — never the username value or the secret.
 */
export interface ConnectorCredentialStatus {
  connector_id: string;
  configured: boolean;
  /** Non-secret instance location (Jira/ServiceNow URL or DB host). null when unset. */
  base_url: string | null;
  /** True when a username is stored — presence only, never the value. */
  has_username: boolean;
  updated_at: string | null;
}

export interface StaticCredentialInput {
  base_url: string;
  username: string;
  /** API token / password. Sent once; never returned by any endpoint. */
  secret: string;
}

/** GET /api/connectors/{id}/credentials — status only; never returns the secret. */
export function fetchConnectorCredentialStatus(
  connectorId: string,
): Promise<ConnectorCredentialStatus> {
  return apiGet<ConnectorCredentialStatus>(
    `/api/connectors/${connectorId}/credentials`,
  );
}

/** POST /api/connectors/{id}/credentials — Owner-only; encrypts into the vault. */
export function saveConnectorCredentials(
  connectorId: string,
  input: StaticCredentialInput,
): Promise<ConnectorCredentialStatus> {
  return apiPost<ConnectorCredentialStatus>(
    `/api/connectors/${connectorId}/credentials`,
    input,
  );
}

/** DELETE /api/connectors/{id}/credentials — Owner-only; revokes the credential. */
export function deleteConnectorCredentials(connectorId: string): Promise<void> {
  return apiDelete<void>(`/api/connectors/${connectorId}/credentials`);
}

/**
 * R18-A3 T5 (AT-558) — outbound-only auth setup paths for connectors offered in a
 * NETWORK_PROFILE=no_public_inbound deployment, where the browser
 * authorization-code flow cannot complete.
 *
 * Two shapes:
 *   - JWT bearer (Salesforce): an Owner enters the connected-app cert private key
 *     + username + login URL once (WRITE-ONLY; never returned) — the access token
 *     mints outbound from it (R18-A3 T2 / AT-555).
 *   - client-credentials (Microsoft Graph Teams/SharePoint, ServiceNow): a single
 *     Owner action acquires a service-identity token outbound; no body, since the
 *     credential is the deployment's app secret, not a per-user entry
 *     (R18-A3 T3/T4 / AT-556/AT-557).
 */

/** Input for the Salesforce JWT-bearer setup form. `privateKey` is write-only. */
export interface JwtBearerCredentialInput {
  login_url: string;
  username: string;
  /** PEM private key. Sent once; never returned by any endpoint. */
  private_key: string;
}

/** GET /api/connectors/{id}/jwt-credentials — status only; never returns the key. */
export function fetchJwtBearerCredentialStatus(
  connectorId: string,
): Promise<ConnectorCredentialStatus> {
  return apiGet<ConnectorCredentialStatus>(
    `/api/connectors/${connectorId}/jwt-credentials`,
  );
}

/** POST /api/connectors/{id}/jwt-credentials — Owner-only; encrypts into the vault. */
export function saveJwtBearerCredentials(
  connectorId: string,
  input: JwtBearerCredentialInput,
): Promise<ConnectorCredentialStatus> {
  return apiPost<ConnectorCredentialStatus>(
    `/api/connectors/${connectorId}/jwt-credentials`,
    input,
  );
}

/** DELETE /api/connectors/{id}/jwt-credentials — Owner-only; revokes the key. */
export function deleteJwtBearerCredentials(connectorId: string): Promise<void> {
  return apiDelete<void>(`/api/connectors/${connectorId}/jwt-credentials`);
}

/** Response for the outbound client-credentials connect. */
export interface ClientCredentialsConnectStatus {
  connector_id: string;
  connected: boolean;
  auth_mode: string;
}

/**
 * POST /api/connectors/{id}/client-credentials — Owner-only; acquires a
 * service-identity token outbound (no callback). Takes no body.
 */
export function connectClientCredentials(
  connectorId: string,
): Promise<ClientCredentialsConnectStatus> {
  return apiPost<ClientCredentialsConnectStatus>(
    `/api/connectors/${connectorId}/client-credentials`,
    {},
  );
}

export function fetchUploads(): Promise<UploadedFile[]> {
  return apiGet<UploadedFile[]>("/api/uploads");
}

export function addUpload(file: {
  name: string;
  sizeLabel?: string;
}): Promise<UploadedFile> {
  return apiPost<UploadedFile>("/api/uploads", {
    name: file.name,
    sizeLabel: file.sizeLabel ?? "—",
  });
}

export function fetchPermissions(): Promise<PermissionRequirement[]> {
  return apiGet<PermissionRequirement[]>("/api/permissions");
}

export function fetchMappings(): Promise<MappingRow[]> {
  return apiGet<MappingRow[]>("/api/mappings");
}

export function fetchConfidence(): Promise<ConfidenceExplanation> {
  return apiGet<ConfidenceExplanation>("/api/confidence/explanation");
}
