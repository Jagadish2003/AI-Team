import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  AlertCircle,
  BarChart2,
  ChevronLeft,
  ChevronRight,
  FileText,
  Link2,
  ListChecks,
  Map,
  Settings,
  Shield,
  Zap,
} from 'lucide-react';
import PageShell from '../components/common/PageShell';
import { Skeleton } from '../components/common/Skeleton';
import StagesGrid from '../components/pilot_roadmap/StagesGrid';
import { useConnectorContext } from '../context/ConnectorContext';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';
import { useDiscoveryRunContext } from '../context/DiscoveryRunContext';
import { useRunContext } from '../context/RunContext';
import { fetchBlueprint } from '../api/blueprintApi';
import { fetchEvidence } from '../api/runApi';
import { fetchRunRoadmap } from '../api/runScopedS9S10Api';
import type { BlueprintResponse } from '../utils/blueprintTypes';
import type { OpportunityCandidate } from '../types/analystReview';
import type { PilotRoadmapModel } from '../types/pilotRoadmap';
import type { EvidenceReview } from '../types/partialResults';
import { runScopedErrorMessage } from '../utils/apiErrors';
import {
  getBlueprintLabel,
  isSalesforceConnected,
} from '../utils/blueprintNaming';
import { useResource } from '../lib/dataCache';
import { cacheKeys } from '../lib/cacheKeys';
import {
  ProjectionAssumptionList,
  projectionAssumptions,
} from '../components/projection/ProjectionAssumptionLedger';
import { ProjectionBandCompact } from '../components/projection/ProjectionBand';
import { ProjectionBasisCompact } from '../components/projection/ProjectionBasis';

function TierBadge({ tier }: { tier?: string }) {
  const t = tier ?? 'Unknown';
  const cls =
    t === 'Quick Win'
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
      : t === 'Strategic'
        ? 'border-amber-500/40 bg-amber-500/10 text-amber-200'
        : 'border-red-500/40 bg-red-500/10 text-red-200';

  return <span className={`rounded-full border px-2 py-0.5 text-xs ${cls}`}>{t}</span>;
}

function DecisionBadge({ value }: { value?: string }) {
  const label = value === 'UNREVIEWED' || !value ? 'PENDING' : value;
  const cls =
    value === 'APPROVED'
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
      : value === 'REJECTED'
        ? 'border-red-500/40 bg-red-500/10 text-red-200'
        : 'border-amber-500/50 bg-amber-500/15 text-amber-300';

  return <span className={`rounded-full border px-2 py-0.5 text-xs ${cls}`}>{label}</span>;
}

function ConfidenceBadge({ level }: { level?: string }) {
  const normalizedLevel = (level ?? 'LOW').toUpperCase();
  const cls =
    normalizedLevel === 'HIGH'
      ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-300'
      : normalizedLevel === 'MEDIUM'
        ? 'border-amber-500/50 bg-amber-500/10 text-amber-300'
        : 'border-red-500/50 bg-red-500/10 text-red-300';

  return (
    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide ${cls}`}>
      {normalizedLevel}
    </span>
  );
}

function StatusPill({ connected }: { connected: boolean }) {
  return (
    <span
      className={`rounded-full border px-2.5 py-1 text-xs ${
        connected
          ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
          : 'border-amber-500/40 bg-amber-500/10 text-amber-200'
      }`}
    >
      {connected ? 'Salesforce connected' : 'Salesforce required'}
    </span>
  );
}

function WorkspaceNotice({
  icon,
  title,
  message,
  actionLabel,
  onAction,
  tone = 'neutral',
}: {
  icon?: React.ReactNode;
  title: string;
  message: string;
  actionLabel?: string;
  onAction?: () => void;
  tone?: 'neutral' | 'warning' | 'error';
}) {
  const iconCls =
    tone === 'error'
      ? 'border-red-500/30 bg-red-500/10 text-red-300'
      : tone === 'warning'
        ? 'border-amber-500/30 bg-amber-500/10 text-amber-300'
        : 'border-accent/20 bg-accent/10 text-accent';

  return (
    <div className="flex min-h-[280px] items-center justify-center rounded-xl border border-border bg-panel px-4 py-8 text-center">
      <div className="max-w-2xl">
        {icon && (
          <div className="mb-4 flex justify-center">
            <div className={`flex h-12 w-12 items-center justify-center rounded-full border ${iconCls}`}>
              {icon}
            </div>
          </div>
        )}
        <h2 className="text-xl font-semibold text-text">{title}</h2>
        <p className="mx-auto mt-3 max-w-xl text-sm leading-relaxed text-muted">{message}</p>
        {actionLabel && onAction && (
          <button
            onClick={onAction}
            className="mt-5 rounded-lg border border-accent/20 bg-accent/5 px-5 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
          >
            {actionLabel}
          </button>
        )}
      </div>
    </div>
  );
}

// Skeleton shaped like the blueprint detail (header + the design/guardrails/
// permissions/evidence blocks), so the real content fills the reserved space
// rather than snapping in after a spinner.
function LoadingState({ blueprintLabel }: { blueprintLabel: string }) {
  return (
    <div
      aria-busy="true"
      aria-label={`Loading ${blueprintLabel}`}
      className="rounded-xl border border-border bg-panel p-5"
    >
      <Skeleton className="h-5 w-64" />
      <Skeleton className="mt-2 h-3 w-96" />
      <div className="mt-5 grid gap-4 lg:grid-cols-2">
        <Skeleton className="h-56 w-full rounded-lg" />
        <Skeleton className="h-56 w-full rounded-lg" />
      </div>
      <Skeleton className="mt-4 h-40 w-full rounded-lg" />
    </div>
  );
}

function OpportunitySelectorPanel({
  opportunities,
  selectedId,
  onSelect,
}: {
  opportunities: OpportunityCandidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="flex h-full flex-col overflow-hidden rounded-xl border border-border bg-panel">
      <div className="flex items-center justify-between border-b border-border px-4 py-3.5">
        <div className="flex items-center gap-2">
          <ListChecks size={15} className="text-accent" />
          <div className="text-[13px] font-semibold uppercase tracking-wide text-text">Opportunities</div>
        </div>
        <div className="rounded-full border border-border bg-bg/30 px-2 py-0.5 text-[11px] font-medium text-muted">
          {opportunities.length}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {opportunities.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-muted">No opportunities available.</div>
        ) : (
          opportunities.map((opp) => {
            const active = selectedId === opp.id;
            return (
              <button
                key={opp.id}
                onClick={() => onSelect(opp.id)}
                className={`w-full border-b border-border px-4 py-3 text-left transition-colors ${
                  active ? 'border-l-2 border-l-accent bg-accent/10' : 'hover:bg-panel2'
                }`}
              >
                <div className={`text-sm font-semibold leading-snug ${active ? 'text-accent' : 'text-text'}`}>
                  {opp.title ?? 'Untitled opportunity'}
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <TierBadge tier={opp.tier} />
                  <DecisionBadge value={opp.decision} />
                </div>
                <div className="mt-2 flex items-center justify-between text-xs text-muted">
                  <span>{opp.category ?? 'Uncategorized'}</span>
                  <ChevronRight size={14} className={active ? 'text-accent' : 'text-muted'} />
                </div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}

function SectionBlock({
  icon,
  title,
  headerRight,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  headerRight?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-border bg-bg/20 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-accent/10 text-accent">
            {icon}
          </span>
          <span className="text-[13px] font-semibold uppercase tracking-wide text-text">{title}</span>
        </div>
        {headerRight}
      </div>
      {children}
    </section>
  );
}

function MetricTile({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-panel px-3 py-2.5">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 text-base font-semibold leading-tight text-text">{value}</div>
    </div>
  );
}

function RoadmapSection({
  model,
  loading,
  preparing,
  error,
  onRetry,
  onOpenBlueprint,
  blueprintLabel,
}: {
  model: PilotRoadmapModel | null;
  loading: boolean;
  preparing: boolean;
  error: string | null;
  onRetry: () => void;
  onOpenBlueprint: (id: string) => void;
  blueprintLabel: string;
}) {
  const showLoading = loading || preparing;

  return (
    <section className="space-y-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-accent/20 bg-accent/10 text-accent">
            <Map size={20} />
          </div>
          <div className="min-w-0">
            <h2 className="text-xl font-semibold text-text">Agent Roadmap</h2>
            <p className="mt-1 max-w-3xl text-sm leading-relaxed text-muted">
              Start with the phased agent rollout plan, then choose an opportunity below to inspect its {blueprintLabel}.
            </p>
          </div>
        </div>

        {model && (
          // R18-C0 P6: the Roadmap presents phases only — the permission/
          // dependency readiness tiles are removed to keep the customer-facing
          // roadmap focused on selected opportunities and rollout progression.
          <div className="grid min-w-[min(100%,320px)] grid-cols-2 gap-2">
            <MetricTile label="Selected" value={model.selectedOpportunityCount} />
            <MetricTile label="Readiness" value={model.overallReadiness} />
          </div>
        )}
      </div>

      <div>
        {showLoading ? (
          // Skeleton shaped like the roadmap's phase/stage cards, so they fill
          // the same space instead of replacing a centered spinner.
          <div
            aria-busy="true"
            aria-label="Loading Agent Roadmap"
            className="grid gap-3 sm:grid-cols-3"
          >
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-40 w-full rounded-lg" />
            ))}
          </div>
        ) : error ? (
          <div className="rounded-lg border border-red-400/20 bg-red-500/10 px-4 py-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <div className="text-sm font-semibold text-red-200">Roadmap unavailable</div>
                <div className="mt-1 text-sm leading-relaxed text-red-100/80">{error}</div>
              </div>
              <button
                type="button"
                onClick={onRetry}
                className="shrink-0 rounded-md border border-red-300/30 bg-red-500/10 px-4 py-2 text-sm font-medium text-red-100 hover:bg-red-500/20"
              >
                Retry
              </button>
            </div>
          </div>
        ) : model ? (
          <StagesGrid stages={model.stages} onOpenReview={onOpenBlueprint} />
        ) : (
          <div className="rounded-lg border border-border bg-bg/20 px-4 py-6 text-sm text-muted">
            Agent Roadmap data is not available for this discovery run yet.
          </div>
        )}
      </div>
    </section>
  );
}

// T41-7: exported for direct unit testing of the permissions section rendering.
// BlueprintPage remains the single consumer in production.
export function BlueprintContent({ blueprint }: { blueprint: BlueprintResponse }) {
  const actions = blueprint.suggestedActions ?? [];
  const guardrails = blueprint.guardrails ?? [];
  const permissions = blueprint.agentforcePermissions ?? [];
  const projection = blueprint.projection ?? null;
  const assumptions = projectionAssumptions(projection);
  const complexity = blueprint.complexity ?? {
    label: 'Assessment unavailable',
    description: 'Implementation complexity will be assessed during design.',
    tier: '',
  };

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-xl border border-border bg-panel">
      <div className="border-b border-border px-5 py-4">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-muted">Agent</div>
        <div className="mt-1 text-xl font-semibold leading-tight text-text">{blueprint.agentName ?? 'Custom Agent'}</div>
        <div className="mt-1.5 font-mono text-xs text-muted">{blueprint.detectorId ?? 'UNKNOWN'}</div>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        <SectionBlock
          icon={<FileText size={16} />}
          title="Agent Purpose"
        >
          <p className="text-sm leading-relaxed text-text">
            {blueprint.agentTopic?.trim()
              ? blueprint.agentTopic
              : 'Agent purpose not available for this opportunity.'}
          </p>
        </SectionBlock>

        {/* 2.0-A1 T4 — the projection band, its evidence label, and its
            strength (with the capped caveat where one applies). Placed above
            the assumptions so the band is never read without them nearby. */}
        {projection?.magnitudeBand && (
          <SectionBlock icon={<BarChart2 size={16} />} title="Projection Band">
            <ProjectionBandCompact projection={projection} />
          </SectionBlock>
        )}

        {assumptions.length > 0 && (
          <SectionBlock icon={<ListChecks size={16} />} title="Projection Assumptions">
            <ProjectionAssumptionList projection={projection} />
          </SectionBlock>
        )}

        <SectionBlock icon={<Zap size={16} />} title="Suggested Agent Actions">
          {actions.length > 0 ? (
            <div className="space-y-2">
              {actions.map((action, index) => (
                <div key={`${action.action}-${index}`} className="rounded-md border border-border bg-bg/30 p-3">
                  <div className="flex items-start gap-3">
                    <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent/15 text-xs font-semibold text-accent">
                      {index + 1}
                    </div>
                    <div className="min-w-0">
                      <div className="text-sm font-semibold text-text">{action.action ?? 'Action'}</div>
                      <div className="mt-0.5 font-mono text-xs text-muted">{action.object ?? ''}</div>
                      <div className="mt-1 text-xs leading-relaxed text-text">{action.detail ?? ''}</div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted">Agent actions will be defined during implementation design.</p>
          )}
        </SectionBlock>

        <SectionBlock icon={<Shield size={16} />} title="Guardrails">
          {guardrails.length > 0 ? (
            <div className="space-y-2">
              {guardrails.map((guardrail, index) => (
                <div key={`${guardrail}-${index}`} className="flex items-start gap-2 text-sm text-text">
                  <AlertCircle size={14} className="mt-0.5 shrink-0 text-amber-300" />
                  <span className="leading-relaxed">{guardrail}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted">Guardrails will be defined during implementation design.</p>
          )}
        </SectionBlock>

        <SectionBlock icon={<Settings size={16} />} title="Agent Permissions Required">
          {/* T41-7: forward-looking framing — agent-specific, future tense.
              No checked/missing status. This is what the agent WILL need,
              not what was required for the discovery run that already succeeded. */}
          {permissions.length > 0 ? (
            <div className="space-y-3">
              <p className="text-xs text-muted leading-relaxed">
                To implement this agent, the agent user profile will need:
              </p>
              <div className="space-y-2">
                {permissions.map((permission, index) => (
                  <div key={`${permission}-${index}`} className="flex items-center gap-2 text-sm text-text">
                    <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                    <span>{permission}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted">Permissions assessment is not yet available for this opportunity.</p>
          )}
        </SectionBlock>

        {projection?.basis && (
          <SectionBlock icon={<BarChart2 size={16} />} title="Projection Basis">
            <ProjectionBasisCompact projection={projection} showTitle={false} />
          </SectionBlock>
        )}

        <SectionBlock icon={<BarChart2 size={16} />} title="Implementation Complexity">
          <div className="text-sm font-semibold text-text">{complexity.label}</div>
          <p className="mt-2 text-sm leading-relaxed text-text">{complexity.description}</p>
          {complexity.tier && <div className="mt-2 text-xs font-semibold text-accent">{complexity.tier}</div>}
        </SectionBlock>
      </div>
    </div>
  );
}

function EvidencePanel({
  blueprint,
  opportunities,
  selectedIdx,
  onNavigate,
  runId,
}: {
  blueprint: BlueprintResponse;
  opportunities: OpportunityCandidate[];
  selectedIdx: number;
  onNavigate: (id: string) => void;
  runId: string | null;
}) {
  const nav = useNavigate();
  const opportunityReviewPath = runId ? `/opportunity-review?runId=${runId}` : '/opportunity-review';
  const evidenceIds = blueprint.evidenceIds ?? [];
  const prevOpp = selectedIdx > 0 ? opportunities[selectedIdx - 1] : null;
  const nextOpp = selectedIdx < opportunities.length - 1 ? opportunities[selectedIdx + 1] : null;

  // The full run evidence set is the SAME for every opportunity — fetch it ONCE
  // per run through the shared cache (deduped, started as soon as runId resolves)
  // and filter client-side by evidenceIds. Previously this re-fetched ALL
  // evidence on every opportunity switch via a keyed effect; now switching
  // opportunities is instant and makes no network call.
  const { data: allEvidence } = useResource<EvidenceReview[]>(
    runId ? cacheKeys.runEvidence(runId) : null,
    () => fetchEvidence(runId as string),
  );
  const evidenceMap = useMemo(() => {
    const map: Record<string, EvidenceReview> = {};
    (allEvidence ?? []).forEach((ev) => {
      map[ev.id] = ev;
    });
    return map;
  }, [allEvidence]);

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-xl border border-border bg-panel">
      <div className="border-b border-border px-4 py-3.5">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <FileText size={15} className="text-accent" />
            <div className="text-[13px] font-semibold uppercase tracking-wide text-text">Grounding Evidence</div>
          </div>
          <div className="rounded-full border border-border bg-bg/30 px-2 py-0.5 text-[11px] font-medium text-muted">
            {evidenceIds.length}
          </div>
        </div>
        <div className="mt-1.5 text-xs text-muted">Linked evidence for this opportunity</div>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        {evidenceIds.length > 0 ? (
          evidenceIds.map((id) => {
            const ev = evidenceMap[id];
            return (
              <div key={id} className="rounded-lg border border-border bg-bg/20 p-3">
                {ev ? (
                  <>
                    <div className="mb-1 flex items-center gap-2">
                      <span className="text-xs font-semibold text-accent">{ev.source}</span>
                      <span className="text-xs text-muted">- {ev.evidenceType}</span>
                      <span className="ml-auto">
                        <ConfidenceBadge level={ev.confidence} />
                      </span>
                    </div>
                    <div className="mb-1 text-sm font-medium text-text">{ev.title}</div>
                    {ev.snippet && <div className="text-xs leading-relaxed text-muted">{ev.snippet}</div>}
                  </>
                ) : (
                  <>
                    <div className="font-mono text-xs text-muted">{id}</div>
                    <div className="mt-1 text-sm text-text">Loading evidence...</div>
                  </>
                )}
              </div>
            );
          })
        ) : (
          <div className="rounded-lg border border-border bg-bg/20 p-4 text-sm text-muted">
            No evidence items linked to this opportunity.
          </div>
        )}
      </div>

      <div className="border-t border-border p-4">
        <button
          onClick={() => nav(opportunityReviewPath)}
          className="mb-3 flex w-full items-center gap-2 rounded-md border border-accent/20 bg-accent/5 px-3 py-2 text-left text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
        >
          <Link2 size={14} className="text-accent" />
          View in Opportunity Review
        </button>

        <div className="flex items-center justify-between text-sm text-text">
          <button
            type="button"
            onClick={() => prevOpp && onNavigate(prevOpp.id)}
            disabled={!prevOpp}
            className="flex items-center gap-1 rounded border border-accent/20 bg-accent/5 px-4 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronLeft className="h-4 w-4" />
            Prev
          </button>
          <span>
            {selectedIdx + 1} of {opportunities.length}
          </span>
          <button
            type="button"
            onClick={() => nextOpp && onNavigate(nextOpp.id)}
            disabled={!nextOpp}
            className="flex items-center gap-1 rounded border border-accent/20 bg-accent/5 px-4 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Next
            <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}

export default function BlueprintPage() {
  const { all: connectors } = useConnectorContext();
  const { opportunities, selectedId, select } = useAnalystReviewContext();
  const { run, computing } = useDiscoveryRunContext();
  const { runId } = useRunContext();
  const location = useLocation();
  const nav = useNavigate();
  const blueprintSectionRef = useRef<HTMLElement | null>(null);

  // blueprint / loading / error are derived from the shared cache below.

  // Roadmap via the shared cache: an opportunity decision/override in Opportunity
  // Review invalidates the run scope (AnalystReviewContext), so the roadmap here
  // refreshes instantly — no manual reload. Key is null (disabled) until a run
  // exists. refetch drives the "still preparing" poll below.
  const {
    data: roadmapData,
    loading: roadmapLoading,
    error: roadmapErrObj,
    refetch: refetchRoadmap,
  } = useResource<PilotRoadmapModel>(
    runId ? cacheKeys.runRoadmap(runId) : null,
    () => fetchRunRoadmap(runId as string),
  );
  const roadmap = roadmapData ?? null;
  const roadmapError = roadmapErrObj
    ? runScopedErrorMessage(roadmapErrObj, 'Failed to load roadmap')
    : null;

  const salesforceConnected = isSalesforceConnected(connectors);
  const blueprintLabel = getBlueprintLabel(salesforceConnected);
  const selectedOpp = opportunities.find((opp) => opp.id === selectedId) ?? null;
  const selectedIdx = opportunities.findIndex((opp) => opp.id === selectedId);
  const runStatus = run?.status?.toLowerCase();
  const runHasMaterializedResults =
    runStatus === 'complete' || runStatus === 'completed' || runStatus === 'partial';
  const roadmapPreparing =
    computing ||
    (Boolean(run) && !runHasMaterializedResults) ||
    /still being prepared/i.test(roadmapError ?? '');
  const requestedOppId = new URLSearchParams(location.search).get('oppId');
  const appliedOppIdRef = useRef<string | null>(null);

  const scrollToBlueprint = useCallback(() => {
    window.setTimeout(() => {
      blueprintSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 0);
  }, []);

  const handleRoadmapBlueprintSelect = useCallback(
    (id: string) => {
      select(id);
      scrollToBlueprint();
    },
    [select, scrollToBlueprint],
  );

  // Apply the URL's ?oppId only when it actually changes — NOT on every
  // selectedId change. Re-asserting on selectedId caused a stale URL param to
  // revert an in-page roadmap row selection (which selects without changing the
  // URL) straight back to the URL's opportunity.
  useEffect(() => {
    if (!requestedOppId) {
      appliedOppIdRef.current = null;
      return;
    }
    if (appliedOppIdRef.current === requestedOppId) return;
    if (!opportunities.some((opp) => opp.id === requestedOppId)) return;
    appliedOppIdRef.current = requestedOppId;
    if (selectedId !== requestedOppId) select(requestedOppId);
  }, [opportunities, requestedOppId, select, selectedId]);

  useEffect(() => {
    if (location.hash === '#blueprint-details') scrollToBlueprint();
  }, [location.hash, scrollToBlueprint]);

  useEffect(() => {
    if (!runId || !roadmapPreparing || roadmapLoading) return;
    const timer = window.setTimeout(() => refetchRoadmap(), 1500);
    return () => window.clearTimeout(timer);
  }, [runId, roadmapPreparing, roadmapLoading, refetchRoadmap]);

  // The selected opportunity's blueprint, on the SHARED cache — keyed per
  // opportunity, so switching between opportunities (or leaving and returning to
  // this page) renders instantly from cache instead of re-fetching every time.
  // PrefetchWorkspaceData warms this key for EVERY opportunity after login, so
  // in practice the blueprint is already there before it is asked for.
  const blueprintKey =
    runId && selectedId && salesforceConnected
      ? cacheKeys.runBlueprint(runId, selectedId)
      : null;
  const {
    data: blueprintData,
    loading,
    error: blueprintErrObj,
  } = useResource<BlueprintResponse>(blueprintKey, () =>
    fetchBlueprint(runId as string, selectedId as string),
  );
  const blueprint = blueprintData ?? null;
  const error = blueprintErrObj ? (blueprintErrObj.message ?? 'Failed to load blueprint') : null;

  const renderBlueprintContent = () => {
    if (!runId) {
      return (
        <WorkspaceNotice
          icon={<Map size={24} />}
          title="No discovery run selected"
          message={`Connect a source in the Integration Hub and run a discovery first to generate the Agent Roadmap and ${blueprintLabel}s.`}
          actionLabel="Go to Integration Hub"
          onAction={() => nav('/integration-hub')}
        />
      );
    }

    if (!salesforceConnected) {
      return (
        <WorkspaceNotice
          icon={<Zap size={24} />}
          title="Connect Salesforce"
          message="Agent Blueprint is available when Salesforce is connected."
          actionLabel="Go to Integration Hub"
          onAction={() => nav('/integration-hub')}
          tone="warning"
        />
      );
    }

    if (!selectedOpp) {
      return (
        <WorkspaceNotice
          icon={<ChevronRight size={24} />}
          title="Select an opportunity"
          message={`Choose an opportunity from the Agent Roadmap above or from Opportunity Review to view its ${blueprintLabel}.`}
          actionLabel="Go to Opportunity Review"
          onAction={() => nav(runId ? `/opportunity-review?runId=${runId}` : '/opportunity-review')}
        />
      );
    }

    if (loading) return <LoadingState blueprintLabel={blueprintLabel} />;

    if (error) {
      return (
        <WorkspaceNotice
          icon={<AlertCircle size={26} />}
          title="Failed to load blueprint"
          message={error}
          tone="error"
        />
      );
    }

    if (!blueprint) return <LoadingState blueprintLabel={blueprintLabel} />;

    return (
      <div
        className="grid gap-4 lg:h-[min(720px,calc(100vh-150px))] lg:min-h-[620px] lg:grid-cols-[minmax(250px,0.8fr)_minmax(360px,1.2fr)_minmax(250px,0.8fr)] xl:gap-6"
      >
        <OpportunitySelectorPanel opportunities={opportunities} selectedId={selectedId} onSelect={select} />
        <BlueprintContent blueprint={blueprint} />
        <EvidencePanel
          blueprint={blueprint}
          opportunities={opportunities}
          selectedIdx={selectedIdx}
          onNavigate={select}
          runId={runId}
        />
      </div>
    );
  };

  return (
    <PageShell
      title={blueprintLabel}
      description={`Review the Agent Roadmap first, then inspect the ${blueprintLabel} for the selected opportunity.`}
      className="bg-bg"
      actions={
        <>
          <StatusPill connected={salesforceConnected} />
          {selectedOpp && <TierBadge tier={selectedOpp.tier} />}
        </>
      }
    >
        <div className="space-y-8">
          {runId ? (
            <RoadmapSection
              model={roadmap}
              loading={roadmapLoading}
              preparing={roadmapPreparing}
              error={roadmapError}
              onRetry={refetchRoadmap}
              onOpenBlueprint={handleRoadmapBlueprintSelect}
              blueprintLabel={blueprintLabel}
            />
          ) : null}

          <section ref={blueprintSectionRef} id="blueprint-details" className="scroll-mt-24">
            <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="flex min-w-0 items-start gap-3">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-accent/20 bg-accent/10 text-accent">
                  <FileText size={20} />
                </div>
                <div className="min-w-0">
                  <h2 className="text-xl font-semibold text-text">Blueprint Details</h2>
                  <p className="mt-1 max-w-3xl text-sm leading-relaxed text-muted">
                    Selected opportunity design, guardrails, permissions, and evidence.
                  </p>
                </div>
              </div>
              {selectedOpp && (
                <div className="shrink-0 rounded-lg border border-border bg-panel px-3 py-2 text-sm text-muted">
                  Selected: <span className="font-medium text-text">{selectedOpp.title}</span>
                </div>
              )}
            </div>
            {renderBlueprintContent()}
          </section>
        </div>
    </PageShell>
  );
}
