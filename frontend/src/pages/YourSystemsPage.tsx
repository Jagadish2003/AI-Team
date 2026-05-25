/**
 * YourSystemsPage — ENG-SB-1 Sprint 9
 *
 * Screen 2 of 4 in the Stack Builder flow.
 * FULL REWRITE — catalog-driven, replacing static system arrays.
 *
 * WHAT CHANGED FROM SPRINT 7:
 *   Old: 7 static arrays (PRIMARY_PLATFORMS, WORK_TRACKING, etc.) defined
 *        in this file. Every possible vendor shown regardless of workspace.
 *   New: Two sections driven entirely by WorkspaceCatalogResponse:
 *     Section 1 — Connected systems (from catalog, grouped A/B/C)
 *     Section 2 — Recommended additions (missing_categories only)
 *
 * WHAT WAS REMOVED:
 *   - All static system arrays (PRIMARY_PLATFORMS, WORK_TRACKING, etc.)
 *   - Salesforce cloud expansion panel (products pre-populated from catalog
 *     by useSetupState.initFromCatalog — ENG-SB-2)
 *   - "Other platforms involved" section (vendor alternatives for primary)
 *
 * WHAT DID NOT CHANGE:
 *   - SystemCard component — same component, same props
 *   - getRecommendationReason — same function
 *   - canProceedFromStep2 gate — at least one primary platform selected
 *   - Connection status legend
 *   - Navigation (Back / Continue to source weighting)
 *   - Dark theme tokens
 *
 * Section 1 — Connected systems:
 *   Systems from catalog, grouped A/B/C.
 *   Only systems in the catalog appear — if GitHub is the only code/engineering
 *   system, only GitHub renders (not GitLab/Bitbucket/Azure Repos).
 *   Pre-selected by default via initFromCatalog (ENG-SB-2).
 *   User can deselect — SystemCard toggles on click.
 *
 * Section 2 — Recommended additions:
 *   Only for categories in catalog.missing_categories.
 *   Category prompt with 1-3 vendor suggestions (not a full catalog).
 *   "Connect in Integration Hub" CTA → /integration-hub?category={id}
 *   Clicking CTA navigates to IH with correct group pre-highlighted.
 *
 * Empty catalog state (AC10):
 *   No systems connected → Section 1 empty state with prompt to connect.
 *   CTA → /integration-hub (no category param — full overview for new workspaces).
 *
 * Sprint 10 note:
 *   defaultIncluded SystemCard visual state (lighter teal for catalog-sourced
 *   vs full emerald for user-confirmed) is deferred to ENG-ARCH-1 Sprint 10.
 *   For Sprint 9, toggle behaviour is correct — SystemCard toggles on click.
 */
import React, { useMemo } from 'react';
import {
  ArrowLeft,
  Code2,
  CircleCheck,
  Database,
  Info,
  ListChecks,
  MessageSquare,
  MoveRight,
  PlusCircle,
  Building2,
} from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import Button from '../components/common/Button';
import { SystemCard } from '../components/stack_builder';
import { useSetupState } from '../components/stack_builder';
import {
  SystemCard as SystemCardType,
  FocusId,
  ConnectionStatus,
} from '../types/stack_builder';
import type { WorkspaceCatalogResponse, CatalogSystemItem } from '../types/workspace_catalog';

// ── Logo / initials map ───────────────────────────────────────────────────────
// Maps system_id → { logoInitials, logoColor, category } for SystemCard rendering.
// Mirrors the static arrays that were previously hardcoded in this file.
// Sprint 9: Neospin and Vitech removed per ENG-IH-4.

const SYSTEM_DISPLAY: Record<string, {
  logoInitials: string;
  logoColor:    string;
  category:     string;
  isSalesforce?: boolean;
}> = {
  // Primary platforms
  salesforce:  { logoInitials: 'SF',   logoColor: 'bg-sky-500',    category: 'CRM / industry',    isSalesforce: true },
  sap:         { logoInitials: 'SAP',  logoColor: 'bg-blue-700',   category: 'ERP' },
  oracle_ebs:  { logoInitials: 'ORC',  logoColor: 'bg-red-700',    category: 'Finance / HR' },
  workday:     { logoInitials: 'WD',   logoColor: 'bg-yellow-600', category: 'HR / finance' },
  dynamics365: { logoInitials: 'D365', logoColor: 'bg-blue-600',   category: 'ERP / CRM' },
  // Operational
  jira:           { logoInitials: 'JR',  logoColor: 'bg-blue-600',   category: 'Issues / backlog' },
  // jira_confluence:{ logoInitials: 'JR',  logoColor: 'bg-blue-600',   category: 'Issues / knowledge' },
  servicenow:     { logoInitials: 'SN',  logoColor: 'bg-green-700',  category: 'ITSM / operations' },
  azure_devops:   { logoInitials: 'ADO', logoColor: 'bg-blue-700',   category: 'ALM / CI/CD' },
  linear:         { logoInitials: 'LN',  logoColor: 'bg-violet-600', category: 'Product / issues' },
  zendesk:        { logoInitials: 'ZD',  logoColor: 'bg-green-600',  category: 'Support' },
  // Comms & knowledge
  slack:      { logoInitials: 'SL',  logoColor: 'bg-purple-600', category: 'Messaging' },
  teams:      { logoInitials: 'MS',  logoColor: 'bg-blue-700',   category: 'Comms / docs' },
  m365:       { logoInitials: 'M',   logoColor: 'bg-blue-700',   category: 'Comms / docs' },
  confluence: { logoInitials: 'CF',  logoColor: 'bg-blue-500',   category: 'Docs / knowledge' },
  sharepoint: { logoInitials: 'SP',  logoColor: 'bg-blue-600',   category: 'Docs / intranet' },
  notion:     { logoInitials: 'NO',  logoColor: 'bg-slate-700',  category: 'Docs / wiki' },
  // Data & engineering
  github:      { logoInitials: 'GH',  logoColor: 'bg-slate-800',  category: 'Source control' },
  gitlab:      { logoInitials: 'GL',  logoColor: 'bg-orange-600', category: 'DevOps' },
  bitbucket:   { logoInitials: 'BB',  logoColor: 'bg-blue-600',   category: 'Source control' },
  azure_repos: { logoInitials: 'AR',  logoColor: 'bg-blue-700',   category: 'Source control' },
  postgresql:  { logoInitials: 'PG',  logoColor: 'bg-blue-700',   category: 'Database' },
  sql_server:  { logoInitials: 'SQL', logoColor: 'bg-red-700',    category: 'Database' },
  oracle_db:   { logoInitials: 'ORC', logoColor: 'bg-red-600',    category: 'Database' },
  databricks:  { logoInitials: 'DB',  logoColor: 'bg-orange-500', category: 'Data platform' },
  snowflake:   { logoInitials: 'SF',  logoColor: 'bg-sky-500',    category: 'Data warehouse' },
  dbt:         { logoInitials: 'dbt', logoColor: 'bg-orange-600', category: 'Transforms' },
};

// ── Group metadata ────────────────────────────────────────────────────────────

interface GroupDef {
  categoryKey: keyof Omit<WorkspaceCatalogResponse, 'missing_categories'>;
  label:       string;
  subLabel:    string;
  icon:        React.ReactNode;
  group:       SystemCardType['group'];
}

const GROUPS: GroupDef[] = [
  {
    categoryKey: 'primary_platforms',
    label:       'Group A — Primary Business Platforms',
    subLabel:    'Where your core operation runs',
    icon:        <Building2 size={16} />,
    group:       'primary_platform',
  },
  {
    categoryKey: 'operational_systems',
    label:       'Group B — Operational Systems',
    subLabel:    'Work tracking and operational signal sources',
    icon:        <ListChecks size={16} />,
    group:       'work_tracking',
  },
  {
    categoryKey: 'comms_knowledge',
    label:       'Group B — Communications & Knowledge',
    subLabel:    'Communication signals and documentation sources',
    icon:        <MessageSquare size={16} />,
    group:       'comms_knowledge',
  },
  {
    categoryKey: 'data_engineering',
    label:       'Group C — Data & Engineering Sources',
    subLabel:    'Source control, databases, and data platform connectors',
    icon:        <Code2 size={16} />,
    group:       'code_engineering',
  },
];

// ── Recommended additions metadata ────────────────────────────────────────────

interface RecommendedAddition {
  categoryId:  string;
  label:       string;
  reason:      string;
  suggestions: string[];   // 1-3 vendor names — not a full catalog
}

const MISSING_CATEGORY_ADDITIONS: Record<string, RecommendedAddition> = {
  primary_platforms: {
    categoryId:  'primary_platforms',
    label:       'Add a primary business platform',
    reason:      'A primary platform is required for discovery. Connect your core system of record.',
    suggestions: ['Salesforce', 'SAP', 'Workday'],
  },
  operational_systems: {
    categoryId:  'operational_systems',
    label:       'Add an operational signal source',
    reason:      'Operational systems provide work tracking signals and corroboration evidence.',
    suggestions: ['Jira', 'ServiceNow', 'Azure DevOps'],
  },
  comms_knowledge: {
    categoryId:  'comms_knowledge',
    label:       'Add a communication source',
    reason:      'Capture escalation signals and team coordination patterns.',
    suggestions: ['Slack', 'Microsoft Teams'],
  },
  documentation: {
    categoryId:  'comms_knowledge',
    label:       'Add a documentation source',
    reason:      'Improve process interpretation — policy docs, SOPs, and workflow guides.',
    suggestions: ['Confluence', 'SharePoint', 'Notion'],
  },
  data_engineering: {
    categoryId:  'data_engineering',
    label:       'Add a data or engineering source',
    reason:      'Source control and data platform signals for change and engineering coverage.',
    suggestions: ['GitHub', 'GitLab', 'PostgreSQL'],
  },
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function catalogItemToSystemCard(
  item: CatalogSystemItem,
  group: SystemCardType['group'],
): SystemCardType {
  const display = SYSTEM_DISPLAY[item.system_id] ?? {
    logoInitials: item.system_id.slice(0, 2).toUpperCase(),
    logoColor:    'bg-slate-600',
    category:     item.name,
  };

  const connectionStatus: ConnectionStatus =
    item.status === 'connected'  ? 'connected'      :
    item.status === 'needs_auth' ? 'needs_auth'     :
    'not_configured';

  return {
    id:               item.system_id,
    name:             item.name,
    category:         display.category,
    group,
    connectionStatus,
    logoInitials:     display.logoInitials,
    logoColor:        display.logoColor,
    isSalesforce:     display.isSalesforce ?? false,
  };
}

function getRecommendationReason(
  systemId: string,
  focusId:  FocusId | null,
): string | undefined {
  if (!focusId) return undefined;

  const map: Partial<Record<string, Partial<Record<FocusId, string>>>> = {
    jira: {
      member_customer_service: 'Recommended for workflow signals',
      core_operations:         'Recommended for workflow signals',
      approvals_compliance:    'Recommended for compliance signals',
      cross_system_handoffs:   'Recommended for handoff signals',
      back_office_productivity:'Recommended for backlog signals',
      engineering_change:      'Recommended for change signals',
      enterprise_wide:         'Recommended for workflow signals',
    },
    servicenow: {
      member_customer_service: 'Recommended for service signals',
      core_operations:         'Recommended for operational signals',
      approvals_compliance:    'Recommended for compliance signals',
      cross_system_handoffs:   'Recommended for incident signals',
      back_office_productivity:'Recommended for process signals',
      engineering_change:      'Recommended for change signals',
      enterprise_wide:         'Recommended for compliance signals',
    },
    confluence: {
      member_customer_service: 'Recommended for process docs',
      approvals_compliance:    'Recommended for policy docs',
      back_office_productivity:'Recommended for process docs',
      enterprise_wide:         'Recommended for process docs',
    },
    slack: {
      member_customer_service: 'Recommended for comms signals',
      cross_system_handoffs:   'Recommended for comms signals',
      enterprise_wide:         'Recommended for comms signals',
    },
    sharepoint: {
      approvals_compliance:    'Recommended for policy docs',
      back_office_productivity:'Recommended for process docs',
    },
  };

  return map[systemId]?.[focusId];
}

// ── Sub-components ────────────────────────────────────────────────────────────

function GroupLabel({ label }: { label: string }) {
  return (
    <div className="text-xs font-semibold uppercase tracking-widest text-muted">
      {label}
    </div>
  );
}

function SubGroupHeader({
  icon, label, count,
}: { icon: React.ReactNode; label: string; count: number }) {
  return (
    <div className="mb-3 flex items-start justify-between gap-3">
      <div className="flex min-w-0 items-center gap-2">
        <span className="shrink-0 text-accent" aria-hidden>{icon}</span>
        <span className="min-w-0 text-sm font-semibold text-text">{label}</span>
      </div>
      {count > 0 && (
        <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-0.5 text-xs font-semibold leading-5 text-emerald-600">
          <CircleCheck size={13} strokeWidth={2.2} aria-hidden="true" />
          {count} selected
        </span>
      )}
    </div>
  );
}

function ConnectionStatusLegend() {
  const items = [
    { color: 'bg-emerald-500', label: 'Connected' },
    { color: 'bg-amber-400',   label: 'Credentials needed' },
    { color: 'bg-slate-300',   label: 'Not yet configured' },
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
      {items.map(item => (
        <div key={item.label} className="flex items-center gap-1.5">
          <div className={`h-2 w-2 rounded-full ${item.color}`} aria-hidden />
          <span className="text-xs text-muted">{item.label}</span>
        </div>
      ))}
      <span className="text-xs text-muted">
        Authentication for unconnected systems is managed in{' '}
        <span className="text-accent">Integration Hub</span>.
      </span>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

interface Props {
  setupState: ReturnType<typeof useSetupState>;
  catalog:    WorkspaceCatalogResponse | null;
}

export default function YourSystemsPage({ setupState, catalog }: Props) {
  const {
    state,
    toggleSystem,
    goTo,
    canProceedFromStep2,
  } = setupState;

  const navigate = useNavigate();

  // ── Section 1: Build catalog-driven system groups ─────────────────────────
  const catalogGroups = useMemo(() => {
    if (!catalog) return [];
    return GROUPS
      .map(groupDef => ({
        groupDef,
        systems: (catalog[groupDef.categoryKey] as CatalogSystemItem[]).map(item =>
          catalogItemToSystemCard(item, groupDef.group)
        ),
      }))
      .filter(g => g.systems.length > 0);
  }, [catalog]);

  const totalConnected = useMemo(() =>
    catalogGroups.reduce((sum, g) => sum + g.systems.length, 0),
  [catalogGroups]);

  // ── Section 2: Recommended additions from missing_categories ─────────────
  const recommendedAdditions = useMemo(() => {
    if (!catalog) return [];
    return catalog.missing_categories
      .map(cat => MISSING_CATEGORY_ADDITIONS[cat])
      .filter(Boolean) as RecommendedAddition[];
  }, [catalog]);

  const countInGroup = (systems: SystemCardType[]) =>
    systems.filter(s => state.selectedSystemIds.includes(s.id)).length;

  const totalSelected = state.selectedSystemIds.length;

  // ── Empty catalog state ───────────────────────────────────────────────────
  // AC10: No systems connected → prompt to connect in Integration Hub
  if (!catalog || totalConnected === 0) {
    return (
      <div className="space-y-5">
        <GroupLabel label="Your connected systems" />
        <section className="rounded-xl border border-border bg-panel p-8 shadow-sm text-center">
          <Building2 size={32} className="mx-auto mb-3 text-muted" aria-hidden />
          <p className="text-sm font-medium text-text mb-1">
            No systems connected yet.
          </p>
          <p className="text-xs text-muted mb-4 leading-relaxed">
            Connect your systems in Integration Hub to start building your discovery stack.
          </p>
          <Button
            variant="tertiary"
            onClick={() => navigate('/integration-hub')}
          >
            Go to Integration Hub
          </Button>
        </section>

        <div className="rounded-xl border border-border bg-panel p-4 shadow-sm">
          <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            <Button variant="tertiary" onClick={() => goTo(1)} className="gap-2">
              <ArrowLeft size={16} strokeWidth={2.2} aria-hidden />
              Back
            </Button>
            <span className="text-sm text-muted">0 systems selected</span>
            <Button variant="tertiary" disabled className="gap-2">
              Continue to source weighting
              <MoveRight size={16} strokeWidth={2.2} aria-hidden />
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5">

      {/* ── Section 1: Connected systems ──────────────────────────────────── */}
      <GroupLabel label="Your connected systems" />

      {catalogGroups.map(({ groupDef, systems }) => (
        <section
          key={groupDef.categoryKey}
          className="rounded-xl border border-border bg-panel p-5 shadow-sm"
        >
          <SubGroupHeader
            icon={groupDef.icon}
            label={groupDef.subLabel}
            count={countInGroup(systems)}
          />

          <div
            role="group"
            aria-label={groupDef.subLabel}
            className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4"
          >
            {systems.map(system => {
              // Normalize jira_confluence to jira for recommendation lookup
              const recommendationSystemId = system.id === 'jira_confluence' ? 'jira' : system.id;
              return (
                <SystemCard
                  key={system.id}
                  system={system}
                  selected={state.selectedSystemIds.includes(system.id)}
                  recommendationReason={getRecommendationReason(recommendationSystemId, state.focusId)}
                  onToggle={toggleSystem}
                />
              );
            })}
          </div>
        </section>
      ))}

      {/* ── Section 2: Recommended additions ─────────────────────────────── */}
      {recommendedAdditions.length > 0 && (
        <>
          <GroupLabel label="Recommended additions" />

          {recommendedAdditions.map(addition => (
            <section
              key={addition.categoryId}
              className="rounded-xl border border-border bg-panel p-5 shadow-sm"
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-sm font-semibold text-text mb-1">
                    {addition.label}
                  </div>
                  <div className="text-xs text-muted leading-relaxed mb-3">
                    {addition.reason}
                  </div>

                  {/* Vendor suggestions — not a full catalog */}
                  <div className="flex flex-wrap gap-2 mb-4">
                    {addition.suggestions.map(vendor => (
                      <span
                        key={vendor}
                        className="rounded-full border border-border bg-panel px-3 py-1 text-xs text-muted"
                      >
                        {vendor}
                      </span>
                    ))}
                  </div>

                  {/* CTA — ENG-SB-1 AC6: navigates with ?category= param */}
                  <button
                    type="button"
                    onClick={() =>
                      navigate(`/integration-hub?category=${addition.categoryId}`)
                    }
                    className={[
                      'flex items-center gap-1.5 text-xs text-accent',
                      'hover:underline focus:outline-none',
                      'focus:ring-2 focus:ring-accent/50 rounded',
                    ].join(' ')}
                  >
                    <PlusCircle size={13} strokeWidth={1.8} aria-hidden />
                    Connect in Integration Hub
                  </button>
                </div>
              </div>
            </section>
          ))}
        </>
      )}

      {/* ── Connection status legend ──────────────────────────────────────── */}
      <section className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <ConnectionStatusLegend />
      </section>

      {/* ── Gate message ─────────────────────────────────────────────────── */}
      <div
        role="alert"
        aria-live="polite"
        className="rounded-xl border border-accent/30 bg-accent/10 px-4 py-3"
      >
        <div className="flex items-start gap-2">
          <Info size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden />
          <p className="text-sm leading-relaxed text-blue-100">
            At least one primary business platform must be selected before continuing.
            Authentication for unconnected systems is managed separately in Integration Hub.
          </p>
        </div>
      </div>

      {/* ── Navigation ───────────────────────────────────────────────────── */}
      <div className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <Button variant="tertiary" onClick={() => goTo(1)} className="gap-2">
            <ArrowLeft size={16} strokeWidth={2.2} aria-hidden />
            Back
          </Button>

          <span
            className="text-sm text-muted"
            aria-live="polite"
            aria-label={`${totalSelected} systems selected`}
          >
            {totalSelected} system{totalSelected !== 1 ? 's' : ''} selected
          </span>

          <Button
            variant="tertiary"
            onClick={() => canProceedFromStep2 && goTo(3)}
            disabled={!canProceedFromStep2}
            className="gap-2"
          >
            Continue to source weighting
            <MoveRight size={16} strokeWidth={2.2} aria-hidden />
          </Button>
        </div>
      </div>
    </div>
  );
}
