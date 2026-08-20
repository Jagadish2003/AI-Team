import { apiGet } from '../lib/apiClient';
import type { PackCertification } from '../types/packCertification';
import type { PackDeprecationNotice } from '../types/packDeprecation';

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
  /**
   * 2.0-C4 T2 (AT-843): the pack's deprecation notice — why it is going away, the
   * date it stops being supported, and what replaces it. Null for a pack that is
   * not deprecated (the normal case) and for an orphaned row, so a picker renders
   * a notice or nothing. Optional because it is additive — a response served
   * before contract v1.23 omits the field entirely.
   */
  deprecation?: PackDeprecationNotice | null;
  /**
   * 2.0-C2 T4 (AT-834): true when this org's certification policy would refuse the
   * pack at activation. Advisory — the gate lives at activation — so a selection
   * surface can grey a pack out instead of 409-ing after a whole run is configured.
   * Absent when the policy could not be read (the annotation is fail-soft; the gate
   * is not).
   */
  activationBlocked?: boolean;
  activationBlockedReason?: string | null;
  /**
   * Whether the policy behind `activationBlocked` could be read. `'unavailable'`
   * means eligibility is INDETERMINATE, not permitted: `activationBlocked` is
   * absent and the activation gate will still refuse (fail-closed). Do not treat a
   * missing `activationBlocked` as "activatable" without checking this.
   */
  activationPolicyStatus?: 'available' | 'unavailable';
}

/** 2.0-C2 T4 (AT-834): the org's activation floor. */
export interface PackCertificationPolicy {
  orgId: string;
  minimumLevel: 'certified' | 'partner' | 'community';
  minimumLevelLabel: string;
  /** False when the floor is `community`, which excludes nothing. */
  restricted: boolean;
  label: string;
  revision: number;
  reason: string | null;
  updatedBy: string | null;
  updatedAt: string | null;
}

export interface PackStateResponse {
  orgId: string;
  packs: PackStateItem[];
  /** Null when the policy could not be read — never silently "unrestricted". */
  certificationPolicy?: PackCertificationPolicy | null;
  /**
   * Distinguishes the two reasons `certificationPolicy` is null: `'available'`
   * means it was read and imposes no restriction; `'unavailable'` means it could
   * not be read, so activation will be refused with a 503 until it can be.
   */
  certificationPolicyStatus?: 'available' | 'unavailable';
}

export async function fetchPackStates(): Promise<PackStateResponse> {
  return apiGet<PackStateResponse>('/api/packs/state');
}

/**
 * True when this org's certification policy could NOT be read.
 *
 * The activation gate fails closed while this display annotation fails soft, so
 * an unreadable policy means activation will be refused even though no row is
 * marked blocked. Checking `certificationPolicy` alone cannot tell you that: it
 * is null both for an unrestricted org and for an unreadable store.
 */
export function isCertificationPolicyIndeterminate(
  response: PackStateResponse | null | undefined,
): boolean {
  return response?.certificationPolicyStatus === 'unavailable';
}

/**
 * A pack's activation eligibility as a THREE-valued answer.
 *
 * `activationBlocked` is absent when the policy could not be read, so a caller
 * doing `if (pack.activationBlocked)` silently treats "we do not know" as
 * "permitted" — which is how every pack came to render as activatable while
 * activation was returning 503. Prefer this helper over the raw field.
 */
export function activationEligibility(
  pack: PackStateItem | null | undefined,
): 'blocked' | 'permitted' | 'unknown' {
  if (!pack) return 'unknown';
  if (pack.activationPolicyStatus === 'unavailable') return 'unknown';
  if (pack.activationBlocked === true) return 'blocked';
  if (pack.activationBlocked === false) return 'permitted';
  return 'unknown';
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

/**
 * `{packId: notice}` for the DEPRECATED packs only (2.0-C4 T2 / AT-843).
 *
 * Packs that are not deprecated are absent rather than mapped to null, so a
 * lookup is falsy for them and a caller cannot accidentally render an empty notice.
 */
export function deprecationsByPackId(
  response: PackStateResponse | null | undefined,
): Record<string, PackDeprecationNotice> {
  const out: Record<string, PackDeprecationNotice> = {};
  for (const pack of response?.packs ?? []) {
    if (pack.deprecation) out[pack.packId] = pack.deprecation;
  }
  return out;
}
