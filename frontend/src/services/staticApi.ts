import { apiGet, apiPost } from "../lib/apiClient";

import type { Connector } from "../types/connector";
import type { UploadedFile } from "../types/upload";
// 1. Add the new types needed for the new functions
import type {
  PermissionRequirement,
  MappingRow,
  ConfidenceExplanation,
} from "../types/normalization";

export type TokenStatus = 'valid' | 'expired' | 'missing';

export interface TokenStatusResponse {
  status: TokenStatus;
  expires_at: string | null;
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
