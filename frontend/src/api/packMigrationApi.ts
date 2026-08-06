import { apiGet, apiPost } from '../lib/apiClient';
import type {
  PackMigrationPlan,
  PackMigrationRecord,
} from '../types/packMigration';

/**
 * packMigrationApi — 2.0-C4 T3 (AT-844) org-config migration off a deprecated pack.
 *
 * Three calls in the order the parent story's AC2 names them: preview the change,
 * apply it on confirmation, revert it. Preview is analyst+ (it quotes the org's saved
 * configuration back); apply and revert are Owner, because they rewrite what every
 * future run for the whole organisation is built from.
 */

/** What the migration WOULD change. Writes nothing. */
export async function previewPackMigration(
  packId: string,
): Promise<PackMigrationPlan> {
  return apiGet<PackMigrationPlan>(
    `/api/packs/${encodeURIComponent(packId)}/migration/preview`,
  );
}

/**
 * Apply a previewed migration.
 *
 * Always pass the `fingerprint` from the plan the user actually saw — without it the
 * backend cannot tell that the configuration moved between preview and confirmation,
 * and "previewed before applying" becomes a convention instead of a guarantee.
 */
export async function applyPackMigration(
  packId: string,
  options: { fingerprint?: string; reason?: string } = {},
): Promise<PackMigrationRecord> {
  return apiPost<PackMigrationRecord>(
    `/api/packs/${encodeURIComponent(packId)}/migration/apply`,
    { confirm: true, ...options },
  );
}

/**
 * Undo an applied migration, restoring the configuration it replaced.
 *
 * `force` is only for a caller that has already been shown the 409 explaining that
 * the configuration has been edited since — reverting then discards that edit.
 */
export async function revertPackMigration(
  migrationId: string,
  options: { force?: boolean; reason?: string } = {},
): Promise<PackMigrationRecord> {
  return apiPost<PackMigrationRecord>(
    `/api/packs/migrations/${encodeURIComponent(migrationId)}/revert`,
    options,
  );
}

/** This org's append-only migration ledger, newest first. */
export async function fetchPackMigrations(): Promise<{
  orgId: string;
  migrations: PackMigrationRecord[];
}> {
  return apiGet('/api/packs/migrations');
}
