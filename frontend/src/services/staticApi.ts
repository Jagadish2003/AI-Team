import { apiGet, apiPost } from '../lib/apiClient';

import type { Connector } from '../types/connector';
import type { UploadedFile } from '../types/upload';
import type { PermissionRequirement } from '../types/normalization';

export function fetchConnectors(): Promise<Connector[]> {
  return apiGet<Connector[]>('/api/connectors');
}

export function connectConnectorApi(connectorId: string): Promise<Connector> {
  return apiPost<Connector>(`/api/connectors/${connectorId}/connect`, { status: 'connected' });
}

export function fetchUploads(): Promise<UploadedFile[]> {
  return apiGet<UploadedFile[]>('/api/uploads');
}

export function addUpload(file: { name: string; sizeLabel?: string }): Promise<UploadedFile> {
  return apiPost<UploadedFile>('/api/uploads', { name: file.name, sizeLabel: file.sizeLabel ?? '—' });
}

export function fetchPermissions(): Promise<PermissionRequirement[]> {
  return apiGet<PermissionRequirement[]>('/api/permissions');
}
