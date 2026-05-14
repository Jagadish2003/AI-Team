/**
 * DiscoveryFocusPage — Screen 1 of 4
 * SB-8 Task 8 Sprint 7
 *
 * "Where should AgentIQ look first?"
 *
 * Layout (top to bottom):
 *   1. Page title + subtext
 *   2. "SELECT A DISCOVERY FOCUS" label + required tag
 *   3. 2-column FocusCard grid (7 cards — last full-width)
 *   4. Two-column optional panel row:
 *      Left  — Industry selector (IndustryId pills, single select)
 *      Right — Template selector (TemplateId pills, single select)
 *   5. TemplateNoteBlock — conditional on templateId
 *   6. Bottom navigation bar:
 *      Left  — info note
 *      Right — "Continue to your systems →" (disabled until focusId set)
 *
 * No StackBuilderProgressBar on Screen 1 — step 1 is active.
 * Progress bar renders from Screen 2 onward.
 *
 * State:
 *   All state managed by useSetupState hook (passed as prop or consumed
 *   from context — this implementation accepts the hook return as props
 *   to keep the screen testable without context setup).
 *
 * suggestedFocus flag (SB-8):
 *   suggestedFocus is read from the selected StackTemplate object directly
 *   (TEMPLATES.find(t => t.id === state.templateId)?.suggestedFocus).
 *   It is NOT defined as a separate TEMPLATE_SUGGESTED_FOCUS constant.
 *
 * Template preselected systems:
 *   setTemplate(id, template.preselectedSystems) — passes the system IDs
 *   from the selected template to the hook, which merges them into
 *   selectedSystemIds and seeds their default weightings.
 *
 * Accessibility:
 *   FocusCard group: role="radiogroup" aria-label="Discovery focus"
 *   Industry pills: role="group" aria-label="Industry"
 *   Template pills: role="group" aria-label="Start from a template"
 *   Continue button: aria-disabled when canProceedFromStep1=false
 *   Required tag: aria-label="required"
 */

import React from 'react';
import {
  FocusId, FocusCard as FocusCardType, IndustryId, Industry,
  TemplateId, StackTemplate,
} from '../types/stack_builder';
import {
  FocusCard, PillTag, TemplateNoteBlock,
} from '../components/stack_builder';
import { useSetupState } from '../components/stack_builder';

// ── Static data ───────────────────────────────────────────────────────────────

const FOCUS_CARDS: FocusCardType[] = [
  {
    id: 'member_customer_service',
    title: 'Member / customer service',
    subtext: 'Service requests, status updates, escalations, and backlogs — where member or customer outcomes are at stake',
    icon: 'ti-users',
  },
  {
    id: 'core_operations',
    title: 'Core operations',
    subtext: 'Where work moves through your organisation — queue management, handoffs, throughput, and processing delays. No specific compliance weighting.',
    icon: 'ti-settings',
  },
  {
    id: 'approvals_compliance',
    title: 'Approvals / compliance',
    subtext: 'Where regulatory obligations govern how work is processed — approval gates, compliance deadlines, audit trails. Compliance signals are weighted highest.',
    icon: 'ti-shield',
  },
  {
    id: 'cross_system_handoffs',
    title: 'Cross-system handoffs',
    subtext: 'Work getting lost between systems, duplicate effort, ownership friction, and integration-point failures',
    icon: 'ti-switch-horizontal',
  },
  {
    id: 'back_office_productivity',
    title: 'Back-office productivity',
    subtext: 'Repetitive manual work, checklist stalls, and admin-heavy processes where automation delivers the clearest return',
    icon: 'ti-list-check',
  },
  {
    id: 'engineering_change',
    title: 'Engineering / change',
    subtext: 'Change coordination, delivery friction, backlog signals, and release-related bottlenecks across engineering and operations',
    icon: 'ti-git-branch',
  },
  {
    id: 'enterprise_wide',
    title: 'Enterprise-wide discovery',
    subtext: 'Broad discovery with no strong operating lens preselected. AgentIQ surfaces signals across all connected systems and ranks opportunities by impact.',
    icon: 'ti-world',
    wide: true,
  },
];

const INDUSTRIES: Industry[] = [
  { id: 'financial_services',   label: 'Financial services' },
  { id: 'public_sector',        label: 'Public sector' },
  { id: 'logistics_supply_chain', label: 'Logistics & supply chain' },
  { id: 'retail_commerce',      label: 'Retail & commerce' },
  { id: 'healthcare',           label: 'Healthcare' },
  { id: 'energy_utilities',     label: 'Energy & utilities' },
  { id: 'manufacturing',        label: 'Manufacturing' },
  { id: 'technology',           label: 'Technology' },
];

const TEMPLATES: StackTemplate[] = [
  {
    id: 'commercial_lending',
    label: 'Commercial lending',
    suggestedFocus: 'approvals_compliance',
    preselectedSystems: ['salesforce_ncino', 'jira', 'servicenow', 'confluence'],
  },
  {
    id: 'public_retirement',
    label: 'Public retirement',
    suggestedFocus: 'approvals_compliance',
    preselectedSystems: ['salesforce_pss', 'jira', 'servicenow', 'confluence'],
  },
  {
    id: 'service_operations',
    label: 'Service operations',
    suggestedFocus: 'member_customer_service',
    preselectedSystems: ['salesforce_sc', 'servicenow', 'confluence'],
  },
  {
    id: 'revenue_operations',
    label: 'Revenue operations',
    suggestedFocus: 'core_operations',
    preselectedSystems: ['salesforce_rc', 'jira', 'confluence'],
  },
];

// ── Screen ────────────────────────────────────────────────────────────────────

interface Props {
  // Accept hook return as props for testability.
  // In production, wire via context or pass directly from the parent router.
  setupState: ReturnType<typeof useSetupState>;
}

export default function DiscoveryFocusPage({ setupState }: Props) {
  const {
    state,
    setFocus,
    setIndustry,
    setTemplate,
    goTo,
    canProceedFromStep1,
  } = setupState;

  // suggestedFocus read from the StackTemplate object — not a separate constant.
  // SB-8 flag: do not define TEMPLATE_SUGGESTED_FOCUS as a standalone lookup.
  const selectedTemplate = TEMPLATES.find(t => t.id === state.templateId) ?? null;

  function handleTemplateSelect(templateId: TemplateId) {
    if (state.templateId === templateId) {
      // Deselect — clear template, keep systems already added
      setTemplate(null, []);
    } else {
      const tmpl = TEMPLATES.find(t => t.id === templateId);
      setTemplate(templateId, tmpl?.preselectedSystems ?? []);
    }
  }

  function handleContinue() {
    if (!canProceedFromStep1) return;
    goTo(2);
  }

  return (
    <div className="min-h-screen bg-paper">
      <div className="max-w-3xl mx-auto px-6 py-10">

        {/* ── Page title ── */}
        <h1 className="text-2xl font-bold text-text mb-2">
          Where should AgentIQ look first?
        </h1>
        <p className="text-sm text-muted leading-relaxed mb-8">
          AgentIQ always assesses AI readiness across your operation. This step tells us
          which workflows and signals to prioritise first.
        </p>

        {/* ── Focus selection label ── */}
        <div className="flex items-center gap-2 mb-3">
          <span className="text-xs font-medium text-muted uppercase tracking-wide">
            Select a discovery focus
          </span>
          <span
            className="text-xs text-red-500 font-medium"
            aria-label="required"
          >
            — required
          </span>
        </div>

        {/* ── Focus card grid ── */}
        <div
          role="radiogroup"
          aria-label="Discovery focus"
          className="grid grid-cols-2 gap-3 mb-8"
        >
          {FOCUS_CARDS.map(card => (
            <FocusCard
              key={card.id}
              card={card}
              selected={state.focusId === card.id}
              onSelect={setFocus}
            />
          ))}
        </div>

        {/* ── Optional panels row ── */}
        <div className="grid grid-cols-2 gap-3 mb-2">

          {/* Industry panel */}
          <div className="rounded-lg border border-border bg-panel p-4">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-sm font-medium text-text">Industry</span>
              <span className="text-xs text-muted">— optional</span>
            </div>
            <p className="text-xs text-muted leading-relaxed mb-3">
              Helps AgentIQ surface relevant systems and adapt signal language to your
              operating context.
            </p>
            <div
              role="group"
              aria-label="Industry"
              className="flex flex-wrap gap-2"
            >
              {INDUSTRIES.map(ind => (
                <PillTag
                  key={ind.id}
                  label={ind.label}
                  selected={state.industryId === ind.id}
                  onToggle={() =>
                    setIndustry(state.industryId === ind.id ? null : ind.id)
                  }
                />
              ))}
            </div>
          </div>

          {/* Template panel */}
          <div className="rounded-lg border border-border bg-panel p-4">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-sm font-medium text-text">Start from a template</span>
              <span className="text-xs text-muted">— optional</span>
            </div>
            <p className="text-xs text-muted leading-relaxed mb-3">
              Templates preselect systems for common operating models. Choose one to
              accelerate setup.
            </p>
            <div
              role="group"
              aria-label="Start from a template"
              className="flex flex-wrap gap-2"
            >
              {TEMPLATES.map(tmpl => (
                <PillTag
                  key={tmpl.id}
                  label={tmpl.label}
                  selected={state.templateId === tmpl.id}
                  onToggle={() => handleTemplateSelect(tmpl.id)}
                />
              ))}
            </div>

            {/* TemplateNoteBlock — suggestedFocus from the StackTemplate object */}
            {selectedTemplate && (
              <TemplateNoteBlock
                templateId={selectedTemplate.id}
                suggestedFocus={selectedTemplate.suggestedFocus}
              />
            )}
          </div>
        </div>

        {/* ── Bottom navigation bar ── */}
        <div className="sticky bottom-0 bg-paper border-t border-border mt-8 -mx-6 px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-2 text-xs text-muted">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true"
              className="flex-shrink-0 text-muted">
              <circle cx="7" cy="7" r="6" stroke="currentColor" strokeWidth="1.2"/>
              <path d="M7 6v4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
              <circle cx="7" cy="4.5" r="0.6" fill="currentColor"/>
            </svg>
            Discovery focus is required. Industry and template are optional accelerators.
          </div>

          <button
            type="button"
            onClick={handleContinue}
            disabled={!canProceedFromStep1}
            aria-disabled={!canProceedFromStep1}
            className={[
              'inline-flex items-center gap-2 rounded-lg px-5 py-2.5',
              'text-sm font-medium transition-colors',
              canProceedFromStep1
                ? 'bg-white text-gray-900 hover:opacity-90 cursor-pointer' // Correct enabled state
                // FIX: Use a dark, muted background with lighter text for the disabled state
                : 'bg-slate-800 text-slate-500 cursor-not-allowed',
              'focus:outline-none focus:ring-2 focus:ring-emerald-500/50',
            ].join(' ')}
          >
            Continue to your systems
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
