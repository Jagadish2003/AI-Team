import React from 'react';
import { useCallback, useEffect, useState } from 'react';
import {
  applyPackMigration,
  previewPackMigration,
  revertPackMigration,
} from '../../api/packMigrationApi';
import type {
  PackMigrationChange,
  PackMigrationPlan,
  PackMigrationRecord,
} from '../../types/packMigration';

/**
 * PackMigrationAssist — 2.0-C4 T3 (AT-844 / AC2).
 *
 * The path out of a deprecated pack, rendered beside the notice that announced it.
 * It walks the three steps AC2 names, in order and visibly:
 *
 *   1. **Preview** — the exact fields that would change, with both values. Loaded on
 *      mount, because a customer reading a deprecation notice is already asking
 *      "what does this mean for my configuration?", and making them click to find
 *      out invites them to skip it.
 *   2. **Apply on confirmation** — one Owner action, and the plan's `fingerprint`
 *      goes back with it, so the thing applied is provably the thing displayed.
 *   3. **Revert** — offered immediately after applying, and never hidden behind a
 *      re-preview. A migration you cannot see how to undo is one people hesitate to
 *      make.
 *
 * Amber, never red, for the same reason the notice is (see `PackDeprecationNotice`):
 * a pack in grace still works, and this is an offered path, not a fault.
 *
 * Renders NOTHING when there is no migration to offer — no replacement declared, or
 * this org's configuration never selected the pack. The notice already states "no
 * replacement pack has been named"; a permanently disabled Migrate button beside it
 * would add nothing but noise.
 */
function errorMessage(error: unknown, fallback: string): string {
  const e = error as { status?: number; body?: unknown; message?: string };
  if (e?.status === 403) {
    return 'Only a workspace owner can change the run configuration.';
  }
  const body = e?.body as { detail?: unknown } | undefined;
  if (typeof body?.detail === 'string' && body.detail) return body.detail;
  return e?.message || fallback;
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—';
  return String(value);
}

function ChangeRow({ change }: { change: PackMigrationChange }) {
  return (
    <li
      data-testid={`pack-migration-change-${change.field}`}
      className="flex flex-wrap items-baseline gap-1.5"
    >
      <code className="rounded bg-black/5 px-1 py-0.5 text-[10px] dark:bg-white/10">
        {change.field}
      </code>
      <span data-testid={`pack-migration-change-${change.field}-from`}>
        {renderValue(change.previousValue)}
      </span>
      <span aria-hidden="true">→</span>
      <span
        className="font-medium"
        data-testid={`pack-migration-change-${change.field}-to`}
      >
        {renderValue(change.newValue)}
      </span>
    </li>
  );
}

export default function PackMigrationAssist({
  packId,
  onMigrated,
  testId,
}: {
  packId: string;
  /** Called after an apply or a revert lands, so the page can refresh its config. */
  onMigrated?: () => void;
  testId?: string;
}) {
  const [plan, setPlan] = useState<PackMigrationPlan | null>(null);
  const [applied, setApplied] = useState<PackMigrationRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadPlan = useCallback(async () => {
    try {
      setPlan(await previewPackMigration(packId));
    } catch {
      // Fail-soft, in the same direction as every other deprecation surface: no
      // migration offered, never a half-rendered one. The notice itself is
      // unaffected — it comes from a different call.
      setPlan(null);
    }
  }, [packId]);

  useEffect(() => {
    let cancelled = false;
    setApplied(null);
    setError(null);
    previewPackMigration(packId)
      .then(result => {
        if (!cancelled) setPlan(result);
      })
      .catch(() => {
        if (!cancelled) setPlan(null);
      });
    return () => {
      cancelled = true;
    };
  }, [packId]);

  const handleApply = async () => {
    if (!plan) return;
    setBusy(true);
    setError(null);
    try {
      const record = await applyPackMigration(packId, {
        fingerprint: plan.fingerprint,
      });
      setApplied(record);
      await loadPlan();
      onMigrated?.();
    } catch (err) {
      setError(errorMessage(err, 'The migration could not be applied.'));
      // The plan is re-read on failure: a 409 means it moved, and leaving the stale
      // one on screen would let the user retry the same rejected change set.
      await loadPlan();
    } finally {
      setBusy(false);
    }
  };

  const handleRevert = async () => {
    if (!applied) return;
    setBusy(true);
    setError(null);
    try {
      await revertPackMigration(applied.id);
      setApplied(null);
      await loadPlan();
      onMigrated?.();
    } catch (err) {
      setError(errorMessage(err, 'The migration could not be reverted.'));
    } finally {
      setBusy(false);
    }
  };

  if (!plan || !plan.available) return null;
  if (!plan.applicable && !applied) return null;

  return (
    <div
      data-testid={testId ?? 'pack-migration-assist'}
      data-pack-id={packId}
      data-state={applied ? 'applied' : 'preview'}
      className="mt-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs leading-relaxed text-amber-700 dark:text-amber-300"
    >
      <p className="font-medium">
        Migrate this configuration to{' '}
        {plan.replacementPackName || plan.replacementPackId}
      </p>

      {applied ? (
        <>
          <p data-testid="pack-migration-applied" className="mt-1">
            Migrated {applied.changes.length} configuration field
            {applied.changes.length === 1 ? '' : 's'} to{' '}
            {plan.replacementPackId}. Future runs use the replacement pack; existing
            runs and findings are unchanged.
          </p>
          <button
            type="button"
            data-testid="pack-migration-revert"
            disabled={busy}
            onClick={handleRevert}
            className="mt-2 rounded border border-amber-500/50 px-2 py-1 text-[11px] font-medium hover:bg-amber-500/10 disabled:opacity-50"
          >
            Undo migration
          </button>
        </>
      ) : (
        <>
          <p className="mt-1 opacity-90">
            These fields in your saved run configuration would change:
          </p>
          <ul data-testid="pack-migration-changes" className="mt-1 space-y-0.5">
            {plan.changes.map(change => (
              <ChangeRow key={change.field} change={change} />
            ))}
          </ul>

          {plan.warnings.map(warning => (
            <p
              key={warning.code}
              data-testid={`pack-migration-warning-${warning.code}`}
              className="mt-1 opacity-90"
            >
              {warning.detail}
            </p>
          ))}

          {/* Named, never silent: a customer told "2 changes applied" who is not told
              a template still points at the old pack has a false picture. */}
          {plan.unmapped.map(item => (
            <p
              key={`${item.field}:${item.value}`}
              data-testid={`pack-migration-unmapped-${item.value}`}
              className="mt-1 opacity-90"
            >
              {item.detail}
            </p>
          ))}

          <button
            type="button"
            data-testid="pack-migration-apply"
            disabled={busy}
            onClick={handleApply}
            className="mt-2 rounded border border-amber-500/50 px-2 py-1 text-[11px] font-medium hover:bg-amber-500/10 disabled:opacity-50"
          >
            Apply migration
          </button>
        </>
      )}

      {error && (
        <p
          data-testid="pack-migration-error"
          role="alert"
          className="mt-1.5 text-rose-600 dark:text-rose-400"
        >
          {error}
        </p>
      )}
    </div>
  );
}

export { PackMigrationAssist };
