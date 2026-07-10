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
 * R18-C0 P4 / AT-566: Disconnect a connector for the caller's org.
 *
 * The single call behind each connected Integration Hub tile's Disconnect action.
 * DELETE /api/connectors/{id} (analyst+) clears WHICHEVER credential kind the org
 * holds for the connector — an OAuth token (revoked + soft-deleted) or a static
 * credential (soft-deleted) — and flips the org's connection state back to
 * disconnected, so re-fetching /api/connectors returns the tile unconnected. The
 * backend is idempotent (a never-/already-disconnected connector still 204s).
 */
export function disconnectConnectorApi(connectorId: string): Promise<void> {
  return apiDelete<void>(`/api/connectors/${connectorId}`);
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
