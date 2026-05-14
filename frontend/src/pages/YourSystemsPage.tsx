/**
 * YourSystemsScreen — Screen 2 of 4
 * SB-9 Task 9 Sprint 7
 *
 * "Which systems should AgentIQ use to understand your operation?"
 *
 * Layout (top to bottom):
 *   1. StackBuilderProgressBar
 *   2. Page title + subtext
 *   3. DiscoveryConfidenceBar
 *   4. GROUP A — PRIMARY BUSINESS PLATFORMS
 *      4a. "Where your core operation runs" — single-select primary platform
 *          → Salesforce cloud expansion panel (when isSalesforce=true selected)
 *      4b. "Other platforms involved in the same workflows" — multi-select (optional)
 *   5. GROUP B — OPERATIONAL SYSTEMS
 *      5a. Work tracking & operations — SystemCard grid
 *      5b. Communications & knowledge — SystemCard grid
 *   6. GROUP C — DATA & ENGINEERING SOURCES
 *      6a. Code & engineering — SystemCard grid
 *      6b. Data & infrastructure — SystemCard grid
 *   7. ConnectionStatusLegend (corrected: not_configured dot bg-slate-300)
 *   8. Gate message (blue info box, always visible)
 *   9. Sticky bottom nav: Back | N systems selected | Continue
 *
 * Primary platform selection:
 *   Single-select enforced at Screen 2 level.
 *   Selecting a new primary deselects the previous primary via toggleSystem.
 *   The hook's toggleSystem handles both add and remove.
 *   Screen 2 calls toggleSystem(prev) then toggleSystem(next) on primary switch.
 *
 * Salesforce cloud expansion:
 *   Renders when the selected primary platform has isSalesforce=true.
 *   Uses toggleSalesforceCloud from useSetupState.
 *   Cloud selection maps to system IDs used in weightings:
 *     salesforce_pss, salesforce_sc, salesforce_ncino, salesforce_fsc,
 *     salesforce_rc, salesforce_hc
 *
 * Recommendation reasons:
 *   Derived from state.focusId at render time.
 *   Function getRecommendationReason(systemId, focusId) returns a string
 *   or undefined. SystemCard renders the string when provided.
 *
 * ConnectionStatusLegend correction (SB-9):
 *   bg-slate-600 → bg-slate-300 for not_configured dot.
 *   ConnectionStatusLegend is corrected in this task since it only renders
 *   on Screen 2. Tracked as a documentation note — component file will be
 *   updated separately in the component cleanup pass.
 *
 * Accessibility:
 *   Group A primary section: role="radiogroup" aria-label="Primary business platform"
 *   Group A additional section: role="group" aria-label="Additional platforms"
 *   Group B/C sub-groups: role="group" with aria-label per sub-group
 *   Gate message: role="alert" aria-live="polite"
 *   System count in footer: aria-live="polite"
 *
 * Props:
 *   setupState — ReturnType<typeof useSetupState>
 */

import React from 'react';
import {
  SystemCard as SystemCardType, SalesforceCloud, FocusId,
} from '../types/stack_builder';
import {
  StackBuilderProgressBar,
  DiscoveryConfidenceBar,
  SystemCard,
} from '../components/stack_builder';
import { useSetupState } from '../components/stack_builder';

// ── Static system data ────────────────────────────────────────────────────────

const PRIMARY_PLATFORMS: SystemCardType[] = [
  { id: 'sap',         name: 'SAP',         category: 'ERP',             group: 'primary_platform', connectionStatus: 'needs_auth',     logoInitials: 'SAP', logoColor: 'bg-blue-700' },
  { id: 'oracle_ebs',  name: 'Oracle EBS',  category: 'Finance · HR',    group: 'primary_platform', connectionStatus: 'not_configured', logoInitials: 'ORC', logoColor: 'bg-red-700' },
  { id: 'workday',     name: 'Workday',     category: 'HR · finance',     group: 'primary_platform', connectionStatus: 'not_configured', logoInitials: 'WD',  logoColor: 'bg-yellow-600' },
  { id: 'dynamics365', name: 'Dynamics 365',category: 'ERP · CRM',       group: 'primary_platform', connectionStatus: 'not_configured', logoInitials: 'D365',logoColor: 'bg-blue-600' },
  { id: 'salesforce',  name: 'Salesforce',  category: 'CRM · industry',  group: 'primary_platform', connectionStatus: 'connected',      logoInitials: 'SF',  logoColor: 'bg-sky-500',  isSalesforce: true },
  { id: 'neospin',     name: 'Neospin',     category: 'Pension admin',    group: 'primary_platform', connectionStatus: 'not_configured', logoInitials: 'NS',  logoColor: 'bg-teal-700' },
  { id: 'vitech',      name: 'Vitech',      category: 'Benefits admin',   group: 'primary_platform', connectionStatus: 'not_configured', logoInitials: 'VT',  logoColor: 'bg-green-700' },
];

const SALESFORCE_CLOUDS: SalesforceCloud[] = [
  { id: 'salesforce_pss', name: 'Public Sector Solutions / Benefits' },
  { id: 'salesforce_sc',  name: 'Service Cloud' },
  { id: 'salesforce_ncino', name: 'nCino' },
  { id: 'salesforce_fsc', name: 'Financial Services Cloud' },
  { id: 'salesforce_rc',  name: 'Revenue Cloud' },
  { id: 'salesforce_hc',  name: 'Health Cloud' },
];

const ADDITIONAL_PLATFORMS: SystemCardType[] = PRIMARY_PLATFORMS.filter(
  p => !p.isSalesforce  // Salesforce excluded from additional — it's already primary
);

const WORK_TRACKING: SystemCardType[] = [
  { id: 'jira',        name: 'Jira',        category: 'Issues · backlog', group: 'work_tracking', connectionStatus: 'connected',      logoInitials: 'JR',  logoColor: 'bg-blue-600' },
  { id: 'servicenow',  name: 'ServiceNow',  category: 'ITSM · operations',group: 'work_tracking', connectionStatus: 'connected',      logoInitials: 'SN',  logoColor: 'bg-green-700' },
  { id: 'azure_devops',name: 'Azure DevOps',category: 'ALM · CI/CD',      group: 'work_tracking', connectionStatus: 'not_configured', logoInitials: 'ADO', logoColor: 'bg-blue-700' },
  { id: 'linear',      name: 'Linear',      category: 'Product · issues', group: 'work_tracking', connectionStatus: 'not_configured', logoInitials: 'LN',  logoColor: 'bg-violet-600' },
  { id: 'zendesk',     name: 'Zendesk',     category: 'Support',          group: 'work_tracking', connectionStatus: 'not_configured', logoInitials: 'ZD',  logoColor: 'bg-green-600' },
];

const COMMS_KNOWLEDGE: SystemCardType[] = [
  { id: 'slack',      name: 'Slack',           category: 'Messaging',       group: 'comms_knowledge', connectionStatus: 'needs_auth',     logoInitials: 'SL',  logoColor: 'bg-purple-600' },
  { id: 'teams',      name: 'Microsoft Teams', category: 'Comms · docs',    group: 'comms_knowledge', connectionStatus: 'not_configured', logoInitials: 'MS',  logoColor: 'bg-blue-700' },
  { id: 'confluence', name: 'Confluence',      category: 'Docs · knowledge',group: 'comms_knowledge', connectionStatus: 'connected',      logoInitials: 'CF',  logoColor: 'bg-blue-500' },
  { id: 'sharepoint', name: 'SharePoint',      category: 'Docs · intranet', group: 'comms_knowledge', connectionStatus: 'not_configured', logoInitials: 'SP',  logoColor: 'bg-blue-600' },
  { id: 'notion',     name: 'Notion',          category: 'Docs · wiki',     group: 'comms_knowledge', connectionStatus: 'not_configured', logoInitials: 'NO',  logoColor: 'bg-slate-700' },
];

const CODE_ENGINEERING: SystemCardType[] = [
  { id: 'github',      name: 'GitHub',       category: 'Source control', group: 'code_engineering', connectionStatus: 'not_configured', logoInitials: 'GH',  logoColor: 'bg-slate-800' },
  { id: 'gitlab',      name: 'GitLab',       category: 'DevOps',         group: 'code_engineering', connectionStatus: 'not_configured', logoInitials: 'GL',  logoColor: 'bg-orange-600' },
  { id: 'bitbucket',   name: 'Bitbucket',    category: 'Source control', group: 'code_engineering', connectionStatus: 'not_configured', logoInitials: 'BB',  logoColor: 'bg-blue-600' },
  { id: 'azure_repos', name: 'Azure Repos',  category: 'Source control', group: 'code_engineering', connectionStatus: 'not_configured', logoInitials: 'AR',  logoColor: 'bg-blue-700' },
];

const DATA_INFRASTRUCTURE: SystemCardType[] = [
  { id: 'postgresql', name: 'PostgreSQL', category: 'Database',      group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'PG',  logoColor: 'bg-blue-700' },
  { id: 'sql_server', name: 'SQL Server', category: 'Database',      group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'SQL', logoColor: 'bg-red-700' },
  { id: 'oracle_db',  name: 'Oracle DB',  category: 'Database',      group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'ORC', logoColor: 'bg-red-600' },
  { id: 'databricks', name: 'Databricks', category: 'Data platform', group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'DB',  logoColor: 'bg-orange-500' },
  { id: 'snowflake',  name: 'Snowflake',  category: 'Data warehouse', group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'SF',  logoColor: 'bg-sky-500' },
  { id: 'dbt',        name: 'dbt',        category: 'Transforms',    group: 'data_infrastructure', connectionStatus: 'not_configured', logoInitials: 'dbt', logoColor: 'bg-orange-600' },
];

// ── Recommendation reasons ────────────────────────────────────────────────────
// Derived from focusId — returns a short reason string or undefined.
// Shown below the system category tag on SystemCard when provided.

function getRecommendationReason(
  systemId: string,
  focusId: FocusId | null,
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

// ── Group label component ─────────────────────────────────────────────────────

function GroupLabel({ label }: { label: string }) {
  return (
    <div className="text-xs font-medium text-muted uppercase tracking-widest mb-4 mt-8">
      {label}
    </div>
  );
}

// ── Sub-group header ──────────────────────────────────────────────────────────

function SubGroupHeader({
  icon, label, count,
}: { icon: React.ReactNode; label: string; count: number }) {
  return (
    <div className="flex items-center gap-2 mb-3">
      <span className="text-muted" aria-hidden="true">{icon}</span>
      <span className="text-sm font-medium text-text">{label}</span>
      {count > 0 && (
        <span className="text-xs text-emerald-500 font-medium">
          {count} selected
        </span>
      )}
    </div>
  );
}

// ── ConnectionStatusLegend (corrected inline) ─────────────────────────────────
// bg-slate-600 → bg-slate-300 for not_configured.
// Component file correction deferred to component cleanup pass.

function ConnectionStatusLegendCorrected() {
  const items = [
    { color: 'bg-emerald-500', label: 'Connected' },
    { color: 'bg-amber-400',   label: 'Credentials needed' },
    { color: 'bg-slate-300',   label: 'Not yet configured' },
  ];
  return (
    <div className="flex items-center gap-5 mt-4 mb-4 flex-wrap">
      {items.map(item => (
        <div key={item.label} className="flex items-center gap-1.5">
          <div className={`h-2 w-2 rounded-full ${item.color}`} aria-hidden="true" />
          <span className="text-xs text-muted">{item.label}</span>
        </div>
      ))}
      <div className="ml-auto">
        <span className="text-xs text-muted">
          Authentication for unconnected systems is managed in{' '}
          <span className="text-accent underline cursor-pointer">Integration Hub</span>
        </span>
      </div>
    </div>
  );
}

// ── Screen ────────────────────────────────────────────────────────────────────

interface Props {
  setupState: ReturnType<typeof useSetupState>;
}

export default function YourSystemsPage({ setupState }: Props) {
  const {
    state,
    steps,
    confidence,
    toggleSystem,
    toggleSalesforceCloud,
    goTo,
    canProceedFromStep2,
  } = setupState;

  // Derive selected primary platform ID
  const selectedPrimaryId = PRIMARY_PLATFORMS.find(p =>
    state.selectedSystemIds.includes(p.id)
  )?.id ?? null;

  const selectedPrimary = PRIMARY_PLATFORMS.find(p => p.id === selectedPrimaryId) ?? null;
  const salesforceSelected = selectedPrimary?.isSalesforce === true;

  // Handle primary platform selection — single select
  function handlePrimarySelect(systemId: string) {
    if (selectedPrimaryId === systemId) {
      // Deselect current primary
      toggleSystem(systemId);
    } else {
      // Deselect previous primary first, then select new
      if (selectedPrimaryId) toggleSystem(selectedPrimaryId);
      toggleSystem(systemId);
    }
  }

  // Additional platforms — exclude selected primary and Salesforce base id
  const availableAdditional = ADDITIONAL_PLATFORMS.filter(
    p => p.id !== selectedPrimaryId && p.id !== 'salesforce'
  );

  // Counts per sub-group
  const countInGroup = (systems: SystemCardType[]) =>
    systems.filter(s => state.selectedSystemIds.includes(s.id)).length;

  const totalSelected = state.selectedSystemIds.length
    + (salesforceSelected ? state.selectedSalesforceClouds.length : 0);

  return (
    <div className="min-h-screen bg-paper">
      <div className="max-w-3xl mx-auto px-6 py-8">

        {/* ── Progress bar ── */}
        <StackBuilderProgressBar steps={steps} />

        {/* ── Page title ── */}
        <h1 className="text-2xl font-bold text-text mb-2">
          Which systems should AgentIQ use to understand your operation?
        </h1>
        <p className="text-sm text-muted leading-relaxed mb-6">
          Map the systems that reflect how your operation actually runs — where work
          happens, where signals exist, and where knowledge is stored.
        </p>

        {/* ── Discovery confidence bar ── */}
        <DiscoveryConfidenceBar state={confidence} />

        {/* ══ GROUP A — PRIMARY BUSINESS PLATFORMS ══ */}
        <GroupLabel label="Group A — Primary business platforms" />

        {/* Group A Card 1: Where your core operation runs */}
        <div className="rounded-xl border border-border bg-panel p-5 mb-3">
          <h2 className="text-sm font-semibold text-text mb-1">
            Where your core operation runs
          </h2>
          <p className="text-xs text-muted leading-relaxed mb-4">
            Select the platform where your primary business workflows live.
            If you select Salesforce, you will be asked which products you use.
          </p>

          <div
            role="radiogroup"
            aria-label="Primary business platform"
            className="grid grid-cols-4 gap-2 mb-3"
          >
            {PRIMARY_PLATFORMS.map(system => (
              <SystemCard
                key={system.id}
                system={system}
                selected={state.selectedSystemIds.includes(system.id)}
                recommendationReason={getRecommendationReason(system.id, state.focusId)}
                onToggle={handlePrimarySelect}
              />
            ))}
          </div>

          {/* Salesforce cloud expansion panel */}
          {salesforceSelected && (
            <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/[0.04] p-4 mt-2">
              <div className="text-xs font-medium text-emerald-600 uppercase tracking-wide mb-3">
                Salesforce — which products do you use?
              </div>
              <div
                role="group"
                aria-label="Salesforce products"
                className="flex flex-wrap gap-2"
              >
                {SALESFORCE_CLOUDS.map(cloud => (
                  <button
                    key={cloud.id}
                    type="button"
                    onClick={() => toggleSalesforceCloud(cloud.id)}
                    className={[
                      'inline-flex items-center rounded-full border px-3 py-1.5',
                      'text-sm font-medium transition-colors cursor-pointer',
                      'focus:outline-none focus:ring-2 focus:ring-emerald-500/50',
                      state.selectedSalesforceClouds.includes(cloud.id)
                        ? 'border-emerald-500 bg-emerald-500/15 text-emerald-600'
                        : 'border-border bg-panel text-muted hover:border-emerald-500/50 hover:text-text',
                    ].join(' ')}
                    aria-pressed={state.selectedSalesforceClouds.includes(cloud.id)}
                  >
                    {cloud.name}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Group A Card 2: Other platforms — optional */}
        <div className="rounded-xl border border-border bg-panel p-5 mb-2">
          <div className="flex items-center gap-2 mb-1">
            <h2 className="text-sm font-semibold text-text">
              Other platforms involved in the same workflows
            </h2>
            <span className="text-xs text-muted">— optional</span>
          </div>
          <p className="text-xs text-muted leading-relaxed mb-4">
            Add any other operational platforms that are materially part of the same
            workflows you want to analyse.
          </p>

          <div
            role="group"
            aria-label="Additional platforms"
            className="grid grid-cols-4 gap-2"
          >
            {availableAdditional.map(system => (
              <SystemCard
                key={system.id}
                system={system}
                selected={state.selectedSystemIds.includes(system.id)}
                onToggle={toggleSystem}
              />
            ))}
          </div>
        </div>

        {/* ══ GROUP B — OPERATIONAL SYSTEMS ══ */}
        <GroupLabel label="Group B — Operational systems" />

        {/* Work tracking & operations */}
        <div className="mb-4">
          <SubGroupHeader
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <rect x="1" y="3" width="12" height="2" rx="1" fill="currentColor"/>
                <rect x="1" y="6.5" width="8" height="2" rx="1" fill="currentColor"/>
                <rect x="1" y="10" width="10" height="2" rx="1" fill="currentColor"/>
              </svg>
            }
            label="Work tracking & operations"
            count={countInGroup(WORK_TRACKING)}
          />
          <div
            role="group"
            aria-label="Work tracking and operations systems"
            className="grid grid-cols-4 gap-2"
          >
            {WORK_TRACKING.map(system => (
              <SystemCard
                key={system.id}
                system={system}
                selected={state.selectedSystemIds.includes(system.id)}
                recommendationReason={getRecommendationReason(system.id, state.focusId)}
                onToggle={toggleSystem}
              />
            ))}
          </div>
        </div>

        {/* Communications & knowledge */}
        <div className="mb-2">
          <SubGroupHeader
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M2 3h10a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1H8l-3 2v-2H2a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"
                  stroke="currentColor" strokeWidth="1.2" fill="none"/>
              </svg>
            }
            label="Communications & knowledge"
            count={countInGroup(COMMS_KNOWLEDGE)}
          />
          <div
            role="group"
            aria-label="Communications and knowledge systems"
            className="grid grid-cols-4 gap-2"
          >
            {COMMS_KNOWLEDGE.map(system => (
              <SystemCard
                key={system.id}
                system={system}
                selected={state.selectedSystemIds.includes(system.id)}
                recommendationReason={getRecommendationReason(system.id, state.focusId)}
                onToggle={toggleSystem}
              />
            ))}
          </div>
        </div>

        {/* ══ GROUP C — DATA & ENGINEERING SOURCES ══ */}
        <GroupLabel label="Group C — Data & engineering sources" />

        {/* Code & engineering */}
        <div className="mb-4">
          <SubGroupHeader
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M4 4L1 7l3 3M10 4l3 3-3 3M8 2l-2 10"
                  stroke="currentColor" strokeWidth="1.2"
                  strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            }
            label="Code & engineering"
            count={countInGroup(CODE_ENGINEERING)}
          />
          <div
            role="group"
            aria-label="Code and engineering systems"
            className="grid grid-cols-4 gap-2"
          >
            {CODE_ENGINEERING.map(system => (
              <SystemCard
                key={system.id}
                system={system}
                selected={state.selectedSystemIds.includes(system.id)}
                onToggle={toggleSystem}
              />
            ))}
          </div>
        </div>

        {/* Data & infrastructure */}
        <div className="mb-4">
          <SubGroupHeader
            icon={
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <ellipse cx="7" cy="4" rx="5" ry="2" stroke="currentColor" strokeWidth="1.2"/>
                <path d="M2 4v3c0 1.1 2.24 2 5 2s5-.9 5-2V4"
                  stroke="currentColor" strokeWidth="1.2"/>
                <path d="M2 7v3c0 1.1 2.24 2 5 2s5-.9 5-2V7"
                  stroke="currentColor" strokeWidth="1.2"/>
              </svg>
            }
            label="Data & infrastructure"
            count={countInGroup(DATA_INFRASTRUCTURE)}
          />
          <div
            role="group"
            aria-label="Data and infrastructure systems"
            className="grid grid-cols-4 gap-2"
          >
            {DATA_INFRASTRUCTURE.map(system => (
              <SystemCard
                key={system.id}
                system={system}
                selected={state.selectedSystemIds.includes(system.id)}
                onToggle={toggleSystem}
              />
            ))}
          </div>
        </div>

        {/* ── Connection status legend (corrected) ── */}
        <ConnectionStatusLegendCorrected />

        {/* ── Gate message ── */}
        <div
          role="alert"
          aria-live="polite"
          className="rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 mb-6"
        >
          <div className="flex items-start gap-2">
            <svg className="flex-shrink-0 text-blue-500 mt-0.5" width="14" height="14"
              viewBox="0 0 14 14" fill="none" aria-hidden="true">
              <circle cx="7" cy="7" r="6" stroke="currentColor" strokeWidth="1.2"/>
              <path d="M7 6v4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
              <circle cx="7" cy="4.5" r="0.6" fill="currentColor"/>
            </svg>
            <p className="text-xs text-blue-700 leading-relaxed">
              At least one primary business platform must be selected before continuing.
              Authentication for unconnected systems is managed separately in Integration Hub.
            </p>
          </div>
        </div>

      </div>

      {/* ── Sticky bottom navigation ── */}
      <div className="sticky bottom-0 bg-paper border-t border-border">
        <div className="max-w-3xl mx-auto px-6 py-4 flex items-center justify-between">

          <button
            type="button"
            onClick={() => goTo(1)}
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

          <span
            className="text-sm text-muted"
            aria-live="polite"
            aria-label={`${totalSelected} systems selected`}
          >
            {totalSelected} system{totalSelected !== 1 ? 's' : ''} selected
          </span>

          <button
            type="button"
            onClick={() => canProceedFromStep2 && goTo(3)}
            disabled={!canProceedFromStep2}
            aria-disabled={!canProceedFromStep2}
            className={[
              'inline-flex items-center gap-2 rounded-lg px-5 py-2.5',
              'text-sm font-medium transition-colors',
              canProceedFromStep2
                ? 'bg-white text-gray-900 hover:opacity-90 cursor-pointer' // Correct enabled state
                // FIX: Use a dark, muted background with lighter text for the disabled state
                : 'bg-slate-800 text-slate-500 cursor-not-allowed',
              'focus:outline-none focus:ring-2 focus:ring-emerald-500/50',
            ].join(' ')}
          >
            Continue to source weighting
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
              <path d="M3 7h8M8 4l3 3-3 3" stroke="currentColor" strokeWidth="1.5"
                strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </button>

        </div>
      </div>
    </div>
  );
}
