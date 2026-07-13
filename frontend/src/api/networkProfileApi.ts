/**
 * R18-A3 T5 (AT-558) — network-profile API wrapper.
 *
 * Typed client over GET /api/network-profile: the deployment's inbound-network
 * posture (`standard` | `no_public_inbound`) plus the per-connector auth
 * capability map. Readable by any authenticated hub viewer (viewer+).
 *
 * Goes through the shared apiClient so the request carries the in-session JWT and
 * never hardcodes a host outside the dev fallback. Callers handle ApiError.
 */
import { apiGet } from '../lib/apiClient';
import type { NetworkProfileResponse } from '../types/networkProfile';

export type { NetworkProfileResponse };

/** GET /api/network-profile — deployment posture + per-connector auth capability. */
export function fetchNetworkProfile(): Promise<NetworkProfileResponse> {
  return apiGet<NetworkProfileResponse>('/api/network-profile');
}
