import { apiGet } from "../lib/apiClient";
import type {
  AttentionHealthResponse,
  ConnectorHealthResponse,
  ContentHealthResponse,
  PackHealthResponse,
  RunHealthResponse,
} from "../types/runHealth";

export const fetchConnectorHealth = (): Promise<ConnectorHealthResponse> =>
  apiGet("/api/run-health/connectors");

export const fetchRunHealth = (): Promise<RunHealthResponse> =>
  apiGet("/api/run-health/runs?limit=10");

export const fetchContentHealth = (): Promise<ContentHealthResponse> =>
  apiGet("/api/run-health/content");

export const fetchPackHealth = (): Promise<PackHealthResponse> =>
  apiGet("/api/run-health/packs");

export const fetchAttentionHealth = (): Promise<AttentionHealthResponse> =>
  apiGet("/api/run-health/attention");
