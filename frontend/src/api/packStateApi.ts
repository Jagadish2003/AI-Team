import { apiGet } from '../lib/apiClient';
import type { PackCertification } from '../types/packCertification';

/**
 * packStateApi — the pack lifecycle + certification read surface (2.0-C1 / 2.0-C2).
 *
 * `GET /api/packs/state` is viewer+ on purpose: anyone who can select a pack, or
 * who sees a "now-disabled pack" label or a Certified badge, must be able to
 * confirm it.
 */
export interface PackStateItem {
  packId: string;
  packName: string;
  packVersion: string | null;
  state: 'active' | 'disabled';
  revision: number;
  reason: string | null;
  updatedBy: string | null;
  updatedAt: string | null;
  pinnedVersion: string | null;
  effectiveVersion: string | null;
  availableVersions: string[];
  registered: boolean;
  /**
   * 2.0-C2 T3 (AT-833): the pack's signature-verified certification level. Null
   * when it could not be resolved, or for an orphaned row whose pack the registry
   * no longer declares — never a guessed level.
   */
  certification: PackCertification | null;
}

export interface PackStateResponse {
  orgId: string;
  packs: PackStateItem[];
}

export async function fetchPackStates(): Promise<PackStateResponse> {
  return apiGet<PackStateResponse>('/api/packs/state');
}

/** `{packId: certification}` for the packs that have a resolvable badge. */
export function certificationsByPackId(
  response: PackStateResponse | null | undefined,
): Record<string, PackCertification> {
  const out: Record<string, PackCertification> = {};
  for (const pack of response?.packs ?? []) {
    if (pack.certification) out[pack.packId] = pack.certification;
  }
  return out;
}
