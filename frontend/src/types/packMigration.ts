/**
 * packMigration.ts — 2.0-C4 T3 (AT-844) org-config pack migration.
 *
 * A deprecated pack that declares a replacement offers a *path*: rewrite this org's
 * saved run configuration so its pack and template selections point at the
 * replacement. The shape below is the preview of that rewrite, and the ledger row it
 * produces when applied.
 *
 * Two words carry the story's AC2:
 *
 *   - `available`  — a migration exists at all (the pack is deprecated AND names a
 *                    registered replacement). False is an ANSWER, with a `reason`
 *                    the surface shows, not an error;
 *   - `applicable` — and there is actually something in this org's configuration to
 *                    change. An `available` migration with nothing to change is the
 *                    normal state for an org that never selected the pack.
 */
import type { PackDeprecationNotice } from './packDeprecation';

/** The saved Stack Builder setup state — the only surface a migration rewrites. */
export type PackMigrationSurface = 'stack_builder_setup_state';

/**
 * One field rewrite, carrying BOTH values.
 *
 * The previous value is what makes the migration reversible: reverting restores it
 * verbatim rather than mapping the replacement back to the deprecated pack, which
 * would also drag back selections that legitimately pointed at the replacement.
 */
export interface PackMigrationChange {
  surface: PackMigrationSurface | string;
  /** The setup-state field, e.g. `packIds`. */
  field: string;
  previousValue: unknown;
  newValue: unknown;
  description: string;
}

/** A reference the migration deliberately did NOT rewrite, and why. */
export interface PackMigrationUnmapped {
  surface: string;
  field: string;
  value: string;
  /** `no_replacement_template` | `ambiguous_replacement_template`. */
  reason: string;
  detail: string;
}

/** Something true about the migration the customer should know before applying. */
export interface PackMigrationWarning {
  code: string;
  detail: string;
}

export interface PackMigrationPlan {
  orgId: string;
  packId: string;
  packName: string;
  replacementPackId: string;
  replacementPackName: string;
  available: boolean;
  applicable: boolean;
  /** Human sentence explaining why no migration is available. Empty when one is. */
  reason: string;
  /**
   * The same thing as a code — `not_deprecated` | `no_replacement_declared` — so a
   * surface branches on the code and displays the sentence, rather than matching
   * on prose. Empty when a migration IS available.
   */
  reasonCode: string;
  changes: PackMigrationChange[];
  unmapped: PackMigrationUnmapped[];
  warnings: PackMigrationWarning[];
  /** The same notice the pack picker shows, so the two cannot word it differently. */
  deprecation: PackDeprecationNotice | null;
  evaluatedOn: string;
  /**
   * Digest of this exact change set. Posting it back on apply makes "previewed
   * before applying" an enforced property: if the configuration moved in between,
   * the apply is refused (409) instead of applying a change set nobody saw.
   */
  fingerprint: string;
}

export interface PackMigrationRecord {
  id: string;
  kind: 'apply' | 'revert';
  orgId: string;
  packId: string;
  replacementPackId: string;
  changes: PackMigrationChange[];
  unmapped: PackMigrationUnmapped[];
  warnings: PackMigrationWarning[];
  reason: string | null;
  actorId: string;
  at: string;
  fingerprint: string;
  /** Set on a revert row, naming the apply it undoes. */
  revertsMigrationId: string | null;
  /** Derived on read: an apply a later revert has undone. */
  reverted: boolean;
  revertedAt: string | null;
  revertedBy: string | null;
  /** False for an apply that had nothing to change (a no-op, not an error). */
  changed: boolean;
}
