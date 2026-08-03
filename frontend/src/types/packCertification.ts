/**
 * packCertification.ts — 2.0-C2 pack certification levels (AT-831 / AT-833).
 *
 * Three levels, ordered. `level` is always the EFFECTIVE one: a pack claiming
 * Certified whose signature does not verify is reported as `community`, with
 * `declaredLevel` preserving what it asked for. The UI must therefore render
 * `level` and never `declaredLevel` as the badge — that is 2.0-C2 AC1 carried
 * through to the surfaces.
 */
export type PackCertificationLevel = 'certified' | 'partner' | 'community';

export interface PackCertification {
  packId: string;
  /** The effective, signature-verified level. Render THIS. */
  level: PackCertificationLevel;
  /** Display label for `level` — served by the backend so wording cannot drift. */
  label: string;
  /** `label`, plus a qualifier when the claim was downgraded or review is due. */
  statusLabel?: string;
  /** What the pack CLAIMED. Differs from `level` when it could not be verified. */
  declaredLevel?: PackCertificationLevel;
  /** The badge is valid but was reviewed against an older platform version. */
  reviewDue?: boolean;
}
