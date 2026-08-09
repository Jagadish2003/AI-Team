import React, { useMemo, useState } from 'react';
import {
  AlertCircle,
  BrainCircuit,
  CheckCircle2,
  History,
  Loader2,
  RotateCcw,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import {
  fetchLearningAdjustmentHistory,
  fetchLearningAdjustmentState,
  resetLearningAdjustment,
} from '../../api/learningApi';
import { useAuthOptional } from '../../context/AuthContext';
import { cacheKeys } from '../../lib/cacheKeys';
import { useDataCache, useResource } from '../../lib/dataCache';
import type {
  LearningAdjustmentGroup,
  LearningAdjustmentHistoryEntry,
} from '../../types/learning';
import ConfirmDialog from '../common/ConfirmDialog';

function readableLabel(value?: string | null): string {
  if (!value) return 'General ranking';
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function groupLabel(group: Pick<LearningAdjustmentGroup, 'signalConcept' | 'detectorId' | 'packId'>): string {
  return readableLabel(group.signalConcept ?? group.detectorId ?? group.packId);
}

function formatDate(value?: string | null): string {
  if (!value) return 'Date unavailable';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Date unavailable';
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}

function plural(value: number, singular: string, pluralForm = `${singular}s`): string {
  return `${value} ${value === 1 ? singular : pluralForm}`;
}

function adjustmentDescription(group: LearningAdjustmentGroup): string {
  if (!group.learningActive || Math.abs(group.netWeight) < Number.EPSILON) {
    return 'Neutral — base ordering is used';
  }
  return group.netWeight > 0
    ? 'May rank similar opportunities higher'
    : 'May rank similar opportunities lower';
}

function historyTitle(entry: LearningAdjustmentHistoryEntry): string {
  switch (entry.changeKind) {
    case 'activated':
      return 'Learning activated';
    case 'deactivated':
      return 'Learning returned to neutral';
    case 'reset':
      return 'Owner reset to neutral';
    default:
      return 'Learning updated';
  }
}

function historyDescription(entry: LearningAdjustmentHistoryEntry): string {
  if (entry.changeKind === 'reset' || !entry.learningActive || Math.abs(entry.netWeight) < Number.EPSILON) {
    return 'Base ordering restored for this learning area.';
  }
  return entry.netWeight > 0
    ? 'Similar opportunities may be ranked higher.'
    : 'Similar opportunities may be ranked lower.';
}

export default function LearningGovernancePanel() {
  const auth = useAuthOptional();
  const isOwner = auth?.user?.role === 'owner';
  const cache = useDataCache();
  const stateResource = useResource(
    isOwner ? cacheKeys.learningAdjustmentState : null,
    fetchLearningAdjustmentState,
  );
  const historyResource = useResource(
    isOwner ? cacheKeys.learningAdjustmentHistory : null,
    () => fetchLearningAdjustmentHistory(1000),
  );
  const [resetOpen, setResetOpen] = useState(false);
  const [resetReason, setResetReason] = useState('');
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState<string | null>(null);
  const [resetSuccess, setResetSuccess] = useState<string | null>(null);

  const state = stateResource.data;
  const history = historyResource.data ?? [];
  const adjustedGroupCount = useMemo(
    () => state?.groups.filter((group) => group.learningActive && Math.abs(group.netWeight) > Number.EPSILON).length ?? 0,
    [state?.groups],
  );
  const baseOrderActive = !state?.enabled || adjustedGroupCount === 0;

  if (!isOwner) return null;

  function closeReset() {
    if (resetting) return;
    setResetOpen(false);
    setResetReason('');
    setResetError(null);
  }

  async function handleReset() {
    const reason = resetReason.trim();
    if (!reason) {
      setResetError('Enter a reason for the audit history.');
      return;
    }

    setResetting(true);
    setResetError(null);
    setResetSuccess(null);
    try {
      const result = await resetLearningAdjustment(reason);
      setResetOpen(false);
      setResetReason('');
      setResetSuccess(
        result.opportunitiesAffected > 0
          ? `Learning reset to neutral. Base order restored for ${plural(result.opportunitiesAffected, 'opportunity', 'opportunities')}.`
          : 'Learning reset to neutral. Rankings now use their base order.',
      );
      cache.invalidate(cacheKeys.learningScope);
      cache.invalidate(cacheKeys.runs);
    } catch {
      setResetError('Could not reset ranking learning. Please try again.');
    } finally {
      setResetting(false);
    }
  }

  const initialLoading = (stateResource.loading && !state) || (historyResource.loading && !historyResource.data);
  const loadError = stateResource.error ?? historyResource.error;

  return (
    <section
      data-testid="learning-governance-panel"
      className="overflow-hidden rounded-xl border border-border/50 bg-panel shadow-sm"
    >
      <div className="flex flex-col gap-4 border-b border-border/50 px-4 py-4 md:flex-row md:items-center md:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border/60 bg-bg/30 text-accent">
            <BrainCircuit size={18} aria-hidden />
          </span>
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-text">Ranking learning</h2>
            <p className="mt-1 text-xs leading-relaxed text-muted">
              Inspect how workspace feedback affects ordering, review its audit history, or return rankings to neutral.
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => {
            setResetSuccess(null);
            setResetError(null);
            setResetOpen(true);
          }}
          disabled={initialLoading || Boolean(loadError)}
          className="inline-flex shrink-0 items-center justify-center gap-2 rounded-lg border border-red-500/35 bg-red-500/10 px-4 py-2 text-sm font-semibold text-red-600 transition-colors hover:bg-red-500/15 disabled:cursor-not-allowed disabled:opacity-50 dark:text-red-300"
        >
          <RotateCcw size={15} aria-hidden />
          Reset learning
        </button>
      </div>

      {initialLoading ? (
        <div className="flex items-center gap-2 px-4 py-8 text-sm text-muted">
          <Loader2 size={16} className="animate-spin text-accent" aria-hidden />
          Loading ranking governance…
        </div>
      ) : loadError ? (
        <div className="m-4 flex flex-col items-start gap-3 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-700 dark:text-red-300">
          <div className="flex items-start gap-2">
            <AlertCircle size={16} className="mt-0.5 shrink-0" aria-hidden />
            <span>Could not load ranking learning details.</span>
          </div>
          <button
            type="button"
            onClick={() => {
              void stateResource.refetch();
              void historyResource.refetch();
            }}
            className="rounded-md border border-red-500/30 px-3 py-1.5 text-xs font-semibold hover:bg-red-500/10"
          >
            Try again
          </button>
        </div>
      ) : state ? (
        <div className="space-y-5 p-4">
          {resetSuccess && (
            <div role="status" className="flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">
              <CheckCircle2 size={16} className="mt-0.5 shrink-0" aria-hidden />
              <span>{resetSuccess}</span>
            </div>
          )}

          <div className="grid gap-3 md:grid-cols-3">
            <div className="rounded-lg border border-border bg-bg/25 px-4 py-3">
              <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
                {baseOrderActive ? <CheckCircle2 size={14} aria-hidden /> : <Sparkles size={14} aria-hidden />}
                Current state
              </div>
              <div className="mt-2 text-sm font-semibold text-text">
                {baseOrderActive ? 'Base order is active' : 'Bounded learning is active'}
              </div>
              <p className="mt-1 text-xs leading-relaxed text-muted">
                {baseOrderActive
                  ? 'No learned adjustment is changing opportunity order.'
                  : `${plural(adjustedGroupCount, 'learning area')} can currently affect ordering.`}
              </p>
            </div>
            <div className="rounded-lg border border-border bg-bg/25 px-4 py-3">
              <div className="text-xs font-semibold uppercase tracking-wide text-muted">Rank movement limit</div>
              <div className="mt-2 text-sm font-semibold text-text">
                At most {plural(state.caps.maxRankMove, 'position')}
              </div>
              <p className="mt-1 text-xs leading-relaxed text-muted">Learning cannot move an opportunity beyond this limit.</p>
            </div>
            <div className="rounded-lg border border-border bg-bg/25 px-4 py-3">
              <div className="text-xs font-semibold uppercase tracking-wide text-muted">Maximum influence</div>
              <div className="mt-2 text-sm font-semibold text-text">
                Up to {Math.round(state.caps.maxScoreFraction * 100)}% of base impact
              </div>
              <p className="mt-1 text-xs leading-relaxed text-muted">Base scoring remains unchanged and recoverable.</p>
            </div>
          </div>

          <div>
            <div className="mb-2 flex items-center justify-between gap-3">
              <h3 className="flex items-center gap-2 text-sm font-semibold text-text">
                <ShieldCheck size={15} className="text-accent" aria-hidden />
                Current adjustments
              </h3>
              <span className="text-xs text-muted">{plural(state.groups.length, 'learning area')}</span>
            </div>
            {state.groups.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted">
                No ranking adjustments have been recorded. Opportunities use their base order.
              </div>
            ) : (
              <div className="grid gap-2 md:grid-cols-2">
                {state.groups.map((group, index) => (
                  <div
                    key={`${group.detectorId ?? 'general'}-${group.packId ?? 'all'}-${index}`}
                    className="rounded-lg border border-border bg-bg/20 px-3 py-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-semibold text-text">{groupLabel(group)}</div>
                        <p className="mt-1 text-xs text-muted">{adjustmentDescription(group)}</p>
                      </div>
                      <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase ${
                        group.learningActive && Math.abs(group.netWeight) > Number.EPSILON
                          ? 'border-accent/30 bg-accent/10 text-accent'
                          : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                      }`}>
                        {group.learningActive && Math.abs(group.netWeight) > Number.EPSILON ? 'Adjusted' : 'Neutral'}
                      </span>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
                      <span>{plural(group.signalCount, 'contributing signal')}</span>
                      <span>{group.hasOutcomeEvidence ? 'Includes measured outcomes' : 'Decision feedback only'}</span>
                      <span>Updated {formatDate(group.updatedAt ?? group.computedAt)}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div>
            <div className="mb-2 flex items-center justify-between gap-3">
              <h3 className="flex items-center gap-2 text-sm font-semibold text-text">
                <History size={15} className="text-accent" aria-hidden />
                Complete adjustment history
              </h3>
              <span className="text-xs text-muted">{plural(history.length, 'recorded change')}</span>
            </div>
            {history.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted">
                No adjustment history has been recorded yet.
              </div>
            ) : (
              <div data-testid="learning-adjustment-history" className="max-h-[420px] divide-y divide-border overflow-y-auto rounded-lg border border-border [scrollbar-gutter:stable]">
                {history.map((entry) => (
                  <div key={entry.historyId} className="bg-bg/15 px-3 py-3">
                    <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
                      <div className="min-w-0">
                        <div className="text-sm font-semibold text-text">{historyTitle(entry)}</div>
                        <div className="mt-0.5 text-xs text-muted">
                          {readableLabel(entry.detectorId ?? entry.packId)} · {historyDescription(entry)}
                        </div>
                        {entry.resetReason && (
                          <div className="mt-1 text-xs text-text">Reason: {entry.resetReason}</div>
                        )}
                      </div>
                      <div className="shrink-0 text-left text-[11px] leading-relaxed text-muted sm:text-right">
                        <div>{formatDate(entry.recordedAt)}</div>
                        <div>By {entry.actorId}</div>
                        <div>Revision {entry.revision}</div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : null}

      <ConfirmDialog
        open={resetOpen}
        title="Reset ranking learning?"
        confirmLabel="Reset to neutral"
        busyLabel="Resetting…"
        busy={resetting}
        onCancel={closeReset}
        onConfirm={() => void handleReset()}
        message={
          <div className="space-y-3">
            <p>
              This returns opportunity rankings to their base order. Evidence, confidence, outcomes, and the complete adjustment history remain unchanged.
            </p>
            <div>
              <label htmlFor="learning-reset-reason" className="mb-1 block text-xs font-semibold text-text">
                Reason <span className="text-red-500">*</span>
              </label>
              <textarea
                id="learning-reset-reason"
                value={resetReason}
                onChange={(event) => {
                  setResetReason(event.target.value);
                  if (resetError) setResetError(null);
                }}
                rows={3}
                maxLength={500}
                autoFocus
                placeholder="Why should ranking learning be reset?"
                className="w-full resize-none rounded-lg border border-border bg-bg px-3 py-2 text-sm text-text outline-none transition-colors placeholder:text-muted/70 focus:border-accent/50 focus:ring-1 focus:ring-accent/30"
              />
              <div className="mt-1 flex justify-between gap-3 text-[11px] text-muted">
                <span>This reason is saved in the audit history.</span>
                <span>{resetReason.length}/500</span>
              </div>
              {resetError && (
                <p role="alert" className="mt-2 text-xs text-red-600 dark:text-red-300">{resetError}</p>
              )}
            </div>
          </div>
        }
      />
    </section>
  );
}
