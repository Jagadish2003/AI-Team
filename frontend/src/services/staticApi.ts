import { apiGet, apiPost } from "../lib/apiClient";

import type { Connector } from "../types/connector";
import type { UploadedFile } from "../types/upload";
// 1. Add the new types needed for the new functions
import type { PermissionRequirement, MappingRow, ConfidenceExplanation } from "../types/normalization";

export function fetchConnectors(): Promise<Connector[]> {
  return apiGet<Connector[]>("/api/connectors");
}

export function connectConnectorApi(connectorId: string): Promise<Connector> {
  return apiPost<Connector>(`/api/connectors/${connectorId}/connect`, { status: "connected" });
}

export function fetchUploads(): Promise<UploadedFile[]> {
  return apiGet<UploadedFile[]>("/api/uploads");
}

export function addUpload(file: { name: string; sizeLabel?: string }): Promise<UploadedFile> {
  return apiPost<UploadedFile>("/api/uploads", { name: file.name, sizeLabel: file.sizeLabel ?? "—" });
}

export function fetchPermissions(): Promise<PermissionRequirement[]> {
  return apiGet<PermissionRequirement[]>("/api/permissions");
}

export function fetchMappings(): Promise<MappingRow[]> {
  return apiGet<MappingRow[]>("/api/mappings");
}

export function fetchConfidence(): Promise<ConfidenceExplanation> {
  return apiGet<ConfidenceExplanation>("/api/confidence");
}