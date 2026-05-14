/**
 * DiscoveryPlanScreen — Screen 4 of 4
 * SB-11 Task 11 Sprint 7
 *
 * "Your discovery plan"
 *
 * Layout (top to bottom):
 *   1. StackBuilderProgressBar — all 4 steps completed
 *   2. Page title + subtext
 *   3. DiscoveryConfidenceBar with showSummary=true (full-width, prominent)
 *   4. Two-column summary row:
 *      Left  — Configuration summary (key/value pairs)
 *      Right — Expected discovery quality (5 QualityRows with coloured dots)
 *   5. Selected systems strip — <span> chips, NOT PillTag
 *      Two dot colours: dark teal fill = primary platform,
 *      light teal outline dot = operational signal source / documentation
 *   6. Recommended additions card (conditional — when additions exist)
 *   7. "Ready to discover" launch block — centred, launch button
 *
 * Systems strip — <span> chips (SB-11 flag):
 *   Chips are read-only display elements, not interactive selectors.
 *   PillTag is an interactive toggle component — wrong affordance here.
 *   <span> chips are styled to match the wireframe without implying
 *   clickability. Primary platform chips use darker teal fill;
 *   signal source / documentation chips use a lighter teal outline.
 *
 * Quality rows:
 *   Derived locally from state — not from useSetupState.
 *   calcQualityRows(state, weightings) returns QualityRow[].
 *   Five rows matching wireframe Image 5:
 *     Workflow bottleneck detection
 *     Cross-system corroboration
 *     Compliance-sensitive workflows
 *     Documentation-driven interpretation
 *     Engineering / change signals
 *
 * Recommended additions:
 *   Derived locally from state.focusId and state.industryId.
 *   Maximum 3 recommendations. Not shown if state has 0 additions.
 *
 * Launch button:
 *   Calls onLaunch prop — parent router handles the actual discovery run.
 *   Styled as a full-width dark button matching the wireframe.
 *
 * Accessibility:
 *   Selected systems strip: role="list" with role="listitem" per chip.
 *   Colour legend: aria-label on each dot distinguishes chip type.
 *   Quality rows: coloured dots have aria-label matching their QualityLevel.
 *   Launch button: aria-label includes confidence level.
 *
 * Props:
 *   setupState — ReturnType<typeof useSetupState>
 *   onLaunch   — called when the user clicks "Start discovery"
 */

import React from 'react';
import {
  QualityRow, QualityLevel, RecommendedAddition,
  SystemRole, FocusId, IndustryId,
} from '../types/stack_builder';
import {
  StackBuilderProgressBar,
  DiscoveryConfidenceBar,
} from '../components/stack_builder';
import { useSetupState } from '../components/stack_builder';

// ── System metadata (same source as Screen 3) ─────────────────────────────────

const SYSTEM_META: Record<string, { name: string }> = {
  sap: { name: 'SAP' }, oracle_ebs: { name: 'Oracle EBS' },
  workday: { name: 'Workday' }, dynamics365: { name: 'Dynamics 365' },
  salesforce: { name: 'Salesforce' }, neospin: { name: 'Neospin' },
  vitech: { name: 'Vitech' },
  salesforce_pss: { name: 'Salesforce PSS' },
  salesforce_sc:  { name: 'Salesforce SC' },
  salesforce_ncino: { name: 'nCino' },
  salesforce_fsc: { name: 'Salesforce FSC' },
  salesforce_rc:  { name: 'Salesforce RC' },
  salesforce_hc:  { name: 'Salesforce HC' },
  jira: { name: 'Jira' }, servicenow: { name: 'ServiceNow' },
  azure_devops: { name: 'Azure DevOps' }, linear: { name: 'Linear' },
  zendesk: { name: 'Zendesk' }, slack: { name: 'Slack' },
  teams: { name: 'Microsoft Teams' }, confluence: { name: 'Confluence' },
  sharepoint: { name: 'SharePoint' }, notion: { name: 'Notion' },
  github: { name: 'GitHub' }, gitlab: { name: 'GitLab' },
  bitbucket: { name: 'Bitbucket' }, azure_repos: { name: 'Azure Repos' },
  postgresql: { name: 'PostgreSQL' }, sql_server: { name: 'SQL Server' },
  oracle_db: { name: 'Oracle DB' }, databricks: { name: 'Databricks' },
  snowflake: { name: 'Snowflake' }, dbt: { name: 'dbt' },
};

function getSystemName(id: string) {
  return SYSTEM_META[id]?.name ?? id;
}

// ── Quality row derivation ────────────────────────────────────────────────────

function calcQualityRows(
  selectedIds: string[],
  weightings: Record<string, { role: SystemRole; workflowFocus: string[] }>,
  showEngineeringRole: boolean,
): QualityRow[] {
  const hasPrimary = selectedIds.some(id => weightings[id]?.role === 'system_of_record' ||
    weightings[id]?.role === 'workflow_system');

  const signalSources = selectedIds.filter(id =>
    weightings[id]?.role === 'operational_signal_source'
  );

  const hasDoc = selectedIds.some(id =>
    weightings[id]?.role === 'documentation_system'
  );

  const hasCompliance = selectedIds.some(id =>
    weightings[id]?.workflowFocus?.includes('compliance_risk') ||
    weightings[id]?.workflowFocus?.includes('approvals')
  );

  const hasEngineering = selectedIds.some(id =>
    weightings[id]?.role === 'engineering_change_system'
  );

  const signalLevel = (n: number): QualityLevel =>
    n >= 2 ? 'strong' : n === 1 ? 'moderate' : 'limited';

  return [
    {
      label: 'Workflow bottleneck detection',
      level: hasPrimary ? 'strong' : 'limited',
      descriptor: hasPrimary
        ? 'Strong — primary system of record connected'
        : 'Limited — no primary platform selected',
    },
    {
      label: 'Cross-system corroboration',
      level: signalLevel(signalSources.length),
      descriptor: signalSources.length >= 2
        ? `Strong — ${signalSources.length} operational signal sources`
        : signalSources.length === 1
        ? 'Moderate — 1 operational signal source'
        : 'Limited — no operational signal sources selected',
    },
    {
      label: 'Compliance-sensitive workflows',
      level: hasCompliance ? 'strong' : 'limited',
      descriptor: hasCompliance
        ? 'Strong — compliance / risk focus confirmed'
        : 'Limited — no compliance focus configured',
    },
    {
      label: 'Documentation-driven interpretation',
      level: hasDoc ? 'moderate' : 'limited',
      descriptor: hasDoc
        ? 'Moderate — one documentation source'
        : 'Limited — no documentation system selected',
    },
    {
      label: 'Engineering / change signals',
      level: hasEngineering ? 'moderate' : 'limited',
      descriptor: hasEngineering
        ? 'Moderate — engineering system configured'
        : 'Limited — no code or engineering system selected',
    },
  ];
}

// ── Recommended additions derivation ─────────────────────────────────────────

function calcRecommendedAdditions(
  selectedIds: string[],
  focusId: FocusId | null,
  industryId: IndustryId | null,
): RecommendedAddition[] {
  const additions: RecommendedAddition[] = [];

  const hasSlack = selectedIds.includes('slack') || selectedIds.includes('teams');
  const hasDoc = selectedIds.some(id => ['confluence', 'sharepoint', 'notion'].includes(id));
  const hasSignal = selectedIds.some(id => ['jira', 'servicenow', 'zendesk', 'linear', 'azure_devops'].includes(id));

  if (!hasSlack) {
    const reason = focusId === 'member_customer_service'
      ? 'Add to capture member services communication signals — case escalation threads, team coordination delays, and cross-team handoff friction relevant to public sector service workflows.'
      : focusId === 'cross_system_handoffs'
      ? 'Add to capture inter-team communication signals — escalation threads and coordination delays that indicate handoff friction.'
      : 'Add to capture communication signals — escalation threads, team coordination delays, and operational discussions.';
    additions.push({ systemId: 'slack', systemName: 'Slack', reason });
  }

  if (!hasDoc) {
    const reason = industryId === 'public_sector'
      ? 'Add to strengthen compliance and documentation evidence — policy documents, approval workflows, and regulatory reference materials common in public sector operating environments.'
      : focusId === 'approvals_compliance'
      ? 'Add to strengthen compliance evidence — policy documents and approval workflow documentation.'
      : 'Add to strengthen process interpretation — knowledge base articles, SOPs, and workflow documentation.';
    additions.push({ systemId: 'sharepoint', systemName: 'SharePoint', reason });
  }

  if (!hasSignal) {
    additions.push({
      systemId: 'jira',
      systemName: 'Jira',
      reason: 'Add to capture workflow signals — ticket backlogs, work queue patterns, and process friction visible in issue tracking data.',
    });
  }

  return additions.slice(0, 3);
}

// ── Quality dot ───────────────────────────────────────────────────────────────

function QualityDot({ level }: { level: QualityLevel }) {
  const classes: Record<QualityLevel, string> = {
    strong:   'bg-emerald-500',
    moderate: 'bg-amber-400',
    limited:  'bg-slate-300',
  };
  return (
    <div
      className={`h-2 w-2 rounded-full flex-shrink-0 mt-1 ${classes[level]}`}
      aria-label={level}
      role="img"
    />
  );
}

// ── Selected system chip — <span>, NOT PillTag ────────────────────────────────
// SB-11 flag: chips are read-only display elements.
// PillTag implies interactive toggle — wrong affordance for a summary view.

type ChipVariant = 'primary' | 'signal';

function SystemChip({ name, variant }: { name: string; variant: ChipVariant }) {
  const classes: Record<ChipVariant, string> = {
    primary: 'border-emerald-500 bg-emerald-500/15 text-emerald-700',
    signal:  'border-emerald-500/40 bg-panel text-muted',
  };
  return (
    <span
      role="listitem"
      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium ${classes[variant]}`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full flex-shrink-0 ${
          variant === 'primary' ? 'bg-emerald-500' : 'bg-emerald-400'
        }`}
        aria-hidden="true"
      />
      {name}
    </span>
  );
}

// ── Recommended addition icon ─────────────────────────────────────────────────

function AdditionIcon({ systemId }: { systemId: string }) {
  const isComms = ['slack', 'teams'].includes(systemId);
  return isComms ? (
    <svg className="flex-shrink-0 text-muted" width="16" height="16" viewBox="0 0 16 16"
      fill="none" aria-hidden="true">
      <path d="M2 3h12a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H9L5 14v-3H2a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"
        stroke="currentColor" strokeWidth="1.2" fill="none"/>
    </svg>
  ) : (
    <svg className="flex-shrink-0 text-muted" width="16" height="16" viewBox="0 0 16 16"
      fill="none" aria-hidden="true">
      <rect x="2" y="2" width="12" height="14" rx="1.5" stroke="currentColor" strokeWidth="1.2"/>
      <path d="M5 6h6M5 9h4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
    </svg>
  );
}

// ── Summary label row ─────────────────────────────────────────────────────────

function SummaryRow({ label, value, valueClass = 'text-text' }: {
  label: string; value: string; valueClass?: string;
}) {
  return (
    <div className="flex justify-between items-baseline py-2 border-b border-border/50 last:border-0">
      <span className="text-xs text-muted">{label}</span>
      <span className={`text-xs font-medium text-right ml-4 ${valueClass}`}>{value}</span>
    </div>
  );
}

// ── Screen ────────────────────────────────────────────────────────────────────

interface Props {
  setupState: ReturnType<typeof useSetupState>;
  onLaunch: () => void;
}

export default function DiscoveryPlanScreen({ setupState, onLaunch }: Props) {
  const { state, steps, confidence, showEngineeringRole } = setupState;

  const qualityRows = calcQualityRows(
    state.selectedSystemIds,
    state.weightings,
    showEngineeringRole,
  );

  const recommendations = calcRecommendedAdditions(
    state.selectedSystemIds,
    state.focusId,
    state.industryId,
  );

  // Classify each system for the chips strip
  const primaryIds = state.selectedSystemIds.filter(id =>
    state.weightings[id]?.role === 'system_of_record' ||
    state.weightings[id]?.role === 'workflow_system'
  );

  const signalIds = state.selectedSystemIds.filter(id => !primaryIds.includes(id));

  // Config summary values
  const FOCUS_LABELS: Record<string, string> = {
    member_customer_service:  'Member / customer service',
    core_operations:          'Core operations',
    approvals_compliance:     'Approvals / compliance',
    cross_system_handoffs:    'Cross-system handoffs',
    back_office_productivity: 'Back-office productivity',
    engineering_change:       'Engineering / change',
    enterprise_wide:          'Enterprise-wide discovery',
  };

  const INDUSTRY_LABELS: Record<string, string> = {
    financial_services: 'Financial services',
    public_sector: 'Public sector',
    logistics_supply_chain: 'Logistics & supply chain',
    retail_commerce: 'Retail & commerce',
    healthcare: 'Healthcare',
    energy_utilities: 'Energy & utilities',
    manufacturing: 'Manufacturing',
    technology: 'Technology',
  };

  const TEMPLATE_LABELS: Record<string, string> = {
    commercial_lending: 'Commercial lending',
    public_retirement:  'Public retirement',
    service_operations: 'Service operations',
    revenue_operations: 'Revenue operations',
  };

  const operationalSources = state.selectedSystemIds.filter(id =>
    state.weightings[id]?.role === 'operational_signal_source'
  );
  const docSystems = state.selectedSystemIds.filter(id =>
    state.weightings[id]?.role === 'documentation_system'
  );

  return (
    <div className="min-h-screen bg-paper">
      <div className="max-w-3xl mx-auto px-6 py-8">

        {/* ── Progress bar ── */}
        <StackBuilderProgressBar steps={steps} />

        {/* ── Page title ── */}
        <h1 className="text-2xl font-bold text-text mb-2">
          Your discovery plan
        </h1>
        <p className="text-sm text-muted leading-relaxed mb-6">
          AgentIQ will analyse your selected systems using the focus and source
          weighting you defined. Review and launch when ready.
        </p>

        {/* ── Confidence bar — prominent, with summary ── */}
        <DiscoveryConfidenceBar state={confidence} showSummary />

        {/* ── Two-column summary row ── */}
        <div className="grid grid-cols-2 gap-3 mb-3">

          {/* Configuration summary */}
          <div className="rounded-xl border border-border bg-panel p-4">
            <div className="flex items-center gap-2 mb-3">
              <svg className="text-muted" width="14" height="14" viewBox="0 0 14 14"
                fill="none" aria-hidden="true">
                <path d="M2 4h10M2 7h7M2 10h5" stroke="currentColor"
                  strokeWidth="1.2" strokeLinecap="round"/>
              </svg>
              <span className="text-xs font-semibold text-text">Configuration summary</span>
            </div>
            <div>
              <SummaryRow
                label="Discovery focus"
                value={state.focusId ? FOCUS_LABELS[state.focusId] : '—'}
                valueClass="text-emerald-600"
              />
              <SummaryRow
                label="Industry"
                value={state.industryId ? INDUSTRY_LABELS[state.industryId] : '—'}
              />
              <SummaryRow
                label="Template"
                value={state.templateId ? TEMPLATE_LABELS[state.templateId] : '—'}
              />
              <SummaryRow
                label="Total systems"
                value={String(state.selectedSystemIds.length)}
              />
              <SummaryRow
                label="Primary platforms"
                value={primaryIds.length > 0
                  ? `${primaryIds.length} — ${primaryIds.map(getSystemName).join(', ')}`
                  : '—'}
              />
              <SummaryRow
                label="Operational signal sources"
                value={operationalSources.length > 0
                  ? `${operationalSources.length} — ${operationalSources.map(getSystemName).join(', ')}`
                  : '—'}
              />
              <SummaryRow
                label="Documentation systems"
                value={docSystems.length > 0
                  ? `${docSystems.length} — ${docSystems.map(getSystemName).join(', ')}`
                  : '—'}
              />
            </div>
          </div>

          {/* Expected discovery quality */}
          <div className="rounded-xl border border-border bg-panel p-4">
            <div className="flex items-center gap-2 mb-3">
              <svg className="text-muted" width="14" height="14" viewBox="0 0 14 14"
                fill="none" aria-hidden="true">
                <path d="M2 10 L5 5 L8 8 L11 3" stroke="currentColor"
                  strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              <span className="text-xs font-semibold text-text">Expected discovery quality</span>
            </div>
            <div className="space-y-3">
              {qualityRows.map(row => (
                <div key={row.label}>
                  <div className="flex items-start gap-2">
                    <QualityDot level={row.level} />
                    <div>
                      <div className="text-xs font-medium text-text leading-tight mb-0.5">
                        {row.label}
                      </div>
                      <div className="text-xs text-muted leading-relaxed">
                        {row.descriptor}
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* ── Selected systems strip ── */}
        <div className="rounded-xl border border-border bg-panel p-4 mb-3">
          <div className="flex items-center gap-2 mb-3">
            <svg className="text-muted" width="14" height="14" viewBox="0 0 14 14"
              fill="none" aria-hidden="true">
              <rect x="1" y="1" width="5.5" height="5.5" rx="1" stroke="currentColor" strokeWidth="1.2"/>
              <rect x="7.5" y="1" width="5.5" height="5.5" rx="1" stroke="currentColor" strokeWidth="1.2"/>
              <rect x="1" y="7.5" width="5.5" height="5.5" rx="1" stroke="currentColor" strokeWidth="1.2"/>
              <rect x="7.5" y="7.5" width="5.5" height="5.5" rx="1" stroke="currentColor" strokeWidth="1.2"/>
            </svg>
            <span className="text-xs font-semibold text-text">Selected systems</span>
          </div>

          {/* Chips — <span> not PillTag, read-only */}
          <div role="list" className="flex flex-wrap gap-2 mb-3">
            {primaryIds.map(id => (
              <SystemChip key={id} name={getSystemName(id)} variant="primary" />
            ))}
            {signalIds.map(id => (
              <SystemChip key={id} name={getSystemName(id)} variant="signal" />
            ))}
          </div>

          {/* Colour legend */}
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full bg-emerald-500 flex-shrink-0" aria-hidden="true"/>
              <span className="text-xs text-muted">Primary platform</span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full bg-emerald-400 flex-shrink-0" aria-hidden="true"/>
              <span className="text-xs text-muted">Operational signal source / documentation</span>
            </div>
          </div>
        </div>

        {/* ── Recommended additions ── */}
        {recommendations.length > 0 && (
          <div className="rounded-xl border border-border bg-panel p-4 mb-6">
            <div className="flex items-center gap-2 mb-3">
              <svg className="text-muted" width="14" height="14" viewBox="0 0 14 14"
                fill="none" aria-hidden="true">
                <circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.2"/>
                <path d="M7 4v3l2 1.5" stroke="currentColor" strokeWidth="1.2"
                  strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              <span className="text-xs font-semibold text-text">Recommended additions</span>
            </div>
            <div className="space-y-4">
              {recommendations.map(rec => (
                <div key={rec.systemId} className="flex items-start gap-3">
                  <AdditionIcon systemId={rec.systemId} />
                  <div>
                    <div className="text-sm font-medium text-text mb-0.5">
                      {rec.systemName}
                    </div>
                    <p className="text-xs text-muted leading-relaxed">
                      {rec.reason}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Ready to discover launch block ── */}
        <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/[0.04] p-6 text-center">
          <div className="text-sm font-medium text-text mb-1">
            Ready to discover
          </div>
          <p className="text-xs text-muted mb-4">
            {state.selectedSystemIds.length} system{state.selectedSystemIds.length !== 1 ? 's' : ''}{' '}
            {state.industryId ? `· ${INDUSTRY_LABELS[state.industryId]}` : ''}{' '}
            {state.focusId ? `· ${FOCUS_LABELS[state.focusId]} focus` : ''}{' '}
            · Confidence: {confidence.level.charAt(0).toUpperCase() + confidence.level.slice(1)}
          </p>
          <button
            type="button"
            onClick={onLaunch}
            aria-label={`Start discovery — confidence ${confidence.level}`}
            className={[
              'inline-flex items-center gap-2 rounded-lg px-8 py-3',
              'text-sm font-medium bg-text text-paper hover:opacity-90',
              'bg-white text-gray-900 hover:opacity-90 cursor-pointer w-full justify-center',
              'focus:outline-none focus:ring-2 focus:ring-emerald-500/50',
            ].join(' ')}
          >
            Start discovery
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
              <path d="M3 7h8M8 4l3 3-3 3" stroke="currentColor" strokeWidth="1.5"
                strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </button>
        </div>

        {/* Bottom padding for scroll comfort */}
        <div className="h-8" />

      </div>

      {/* ── Back navigation (no Continue — launch replaces it) ── */}
      <div className="sticky bottom-0 bg-paper border-t border-border">
        <div className="max-w-3xl mx-auto px-6 py-4">
          <button
            type="button"
            onClick={() => setupState.goTo(3)}
            className={[
              'inline-flex items-center gap-2 rounded-lg border border-border px-4 py-2.5',
              'text-sm font-medium text-text hover:bg-panel transition-colors',
              'focus:outline-none focus:ring-2 focus:ring-emerald-500/50',
            ].join(' ')}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
              <path d="M11 7H3M6 4L3 7l3 3" stroke="currentColor" strokeWidth="1.5"
                strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            Back
          </button>
        </div>
      </div>
    </div>
  );
}
