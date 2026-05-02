import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  AlertCircle,
  BarChart2,
  ChevronLeft,
  ChevronRight,
  FileText,
  Link2,
  Settings,
  Shield,
  Zap,
} from 'lucide-react';
import { InfoPanel } from '../components/common/InfoPanel';
import LoadingPanel from '../components/common/LoadingPanel';
import TopNav from '../components/common/TopNav';
import { useConnectorContext } from '../context/ConnectorContext';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';
import { useRunContext } from '../context/RunContext';
import { fetchBlueprint } from '../api/blueprintApi';
import type { BlueprintResponse } from '../utils/blueprintTypes';
import type { OpportunityCandidate } from '../types/analystReview';

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
  const label = value === 'UNREVIEWED' ? 'Pending' : (value ?? 'Unknown');
  const cls =
    value === 'APPROVED'
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
      : value === 'REJECTED'
        ? 'border-red-500/40 bg-red-500/10 text-red-200'
        : 'border-border bg-bg/20 text-muted';

  return <span className={`rounded-full border px-2 py-0.5 text-xs ${cls}`}>{label}</span>;
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

function EmptyPanel({
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
    <InfoPanel
      icon={icon}
      iconClassName={iconCls}
      title={title}
      message={message}
      actionLabel={actionLabel}
      onAction={onAction}
    />
  );
}

function LoadingState() {
  return (
    <LoadingPanel
      title="Loading blueprint"
      subtitle="Fetching the Agentforce Blueprint for the selected opportunity."
    />
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
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="text-lg font-semibold text-text">Opportunities</div>
        <div className="text-xs text-muted">{opportunities.length} found</div>
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
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-border bg-bg/20 p-4">
      <div className="mb-3 flex items-center gap-2">
        <span className="text-accent">{icon}</span>
        <span className="text-sm font-semibold text-text">{title}</span>
      </div>
      {children}
    </section>
  );
}

// T41-7: exported for direct unit testing of the permissions section rendering.
// BlueprintPage remains the single consumer in production.
export function BlueprintContent({ blueprint }: { blueprint: BlueprintResponse }) {
  const actions = blueprint.suggestedActions ?? [];
  const guardrails = blueprint.guardrails ?? [];
  const permissions = blueprint.agentforcePermissions ?? [];
  const complexity = blueprint.complexity ?? {
    label: 'Assessment unavailable',
    description: 'Implementation complexity will be assessed during design.',
    tier: '',
  };

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-xl border border-border bg-panel">
      <div className="border-b border-border px-5 py-4">
        <div className="text-xs font-semibold uppercase text-muted">Agentforce Agent</div>
        <div className="mt-1 text-2xl font-semibold text-text">{blueprint.agentName ?? 'Custom Agent'}</div>
        <div className="mt-1 font-mono text-xs text-muted">{blueprint.detectorId ?? 'UNKNOWN'}</div>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        <SectionBlock icon={<FileText size={16} />} title="Agent Purpose">
          <div className="mb-2 flex items-center gap-2">
            {blueprint.agentTopicIsLlm && (
              <span className="rounded-full border border-border bg-bg/30 px-2 py-0.5 text-xs text-muted">
                Claude
              </span>
            )}
          </div>
          <p className="text-sm leading-relaxed text-text">
            {blueprint.agentTopic?.trim()
              ? blueprint.agentTopic
              : 'Agent purpose not available for this opportunity.'}
          </p>
        </SectionBlock>

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

        <SectionBlock icon={<Settings size={16} />} title="Agentforce Permissions Required">
          {/* T41-7: forward-looking framing — agent-specific, future tense.
              No checked/missing status. This is what the agent WILL need,
              not what was required for the discovery run that already succeeded. */}
          {permissions.length > 0 ? (
            <div className="space-y-3">
              <p className="text-xs text-muted leading-relaxed">
                To implement this Agentforce agent, the agent user profile will need:
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

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-xl border border-border bg-panel">
      <div className="border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <FileText size={16} className="text-accent" />
          <div className="text-lg font-semibold text-text">Grounding Evidence</div>
        </div>
        <div className="mt-1 text-xs text-muted">{evidenceIds.length} linked evidence item(s)</div>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        {evidenceIds.length > 0 ? (
          evidenceIds.map((id) => (
            <div key={id} className="rounded-lg border border-border bg-bg/20 p-3">
              <div className="font-mono text-xs text-muted">{id}</div>
              <div className="mt-1 text-sm text-text">Evidence record linked to this opportunity.</div>
            </div>
          ))
        ) : (
          <div className="rounded-lg border border-border bg-bg/20 p-4 text-sm text-muted">
            No evidence items linked to this opportunity.
          </div>
        )}
      </div>

      <div className="border-t border-border p-4">
        <button
          onClick={() => nav(opportunityReviewPath)}
          className="mb-3 flex w-full items-center gap-2 rounded-md border border-border bg-bg/20 px-3 py-2 text-left text-sm text-text transition hover:bg-panel2"
        >
          <Link2 size={14} className="text-accent" />
          View in Opportunity Review
        </button>

        <div className="flex items-center justify-between text-sm text-text">
          <button
            type="button"
            onClick={() => prevOpp && onNavigate(prevOpp.id)}
            disabled={!prevOpp}
            className="flex items-center gap-1 rounded border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40"
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
            className="flex items-center gap-1 rounded border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40"
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
  const { runId } = useRunContext();
  const nav = useNavigate();

  const [blueprint, setBlueprint] = useState<BlueprintResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const salesforceConnected = connectors.some(
    (connector) => connector.id === 'salesforce' && connector.status === 'connected',
  );
  const selectedOpp = opportunities.find((opp) => opp.id === selectedId) ?? null;
  const selectedIdx = opportunities.findIndex((opp) => opp.id === selectedId);

  useEffect(() => {
    if (!runId || !selectedId || !salesforceConnected) {
      setBlueprint(null);
      setError(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    setBlueprint(null);

    fetchBlueprint(runId, selectedId)
      .then((data) => {
        if (!cancelled) setBlueprint(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err?.message ?? 'Failed to load blueprint');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [runId, selectedId, salesforceConnected]);

  const renderContent = () => {
    if (!salesforceConnected) {
      return (
        <EmptyPanel
          title="Connect Salesforce"
          message="Agentforce Blueprint is available when Salesforce is connected."
          actionLabel="Go to Integration Hub"
          onAction={() => nav('/integration-hub')}
          tone="warning"
        />
      );
    }

    if (!runId) {
      return (
        <EmptyPanel
          title="No discovery run selected"
          message="Run a discovery first to generate Agentforce Blueprints."
          actionLabel="Go to Discovery Run"
          onAction={() => nav('/discovery-run')}
        />
      );
    }

    if (!selectedOpp) {
      return (
        <EmptyPanel
          title="Select an opportunity"
          message="Choose an opportunity in Opportunity Review to view its Agentforce Blueprint."
          actionLabel="Go to Opportunity Review"
          onAction={() => nav(runId ? `/opportunity-review?runId=${runId}` : '/opportunity-review')}
        />
      );
    }

    if (loading) return <LoadingState />;

    if (error) {
      return (
        <EmptyPanel
          icon={<AlertCircle size={26} />}
          title="Failed to load blueprint"
          message={error}
          tone="error"
        />
      );
    }

    if (!blueprint) return <LoadingState />;

    return (
      <div
        className="grid gap-6"
        style={{
          gridTemplateColumns: '27.5% minmax(0, 43%) 27.5%',
          height: 'calc(100vh - 190px)',
          minHeight: '640px',
        }}
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
    <div className="min-h-screen bg-bg text-text">
      <TopNav />

      <div className="w-full px-8 py-6 pb-10">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <div className="text-2xl font-semibold text-text">Agentforce Blueprint</div>
            <div className="mt-1 text-sm text-muted">
              Agent design generated from the selected opportunity and its discovery evidence.
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <StatusPill connected={salesforceConnected} />
            {selectedOpp && <TierBadge tier={selectedOpp.tier} />}
          </div>
        </div>

        {renderContent()}
      </div>
    </div>
  );
}
