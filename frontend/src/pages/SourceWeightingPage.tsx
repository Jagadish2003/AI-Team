/**
 * SourceWeightingScreen — Screen 3 of 4
 * SB-10 Task 10 Sprint 7
 *
 * "How should AgentIQ treat each system?"
 *
 * Layout (top to bottom):
 *   1. StackBuilderProgressBar
 *   2. Page title + subtext
 *   3. DiscoveryConfidenceBar
 *   4. SystemWeightingCard — one per selected system ID
 *      Smart defaults already populated from useSetupState.
 *      User confirms or adjusts role / priority / workflow focus per system.
 *   5. Engineering info block (conditional — when showEngineeringRole=true)
 *   6. Sticky bottom nav: Back | Continue to discovery plan
 *
 * No proceed gate on Screen 3:
 *   "Continue to discovery plan →" is always enabled.
 *   All systems have default weightings seeded by the hook.
 *   Confirming weightings is encouraged but not required to proceed.
 *   The confidence bar reflects confirmation progress in real time.
 *
 * System metadata:
 *   SYSTEM_META maps every possible system ID to { name, logoInitials, logoColor }.
 *   Covers all systems from Screen 2 plus Salesforce cloud IDs added via
 *   template preselection. SystemWeightingCard receives these as props.
 *
 * updateWeighting API:
 *   useSetupState exposes updateWeighting(updated: SystemWeighting).
 *   onChange prop on SystemWeightingCard maps directly:
 *   onChange={updateWeighting}
 *
 * Salesforce cloud system IDs:
 *   When a template preselects systems, Salesforce cloud IDs
 *   (salesforce_pss, salesforce_sc etc.) appear in selectedSystemIds.
 *   SYSTEM_META covers all six cloud IDs so WeightingCards render correctly
 *   even when the user arrives via template rather than manual selection.
 *
 * Accessibility:
 *   Each SystemWeightingCard header is a <button> (handled by component).
 *   Engineering info block: role="note".
 *   Sticky nav: Back button goTo(2), Continue button goTo(4).
 *
 * Props:
 *   setupState — ReturnType<typeof useSetupState>
 */

import React from 'react';
import {
  StackBuilderProgressBar,
  DiscoveryConfidenceBar,
  SystemWeightingCard,
} from '../components/stack_builder';
import { useSetupState } from '../components/stack_builder';

// ── System metadata ───────────────────────────────────────────────────────────
// Maps every system ID that can appear in selectedSystemIds to display data.
// Covers Screen 2 manual selections + template preselected cloud IDs.

const SYSTEM_META: Record<string, { name: string; logoInitials: string; logoColor: string }> = {
  // Primary platforms
  sap:          { name: 'SAP',          logoInitials: 'SAP', logoColor: 'bg-blue-700' },
  oracle_ebs:   { name: 'Oracle EBS',   logoInitials: 'ORC', logoColor: 'bg-red-700' },
  workday:      { name: 'Workday',      logoInitials: 'WD',  logoColor: 'bg-yellow-600' },
  dynamics365:  { name: 'Dynamics 365', logoInitials: 'D365',logoColor: 'bg-blue-600' },
  salesforce:   { name: 'Salesforce',   logoInitials: 'SF',  logoColor: 'bg-sky-500' },
  neospin:      { name: 'Neospin',      logoInitials: 'NS',  logoColor: 'bg-teal-700' },
  vitech:       { name: 'Vitech',       logoInitials: 'VT',  logoColor: 'bg-green-700' },

  // Salesforce cloud IDs (template preselection + manual cloud picker)
  salesforce_pss:   { name: 'Salesforce — Public Sector Solutions', logoInitials: 'SF', logoColor: 'bg-sky-500' },
  salesforce_sc:    { name: 'Salesforce — Service Cloud',           logoInitials: 'SF', logoColor: 'bg-sky-500' },
  salesforce_ncino: { name: 'Salesforce — nCino',                   logoInitials: 'SF', logoColor: 'bg-sky-500' },
  salesforce_fsc:   { name: 'Salesforce — Financial Services Cloud', logoInitials: 'SF', logoColor: 'bg-sky-500' },
  salesforce_rc:    { name: 'Salesforce — Revenue Cloud',            logoInitials: 'SF', logoColor: 'bg-sky-500' },
  salesforce_hc:    { name: 'Salesforce — Health Cloud',             logoInitials: 'SF', logoColor: 'bg-sky-500' },

  // Work tracking & operations
  jira:         { name: 'Jira',         logoInitials: 'JR',  logoColor: 'bg-blue-600' },
  servicenow:   { name: 'ServiceNow',   logoInitials: 'SN',  logoColor: 'bg-green-700' },
  azure_devops: { name: 'Azure DevOps', logoInitials: 'ADO', logoColor: 'bg-blue-700' },
  linear:       { name: 'Linear',       logoInitials: 'LN',  logoColor: 'bg-violet-600' },
  zendesk:      { name: 'Zendesk',      logoInitials: 'ZD',  logoColor: 'bg-green-600' },

  // Communications & knowledge
  slack:      { name: 'Slack',           logoInitials: 'SL',  logoColor: 'bg-purple-600' },
  teams:      { name: 'Microsoft Teams', logoInitials: 'MS',  logoColor: 'bg-blue-700' },
  confluence: { name: 'Confluence',      logoInitials: 'CF',  logoColor: 'bg-blue-500' },
  sharepoint: { name: 'SharePoint',      logoInitials: 'SP',  logoColor: 'bg-blue-600' },
  notion:     { name: 'Notion',          logoInitials: 'NO',  logoColor: 'bg-slate-700' },

  // Code & engineering
  github:      { name: 'GitHub',      logoInitials: 'GH',  logoColor: 'bg-slate-800' },
  gitlab:      { name: 'GitLab',      logoInitials: 'GL',  logoColor: 'bg-orange-600' },
  bitbucket:   { name: 'Bitbucket',   logoInitials: 'BB',  logoColor: 'bg-blue-600' },
  azure_repos: { name: 'Azure Repos', logoInitials: 'AR',  logoColor: 'bg-blue-700' },

  // Data & infrastructure
  postgresql: { name: 'PostgreSQL', logoInitials: 'PG',  logoColor: 'bg-blue-700' },
  sql_server: { name: 'SQL Server', logoInitials: 'SQL', logoColor: 'bg-red-700' },
  oracle_db:  { name: 'Oracle DB',  logoInitials: 'ORC', logoColor: 'bg-red-600' },
  databricks: { name: 'Databricks', logoInitials: 'DB',  logoColor: 'bg-orange-500' },
  snowflake:  { name: 'Snowflake',  logoInitials: 'SF',  logoColor: 'bg-sky-500' },
  dbt:        { name: 'dbt',        logoInitials: 'dbt', logoColor: 'bg-orange-600' },
};

// Fallback for any system ID not in SYSTEM_META (future-proofing)
function getSystemMeta(id: string) {
  return SYSTEM_META[id] ?? {
    name: id,
    logoInitials: id.slice(0, 2).toUpperCase(),
    logoColor: 'bg-slate-600',
  };
}

// ── Screen ────────────────────────────────────────────────────────────────────

interface Props {
  setupState: ReturnType<typeof useSetupState>;
}

export default function SourceWeightingScreen({ setupState }: Props) {
  const {
    state,
    steps,
    confidence,
    updateWeighting,
    showEngineeringRole,
    goTo,
  } = setupState;

  return (
    <div className="min-h-screen bg-paper">
      <div className="max-w-3xl mx-auto px-6 py-8">

        {/* ── Progress bar ── */}
        <StackBuilderProgressBar steps={steps} />

        {/* ── Page title ── */}
        <h1 className="text-2xl font-bold text-text mb-2">
          How should AgentIQ treat each system?
        </h1>
        <p className="text-sm text-muted leading-relaxed mb-6">
          Confirm how each system contributes to discovery so AgentIQ can weight
          evidence correctly. Smart defaults are already filled — confirm or adjust.
        </p>

        {/* ── Discovery confidence bar ── */}
        <DiscoveryConfidenceBar state={confidence} />

        {/* ── Weighting cards — one per selected system ── */}
        <div className="space-y-3">
          {state.selectedSystemIds.map((id, index) => {
            const meta = getSystemMeta(id);
            const weighting = state.weightings[id];

            // Guard: skip if weighting not yet seeded (should not happen in practice)
            if (!weighting) return null;

            return (
              <SystemWeightingCard
                key={id}
                id={`weighting-card-${id}`}
                systemName={meta.name}
                logoInitials={meta.logoInitials}
                logoColor={meta.logoColor}
                weighting={weighting}
                showEngineeringRole={showEngineeringRole}
                onChange={updateWeighting}
                onConfirm={() => {
                  // Scroll to next unconfirmed card after confirming this one
                  const nextUnconfirmed = state.selectedSystemIds.slice(index + 1).find(
                    nextId => !state.weightings[nextId]?.confirmed
                  );
                  if (nextUnconfirmed) {
                    const el = document.getElementById(`weighting-card-${nextUnconfirmed}`);
                    el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                  }
                }}
              />
            );
          })}
        </div>

        {/* ── Engineering / change system info block ── */}
        {showEngineeringRole && (
          <div
            role="note"
            className="mt-4 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3"
          >
            <div className="flex items-start gap-2">
              <svg className="flex-shrink-0 text-blue-500 mt-0.5" width="14" height="14"
                viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <circle cx="7" cy="7" r="6" stroke="currentColor" strokeWidth="1.2"/>
                <path d="M7 6v4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
                <circle cx="7" cy="4.5" r="0.6" fill="currentColor"/>
              </svg>
              <div>
                <div className="text-xs font-medium text-blue-700 mb-1">
                  When to use Engineering / change system
                </div>
                <p className="text-xs text-blue-600 leading-relaxed">
                  Use this role when a system primarily reflects technical change activity,
                  release work, or engineering backlog — not business workflow execution.
                  Most relevant when GitHub, GitLab, Bitbucket, Azure Repos, or Azure DevOps
                  are selected.
                </p>
              </div>
            </div>
          </div>
        )}

      </div>

      {/* ── Sticky bottom navigation ── */}
      <div className="sticky bottom-0 bg-paper border-t border-border">
        <div className="max-w-3xl mx-auto px-6 py-4 flex items-center justify-between">

          <button
            type="button"
            onClick={() => goTo(2)}
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

          <button
            type="button"
            onClick={() => goTo(4)}
            className={[
              'inline-flex items-center gap-2 rounded-lg px-5 py-2.5',
              'text-sm font-medium bg-text text-paper hover:opacity-90',
              'bg-white text-gray-900 hover:opacity-90 cursor-pointer',
              'focus:outline-none focus:ring-2 focus:ring-emerald-500/50',
            ].join(' ')}
          >
            Continue to discovery plan
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
