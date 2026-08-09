import React, { useMemo, useState } from 'react';
import {
  Activity,
  CalendarDays,
  RefreshCw,
  RotateCcw,
  XCircle,
} from 'lucide-react';
import {
  dismissOpportunity,
  fetchOpportunityLifecycle,
  recordOpportunityAction,
  reopenOpportunity,
} from '../../api/outcomeApi';
import ConfirmDialog from '../common/ConfirmDialog';
import { useToast } from '../common/Toast';
import { useAuthOptional } from '../../context/AuthContext';
import { ApiError } from '../../lib/apiClient';
import { cacheKeys } from '../../lib/cacheKeys';
import { useDataCache, useResource } from '../../lib/dataCache';
import type { OpportunityLifecycle } from '../../types/outcome';

type PendingMutation = 'action' | 'dismiss' | 'reopen' | null;
type ConfirmMutation = Exclude<PendingMutation, 'action' | null>;

const STATE_LABELS: Record<string, string> = {
  open: 'Open',
  actioned: 'Action recorded',
  monitoring: 'Monitoring',
  measured: 'Measured',
  stalled: 'Monitoring needs attention',
  dismissed: 'Dismissed',
};

const STATE_DESCRIPTIONS: Record<string, string> = {
  open: 'Record when the customer deploys an agent or another operational change.',
  actioned: 'The action date is recorded. A later discovery run starts monitoring.',
  monitoring: 'Post-action signals are being compared with the frozen baseline.',
  measured: 'At least one post-action movement comparison is available.',
  stalled: 'A comparable post-action measurement could not be produced yet.',
  dismissed: 'This opportunity is no longer being actively tracked.',
};

function localToday(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

function formatDate(value?: string | null): string {
  if (!value) return 'Not recorded';
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(value);
  const parsed = new Date(dateOnly ? `${value}T00:00:00` : value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  });
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const body = error.body as { detail?: unknown } | null;
    if (typeof body?.detail === 'string' && body.detail.trim()) return body.detail;
  }
  return error instanceof Error && error.message
    ? error.message
    : 'The lifecycle could not be updated.';
}

function stateTone(state: string): string {
  if (state === 'measured') {
    return 'border-emerald-500/35 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300';
  }
  if (state === 'stalled') {
    return 'border-amber-500/35 bg-amber-500/10 text-amber-700 dark:text-amber-300';
  }
  if (state === 'dismissed') {
    return 'border-border bg-bg/50 text-muted';
  }
  return 'border-accent/30 bg-accent/10 text-accent';
}

export default function OpportunityLifecyclePanel({
  opportunityIdentity,
}: {
  opportunityIdentity?: string | null;
}) {
  const auth = useAuthOptional();
  const role = auth?.user?.role;
  const canManage = auth === undefined || role === 'owner' || role === 'analyst';
  const enabled = Boolean(opportunityIdentity) && canManage;
  const key = enabled
    ? cacheKeys.opportunityLifecycle(opportunityIdentity as string)
    : null;
  const { data, loading, error, refetch } = useResource<OpportunityLifecycle>(
    key,
    () => fetchOpportunityLifecycle(opportunityIdentity as string),
    { enabled },
  );
  const cache = useDataCache();
  const { push } = useToast();
  const [actionDate, setActionDate] = useState('');
  const [pending, setPending] = useState<PendingMutation>(null);
  const [confirmMutation, setConfirmMutation] = useState<ConfirmMutation | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);

  const legalNextStates = useMemo(
    () => new Set(data?.legalNextStates ?? []),
    [data?.legalNextStates],
  );
  const state = data?.state ?? 'open';

  if (!opportunityIdentity) return null;

  async function applyMutation(kind: Exclude<PendingMutation, null>) {
    if (!opportunityIdentity || pending) return;
    if (kind === 'action' && !actionDate) {
      setMutationError('Choose the date the action or change was deployed.');
      return;
    }

    setPending(kind);
    setMutationError(null);
    try {
      const updated =
        kind === 'action'
          ? await recordOpportunityAction(opportunityIdentity, actionDate)
          : kind === 'dismiss'
            ? await dismissOpportunity(opportunityIdentity)
            : await reopenOpportunity(opportunityIdentity);

      cache.setData(cacheKeys.opportunityLifecycle(opportunityIdentity), updated);
      cache.invalidateExact(cacheKeys.opportunityOutcome(opportunityIdentity));
      cache.invalidateExact(cacheKeys.outcomePortfolio);
      setConfirmMutation(null);
      if (kind !== 'action') setActionDate('');
      push(
        kind === 'action'
          ? 'Action/change recorded. Monitoring will begin when a later run lands.'
          : kind === 'dismiss'
            ? 'Opportunity dismissed.'
            : 'Opportunity reopened.',
        'success',
      );
    } catch (caught) {
      const message = errorMessage(caught);
      setMutationError(message);
      push(message, 'error');
    } finally {
      setPending(null);
    }
  }

  if (!canManage) {
    return (
      <section className="mt-4 rounded-xl border border-border bg-panel px-5 py-4">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
          <Activity className="h-4 w-4 text-accent" aria-hidden />
          Opportunity Lifecycle
        </div>
        <p className="mt-2 text-sm text-muted">
          Lifecycle details and controls are available to Analysts and Owners.
        </p>
      </section>
    );
  }

  return (
    <section className="mt-4 rounded-xl border border-border bg-panel" data-testid="opportunity-lifecycle-panel">
      <div className="flex flex-wrap items-start justify-between gap-4 px-5 py-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
            <Activity className="h-4 w-4 text-accent" aria-hidden />
            Opportunity Lifecycle
          </div>
          <p className="mt-1 text-sm text-muted">
            {loading && !data
              ? 'Loading lifecycle status...'
              : data
                ? STATE_DESCRIPTIONS[state] ?? 'The current tracking state for this opportunity.'
                : 'Lifecycle status is unavailable.'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {data ? (
            <span
              data-testid="opportunity-lifecycle-state"
              className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${stateTone(state)}`}
            >
              {STATE_LABELS[state] ?? state}
            </span>
          ) : null}
          <button
            type="button"
            onClick={() => void refetch()}
            disabled={loading || Boolean(pending)}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-border bg-bg/30 text-muted transition hover:bg-panel2 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
            aria-label="Refresh lifecycle status"
            title="Refresh lifecycle status"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} aria-hidden />
          </button>
        </div>
      </div>

      {error ? (
        <div className="border-t border-border px-5 py-4 text-sm text-danger">
          Lifecycle status could not be loaded for this opportunity.
        </div>
      ) : null}

      {data ? (
        <div className="border-t border-border px-5 py-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="rounded-lg border border-border bg-bg/30 px-3 py-2">
              <div className="text-xs font-semibold uppercase tracking-wide text-muted">Current state</div>
              <div className="mt-1 text-sm font-semibold text-text">{STATE_LABELS[state] ?? state}</div>
            </div>
            <div className="rounded-lg border border-border bg-bg/30 px-3 py-2">
              <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted">
                <CalendarDays className="h-3.5 w-3.5" aria-hidden />
                Recorded action date
              </div>
              <div className="mt-1 text-sm font-semibold text-text" data-testid="opportunity-action-date">
                {formatDate(data.actionDate)}
              </div>
            </div>
          </div>

          {state === 'open' && legalNextStates.has('actioned') ? (
            <form
              className="mt-4 rounded-lg border border-accent/20 bg-accent/5 p-3"
              onSubmit={(event) => {
                event.preventDefault();
                void applyMutation('action');
              }}
            >
              <label htmlFor={`action-date-${opportunityIdentity}`} className="text-sm font-semibold text-text">
                Action/deployment date
              </label>
              <p className="mt-1 text-xs leading-relaxed text-muted">
                This date creates the before-and-after boundary used for monitoring.
              </p>
              <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-end">
                <input
                  id={`action-date-${opportunityIdentity}`}
                  type="date"
                  required
                  max={localToday()}
                  value={actionDate}
                  onChange={(event) => setActionDate(event.target.value)}
                  disabled={Boolean(pending)}
                  className="h-10 rounded-lg border border-border bg-panel px-3 text-sm text-text focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:opacity-60"
                />
                <button
                  type="submit"
                  disabled={!actionDate || Boolean(pending)}
                  className="inline-flex h-10 items-center justify-center rounded-lg border border-accent/30 bg-accent px-4 text-sm font-semibold text-white transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {pending === 'action' ? 'Recording...' : 'Record action/change'}
                </button>
              </div>
            </form>
          ) : null}

          <div className="mt-4 flex flex-wrap gap-2">
            {legalNextStates.has('dismissed') ? (
              <button
                type="button"
                onClick={() => setConfirmMutation('dismiss')}
                disabled={Boolean(pending)}
                className="inline-flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm font-semibold text-red-600 transition hover:bg-red-500/10 disabled:opacity-50 dark:text-red-300"
              >
                <XCircle className="h-4 w-4" aria-hidden />
                Dismiss
              </button>
            ) : null}
            {state !== 'open' && legalNextStates.has('open') ? (
              <button
                type="button"
                onClick={() => setConfirmMutation('reopen')}
                disabled={Boolean(pending)}
                className="inline-flex items-center gap-2 rounded-lg border border-border bg-bg/30 px-3 py-2 text-sm font-semibold text-text transition hover:bg-panel2 disabled:opacity-50"
              >
                <RotateCcw className="h-4 w-4" aria-hidden />
                Reopen
              </button>
            ) : null}
          </div>

          {mutationError ? (
            <p className="mt-3 text-sm text-danger" role="alert">
              {mutationError}
            </p>
          ) : null}
        </div>
      ) : null}

      <ConfirmDialog
        open={confirmMutation !== null}
        title={confirmMutation === 'dismiss' ? 'Dismiss opportunity' : 'Reopen opportunity'}
        message={
          confirmMutation === 'dismiss'
            ? 'Dismiss this opportunity from active tracking? Its lifecycle history will be retained.'
            : data?.actionDate
              ? 'Reopening clears the recorded action date and removes its measurements from active outcome views. The history remains auditable.'
              : 'Reopen this opportunity for review? Its lifecycle history will be retained.'
        }
        confirmLabel={confirmMutation === 'dismiss' ? 'Dismiss' : 'Reopen'}
        busyLabel={confirmMutation === 'dismiss' ? 'Dismissing...' : 'Reopening...'}
        danger={confirmMutation === 'dismiss'}
        busy={pending === confirmMutation}
        onConfirm={() => {
          if (confirmMutation) void applyMutation(confirmMutation);
        }}
        onCancel={() => {
          if (!pending) setConfirmMutation(null);
        }}
      />
    </section>
  );
}
