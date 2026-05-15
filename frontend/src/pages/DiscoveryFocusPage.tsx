import React from 'react';
import { Info, MoveRight } from 'lucide-react';
import Button from '../components/common/Button';
import {
  FocusCard as FocusCardType,
  Industry,
  TemplateId,
  StackTemplate,
} from '../types/stack_builder';
import {
  FocusCard,
  PillTag,
  TemplateNoteBlock,
} from '../components/stack_builder';
import { useSetupState } from '../components/stack_builder';

const FOCUS_CARDS: FocusCardType[] = [
  {
    id: 'member_customer_service',
    title: 'Member / customer service',
    subtext: 'Service requests, status updates, escalations, and backlogs where member or customer outcomes are at stake.',
    icon: 'ti-users',
  },
  {
    id: 'core_operations',
    title: 'Core operations',
    subtext: 'Queue management, handoffs, throughput, and processing delays across the main operating workflow.',
    icon: 'ti-settings',
  },
  {
    id: 'approvals_compliance',
    title: 'Approvals / compliance',
    subtext: 'Approval gates, compliance deadlines, audit trails, and workflows with regulatory obligations.',
    icon: 'ti-shield',
  },
  {
    id: 'cross_system_handoffs',
    title: 'Cross-system handoffs',
    subtext: 'Work getting lost between systems, duplicate effort, ownership friction, and integration-point failures.',
    icon: 'ti-switch-horizontal',
  },
  {
    id: 'back_office_productivity',
    title: 'Back-office productivity',
    subtext: 'Repetitive manual work, checklist stalls, and admin-heavy processes with clear automation upside.',
    icon: 'ti-list-check',
  },
  {
    id: 'engineering_change',
    title: 'Engineering / change',
    subtext: 'Change coordination, delivery friction, backlog signals, and release-related bottlenecks.',
    icon: 'ti-git-branch',
  },
  {
    id: 'enterprise_wide',
    title: 'Enterprise-wide discovery',
    subtext: 'Broad discovery with no strong operating lens preselected. AgentIQ surfaces signals across all connected systems.',
    icon: 'ti-world',
    wide: true,
  },
];

const INDUSTRIES: Industry[] = [
  { id: 'financial_services', label: 'Financial services' },
  { id: 'public_sector', label: 'Public sector' },
  { id: 'logistics_supply_chain', label: 'Logistics & supply chain' },
  { id: 'retail_commerce', label: 'Retail & commerce' },
  { id: 'healthcare', label: 'Healthcare' },
  { id: 'energy_utilities', label: 'Energy & utilities' },
  { id: 'manufacturing', label: 'Manufacturing' },
  { id: 'technology', label: 'Technology' },
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

interface Props {
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

  const selectedTemplate = TEMPLATES.find(t => t.id === state.templateId) ?? null;

  function handleTemplateSelect(templateId: TemplateId) {
    if (state.templateId === templateId) {
      setTemplate(null, []);
      return;
    }

    const tmpl = TEMPLATES.find(t => t.id === templateId);
    setTemplate(templateId, tmpl?.preselectedSystems ?? []);
  }

  function handleContinue() {
    if (!canProceedFromStep1) return;
    goTo(2);
  }

  return (
    <div className="space-y-5">
      <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-text">Discovery focus</h2>
            <p className="mt-1 text-sm leading-relaxed text-muted">
              Select the operating lens AgentIQ should prioritize first.
            </p>
          </div>
          <span className="rounded-full border border-red-500/30 bg-red-500/10 px-2.5 py-1 text-xs font-medium text-red-200">
            Required
          </span>
        </div>

        <div
          role="radiogroup"
          aria-label="Discovery focus"
          className="grid grid-cols-1 gap-3 md:grid-cols-2"
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
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
          <div className="mb-1 flex items-center gap-2">
            <h2 className="text-sm font-semibold text-text">Industry</h2>
            <span className="text-xs text-muted">Optional</span>
          </div>
          <p className="mb-4 text-xs leading-relaxed text-muted">
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
        </section>

        <section className="rounded-xl border border-border bg-panel p-5 shadow-sm">
          <div className="mb-1 flex items-center gap-2">
            <h2 className="text-sm font-semibold text-text">Start from a template</h2>
            <span className="text-xs text-muted">Optional</span>
          </div>
          <p className="mb-4 text-xs leading-relaxed text-muted">
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

          {selectedTemplate && (
            <TemplateNoteBlock
              templateId={selectedTemplate.id}
              suggestedFocus={selectedTemplate.suggestedFocus}
            />
          )}
        </section>
      </div>

      <div className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div className="flex items-start gap-2 text-sm text-muted">
            <Info size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden="true" />
            <span>Discovery focus is required. Industry and template are optional accelerators.</span>
          </div>

          <Button
            onClick={handleContinue}
            disabled={!canProceedFromStep1}
            className="gap-2"
          >
            Continue to your systems
            <MoveRight size={16} strokeWidth={2.2} aria-hidden="true" />
          </Button>
        </div>
      </div>
    </div>
  );
}
